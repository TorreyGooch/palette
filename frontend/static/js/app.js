// Core app state and shared utilities

const App = {
  currentPage: null,

  async init() {
    const status = await api('/api/library');
    if (!status.configured) {
      document.getElementById('setup-overlay').classList.remove('hidden');
      document.getElementById('app').classList.add('hidden');
      return;
    }
    this.enterApp(status);
  },

  enterApp(status) {
    document.getElementById('setup-overlay').classList.add('hidden');
    document.getElementById('app').classList.remove('hidden');
    document.getElementById('sidebar-lib-path').textContent = status.path;
    document.querySelectorAll('.sidebar-nav li').forEach(li => {
      li.onclick = () => this.navigate(li.dataset.page);
    });
    this.navigate('library');
  },

  navigate(page) {
    this.currentPage = page;
    document.querySelectorAll('.sidebar-nav li').forEach(li => {
      li.classList.toggle('active', li.dataset.page === page);
    });
    document.querySelectorAll('.page').forEach(p => p.classList.remove('active'));
    document.getElementById(`page-${page}`).classList.add('active');

    const handlers = {
      import: () => Import.load(),
      clip: () => Clip.load(),
      library: () => Library.load(),
      export: () => ExportPage.load(),
      quotes: () => Quotes.load(),
    };
    handlers[page]?.();
  },
};

async function setupLibrary() {
  const path = document.getElementById('setup-path').value.trim();
  if (!path) { toast('Enter a folder path', 'error'); return; }
  try {
    await api('/api/library', { method: 'POST', body: { path } });
    const status = await api('/api/library');
    toast('Library ready', 'success');
    App.enterApp(status);
  } catch (e) { toast(e.message, 'error'); }
}

// ── API helper ────────────────────────────────────────────────────────────────

async function api(path, options = {}) {
  const res = await fetch(path, {
    headers: { 'Content-Type': 'application/json' },
    ...options,
    body: options.body ? (typeof options.body === 'string' ? options.body : JSON.stringify(options.body)) : undefined,
  });
  if (!res.ok) {
    const err = await res.json().catch(() => ({ detail: res.statusText }));
    throw new Error(err.detail || 'Request failed');
  }
  return res.json();
}

// ── Toast ─────────────────────────────────────────────────────────────────────

function toast(msg, type = '') {
  const el = document.createElement('div');
  el.className = `toast${type ? ' toast-' + type : ''}`;
  el.textContent = msg;
  document.getElementById('toast-container').appendChild(el);
  setTimeout(() => el.remove(), 3500);
}

// ── Utilities ─────────────────────────────────────────────────────────────────

function esc(s) {
  return String(s ?? '').replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;');
}

function fmtDuration(s) {
  if (s == null) return '';
  s = Math.round(s);
  const m = Math.floor(s / 60);
  const sec = s % 60;
  return `${m}:${String(sec).padStart(2, '0')}`;
}

function fmtPrecise(s) {
  const m = Math.floor(s / 60);
  const sec = (s % 60).toFixed(3).padStart(6, '0');
  return `${String(m).padStart(2, '0')}:${sec}`;
}

function fmtBytes(b) {
  if (b > 1048576) return (b / 1048576).toFixed(1) + ' MB';
  if (b > 1024) return (b / 1024).toFixed(0) + ' KB';
  return b + ' B';
}

document.addEventListener('DOMContentLoaded', () => App.init());
