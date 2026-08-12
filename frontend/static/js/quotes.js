const Quotes = {
  hits: [],
  palettes: [],
  status: null,

  async load() {
    try {
      this.status = await api('/api/qs/status');
      this.palettes = await api('/api/palettes');
    } catch (e) {
      document.getElementById('quotes-status').textContent =
        'quotesource not configured: ' + e.message;
      return;
    }
    this.renderStatus();
    this.renderSourceFilter();
  },

  renderStatus() {
    const t = this.status.totals;
    const emb = this.status.embeddings;
    const cov = emb && emb.chunks ? ` · embeddings ${(emb.coverage * 100).toFixed(0)}%` : '';
    document.getElementById('quotes-status').textContent =
      `${t.episodes} episodes · ${t.captions + t.whisper} transcribed${cov}`;
  },

  renderSourceFilter() {
    const sel = document.getElementById('q-source');
    sel.innerHTML = '<option value="">all sources</option>' +
      this.status.sources.map(s =>
        `<option value="${esc(s.source)}">${esc(s.source)} (${s.episodes})</option>`).join('');
  },

  async search() {
    const q = document.getElementById('q-query').value.trim();
    if (!q) { toast('Enter a query', 'error'); return; }
    const params = new URLSearchParams({ q, mode: document.getElementById('q-mode').value });
    for (const [id, key] of [['q-source', 'source'], ['q-person', 'person'],
                             ['q-after', 'after'], ['q-before', 'before']]) {
      const v = document.getElementById(id).value.trim();
      if (v) params.set(key, v);
    }
    params.set('limit', '25');

    const btn = document.getElementById('q-search-btn');
    btn.disabled = true;
    btn.textContent = 'Searching…';
    try {
      const res = await api('/api/qs/search?' + params.toString());
      this.hits = res.hits;
      if (res.coverage !== undefined && res.coverage < 1) {
        toast(`Note: ${(res.coverage * 100).toFixed(0)}% of corpus embedded so far`);
      }
      this.renderHits();
    } catch (e) {
      toast(e.message, 'error');
    } finally {
      btn.disabled = false;
      btn.textContent = 'Search';
    }
  },

  renderHits() {
    const el = document.getElementById('q-results');
    if (this.hits.length === 0) {
      el.innerHTML = '<div style="color:var(--muted);padding:16px">No hits</div>';
      return;
    }
    el.innerHTML = this.hits.map((h, i) => `
      <div class="q-hit" id="q-hit-${i}">
        <div class="q-hit-head">
          <span class="q-score">${h.score.toFixed(2)}</span>
          <span class="q-title">${esc(h.episode_title || h.episode_id)}</span>
          <span class="q-date">${esc(h.upload_date || '')}</span>
          <span class="q-time">${fmtDuration(h.start)}</span>
        </div>
        <div class="q-text">${esc(h.text)}</div>
        <div class="q-actions">
          <a class="btn btn-ghost btn-sm" href="${esc(h.url_ts)}" target="_blank">▶ YouTube</a>
          <button class="btn btn-ghost btn-sm" onclick="Quotes.toggleContext(${i})">Context</button>
          <button class="btn btn-primary btn-sm" onclick="Quotes.togglePull(${i})">Pull…</button>
        </div>
        <div class="q-context hidden" id="q-context-${i}"></div>
        <div class="q-pull hidden" id="q-pull-${i}">
          <select id="q-pull-mode-${i}" style="width:90px">
            <option value="av">av</option>
            <option value="audio">audio</option>
          </select>
          <select id="q-pull-precision-${i}" style="width:190px" title="rough: fast stream copy with ~10s slop each side, original quality — trim with the Clip tools. exact: slower re-encode, clip starts on the quote.">
            <option value="rough">rough (fast, trim later)</option>
            <option value="exact">exact (slow, precise)</option>
          </select>
          <input type="text" id="q-pull-palette-${i}" list="q-palette-names"
                 placeholder="palette name…" style="width:160px">
          <input type="text" id="q-pull-person-${i}" placeholder="person…" style="width:140px">
          <button class="btn btn-success btn-sm" onclick="Quotes.pull(${i})">Stage</button>
        </div>
      </div>`).join('');

    document.getElementById('q-palette-datalist').innerHTML =
      `<datalist id="q-palette-names">` +
      this.palettes.map(p => `<option value="${esc(p.name)}">`).join('') +
      `</datalist>`;
  },

  async toggleContext(i) {
    const el = document.getElementById(`q-context-${i}`);
    if (!el.classList.contains('hidden')) { el.classList.add('hidden'); return; }
    const h = this.hits[i];
    el.innerHTML = '<div style="color:var(--muted);padding:6px">loading…</div>';
    el.classList.remove('hidden');
    try {
      const ctx = await api(`/api/qs/context?episode_id=${encodeURIComponent(h.episode_id)}&start=${h.start}&end=${h.end}&window=25`);
      el.innerHTML = ctx.segments.map(s => {
        const inHit = s.end >= h.start && s.start <= h.end;
        return `<div class="q-ctx-line${inHit ? ' q-ctx-hit' : ''}">
          <span class="q-ctx-ts">${fmtDuration(s.start)}</span> ${esc(s.text)}</div>`;
      }).join('');
    } catch (e) {
      el.innerHTML = `<div style="color:var(--red);padding:6px">${esc(e.message)}</div>`;
    }
  },

  togglePull(i) {
    const el = document.getElementById(`q-pull-${i}`);
    el.classList.toggle('hidden');
    // Opening the form starts caching the episode media in the background,
    // so by the time palette/person are filled in the download is underway.
    if (!el.classList.contains('hidden')) {
      const mode = document.getElementById(`q-pull-mode-${i}`).value;
      api('/api/qs/warm', {
        method: 'POST',
        body: { episode_id: this.hits[i].episode_id, mode },
      }).catch(() => {});
    }
  },

  init() {
    const q = document.getElementById('q-query');
    if (q) q.addEventListener('keydown', e => { if (e.key === 'Enter') this.search(); });
  },

  async pull(i) {
    const h = this.hits[i];
    const body = {
      episode_id: h.episode_id,
      start: h.start,
      end: h.end,
      mode: document.getElementById(`q-pull-mode-${i}`).value,
      rough: document.getElementById(`q-pull-precision-${i}`).value === 'rough',
      palette: document.getElementById(`q-pull-palette-${i}`).value.trim(),
      person: document.getElementById(`q-pull-person-${i}`).value.trim(),
    };
    const btn = document.querySelector(`#q-pull-${i} button`);
    btn.disabled = true;

    let job_id;
    try {
      ({ job_id } = await api('/api/qs/pull', { method: 'POST', body }));
    } catch (e) {
      toast(e.message, 'error');
      btn.disabled = false;
      return;
    }

    const t0 = Date.now();
    const poll = setInterval(async () => {
      let job;
      try {
        job = await api(`/api/qs/pull/${job_id}`);
      } catch { return; }  // transient poll failure; keep trying
      const secs = Math.round((Date.now() - t0) / 1000);
      if (!job.done) {
        btn.textContent = `${job.stage} · ${secs}s`;
        return;
      }
      clearInterval(poll);
      btn.disabled = false;
      btn.textContent = 'Stage';
      if (job.error) {
        toast(`Pull failed: ${job.error}`, 'error');
      } else {
        toast(`Staged after ${secs}s: ${job.item.title}`, 'success');
        document.getElementById(`q-pull-${i}`).classList.add('hidden');
      }
    }, 1500);
  },
};

document.addEventListener('DOMContentLoaded', () => Quotes.init());
