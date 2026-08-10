const Import = {
  async load() {
    // nothing to preload
  },

  async downloadUrl() {
    const url = document.getElementById('dl-url').value.trim();
    const start = document.getElementById('dl-start').value.trim();
    const end = document.getElementById('dl-end').value.trim();
    if (!url) { toast('Enter a URL', 'error'); return; }

    const btn = document.getElementById('dl-btn');
    btn.disabled = true;
    btn.textContent = 'Downloading…';
    try {
      const result = await api('/api/items/download', {
        method: 'POST',
        body: { url, start_time: start || null, end_time: end || null },
      });
      document.getElementById('dl-url').value = '';
      document.getElementById('dl-start').value = '';
      document.getElementById('dl-end').value = '';
      toast(`Added ${result.length} item(s) to library`, 'success');
    } catch (e) {
      toast(e.message, 'error');
    } finally {
      btn.disabled = false;
      btn.textContent = 'Download';
    }
  },

  async importFiles(files) {
    let ok = 0;
    for (const file of files) {
      const fd = new FormData();
      fd.append('file', file);
      try {
        const res = await fetch('/api/items/import', { method: 'POST', body: fd });
        if (res.ok) ok++;
      } catch {}
    }
    toast(`Imported ${ok} of ${files.length} file(s)`, ok === files.length ? 'success' : 'error');
  },
};

document.addEventListener('DOMContentLoaded', () => {
  const zone = document.getElementById('drop-zone');
  if (!zone) return;
  zone.addEventListener('click', () => document.getElementById('file-input').click());
  zone.addEventListener('dragover', e => { e.preventDefault(); zone.classList.add('dragover'); });
  zone.addEventListener('dragleave', () => zone.classList.remove('dragover'));
  zone.addEventListener('drop', e => {
    e.preventDefault();
    zone.classList.remove('dragover');
    Import.importFiles(e.dataTransfer.files);
  });
  document.getElementById('file-input').addEventListener('change', e => {
    Import.importFiles(e.target.files);
    e.target.value = '';
  });
});
