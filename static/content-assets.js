(() => {
  'use strict';
  const context = document.getElementById('asset-context');
  if (!context) return;
  const {id, revision} = JSON.parse(context.textContent);
  const base = `/content/${id}/assets`;
  const status = document.getElementById('asset-status');
  const results = document.getElementById('asset-results');
  const review = document.getElementById('asset-review');
  let selected;
  async function api(path, options) {
    const response = await fetch(base + path, options);
    const data = await response.json();
    if (!response.ok || !data.ok) throw new Error(data.error || 'Asset operation failed');
    return data;
  }
  async function run(action) {
    status.textContent = 'Working…';
    try { await action(); status.textContent = 'Ready for review.'; }
    catch (error) { status.textContent = error.message; }
  }
  function choose(asset) {
    selected = asset;
    review.hidden = false;
    document.getElementById('asset-preview').src = asset.url;
    review.elements.alt.value = asset.alt || '';
    review.elements.reviewed.checked = false;
    document.getElementById('asset-provenance').textContent = 'Provenance: ' + JSON.stringify(asset.provenance || {});
    review.scrollIntoView({block:'nearest', behavior:'smooth'});
  }
  function cards(items, stock) {
    results.replaceChildren();
    if (!items.length) { results.textContent = 'No images found. Try another search or upload a relevant illustration.'; return; }
    items.forEach(item => {
      const card = document.createElement('div'); card.className = 'asset-card';
      const img = document.createElement('img'); img.src = item.thumbnail_url || item.thumbnail || item.url; img.alt = item.alt || ''; img.loading = 'lazy';
      const text = document.createElement('p'); text.textContent = item.alt || item.description || 'Image';
      const credit = document.createElement('p'); credit.textContent = item.photographer || item.provenance?.creator || '';
      const button = document.createElement('button'); button.className = 'btn'; button.textContent = stock ? 'Import for review' : 'Review image';
      button.addEventListener('click', () => run(async () => {
        button.disabled = true;
        try { choose(stock ? (await api('/import', {method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({provider_id:item.provider_id || item.id,revision})})).asset : item); }
        finally { button.disabled = false; }
      }));
      card.append(img, text, credit, button); results.append(card);
    });
  }
  document.getElementById('asset-library').addEventListener('click', () => run(async () => cards((await api('')).assets, false)));
  document.getElementById('asset-search').addEventListener('submit', event => {
    event.preventDefault(); run(async () => cards((await api('/search?q=' + encodeURIComponent(event.target.elements.q.value))).results, true));
  });
  document.getElementById('asset-upload').addEventListener('submit', event => {
    event.preventDefault();
    run(async () => { const data = new FormData(event.target); data.set('revision', revision); choose((await api('/upload', {method:'POST',body:data})).asset); });
  });
  review.addEventListener('submit', event => {
    event.preventDefault();
    run(async () => {
      await api('/attach', {method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({revision,asset_id:selected.id,index:Number(review.elements.index.value),alt:review.elements.alt.value,caption:review.elements.caption.value,reviewed:review.elements.reviewed.checked})});
      location.reload();
    });
  });
})();
