const Quotes = {
  hits: [],
  palettes: [],
  status: null,

  server: null,
  serverBusy: false,

  async load() {
    // Server state first: if the corpus API is down, every call below fails,
    // and "the server is stopped" is a far more useful thing to show than a
    // connection error.
    this.refreshServer();
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

  async refreshServer() {
    if (this.serverBusy) return;
    try {
      this.server = await api('/api/qs/server');
    } catch (e) {
      this.server = { running: false, unreachable: true, error: e.message };
    }
    this.renderServer();
  },

  async serverAction(action) {
    if (this.serverBusy) return;
    if (action === 'stop' && !confirm('Stop the corpus server? Search and pulls will stop working until you start it again.')) return;

    this.serverBusy = true;
    this.setServerDot('busy', `${action}ing…`);
    try {
      this.server = await api('/api/qs/server', {
        method: 'POST', body: JSON.stringify({ action }),
      });
      toast(this.server.note || `server ${action} ok`);
    } catch (e) {
      this.server = { running: false, unreachable: true, error: e.message };
      toast(`server ${action} failed: ${e.message}`, 'error');
    } finally {
      this.serverBusy = false;
      this.renderServer();
    }
    // Corpus totals are unknown until it is up, so reload them once it is.
    if (this.server && this.server.running) {
      try {
        this.status = await api('/api/qs/status');
        this.renderStatus();
        this.renderSourceFilter();
      } catch (e) { /* status line already says what it can */ }
    }
  },

  setServerDot(kind, text) {
    const dot = document.getElementById('q-server-dot');
    if (dot) dot.className = `server-dot server-dot--${kind}`;
    const state = document.getElementById('q-server-state');
    if (state) state.textContent = text;
  },

  renderServer() {
    const s = this.server || {};
    const meters = document.getElementById('q-server-meters');
    const [start, restart, stop] = ['q-server-start', 'q-server-restart', 'q-server-stop']
      .map(id => document.getElementById(id));
    if (!meters || !start) return;

    if (s.unreachable) {
      this.setServerDot('unknown', s.error || 'cannot reach the server machine');
      meters.textContent = '';
      start.disabled = restart.disabled = stop.disabled = true;
      return;
    }

    if (s.running) {
      this.setServerDot('up', `running on :${s.port}${s.uptime ? ' · up ' + s.uptime : ''}`);
      // What it is costing, so the decision to stop it is an informed one.
      const gpuPct = s.gpu_total_mb
        ? ` (${Math.round(100 * s.gpu_used_mb / s.gpu_total_mb)}%)` : '';
      meters.innerHTML =
        `<span>build ${esc(s.version || '?')}</span>` +
        `<span>app RAM ${s.rss_mb} MB</span>` +
        `<span>machine free ${(s.mem_available_mb / 1024).toFixed(1)} GB</span>` +
        `<span>VRAM ${s.gpu_used_mb}/${s.gpu_total_mb} MB${gpuPct}</span>` +
        `<span>GPU ${s.gpu_util}%</span>`;
    } else {
      this.setServerDot('down', 'stopped — search and pulls are unavailable');
      meters.innerHTML = s.gpu_total_mb
        ? `<span>machine free ${(s.mem_available_mb / 1024).toFixed(1)} GB</span>` +
          `<span>VRAM ${s.gpu_used_mb}/${s.gpu_total_mb} MB</span>`
        : '';
    }
    start.disabled = !!s.running;
    restart.disabled = false;
    stop.disabled = !s.running;
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
          <select id="q-pull-precision-${i}" style="width:210px" title="rough: fast stream copy with ~10s slop, original quality — trim later. exact: re-encoded, starts on the quote. word-accurate: whispers a window and snaps to the waveform, audio only, writes a per-word manifest.">
            <option value="rough">rough (fast, trim later)</option>
            <option value="exact">exact (slow, precise)</option>
            <option value="word">word-accurate (+manifest)</option>
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
    const precision = document.getElementById(`q-pull-precision-${i}`).value;
    const wordAccurate = precision === 'word';
    const body = {
      episode_id: h.episode_id,
      start: h.start,
      end: h.end,
      mode: document.getElementById(`q-pull-mode-${i}`).value,
      rough: precision === 'rough',
      palette: document.getElementById(`q-pull-palette-${i}`).value.trim(),
      person: document.getElementById(`q-pull-person-${i}`).value.trim(),
    };
    const endpoint = wordAccurate ? '/api/qs/cut' : '/api/qs/pull';
    const btn = document.querySelector(`#q-pull-${i} button`);
    btn.disabled = true;

    let job_id;
    try {
      ({ job_id } = await api(endpoint, { method: 'POST', body }));
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
        toast(`${wordAccurate ? 'Cut' : 'Pull'} failed: ${job.error}`, 'error');
      } else {
        const d = job.item.cut_diagnostics;
        const extra = d
          ? ` · ${job.item.words.length} words, lead ${d.lead_silence_ms}ms`
          : '';
        toast(`Staged after ${secs}s: ${job.item.title}${extra}`, 'success');
        document.getElementById(`q-pull-${i}`).classList.add('hidden');
      }
    }, 1500);
  },
};

document.addEventListener('DOMContentLoaded', () => Quotes.init());
