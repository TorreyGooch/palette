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
        body: { name: this.board.name, description: this.board.description || '',
                aspect: this.board.aspect ?? null, panels: this.board.panels },
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

  // The two prompts. `note` has its own setter because it is not written at a
  // model: it says why the beat is here and survives every prompt rewrite.
  setText(idx, field, value) {
    if (!this.board) return;
    this.board.panels[idx][field] = value;
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
    this.applyAspect();
    const desc = document.getElementById('sb-description');
    desc.value = this.board.description || '';
    this.fit(desc);
    this.setSaveState('saved');
    this.pickerOpen = false;
    this.renderPicker();
    this.renderPanels();
    document.getElementById('sb-render-result').innerHTML = '';
  },

  // The frame shape belongs to the piece, so it is saved on the board and the
  // panels are drawn in it. It used to be a render-only dropdown that reset on
  // every load, so a vertical video was worked on in sideways 16:9 boxes.
  setAspect(value) {
    if (!this.board) return;
    const n = parseFloat(value);
    this.board.aspect = isNaN(n) ? null : n;
    this.applyAspect();
    this.renderEstimate();
    this.queueSave();
  },

  applyAspect() {
    const aspect = this.board?.aspect || (16 / 9);
    const panels = document.getElementById('sb-panels');
    if (panels) panels.style.setProperty('--sb-aspect', String(aspect));
    const select = document.getElementById('sb-aspect');
    if (!select) return;
    // Pick the listed shape this is; a value set through the API that is not
    // in the list gets its own entry rather than silently showing another.
    const match = [...select.options].find(o => Math.abs(parseFloat(o.value) - aspect) < 0.005);
    if (match) { select.value = match.value; return; }
    const custom = new Option(`${aspect.toFixed(3)} — set on this board`, String(aspect));
    select.add(custom);
    select.value = custom.value;
  },

  // What the piece is. Not a beat's business: a beat only knows its own frame.
  describeBoard(value) {
    if (!this.board) return;
    this.board.description = value;
    this.queueSave();
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
          ${this.continuesFrom(panels, i)}
          <span style="flex:1"></span>
          <button class="btn btn-sm" title="Move earlier"
                  onclick="Storyboard.move(${i}, -1)">↑</button>
          <button class="btn btn-sm" title="Move later"
                  onclick="Storyboard.move(${i}, 1)">↓</button>
          <button class="btn btn-sm btn-danger" title="Remove panel"
                  onclick="Storyboard.removePanel(${i})">✕</button>
        </div>
        <div class="sb-thumb">${this.thumbFor(p)}</div>
        ${this.candidateBar(p, i)}
        ${this.narrationBlock(p, i)}
        <textarea class="sb-note" rows="3" placeholder="Note — why this beat is here"
                  oninput="Storyboard.setNote(${i}, this.value)">${esc(p.note || '')}</textarea>
        <textarea class="sb-prompt" rows="2"
                  placeholder="Image prompt — what is in the shot, and how it is shot"
                  oninput="Storyboard.setText(${i}, 'image_prompt', this.value)">${esc(p.image_prompt || '')}</textarea>
        ${this.generateRow(p, i)}
        <textarea class="sb-prompt sb-video-prompt" rows="2"
                  placeholder="Video prompt — written last, once the beat is settled"
                  oninput="Storyboard.setText(${i}, 'video_prompt', this.value)">${esc(p.video_prompt || '')}</textarea>
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
    this.fitAll(el);
  },

  // Every text on a board is shown whole. A fixed-height box that clips its
  // own contents made reading a board a chore: five beats with three written
  // fields each was fifteen drag-to-resize gestures just to see what was
  // there. So a box is exactly as tall as its text, on render, on every
  // keystroke, and again when the window width changes the wrapping.
  fit(ta) {
    // An empty box keeps its `rows` height. Measuring one sizes it to its
    // wrapped *placeholder* - an empty board description came out 1294px tall
    // - and there is no text in it to show anyway.
    if (!ta.value) { ta.style.height = ''; return; }
    // No width yet means no wrapping to measure; the observer below refits.
    if (!ta.clientWidth) return;
    const cs = getComputedStyle(ta);
    ta.style.height = 'auto';
    const extra = cs.boxSizing === 'border-box'
      ? ta.offsetHeight - ta.clientHeight
      : -(parseFloat(cs.paddingTop) + parseFloat(cs.paddingBottom));
    // +1: line-height is fractional (12px x 1.45), scrollHeight is rounded.
    ta.style.height = `${Math.ceil(ta.scrollHeight + extra) + 1}px`;
  },

  fitAll(root = document) {
    root.querySelectorAll(FIT_SELECTOR).forEach(ta => this.fit(ta));
  },

  // A beat can be seen, heard, or merely asked for, and the three failure
  // modes must not look alike. "image unavailable" used to be shown for all
  // of them, so a quote-only beat was indistinguishable from a lost file —
  // which destroys the exact signal `missing[]` exists to carry.
  // Which candidate a beat is showing. Deliberately not saved: it is where
  // you are looking, not a decision, and a board is a record of decisions.
  shown: {},

  // Generate jobs in flight, by beat id. Where you are waiting, not a
  // decision, so like `shown` it is never saved.
  generating: {},

  // Where browsing starts: on the chosen reference if there is one, so
  // opening a board shows what was picked rather than always the first.
  shownIndex(p) {
    const list = p.candidate_items || [];
    if (!list.length) return null;
    if (this.shown[p.id] != null) return Math.min(this.shown[p.id], list.length - 1);
    const chosen = list.findIndex(c => c.id === p.item_id);
    return chosen >= 0 ? chosen : 0;
  },

  cycle(idx, delta) {
    const p = this.board.panels[idx];
    const n = (p.candidate_items || []).length;
    if (!n) return;
    const at = ((this.shownIndex(p) ?? 0) + delta + n) % n;
    this.shown[p.id] = at;
    this.renderPanels();
  },

  // The one moment a person's taste enters. Everything up to here can be
  // done unattended; this cannot, which is why generating never does it.
  chooseCandidate(idx) {
    const p = this.board.panels[idx];
    const at = this.shownIndex(p) ?? 0;
    const chosen = (p.candidate_items || [])[at];
    if (!chosen) return;
    // Click the chosen one again to unpick: a choice is reconsiderable, and
    // the other references stay listed precisely so it can be.
    p.item_id = p.item_id === chosen.id ? null : chosen.id;
    this.save(true);
  },

  // The navigation used to live inside the image box, which is a fixed 16:9
  // with overflow hidden and centred content. Once a reference loaded, the
  // image filled the box and pushed the arrows out of its bottom edge - so
  // after a generate there was no visible way to reach the second or third.
  // It is its own row now, under the picture, and cannot be clipped by it.
  candidateBar(p, i) {
    const list = p.candidate_items || [];
    if (!list.length) return '';
    const at = this.shownIndex(p);
    const ref = list[at];
    const picked = p.item_id === ref.id;
    const many = list.length > 1;
    return `
      <div class="sb-cand-bar">
        <button class="btn btn-sm" onclick="Storyboard.cycle(${i}, -1)"
                ${many ? '' : 'disabled'} title="Previous reference">‹</button>
        <span class="sb-cand-count">${at + 1} / ${list.length}</span>
        <button class="btn btn-sm" onclick="Storyboard.cycle(${i}, 1)"
                ${many ? '' : 'disabled'} title="Next reference">›</button>
        <button class="btn btn-sm ${picked ? 'btn-primary' : ''}"
                onclick="Storyboard.chooseCandidate(${i})"
                title="${picked ? 'Chosen - click to unpick' : 'Use this one'}"
                >${picked ? '✓ chosen' : 'use this'}</button>
      </div>`;
  },

  thumbFor(p) {
    // With references, the box shows the one being browsed - which starts on
    // the chosen one - so cycling changes the picture you are looking at. A
    // chosen image that is not a reference (a library image) still shows
    // until someone starts browsing.
    const list = p.candidate_items || [];
    const browsing = this.shown[p.id] != null;
    const chosenIsRef = list.some(c => c.id === p.item_id);
    if (list.length && (!p.item_id || chosenIsRef || browsing)) {
      const at = this.shownIndex(p);
      const ref = list[at];
      return `<img src="${esc(ref.image_url)}" alt="reference ${at + 1} of ${list.length}">`;
    }
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
    if ((p.image_prompt || p.video_prompt || '').trim()) {
      return '<div class="sb-placeholder">written only — nothing shot yet</div>';
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
          <span class="sb-dur" title="${n.hold_s ? `Runs until the next beat starts, including a ${n.hold_s.toFixed(2)}s pause` : 'First word to last word'}">${
            n.duration == null ? '—' : n.duration.toFixed(1) + 's'}${n.hold_s ? ' ·hold' : ''}</span>
          ${at ? `<span class="sb-at" title="Start and end within the piece">${
            fmtDuration(at.at)} → ${fmtDuration(at.until)}</span>` : ''}
          <span style="flex:1"></span>
          <span class="sb-precision" title="${n.precision === 'word'
            ? 'Times read from the clip word manifest'
            : 'No word manifest — the beat is the whole clip'}">${esc(n.precision)}</span>
        </div>
        ${who || show ? `<div class="sb-attrib">${esc(who || '')}${
          who && show ? ' · ' : ''}${esc(show || '')}</div>` : ''}
        ${n.transcription_flags?.suspect ? `<div class="sb-flag" title="${
          esc(n.transcription_flags.reasons.join('\n'))}">⚠ transcript looks wrong here: ${
          esc(n.transcription_flags.reasons[0])}</div>` : ''}
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
        ${this.pauseStrip(p, i)}
      </div>`;
  },

  // The pause structure, which is the actual creative handle and was the one
  // thing the page could not show. Every qs cut clip has per-word times in its
  // manifest, so the gaps are subtraction — no GPU, no network, no new data.
  //
  // Prose cannot show you a hold. On this board the split that looked obvious
  // from reading the transcript was the *weakest* pause in the clip; the real
  // break was a 1360ms hesitation before the payload, visible only here.
  pauseStrip(p, i) {
    const words = (p.narration || {}).words || [];
    if (words.length < 2) return '';
    const gaps = words.map(w => w.gap_before).filter(g => g != null);
    const longest = gaps.length ? Math.max(...gaps) : 0;

    const bits = words.map((w, k) => {
      // The first word's gap is where this beat begins, not a split point
      // inside it, so it is shown and not clickable.
      const splittable = k > 0;
      const gap = w.gap_before;
      let sep = '';
      if (gap != null && (k > 0 || gap >= 0.12)) {
        const strong = longest > 0 && gap >= Math.max(0.25, longest * 0.6);
        sep = `<span class="sb-gap${strong ? ' strong' : ''}${splittable ? ' hit' : ''}"
                     title="${Math.round(gap * 1000)} ms pause${
                       splittable ? ' — click to split the beat here' : ''}"
                     ${splittable ? `onclick="Storyboard.splitAt(${i}, ${w.index})"` : ''}
                     >${gap >= 0.12 ? Math.round(gap * 1000) : ''}</span>`;
      }
      // Any word can start a new beat, not only one after a pause: a shot
      // changes where the picture needs it to, and the timing stays whole
      // either way because a beat runs until the next one starts.
      const word = splittable
        ? `<span class="sb-word hit" title="Start a new beat at this word"
                 onclick="Storyboard.splitAt(${i}, ${w.index})">${esc(w.word)}</span>`
        : `<span class="sb-word">${esc(w.word)}</span>`;
      return sep + word;
    });

    // Said on the page, not only in a tooltip: splitting was built and then
    // went unfound, because nothing visible said the numbers were clickable.
    return `<div class="sb-pauses">
      <div class="sb-pauses-hint">click any word to start a new beat there</div>${
      bits.join('')}</div>`;
  },

  // Split a beat at a pause: the words before it stay, the rest become a new
  // beat immediately after. Done by the server rather than by rewriting the
  // panel list here, so it is one primitive whether a person clicks it or a
  // session asks for it in conversation - and so no stale autosave can undo it.
  async splitAt(idx, wordIndex) {
    if (!this.board?.panels[idx]?.narration) return;
    // Flush first. The split is written server-side, and an edit still
    // waiting in the autosave queue would otherwise be lost to it.
    await this.save();
    const beat = this.board.panels[idx];
    if (!beat?.id) return;
    try {
      this.board = await api(
        `/api/storyboards/${encodeURIComponent(this.board.id)}/panels/${
          encodeURIComponent(beat.id)}/split`,
        { method: 'POST', body: { at_word: wordIndex } });
    } catch (e) { toast(e.message, 'error'); return; }
    this.renderPanels();
    const summary = this.boards.find(b => b.id === this.board.id);
    if (summary) summary.panels = this.board.panels.length;
    this.renderBoardList();
  },

  // "continues 2" on a beat that picks up the previous beat's quote at the
  // next word, so a split reads as one quote in several shots rather than as
  // separate quotes that happen to sit together.
  continuesFrom(panels, i) {
    const prev = panels[i - 1]?.narration;
    const cur = panels[i]?.narration;
    if (!prev || !cur || prev.missing || cur.missing) return '';
    if (prev.item_id !== cur.item_id || prev.word_end == null) return '';
    if (cur.word_start !== prev.word_end + 1) return '';
    return `<span class="sb-continues" title="The same quote as beat ${i}, carried on at the next word">continues ${i}</span>`;
  },

  // ── Generating references ─────────────────────────────────────────────────

  generateRow(p, i) {
    const busy = this.generating[p.id];
    const has = (p.candidate_items || []).length;
    const label = busy ? this.generateLabel(busy)
      : has ? 'Generate 3 more' : 'Generate 3';
    return `
      <div class="sb-gen">
        <button class="btn btn-sm" id="sb-gen-${esc(p.id)}" ${busy ? 'disabled' : ''}
                title="Render this beat's image prompt into three references. Nothing is chosen for you."
                onclick="Storyboard.generate(${i})">${esc(label)}</button>
      </div>`;
  },

  generateLabel(busy) {
    const secs = Math.round((Date.now() - busy.t0) / 1000);
    return `${busy.stage || 'queued'} · ${secs}s`;
  },

  // The image prompt as written on the beat, rendered into references that
  // land on this beat as candidates. Generating never selects; choosing
  // stays with whoever cycles through them.
  async generate(idx) {
    if (!this.board?.panels[idx]) return;
    // The references attach to the beat by id on the server, so the beat and
    // its prompt have to be there first.
    await this.save();
    const beat = this.board.panels[idx];
    if (!beat?.id || this.generating[beat.id]) return;
    const prompt = (beat.image_prompt || '').trim();
    if (!prompt) {
      toast('Write an image prompt on this beat first - that is what gets rendered', 'error');
      return;
    }
    const boardId = this.board.id;
    const beatId = beat.id;
    let job_id;
    try {
      ({ job_id } = await api('/api/generate', {
        method: 'POST',
        body: { prompt, count: 3, board_id: boardId, beat_id: beatId },
      }));
    } catch (e) { toast(e.message, 'error'); return; }

    this.generating[beatId] = { stage: 'queued', t0: Date.now() };
    this.paintGenerate(beatId);
    const poll = setInterval(async () => {
      let job;
      try { job = await api(`/api/qs/pull/${job_id}`); } catch { return; }
      const busy = this.generating[beatId];
      if (!busy) { clearInterval(poll); return; }
      busy.stage = job.stage;
      if (!job.done) { this.paintGenerate(beatId); return; }
      clearInterval(poll);
      delete this.generating[beatId];
      if (job.error) {
        toast(`Generate failed: ${job.error}`, 'error');
        this.paintGenerate(beatId);
        return;
      }
      if (job.warning) toast(job.warning);
      toast(`${(job.item_ids || []).length} references ready - cycle through them on the beat`, 'success');
      if (this.board?.id === boardId) await this.refreshKeepingFocus();
    }, 1500);
  },

  // Update one button in place; a full re-render every poll would take the
  // cursor out of whatever is being typed.
  paintGenerate(beatId) {
    const btn = document.getElementById(`sb-gen-${beatId}`);
    if (!btn) return;
    const busy = this.generating[beatId];
    btn.disabled = !!busy;
    const beat = this.board?.panels.find(p => p.id === beatId);
    btn.textContent = busy ? this.generateLabel(busy)
      : (beat?.candidate_items || []).length ? 'Generate 3 more' : 'Generate 3';
  },

  // Show references that arrived while the page was open. A save rather than
  // a plain reload: it sends anything typed in the meantime, the server keeps
  // the candidates the page has not heard of, and the response has both. The
  // re-render would drop the cursor, so it is put back where it was.
  async refreshKeepingFocus() {
    const active = document.activeElement;
    const panelEl = active?.closest?.('.sb-panel');
    let restore = null;
    if (panelEl && active.tagName === 'TEXTAREA') {
      restore = {
        index: [...panelEl.parentElement.children].indexOf(panelEl),
        cls: active.className,
        from: active.selectionStart,
        to: active.selectionEnd,
      };
    }
    await this.save(true);
    if (!restore) return;
    const panel = document.getElementById('sb-panels')?.children[restore.index];
    const ta = panel?.querySelector(`textarea[class="${restore.cls}"]`);
    if (ta) { ta.focus(); ta.setSelectionRange(restore.from, restore.to); }
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

  // ── Viewing a render ──────────────────────────────────────────────────────

  lastRender: null,

  showRender(result = this.lastRender) {
    if (!result) return;
    const url = `/api/exports/${encodeURIComponent(result.filename)}`;
    const img = document.getElementById('sb-viewer-img');
    img.src = url;
    // A button rather than a link: .btn sets no colours of its own, so an <a>
    // with it rendered as the browser default blue underline on the dark bar,
    // while the buttons beside it got the default button face.
    this.renderUrl = url;
    document.getElementById('sb-viewer-meta').innerHTML =
      `<strong>${esc(result.filename)}</strong>
       <span>${esc(result.grid)} · ${result.width}×${result.height} · ${fmtBytes(result.size_bytes)}</span>
       <span class="sb-viewer-path">saved in the library's exports folder</span>`;
    const missing = result.missing || [];
    const warn = document.getElementById('sb-viewer-warn');
    warn.classList.toggle('hidden', !missing.length);
    warn.textContent = missing.length
      ? `Panel${missing.length === 1 ? '' : 's'} ${missing.join(', ')} had no image file and rendered as a placeholder.`
      : '';
    this.setRenderZoom(false);
    document.getElementById('sb-viewer').classList.remove('hidden');
  },

  renderUrl: null,

  openRenderTab() {
    if (this.renderUrl) window.open(this.renderUrl, "_blank", "noopener");
  },

  // Close on the Close button, Esc, or a click on the dark backdrop - but not
  // on a click inside the bar or on the image itself.
  closeRender(event) {
    if (event && event.target.id !== 'sb-viewer' && event.target.id !== 'sb-viewer-stage') return;
    document.getElementById('sb-viewer').classList.add('hidden');
  },

  // Fitted to the window by default, so a whole board is visible at once;
  // actual size for reading captions, scrolling inside the stage.
  toggleRenderZoom() {
    const stage = document.getElementById('sb-viewer-stage');
    this.setRenderZoom(!stage.classList.contains('actual'));
  },

  setRenderZoom(actual) {
    document.getElementById('sb-viewer-stage').classList.toggle('actual', actual);
    document.getElementById('sb-viewer-zoom').textContent = actual ? 'Fit to window' : 'Actual size';
  },

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
      this.lastRender = result;
      this.showRender(result);
      document.getElementById('sb-render-result').innerHTML = `
        <div class="section-card">
          <h3 style="display:flex;align-items:center;gap:10px">
            <span style="flex:1">${esc(result.filename)} — ${result.grid}, ${result.width}×${result.height}, ${fmtBytes(result.size_bytes)}</span>
            <button class="btn btn-sm" onclick="Storyboard.showRender()">View</button>
          </h3>
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

document.addEventListener('keydown', e => {
  const viewer = document.getElementById('sb-viewer');
  if (e.key === 'Escape' && viewer && !viewer.classList.contains('hidden')) {
    viewer.classList.add('hidden');
  }
});

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

// Auto-grow is wired once, by delegation, rather than into each template's
// oninput: panels are re-rendered wholesale and a listener per textarea would
// have to be re-attached every time.
const FIT_SELECTOR = '.sb-note, .sb-prompt, .sb-board-desc';
document.addEventListener('input', e => {
  if (e.target.matches?.(FIT_SELECTOR)) Storyboard.fit(e.target);
});
// Wrapping depends on width, and width changes for reasons no event names:
// the editor being un-hidden, the sidebar, the window. So watch the width
// itself. Height is ignored on purpose - fitting changes the editor's height,
// and reacting to that would loop.
let fitWidth = 0;
const fitObserver = new ResizeObserver(entries => {
  const w = Math.round(entries[0].contentRect.width);
  if (w && w !== fitWidth) {
    fitWidth = w;
    Storyboard.fitAll();
  }
});
document.addEventListener('DOMContentLoaded', () => {
  const editor = document.getElementById('sb-editor');
  if (editor) fitObserver.observe(editor);
});
