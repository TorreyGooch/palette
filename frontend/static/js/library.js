const Library = {
  items: [],
  filtered: [],
  palettes: [],
  tags: [],
  filterType: '',        // '' | 'image' | 'video'
  filterTag: '',
  filterPalette: '',
  selected: new Set(),
  focusedId: null,
  lastClickIdx: null,
  _currentItem: null,

  async load() {
    await this.refresh();
  },

  async refresh() {
    try {
      [this.items, this.palettes, this.tags] = await Promise.all([
        api('/api/items'),
        api('/api/palettes'),
        api('/api/tags'),
      ]);
    } catch { this.items = []; this.palettes = []; this.tags = []; }
    this.applyFilter();
    this.renderFilters();
    this.renderGrid();
    this.renderBatchBar();
    if (this.focusedId) {
      const still = this.items.find(i => i.id === this.focusedId);
      if (still) this.openDetail(still);
      else this.closeDetail();
    }
  },

  applyFilter() {
    this.filtered = this.items.filter(i => {
      if (this.filterType && i.type !== this.filterType) return false;
      if (this.filterTag && !i.tags.includes(this.filterTag)) return false;
      if (this.filterPalette && !i.palettes.includes(this.filterPalette)) return false;
      return true;
    });
    const ids = new Set(this.filtered.map(i => i.id));
    for (const id of this.selected) {
      if (!ids.has(id)) this.selected.delete(id);
    }
  },

  setTypeFilter(t) {
    this.filterType = t;
    this.applyFilter(); this.renderFilters(); this.renderGrid(); this.renderBatchBar();
  },
  setTagFilter(t) {
    this.filterTag = t;
    this.applyFilter(); this.renderFilters(); this.renderGrid(); this.renderBatchBar();
  },
  setPaletteFilter(p) {
    this.filterPalette = p;
    this.applyFilter(); this.renderFilters(); this.renderGrid(); this.renderBatchBar();
  },

  renderFilters() {
    // Type buttons
    document.querySelectorAll('[data-typefilter]').forEach(b => {
      b.classList.toggle('active-all', b.dataset.typefilter === this.filterType);
    });

    // Palette chips
    const pel = document.getElementById('palette-filter-list');
    pel.innerHTML = `<button class="filter-btn ${this.filterPalette === '' ? 'active-all' : ''}"
        onclick="Library.setPaletteFilter('')">All</button>` +
      this.palettes.map(p => `
        <button class="filter-btn ${this.filterPalette === p.id ? 'active-all' : ''}"
          onclick="Library.setPaletteFilter('${esc(p.id)}')">${esc(p.name)} (${p.count})</button>`).join('') +
      `<button class="filter-btn" onclick="Library.newPalette()" title="New palette">+ palette</button>`;

    // Tag dropdown
    const tsel = document.getElementById('tag-filter-select');
    const cur = this.filterTag;
    tsel.innerHTML = '<option value="">All tags</option>' +
      this.tags.map(t => `<option value="${esc(t.tag)}" ${t.tag === cur ? 'selected' : ''}>${esc(t.tag)} (${t.count})</option>`).join('');

    // Stats
    document.getElementById('library-stats').textContent =
      `${this.filtered.length} of ${this.items.length} items`;

    // Batch palette select
    const bsel = document.getElementById('batch-palette-select');
    if (bsel) {
      bsel.innerHTML = '<option value="">palette…</option>' +
        this.palettes.map(p => `<option value="${esc(p.id)}">${esc(p.name)}</option>`).join('');
    }
  },

  async newPalette() {
    const name = prompt('New palette name:');
    if (!name || !name.trim()) return;
    try {
      await api('/api/palettes', { method: 'POST', body: { name: name.trim() } });
      await this.refresh();
    } catch (e) { toast(e.message, 'error'); }
  },

  renderGrid() {
    const grid = document.getElementById('lib-grid');
    if (!grid) return;
    if (this.filtered.length === 0) {
      grid.innerHTML = '<div style="color:var(--muted);font-size:13px;grid-column:1/-1;padding:20px">No items match this filter</div>';
      return;
    }
    grid.innerHTML = this.filtered.map((item, idx) => {
      const sel = this.selected.has(item.id);
      const focused = this.focusedId === item.id;
      const badge = item.type === 'video'
        ? `<span class="badge badge-approved">▶ ${fmtDuration(item.duration)}</span>`
        : item.type === 'audio'
          ? `<span class="badge badge-flagged">♪ ${fmtDuration(item.duration)}</span>`
          : `<span class="badge badge-unreviewed">img</span>`;
      return `
        <div class="thumb-card ${sel ? 'selected' : ''} ${focused ? 'focused' : ''}"
             onclick="Library.handleClick(event, '${esc(item.id)}', ${idx})">
          <div class="check-mark">✓</div>
          <img src="/api/items/${esc(item.id)}/thumbnail" loading="lazy"
               onerror="this.style.background='#111'" alt="">
          <div class="thumb-status">${badge}</div>
          <div class="thumb-info">
            <div class="thumb-dur" style="font-size:11px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis">${esc(item.title)}</div>
            <div class="thumb-id">${item.tags.slice(0, 3).map(esc).join(' · ') || '<span style="opacity:.5">untagged</span>'}</div>
          </div>
        </div>`;
    }).join('');
  },

  handleClick(event, itemId, idx) {
    if (event.shiftKey && this.lastClickIdx !== null) {
      const lo = Math.min(this.lastClickIdx, idx);
      const hi = Math.max(this.lastClickIdx, idx);
      for (let i = lo; i <= hi; i++) this.selected.add(this.filtered[i].id);
    } else if (event.ctrlKey || event.metaKey) {
      if (this.selected.has(itemId)) this.selected.delete(itemId);
      else this.selected.add(itemId);
    } else {
      this.selected.clear();
      this.selected.add(itemId);
      this.openDetail(this.filtered[idx]);
    }
    this.lastClickIdx = idx;
    this.renderGrid();
    this.renderBatchBar();
  },

  renderBatchBar() {
    const bar = document.getElementById('lib-batch-bar');
    if (!bar) return;
    const count = this.selected.size;
    bar.classList.toggle('hidden', count === 0);
    document.getElementById('lib-sel-count').textContent = `${count} selected`;
  },

  async applyBatchTag(action) {
    const tag = document.getElementById('lib-batch-tag-input').value.trim();
    if (!tag) { toast('Enter a tag', 'error'); return; }
    try {
      await api('/api/items/batch-tag', {
        method: 'POST',
        body: { item_ids: [...this.selected], tag, action },
      });
      document.getElementById('lib-batch-tag-input').value = '';
      toast(`Tag "${tag}" ${action === 'add' ? 'added' : 'removed'}`, 'success');
      await this.refresh();
    } catch (e) { toast(e.message, 'error'); }
  },

  async applyBatchPalette(action) {
    const pid = document.getElementById('batch-palette-select').value;
    if (!pid) { toast('Choose a palette', 'error'); return; }
    try {
      await api('/api/items/batch-palette', {
        method: 'POST',
        body: { item_ids: [...this.selected], palette_id: pid, action },
      });
      toast(`Palette ${action === 'add' ? 'assigned' : 'removed'}`, 'success');
      await this.refresh();
    } catch (e) { toast(e.message, 'error'); }
  },

  async batchDelete() {
    const count = this.selected.size;
    if (count === 0) return;
    if (!confirm(`Delete ${count} item${count !== 1 ? 's' : ''} from the library? Files are removed too. This cannot be undone.`)) return;
    const ids = [...this.selected];
    let failed = 0;
    for (const id of ids) {
      try { await api(`/api/items/${id}`, { method: 'DELETE' }); }
      catch { failed++; }
    }
    this.selected.clear();
    if (this.focusedId && ids.includes(this.focusedId)) this.closeDetail();
    await this.refresh();
    toast(failed ? `Deleted ${ids.length - failed}, ${failed} failed` : `Deleted ${ids.length} items`);
  },

  clearSelection() {
    this.selected.clear();
    this.renderGrid();
    this.renderBatchBar();
  },

  // ── Detail panel ──────────────────────────────────────────────────────────

  openDetail(item) {
    this.focusedId = item.id;
    this._currentItem = item;
    document.getElementById('lib-detail-empty').classList.add('hidden');
    document.getElementById('lib-detail-content').classList.remove('hidden');

    const vid = document.getElementById('lib-detail-video');
    const img = document.getElementById('lib-detail-image');
    const aud = document.getElementById('lib-detail-audio');
    vid.classList.add('hidden');
    img.classList.add('hidden');
    aud.classList.add('hidden');
    vid.pause?.();
    aud.pause?.();
    const src = `/api/media/${encodeURIComponent(item.filename)}`;
    if (item.type === 'video') {
      vid.classList.remove('hidden');
      vid.src = src;
      vid.loop = true;
      vid.load();
    } else if (item.type === 'audio') {
      aud.classList.remove('hidden');
      aud.src = src;
      aud.load();
    } else {
      img.classList.remove('hidden');
      img.src = src;
    }

    document.getElementById('lib-detail-title').value = item.title;
    document.getElementById('lib-detail-meta').textContent =
      `${item.filename}${item.duration ? ' · ' + fmtDuration(item.duration) : ''}${item.fps ? ' · ' + item.fps.toFixed(2) + ' fps' : ''}`;

    this.renderDetailTags();
    this.renderDetailPalettes();
  },

  closeDetail() {
    this.focusedId = null;
    this._currentItem = null;
    const vid = document.getElementById('lib-detail-video');
    vid?.pause?.();
    document.getElementById('lib-detail-empty').classList.remove('hidden');
    document.getElementById('lib-detail-content').classList.add('hidden');
  },

  renderDetailTags() {
    const item = this._currentItem;
    const el = document.getElementById('lib-detail-tags');
    el.innerHTML = item.tags.map((t, i) => `
      <span class="tag-pill">
        ${esc(t)}
        <span class="remove-tag" onclick="Library.removeDetailTag(${i})">×</span>
      </span>`).join('') || '<span style="color:var(--muted);font-size:12px">No tags</span>';
  },

  renderDetailPalettes() {
    const item = this._currentItem;
    const el = document.getElementById('lib-detail-palettes');
    const names = item.palettes
      .map(pid => this.palettes.find(p => p.id === pid))
      .filter(Boolean);
    el.innerHTML = names.map(p => `
      <span class="tag-pill">
        ${esc(p.name)}
        <span class="remove-tag" onclick="Library.removeDetailPalette('${esc(p.id)}')">×</span>
      </span>`).join('') || '<span style="color:var(--muted);font-size:12px">Not in any palette</span>';

    const sel = document.getElementById('lib-detail-palette-select');
    const available = this.palettes.filter(p => !item.palettes.includes(p.id));
    sel.innerHTML = '<option value="">add to palette…</option>' +
      available.map(p => `<option value="${esc(p.id)}">${esc(p.name)}</option>`).join('');
  },

  async saveTitle() {
    if (!this._currentItem) return;
    const title = document.getElementById('lib-detail-title').value.trim();
    try {
      await api(`/api/items/${this._currentItem.id}`, { method: 'PATCH', body: { title } });
      toast('Title saved', 'success');
      await this.refresh();
    } catch (e) { toast(e.message, 'error'); }
  },

  async addDetailTag() {
    if (!this._currentItem) return;
    const input = document.getElementById('lib-detail-tag-input');
    const tag = input.value.trim();
    if (!tag) return;
    const tags = [...this._currentItem.tags];
    if (!tags.includes(tag)) tags.push(tag);
    try {
      const updated = await api(`/api/items/${this._currentItem.id}`, {
        method: 'PATCH', body: { tags },
      });
      this._currentItem.tags = updated.tags;
      input.value = '';
      this.renderDetailTags();
      await this.refresh();
    } catch (e) { toast(e.message, 'error'); }
  },

  async removeDetailTag(idx) {
    if (!this._currentItem) return;
    const tags = [...this._currentItem.tags];
    tags.splice(idx, 1);
    try {
      const updated = await api(`/api/items/${this._currentItem.id}`, {
        method: 'PATCH', body: { tags },
      });
      this._currentItem.tags = updated.tags;
      this.renderDetailTags();
      await this.refresh();
    } catch (e) { toast(e.message, 'error'); }
  },

  async addDetailPalette() {
    if (!this._currentItem) return;
    const pid = document.getElementById('lib-detail-palette-select').value;
    if (!pid) return;
    const palettes = [...this._currentItem.palettes, pid];
    try {
      const updated = await api(`/api/items/${this._currentItem.id}`, {
        method: 'PATCH', body: { palettes },
      });
      this._currentItem.palettes = updated.palettes;
      this.renderDetailPalettes();
      await this.refresh();
    } catch (e) { toast(e.message, 'error'); }
  },

  async removeDetailPalette(pid) {
    if (!this._currentItem) return;
    const palettes = this._currentItem.palettes.filter(p => p !== pid);
    try {
      const updated = await api(`/api/items/${this._currentItem.id}`, {
        method: 'PATCH', body: { palettes },
      });
      this._currentItem.palettes = updated.palettes;
      this.renderDetailPalettes();
      await this.refresh();
    } catch (e) { toast(e.message, 'error'); }
  },

  async deleteDetailItem() {
    if (!this._currentItem) return;
    if (!confirm('Delete this item and its file? This cannot be undone.')) return;
    try {
      await api(`/api/items/${this._currentItem.id}`, { method: 'DELETE' });
      this.closeDetail();
      await this.refresh();
    } catch (e) { toast(e.message, 'error'); }
  },

  sendToExport() {
    if (!this._currentItem || this._currentItem.type !== 'video') return;
    ExportPage.preselectId = this._currentItem.id;
    App.navigate('export');
  },
};

document.addEventListener('DOMContentLoaded', () => {
  const tagInput = document.getElementById('lib-detail-tag-input');
  if (tagInput) {
    tagInput.addEventListener('keydown', e => {
      if (e.key === 'Enter') Library.addDetailTag();
    });
  }
  const batchInput = document.getElementById('lib-batch-tag-input');
  if (batchInput) {
    batchInput.addEventListener('keydown', e => {
      if (e.key === 'Enter') Library.applyBatchTag('add');
    });
  }
});
