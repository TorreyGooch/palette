// Export paths now contain a folder. encodeURIComponent would turn the "/"
// into %2F and the route would never match, so encode each segment instead.
function encPath(p) {
  return String(p || '').split('/').map(encodeURIComponent).join('/');
}

const ExportPage = {
  videos: [],
  exports: [],
  selectedItem: null,
  preselectId: null,   // set by Library "send to export"
  mode: 'sheet',       // 'sheet' | 'video'

  async load() {
    try {
      [this.videos, this.exports] = await Promise.all([
        api('/api/items?type=video'),
        api('/api/exports'),
      ]);
    } catch { this.videos = []; this.exports = []; }

    if (this.preselectId) {
      const item = this.videos.find(v => v.id === this.preselectId);
      this.preselectId = null;
      if (item) this.selectedItem = item;
    }
    this.renderSourceList();
    this.renderEstimate();
    this.renderHistory();
  },

  renderSourceList() {
    const el = document.getElementById('export-source-list');
    if (!el) return;
    if (this.videos.length === 0) {
      el.innerHTML = '<div style="color:var(--muted);font-size:13px;padding:8px">No videos in library</div>';
      return;
    }
    el.innerHTML = this.videos.map(v => `
      <div class="source-select-item ${this.selectedItem?.id === v.id ? 'active' : ''}"
           onclick="ExportPage.selectItem('${esc(v.id)}')">
        <div class="src-name">${esc(v.title)}</div>
        <div class="src-concept">${fmtDuration(v.duration)} · ${v.fps ? v.fps.toFixed(2) + ' fps' : ''}</div>
      </div>`).join('');
  },

  selectItem(iid) {
    this.selectedItem = this.videos.find(v => v.id === iid) || null;
    this.renderSourceList();
    this.renderEstimate();
    document.getElementById('export-result').innerHTML = '';
  },

  setMode(mode) {
    this.mode = mode;
    document.querySelectorAll('#page-export .tab-btn').forEach(b =>
      b.classList.toggle('active', b.dataset.mode === mode));
    document.getElementById('sheet-controls').classList.toggle('hidden', mode !== 'sheet');
    document.getElementById('video-controls').classList.toggle('hidden', mode !== 'video');
    this.renderEstimate();
  },

  renderEstimate() {
    const el = document.getElementById('sheet-estimate');
    if (!el) return;
    if (!this.selectedItem || this.mode !== 'sheet') { el.textContent = ''; return; }
    const item = this.selectedItem;
    const everyN = parseInt(document.getElementById('sheet-every-n').value) || 30;
    const cols = parseInt(document.getElementById('sheet-cols').value) || 4;
    const rows = parseInt(document.getElementById('sheet-rows').value) || 0;
    const fps = item.fps || 30;
    const start = parseFloat(document.getElementById('sheet-start').value) || 0;
    const endRaw = parseFloat(document.getElementById('sheet-end').value);
    const end = isNaN(endRaw) ? (item.duration || 0) : endRaw;
    const span = Math.max(0, end - start);
    const tiles = Math.floor(Math.floor(span * fps) / everyN) + 1;

    if (!rows) {
      el.textContent = `≈ ${tiles} tiles → one ${cols}×${Math.ceil(tiles / cols)} sheet`;
      el.style.color = tiles > 400 ? 'var(--warn, #e0a030)' : 'var(--muted)';
      return;
    }
    const perSheet = cols * rows;
    const sheets = Math.max(1, Math.ceil(tiles / perSheet));
    const secsPer = (perSheet * everyN) / fps;
    el.textContent = `≈ ${tiles} tiles → ${sheets} sheet${sheets === 1 ? '' : 's'} `
      + `of ${cols}×${rows} (${secsPer.toFixed(0)}s each)`;
    el.style.color = 'var(--muted)';
  },

  async runSheetExport() {
    if (!this.selectedItem) { toast('Select a video', 'error'); return; }
    const btn = document.getElementById('sheet-export-btn');
    btn.disabled = true;
    btn.textContent = 'Rendering…';
    try {
      const result = await api('/api/export/contact-sheet', {
        method: 'POST',
        body: {
          item_id: this.selectedItem.id,
          every_n: parseInt(document.getElementById('sheet-every-n').value) || 30,
          cols: parseInt(document.getElementById('sheet-cols').value) || 4,
          rows: document.getElementById('sheet-rows').value || null,
          tile_width: parseInt(document.getElementById('sheet-tile-width').value) || 320,
          padding: parseInt(document.getElementById('sheet-padding').value) || 8,
          order: document.getElementById('sheet-order').value,
          labels: document.getElementById('sheet-labels').checked,
          max_width: document.getElementById('sheet-max-width').value || null,
          start: document.getElementById('sheet-start').value || null,
          end: document.getElementById('sheet-end').value || null,
        },
      });
      const sheets = result.sheets || [];
      toast(`${result.frames} frames → ${sheets.length} sheet${sheets.length === 1 ? '' : 's'}`,
            'success');
      // The folder is the deliverable for a series — you hand the path over,
      // you don't save forty images out of the browser one at a time.
      const folder = result.dir_path ? `
        <div style="background:var(--bg);border:1px solid var(--border);border-radius:5px;
                    padding:10px;margin-bottom:12px;font-size:12px">
          <div style="color:var(--muted);margin-bottom:6px">
            ${sheets.length} sheets + index.json saved to:
          </div>
          <div style="display:flex;align-items:center;gap:8px">
            <code style="flex:1;word-break:break-all">${esc(result.dir_path)}</code>
            <button class="btn btn-sm" onclick="ExportPage.copyPath(this)"
                    data-path="${esc(result.dir_path)}">Copy path</button>
          </div>
          <div style="color:var(--muted);margin-top:6px">
            index: <a href="/api/exports/${encPath(result.index)}" target="_blank"
                      style="color:var(--accent)">index.json</a>
          </div>
        </div>` : '';
      document.getElementById('export-result').innerHTML = `
        <div class="section-card">
          <h3>Result — ${result.frames} frames across ${sheets.length} sheet${sheets.length === 1 ? '' : 's'}</h3>
          ${folder}
          ${sheets.map(s => `
            <div style="margin-bottom:14px">
              <div style="font-size:12px;color:var(--muted);margin-bottom:4px">
                ${esc(s.filename)} — ${s.grid}, ${s.width}×${s.height}${
                  s.start_time != null
                    ? ` · ${fmtDuration(s.start_time)}–${fmtDuration(s.end_time)}`
                    : ''}
              </div>
              <a href="/api/exports/${encPath(s.url)}" target="_blank">
                <img src="/api/exports/${encPath(s.url)}" loading="lazy"
                     style="max-width:100%;border-radius:5px;border:1px solid var(--border)">
              </a>
            </div>`).join('')}
        </div>`;
      this.exports = await api('/api/exports');
      this.renderHistory();
    } catch (e) {
      toast(e.message, 'error');
    } finally {
      btn.disabled = false;
      btn.textContent = 'Render Contact Sheet';
    }
  },

  async runVideoExport() {
    if (!this.selectedItem) { toast('Select a video', 'error'); return; }
    const btn = document.getElementById('video-export-btn');
    btn.disabled = true;
    btn.textContent = 'Exporting…';
    try {
      const result = await api('/api/export/video', {
        method: 'POST',
        body: {
          item_id: this.selectedItem.id,
          start: document.getElementById('vexp-start').value || null,
          end: document.getElementById('vexp-end').value || null,
          scale_width: document.getElementById('vexp-width').value || null,
          fps: document.getElementById('vexp-fps').value || null,
        },
      });
      toast(`Video exported (${fmtBytes(result.size_bytes)})`, 'success');
      document.getElementById('export-result').innerHTML = `
        <div class="section-card">
          <h3>Result — ${esc(result.filename)} (${fmtBytes(result.size_bytes)})</h3>
          <video src="/api/exports/${encodeURIComponent(result.filename)}" controls
                 style="max-width:100%;border-radius:5px"></video>
        </div>`;
      this.exports = await api('/api/exports');
      this.renderHistory();
    } catch (e) {
      toast(e.message, 'error');
    } finally {
      btn.disabled = false;
      btn.textContent = 'Export Video';
    }
  },

  async copyPath(btn) {
    try {
      await navigator.clipboard.writeText(btn.dataset.path);
      const was = btn.textContent;
      btn.textContent = 'Copied';
      setTimeout(() => { btn.textContent = was; }, 1500);
    } catch {
      toast('Clipboard blocked — select the path and copy it', 'error');
    }
  },

  renderHistory() {
    const el = document.getElementById('export-history-list');
    if (!el) return;
    if (this.exports.length === 0) {
      el.innerHTML = '<div style="color:var(--muted);font-size:13px">No exports yet</div>';
      return;
    }
    el.innerHTML = this.exports.map(e => {
      // A series opens on its first sheet; the folder itself isn't servable.
      const href = e.kind === 'series' ? e.first : e.filename;
      const meta = e.kind === 'series'
        ? `${e.sheets} sheets · ${fmtBytes(e.size_bytes)}`
        : fmtBytes(e.size_bytes);
      return `
      <div class="export-entry">
        <div class="exp-name">
          ${e.kind === 'series' ? '📁 ' : ''}<a href="/api/exports/${encPath(href)}"
             target="_blank" style="color:var(--accent);text-decoration:none">${esc(e.filename)}</a>
          ${e.kind === 'series' && e.path
            ? `<button class="btn btn-sm" style="margin-left:8px"
                       onclick="ExportPage.copyPath(this)"
                       data-path="${esc(e.path)}">Copy path</button>` : ''}
        </div>
        <div class="exp-meta">${meta}</div>
      </div>`;
    }).join('');
  },
};

document.addEventListener('DOMContentLoaded', () => {
  ['sheet-every-n', 'sheet-cols', 'sheet-rows', 'sheet-start', 'sheet-end'].forEach(id => {
    const el = document.getElementById(id);
    if (el) el.addEventListener('input', () => ExportPage.renderEstimate());
  });
});
