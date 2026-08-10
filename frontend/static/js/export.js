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
    const fps = item.fps || 30;
    const totalFrames = Math.floor((item.duration || 0) * fps);
    const tiles = Math.floor(totalFrames / everyN) + 1;
    const rows = Math.ceil(tiles / cols);
    el.textContent = `≈ ${tiles} tiles → ${cols}×${rows} grid`;
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
          tile_width: parseInt(document.getElementById('sheet-tile-width').value) || 320,
          padding: parseInt(document.getElementById('sheet-padding').value) || 8,
          order: document.getElementById('sheet-order').value,
          labels: document.getElementById('sheet-labels').checked,
          max_width: document.getElementById('sheet-max-width').value || null,
          start: document.getElementById('sheet-start').value || null,
          end: document.getElementById('sheet-end').value || null,
        },
      });
      toast(`Sheet rendered: ${result.frames} frames, ${result.grid}`, 'success');
      document.getElementById('export-result').innerHTML = `
        <div class="section-card">
          <h3>Result — ${esc(result.filename)} (${result.width}×${result.height})</h3>
          <a href="/api/exports/${encodeURIComponent(result.filename)}" target="_blank">
            <img src="/api/exports/${encodeURIComponent(result.filename)}"
                 style="max-width:100%;border-radius:5px;border:1px solid var(--border)">
          </a>
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

  renderHistory() {
    const el = document.getElementById('export-history-list');
    if (!el) return;
    if (this.exports.length === 0) {
      el.innerHTML = '<div style="color:var(--muted);font-size:13px">No exports yet</div>';
      return;
    }
    el.innerHTML = this.exports.map(e => `
      <div class="export-entry">
        <div class="exp-name">
          <a href="/api/exports/${encodeURIComponent(e.filename)}" target="_blank"
             style="color:var(--accent);text-decoration:none">${esc(e.filename)}</a>
        </div>
        <div class="exp-meta">${fmtBytes(e.size_bytes)}</div>
      </div>`).join('');
  },
};

document.addEventListener('DOMContentLoaded', () => {
  ['sheet-every-n', 'sheet-cols'].forEach(id => {
    const el = document.getElementById(id);
    if (el) el.addEventListener('input', () => ExportPage.renderEstimate());
  });
});
