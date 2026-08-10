// Keyframe-based clip carving, adapted from vidset's split workflow.
// Clips are spans between adjacent keyframes; boundary frames are shared.

const Clip = {
  videos: [],
  selectedItem: null,
  keyframes: [],
  keyframeImages: {},      // timestamp -> data URL captured at mark time
  fps: 30,
  loopClip: null,

  async load() {
    try {
      this.videos = await api('/api/items?type=video');
    } catch { this.videos = []; }
    this.renderSourceList();
    this.renderKeyframes();
  },

  renderSourceList() {
    const el = document.getElementById('clip-source-list');
    if (!el) return;
    if (this.videos.length === 0) {
      el.innerHTML = '<div style="color:var(--muted);font-size:13px;padding:8px">No videos in library — import some first</div>';
      return;
    }
    el.innerHTML = this.videos.map(v => `
      <div class="source-select-item ${this.selectedItem?.id === v.id ? 'active' : ''}"
           onclick="Clip.selectItem('${esc(v.id)}')">
        <div class="src-name">${esc(v.title)}</div>
        <div class="src-concept">${fmtDuration(v.duration)} · ${esc(v.filename)}</div>
      </div>`).join('');
  },

  async selectItem(iid) {
    this.selectedItem = this.videos.find(v => v.id === iid) || null;
    this.keyframes = [];
    this.keyframeImages = {};
    this.loopClip = null;
    this.fps = this.selectedItem?.fps || 30;
    this._clearPreview();
    this.renderSourceList();
    this.renderVideo();
    this.renderKeyframes();
    document.getElementById('clip-fps-display').textContent =
      this.fps ? `${this.fps.toFixed(2)} fps` : '— fps';
  },

  renderVideo() {
    const vid = document.getElementById('clip-video');
    if (!vid || !this.selectedItem) return;
    vid.src = `/api/media/${encodeURIComponent(this.selectedItem.filename)}`;
    vid.load();
    vid.ontimeupdate = () => {
      this.updateTimeDisplay();
      this.updateKeyframePreview(vid.currentTime);
      if (this.loopClip && vid.currentTime >= this.loopClip.end) {
        vid.currentTime = this.loopClip.start;
      }
    };
  },

  toFrame(t) { return Math.round(t * this.fps); },

  updateTimeDisplay() {
    const vid = document.getElementById('clip-video');
    if (!vid) return;
    const el = document.getElementById('clip-frame-display');
    if (el) el.textContent = `f ${this.toFrame(vid.currentTime)}`;
  },

  stepFrames(n) {
    const vid = document.getElementById('clip-video');
    if (!vid) return;
    vid.currentTime = Math.max(0, vid.currentTime + n * (1 / this.fps));
    this.updateTimeDisplay();
  },

  markKeyframe() {
    const vid = document.getElementById('clip-video');
    if (!vid || !this.selectedItem) { toast('Select a video first', 'error'); return; }
    const t = parseFloat(vid.currentTime.toFixed(6));
    if (this.keyframes.some(k => Math.abs(k - t) < 0.001)) return;

    try {
      const canvas = document.createElement('canvas');
      canvas.width = vid.videoWidth || 640;
      canvas.height = vid.videoHeight || 360;
      canvas.getContext('2d').drawImage(vid, 0, 0, canvas.width, canvas.height);
      this.keyframeImages[t] = canvas.toDataURL('image/jpeg', 0.85);
    } catch (e) {}

    this.keyframes.push(t);
    this.keyframes.sort((a, b) => a - b);
    this.renderKeyframes();
  },

  removeKeyframe(idx) {
    const removed = this.keyframes[idx];
    if (this.loopClip && Math.abs(this.loopClip.start - removed) < 0.001) {
      this.loopClip = null;
    }
    delete this.keyframeImages[removed];
    this.keyframes.splice(idx, 1);
    this.renderKeyframes();
  },

  seekToKeyframe(t) {
    const vid = document.getElementById('clip-video');
    if (vid) vid.currentTime = t;
  },

  selectLoop(idx) {
    const start = this.keyframes[idx];
    const end = this.keyframes[idx + 1];
    if (start === undefined || end === undefined) return;
    if (this.loopClip && Math.abs(this.loopClip.start - start) < 0.001) {
      this.loopClip = null;
    } else {
      this.loopClip = { start, end };
      const vid = document.getElementById('clip-video');
      if (vid) { vid.currentTime = start; vid.play(); }
    }
    this.renderKeyframes();
  },

  clearKeyframes() {
    if (this.keyframes.length === 0) return;
    if (!confirm('Clear all keyframes?')) return;
    this.keyframes = [];
    this.keyframeImages = {};
    this.loopClip = null;
    this._clearPreview();
    this.renderKeyframes();
  },

  updateKeyframePreview(currentTime) {
    let refT = null;
    for (const t of this.keyframes) {
      if (t <= currentTime + 0.001) refT = t;
      else break;
    }
    const img = document.getElementById('clip-kf-img');
    const empty = document.getElementById('clip-kf-empty');
    const info = document.getElementById('clip-kf-info');
    if (!img) return;
    const dataUrl = refT !== null ? this.keyframeImages[refT] : null;
    if (dataUrl) {
      img.src = dataUrl;
      img.classList.add('visible');
      if (empty) empty.style.display = 'none';
      const kfIdx = this.keyframes.indexOf(refT);
      if (info) info.textContent = `Keyframe ${kfIdx + 1} · f ${this.toFrame(refT)}`;
    } else {
      img.classList.remove('visible');
      if (empty) empty.style.display = 'flex';
      if (info) info.textContent = '';
    }
  },

  _clearPreview() {
    const img = document.getElementById('clip-kf-img');
    const empty = document.getElementById('clip-kf-empty');
    const info = document.getElementById('clip-kf-info');
    if (img) { img.classList.remove('visible'); img.src = ''; }
    if (empty) empty.style.display = 'flex';
    if (info) info.textContent = '';
  },

  renderKeyframes() {
    const el = document.getElementById('clip-keyframe-list');
    if (!el) return;

    if (this.keyframes.length === 0) {
      el.innerHTML = `<div style="color:var(--muted);font-size:13px;padding:10px 0">
        No keyframes yet. Play the video and press <kbd style="background:var(--surface2);border:1px solid var(--border);border-radius:3px;padding:1px 5px">M</kbd> to mark a cut point.
      </div>`;
      return;
    }

    let html = '<div class="keyframe-list">';
    this.keyframes.forEach((t, i) => {
      const hasClip = i < this.keyframes.length - 1;
      const dur = hasClip ? this.keyframes[i + 1] - t : null;
      const looping = this.loopClip && Math.abs(this.loopClip.start - t) < 0.001;

      let clipPill = '<span class="kf-no-clip"></span>';
      if (hasClip) {
        const label = `Clip ${i + 1} · ${fmtDuration(dur)} · ${this.toFrame(dur)}f`;
        clipPill = `<span class="kf-clip-pill"
          onclick="event.stopPropagation(); Clip.selectLoop(${i})">${label}</span>`;
      }

      html += `
        <div class="keyframe-row${looping ? ' loop-active' : ''}" onclick="Clip.seekToKeyframe(${t})">
          <span class="kf-num">${i + 1}</span>
          <span class="kf-time" title="${fmtPrecise(t)}">f ${this.toFrame(t)}</span>
          ${clipPill}
          <button class="btn btn-ghost btn-sm" style="flex-shrink:0"
            onclick="event.stopPropagation();Clip.removeKeyframe(${i})">✕</button>
        </div>`;
    });
    html += '</div>';

    const clipCount = this.keyframes.length - 1;
    html += clipCount < 1
      ? '<div style="color:var(--muted);font-size:12px;margin-top:8px">Mark at least 2 keyframes to define a clip.</div>'
      : `<div style="color:var(--muted);font-size:12px;margin-top:8px">${clipCount} clip${clipCount !== 1 ? 's' : ''} ready to extract. Tags and palettes are inherited from the source.</div>`;

    el.innerHTML = html;
  },

  async extractAll() {
    if (!this.selectedItem) { toast('Select a video first', 'error'); return; }
    if (this.keyframes.length < 2) { toast('Mark at least 2 keyframes first', 'error'); return; }

    const segments = [];
    for (let i = 0; i < this.keyframes.length - 1; i++) {
      segments.push({ start: this.keyframes[i], end: this.keyframes[i + 1] });
    }

    const btn = document.getElementById('clip-extract-btn');
    btn.disabled = true;
    btn.textContent = 'Extracting…';
    try {
      const clips = await api(`/api/items/${this.selectedItem.id}/extract`, {
        method: 'POST',
        body: { segments },
      });
      toast(`Added ${clips.length} clips to library`, 'success');
      this.keyframes = [];
      this.keyframeImages = {};
      this.loopClip = null;
      this._clearPreview();
      this.renderKeyframes();
      await this.load();
    } catch (e) {
      toast(e.message, 'error');
    } finally {
      btn.disabled = false;
      btn.textContent = 'Extract Clips';
    }
  },
};

// Capture-phase keyboard handler (same pattern as vidset)
document.addEventListener('keydown', e => {
  if (App.currentPage !== 'clip') return;
  if (['INPUT', 'TEXTAREA', 'SELECT'].includes(e.target.tagName)) return;

  if (e.key === 'ArrowRight' || e.key === 'ArrowLeft') {
    e.preventDefault();
    e.stopPropagation();
    const forward = e.key === 'ArrowRight';
    Clip.stepFrames(forward ? (e.shiftKey ? 10 : 1) : (e.shiftKey ? -10 : -1));
  } else if (e.key === 'm' || e.key === 'M') {
    Clip.markKeyframe();
  }
}, { capture: true });
