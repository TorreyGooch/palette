// Storyboard builder: pick panels, order them, say why each is there.
//
// The board is the source of truth on the server; this keeps a local copy and
// pushes the whole panel list on every change. Edits are debounced so typing a
// note is not one request per keystroke, and the list is never re-rendered on
// a save response — that would yank focus out of the textarea being typed in.

const Storyboard = {
  boards: [],
  board: null,          // the open board, panels included
  images: [],           // library images, for the picker
  videos: [],           // library videos, for the "source" dropdown
  saveTimer: null,
  dragFrom: null,
  pickerOpen: false,

  async load() {
    try {
      [this.boards, this.images, this.videos] = await Promise.all([
        api('/api/storyboards'),
        api('/api/items?type=image'),
        api('/api/items?type=video'),
      ]);
    } catch { this.boards = []; this.images = []; this.videos = []; }
    this.renderBoardList();
    if (this.board) {
      const still = this.boards.some(b => b.id === this.board.id);
      if (!still) this.board = null;
    }
    this.renderEditor();
  },

  // ── Boards ────────────────────────────────────────────────────────────────

  async createBoard() {
    const name = (prompt('Name this storyboard', 'Untitled board') || '').trim();
    if (name === '') return;
    try {
      const board = await api('/api/storyboards', { method: 'POST', body: { name } });
      this.boards = await api('/api/storyboards');
      this.renderBoardList();
      await this.openBoard(board.id);
      toast('Board created', 'success');
    } catch (e) { toast(e.message, 'error'); }
  },

  async openBoard(bid) {
    try {
      this.board = await api(`/api/storyboards/${encodeURIComponent(bid)}`);
    } catch (e) { toast(e.message, 'error'); return; }
    this.renderBoardList();
    this.renderEditor();
  },

  async deleteBoard() {
    if (!this.board) return;
    if (!confirm(`Delete "${this.board.name}" and its ${this.board.panels.length} panel notes?\n\nThe images stay in the library.`)) return;
    try {
      await api(`/api/storyboards/${encodeURIComponent(this.board.id)}`, { method: 'DELETE' });
      this.board = null;
      toast('Board deleted', 'success');
      await this.load();
    } catch (e) { toast(e.message, 'error'); }
  },

  renderBoardList() {
    const el = document.getElementById('sb-board-list');
    if (!el) return;
    if (this.boards.length === 0) {
      el.innerHTML = '<div style="color:var(--muted);font-size:13px;padding:8px">No boards yet</div>';
      return;
    }
    el.innerHTML = this.boards.map(b => `
      <div class="source-select-item ${this.board?.id === b.id ? 'active' : ''}"
           onclick="Storyboard.openBoard('${esc(b.id)}')">
        <div class="src-name">${esc(b.name)}</div>
        <div class="src-concept">${b.panels} panel${b.panels === 1 ? '' : 's'}</div>
      </div>`).join('');
  },

  // ── Saving ────────────────────────────────────────────────────────────────

  queueSave() {
    clearTimeout(this.saveTimer);
    this.setSaveState('unsaved');
    this.saveTimer = setTimeout(() => this.save(), 600);
  },

  async save(rerender = false) {
    if (!this.board) return;
    clearTimeout(this.saveTimer);
    this.setSaveState('saving');
    try {
      const updated = await api(`/api/storyboards/${encodeURIComponent(this.board.id)}`, {
        method: 'PATCH',
        body: { name: this.board.name, panels: this.board.panels },
      });
      this.board = updated;
      this.setSaveState('saved');
      // The server derives each frame number from timecode and source rate,
      // so those cells are refreshed in place rather than left showing a
      // stale value the user never typed.
      if (rerender) this.renderPanels();
      else this.refreshDerived();
      const summary = this.boards.find(b => b.id === this.board.id);
      if (summary) { summary.panels = this.board.panels.length; summary.name = this.board.name; }
      this.renderBoardList();
    } catch (e) {
      this.setSaveState('error');
      toast(e.message, 'error');
    }
  },

  setSaveState(state) {
    const el = document.getElementById('sb-save-state');
    if (!el) return;
    const text = { unsaved: 'unsaved changes…', saving: 'saving…', saved: 'saved', error: 'save failed' };
    el.textContent = text[state] || '';
    el.style.color = state === 'error' ? 'var(--red)'
      : state === 'saved' ? 'var(--muted)' : 'var(--orange)';
  },

  refreshDerived() {
    (this.board?.panels || []).forEach(p => {
      const el = document.getElementById(`sb-frame-${p.id}`);
      if (el) el.textContent = p.frame == null ? '—' : `f${p.frame}`;
    });
  },

  // ── Adding panels ─────────────────────────────────────────────────────────

  async addItems(itemIds) {
    if (!this.board || itemIds.length === 0) return;
    try {
      this.board = await api(
        `/api/storyboards/${encodeURIComponent(this.board.id)}/panels`,
        { method: 'POST', body: { item_ids: itemIds } });
      this.renderPanels();
      this.setSaveState('saved');
      const summary = this.boards.find(b => b.id === this.board.id);
      if (summary) summary.panels = this.board.panels.length;
      this.renderBoardList();
    } catch (e) { toast(e.message, 'error'); }
  },

  async importFiles(files) {
    if (!this.board) { toast('Open a board first', 'error'); return; }
    const list = Array.from(files);
    // A dropped video would import fine and then render as a blank panel,
    // so it is refused here rather than silently becoming a hole in the board.
    const images = list.filter(f => f.type.startsWith('image/'));
    const skipped = list.length - images.length;
    if (images.length === 0) {
      toast(skipped ? 'Only image files can become panels' : 'Nothing to add', 'error');
      return;
    }

    const zone = document.getElementById('sb-drop-zone');
    if (zone) zone.classList.add('busy');
    const ids = [];
    let failed = 0;
    for (const file of images) {
      const fd = new FormData();
      fd.append('file', file);
      try {
        const res = await fetch('/api/items/import', { method: 'POST', body: fd });
        if (!res.ok) { failed++; continue; }
        const item = await res.json();
        if (item && item.id) ids.push(item.id);
      } catch { failed++; }
    }
    if (zone) zone.classList.remove('busy');

    await this.addItems(ids);
    this.images = await api('/api/items?type=image').catch(() => this.images);
    const bits = [`${ids.length} panel${ids.length === 1 ? '' : 's'} added`];
    if (skipped) bits.push(`${skipped} non-image skipped`);
    if (failed) bits.push(`${failed} failed`);
    toast(bits.join(' · '), failed ? 'error' : 'success');
  },

  togglePicker() {
    this.pickerOpen = !this.pickerOpen;
    this.renderPicker();
  },

  renderPicker() {
    const el = document.getElementById('sb-picker');
    const btn = document.getElementById('sb-picker-btn');
    if (!el) return;
    el.classList.toggle('hidden', !this.pickerOpen);
    if (btn) btn.textContent = this.pickerOpen ? 'Hide library images' : 'Add from library';
    if (!this.pickerOpen) return;
    if (this.images.length === 0) {
      el.innerHTML = '<div style="color:var(--muted);font-size:13px;padding:8px">No images in the library yet — drop some above.</div>';
      return;
    }
    el.innerHTML = `
      <div class="sb-picker-grid">
        ${this.images.map(i => `
          <label class="sb-pick" title="${esc(i.title)}">
            <input type="checkbox" value="${esc(i.id)}">
            <img src="/api/items/${esc(i.id)}/thumbnail" loading="lazy" alt="">
            <span>${esc(i.title)}</span>
          </label>`).join('')}
      </div>
      <button class="btn btn-sm btn-primary" style="margin-top:10px"
              onclick="Storyboard.addChecked()">Add checked panels</button>`;
  },

  addChecked() {
    const ids = Array.from(
      document.querySelectorAll('#sb-picker input[type=checkbox]:checked')
    ).map(c => c.value);
    if (ids.length === 0) { toast('Nothing checked', 'error'); return; }
    this.addItems(ids).then(() => {
      document.querySelectorAll('#sb-picker input[type=checkbox]:checked')
        .forEach(c => { c.checked = false; });
    });
  },

  // ── Panel edits ───────────────────────────────────────────────────────────

  setNote(idx, value) {
    if (!this.board) return;
    this.board.panels[idx].note = value;
    this.queueSave();
  },

  setPrompt(idx, value) {
    if (!this.board) return;
    this.board.panels[idx].video_prompt = value;
    this.queueSave();
  },

  // Changing the word range changes the quote, its duration, and where every
  // later beat falls — all of which the server derives. So this re-renders on
  // the response rather than patching a number in place.
  setWordStart(idx, value) { this.setWord(idx, 'word_start', value); },
  setWordEnd(idx, value) { this.setWord(idx, 'word_end', value); },

  setWord(idx, field, value) {
    if (!this.board) return;
    const panel = this.board.panels[idx];
    if (!panel?.narration) return;
    const n = parseInt(value, 10);
    panel.narration[field] = (value === '' || isNaN(n)) ? null : n;
    this.save(true);
  },

  setTimecode(idx, value) {
    if (!this.board) return;
    const n = parseFloat(value);
    this.board.panels[idx].timecode = (value === '' || isNaN(n)) ? null : n;
    this.save();
  },

  setSource(idx, value) {
    if (!this.board) return;
    this.board.panels[idx].source_item_id = value || null;
    this.save();
  },

  removePanel(idx) {
    if (!this.board) return;
    this.board.panels.splice(idx, 1);
    this.renderPanels();
    this.save(true);
  },

  move(idx, delta) {
    if (!this.board) return;
    const to = idx + delta;
    if (to < 0 || to >= this.board.panels.length) return;
    const [p] = this.board.panels.splice(idx, 1);
    this.board.panels.splice(to, 0, p);
    this.renderPanels();
    this.save(true);
  },

  // ── Drag reorder ──────────────────────────────────────────────────────────
  // Only the grip arms dragging. A permanently draggable card makes the note
  // textarea inside it impossible to select text in.

  armDrag(grip) {
    const card = grip.closest('.sb-panel');
    if (card) card.draggable = true;
  },

  dragStart(ev, idx) {
    this.dragFrom = idx;
    ev.dataTransfer.effectAllowed = 'move';
    try { ev.dataTransfer.setData('text/plain', String(idx)); } catch {}
    ev.currentTarget.classList.add('dragging');
  },

  dragEnd(ev) {
    ev.currentTarget.classList.remove('dragging');
    ev.currentTarget.draggable = false;
    document.querySelectorAll('.sb-panel.drop-target')
      .forEach(e => e.classList.remove('drop-target'));
    this.dragFrom = null;
  },

  dragOver(ev, idx) {
    if (this.dragFrom === null || this.dragFrom === idx) return;
    ev.preventDefault();
    ev.dataTransfer.dropEffect = 'move';
    ev.currentTarget.classList.add('drop-target');
  },

  dragLeave(ev) {
    ev.currentTarget.classList.remove('drop-target');
  },

  drop(ev, idx) {
    ev.preventDefault();
    ev.currentTarget.classList.remove('drop-target');
    const from = this.dragFrom;
    if (from === null || from === idx) return;
    const [p] = this.board.panels.splice(from, 1);
    this.board.panels.splice(idx, 0, p);
    this.dragFrom = null;
    this.renderPanels();
    this.save(true);
  },

  // ── Rendering the editor ──────────────────────────────────────────────────

  renderEditor() {
    const empty = document.getElementById('sb-empty');
    const editor = document.getElementById('sb-editor');
    if (!empty || !editor) return;
    empty.classList.toggle('hidden', !!this.board);
    editor.classList.toggle('hidden', !this.board);
    if (!this.board) return;
    document.getElementById('sb-name').value = this.board.name || '';
    this.setSaveState('saved');
    this.pickerOpen = false;
    this.renderPicker();
    this.renderPanels();
    document.getElementById('sb-render-result').innerHTML = '';
  },

  renameBoard(value) {
    if (!this.board) return;
    this.board.name = value;
    this.queueSave();
  },

  sourceOptions(selected) {
    return ['<option value="">— no source —</option>'].concat(
      this.videos.map(v =>
        `<option value="${esc(v.id)}"${v.id === selected ? ' selected' : ''}>${esc(v.title)}</option>`)
    ).join('');
  },

  renderPanels() {
    const el = document.getElementById('sb-panels');
    if (!el) return;
    const panels = this.board?.panels || [];
    document.getElementById('sb-count').textContent =
      `${panels.length} panel${panels.length === 1 ? '' : 's'}`;
    this.renderEstimate();

    if (panels.length === 0) {
      el.innerHTML = '<div style="color:var(--muted);font-size:13px;padding:14px">'
        + 'No panels yet. Drop images above, or add them from the library.</div>';
      return;
    }

    el.innerHTML = panels.map((p, i) => `
      <div class="sb-panel${p.missing ? ' missing' : ''}"
           ondragstart="Storyboard.dragStart(event, ${i})"
           ondragend="Storyboard.dragEnd(event)"
           ondragover="Storyboard.dragOver(event, ${i})"
           ondragleave="Storyboard.dragLeave(event)"
           ondrop="Storyboard.drop(event, ${i})">
        <div class="sb-panel-head">
          <span class="sb-grip" onmousedown="Storyboard.armDrag(this)"
                title="Drag to reorder">⠿</span>
          <span class="sb-num">${i + 1}</span>
          <span style="flex:1"></span>
          <button class="btn btn-sm" title="Move earlier"
                  onclick="Storyboard.move(${i}, -1)">↑</button>
          <button class="btn btn-sm" title="Move later"
                  onclick="Storyboard.move(${i}, 1)">↓</button>
          <button class="btn btn-sm btn-danger" title="Remove panel"
                  onclick="Storyboard.removePanel(${i})">✕</button>
        </div>
        <div class="sb-thumb">${this.thumbFor(p)}</div>
        ${this.narrationBlock(p, i)}
        <textarea class="sb-note" rows="3" placeholder="Shot note — what happens, why it is here"
                  oninput="Storyboard.setNote(${i}, this.value)">${esc(p.note || '')}</textarea>
        <textarea class="sb-prompt" rows="2"
                  placeholder="Video prompt — what to generate for this beat"
                  oninput="Storyboard.setPrompt(${i}, this.value)">${esc(p.video_prompt || '')}</textarea>
        <div class="sb-meta">
          <select onchange="Storyboard.setSource(${i}, this.value)"
                  title="Which video this frame came from">
            ${this.sourceOptions(p.source_item_id)}
          </select>
          <input type="number" step="0.01" min="0" placeholder="sec"
                 value="${p.timecode ?? ''}"
                 title="Timecode in the source video"
                 onchange="Storyboard.setTimecode(${i}, this.value)">
          <span class="sb-frame" id="sb-frame-${esc(p.id)}"
                title="Frame, derived from timecode and the source frame rate">${
            p.frame == null ? '—' : 'f' + p.frame}</span>
        </div>
      </div>`).join('');
  },

  // A beat can be seen, heard, or merely asked for, and the three failure
  // modes must not look alike. "image unavailable" used to be shown for all
  // of them, so a quote-only beat was indistinguishable from a lost file —
  // which destroys the exact signal `missing[]` exists to carry.
  thumbFor(p) {
    if (p.image_url) {
      return `<img src="${esc(p.image_url)}" loading="lazy" alt="${esc(p.title)}">`;
    }
    if (p.missing) return '<div class="sb-missing">image unavailable</div>';
    if (p.narration) {
      const text = p.narration.text;
      return text
        ? `<div class="sb-quote">${esc(text)}</div>`
        : '<div class="sb-placeholder">narration — no visual yet</div>';
    }
    if ((p.video_prompt || '').trim()) {
      return '<div class="sb-placeholder">prompt only — nothing shot yet</div>';
    }
    return '<div class="sb-placeholder">empty beat</div>';
  },

  // Where this beat falls in the piece. Laid out server-side from the words,
  // because the words are the spine — a beat's duration is its narration's.
  timelineFor(id) {
    return (this.board?.timeline || []).find(t => t.id === id) || null;
  },

  narrationBlock(p, i) {
    const n = p.narration;
    if (!n) return '';
    if (n.missing) {
      return '<div class="sb-narr sb-missing">narration clip is gone from the library</div>';
    }
    const at = this.timelineFor(p.id);
    const who = (n.attribution || {}).person;
    const show = (n.attribution || {}).episode_title;
    // Word indices rather than seconds: they mean something to a person —
    // "lobster" to "antidepressants" rather than 477.45 to 487.15. They stay
    // valid across a re-cut but do not keep pointing at the same words, which
    // is what beats_drifted in a recut result exists to report.
    return `
      <div class="sb-narr">
        <div class="sb-narr-time">
          <span class="sb-dur">${n.duration == null ? '—' : n.duration.toFixed(1) + 's'}</span>
          ${at ? `<span class="sb-at" title="Start and end within the piece">${
            fmtDuration(at.at)} → ${fmtDuration(at.until)}</span>` : ''}
          <span style="flex:1"></span>
          <span class="sb-precision" title="${n.precision === 'word'
            ? 'Times read from the clip word manifest'
            : 'No word manifest — the beat is the whole clip'}">${esc(n.precision)}</span>
        </div>
        ${who || show ? `<div class="sb-attrib">${esc(who || '')}${
          who && show ? ' · ' : ''}${esc(show || '')}</div>` : ''}
        <div class="sb-words">
          <label>words</label>
          <input type="number" min="0" step="1" placeholder="0"
                 value="${n.word_start ?? ''}"
                 title="First word of the quote, by index"
                 onchange="Storyboard.setWordStart(${i}, this.value)">
          <span>–</span>
          <input type="number" min="0" step="1" placeholder="end"
                 value="${n.word_end ?? ''}"
                 title="Last word of the quote, by index"
                 onchange="Storyboard.setWordEnd(${i}, this.value)">
          <span class="sb-wordcount" title="${n.word_count || 0} of the clip's ${
            n.word_total || 0} words are in this beat">of ${n.word_total || 0}</span>
        </div>
        ${n.audio_url ? `<audio controls preload="none" src="${esc(n.audio_url)}"></audio>` : ''}
      </div>`;
  },

  renderEstimate() {
    const el = document.getElementById('sb-estimate');
    if (!el || !this.board) return;
    const n = this.board.panels.length;
    if (n === 0) { el.textContent = ''; return; }
    const cols = parseInt(document.getElementById('sb-cols').value) || 3;
    const tw = parseInt(document.getElementById('sb-tile-width').value) || 360;
    const pad = parseInt(document.getElementById('sb-padding').value) || 16;
    const aspect = parseFloat(document.getElementById('sb-aspect').value) || (16 / 9);
    const rows = Math.ceil(n / cols);
    const w = cols * tw + (cols + 1) * pad;
    // Caption height is not known until the notes are wrapped, so this is the
    // panel area only — an honest floor rather than a guessed total.
    const h = rows * Math.round(tw / aspect) + (rows + 1) * pad;
    const maxW = parseInt(document.getElementById('sb-max-width').value) || 0;
    const scale = maxW && w > maxW ? maxW / w : 1;
    el.textContent = `${cols}×${rows} · about ${Math.round(w * scale)}×${Math.round(h * scale)}px before captions`;
  },

  // ── Render ────────────────────────────────────────────────────────────────

  async render() {
    if (!this.board) return;
    if (this.board.panels.length === 0) { toast('Board has no panels', 'error'); return; }
    const btn = document.getElementById('sb-render-btn');
    btn.disabled = true;
    btn.textContent = 'Rendering…';
    try {
      await this.save();          // never render a board that is mid-edit
      const body = {
        cols: parseInt(document.getElementById('sb-cols').value) || 3,
        tile_width: parseInt(document.getElementById('sb-tile-width').value) || 360,
        padding: parseInt(document.getElementById('sb-padding').value) || 16,
        aspect: parseFloat(document.getElementById('sb-aspect').value) || null,
        max_width: document.getElementById('sb-max-width').value || null,
      };
      if (!document.getElementById('sb-show-title').checked) body.title = '';
      const result = await api(
        `/api/storyboards/${encodeURIComponent(this.board.id)}/render`,
        { method: 'POST', body });

      const missing = (result.missing || []).length;
      toast(`${result.panels} panels → ${result.grid} PNG`
            + (missing ? ` (${missing} image${missing === 1 ? '' : 's'} missing)` : ''),
            missing ? 'error' : 'success');
      document.getElementById('sb-render-result').innerHTML = `
        <div class="section-card">
          <h3>${esc(result.filename)} — ${result.grid}, ${result.width}×${result.height}, ${fmtBytes(result.size_bytes)}</h3>
          ${missing ? `<div style="color:var(--orange);font-size:12px;margin-bottom:8px">
            Panel${missing === 1 ? '' : 's'} ${result.missing.join(', ')} had no image file and rendered as a placeholder.</div>` : ''}
          <a href="/api/exports/${encodeURIComponent(result.filename)}" target="_blank">
            <img src="/api/exports/${encodeURIComponent(result.filename)}"
                 style="max-width:100%;border-radius:5px;border:1px solid var(--border)">
          </a>
        </div>`;
    } catch (e) {
      toast(e.message, 'error');
    } finally {
      btn.disabled = false;
      btn.textContent = 'Render Storyboard';
    }
  },
};

document.addEventListener('DOMContentLoaded', () => {
  ['sb-cols', 'sb-tile-width', 'sb-padding', 'sb-aspect', 'sb-max-width'].forEach(id => {
    const el = document.getElementById(id);
    if (el) el.addEventListener('input', () => Storyboard.renderEstimate());
  });

  const zone = document.getElementById('sb-drop-zone');
  if (!zone) return;
  const input = document.getElementById('sb-file-input');
  zone.addEventListener('click', () => input.click());
  zone.addEventListener('dragover', e => { e.preventDefault(); zone.classList.add('dragover'); });
  zone.addEventListener('dragleave', () => zone.classList.remove('dragover'));
  zone.addEventListener('drop', e => {
    e.preventDefault();
    zone.classList.remove('dragover');
    Storyboard.importFiles(e.dataTransfer.files);
  });
  input.addEventListener('change', e => {
    Storyboard.importFiles(e.target.files);
    e.target.value = '';
  });
});
