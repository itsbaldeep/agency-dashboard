(() => {
  'use strict';
  const context = document.getElementById('asset-context');
  if (!context) return;
  const {id, title, revision: initialRevision, defaultIndex} = JSON.parse(context.textContent);
  let revision = initialRevision;
  const base = `/content/${id}/assets`;
  const status = document.getElementById('asset-status');
  const results = document.getElementById('asset-results');
  const review = document.getElementById('asset-review');
  const studio = document.getElementById('asset-studio');
  const suggestionResults = document.getElementById('asset-suggestions-results');
  const suggestionStatus = document.getElementById('asset-suggestions-status');
  const suggestionTask = document.getElementById('asset-suggestions-task');
  let selected;
  let suggestionsLoaded = false;
  let suggestionPoll = 0;
  let suggestionPolling = false;
  const suggestionRefresh = document.getElementById('asset-suggestions-refresh');
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
  function provenanceText(asset) {
    const provenance = asset.provenance || {};
    const creator = asset.creator || asset.photographer || provenance.creator || provenance.photographer;
    const provider = asset.provider || provenance.provider || provenance.source;
    const rights = asset.rights || provenance.rights || provenance.license || provenance.kind;
    const kind = asset.kind || provenance.kind;
    return [kind && `Type: ${kind}`, creator && `Creator: ${creator}`, provider && `Source: ${provider}`, rights && `Rights: ${rights}`].filter(Boolean).join(' · ') || 'Provenance is recorded in the asset review.';
  }
  async function prepareCandidate(asset) {
    if (asset.kind !== 'stock') return asset;
    return (await api('/import', {method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({provider_id:asset.provider_id || asset.id,revision})})).asset;
  }
  function choose(asset, options = {}) {
    selected = asset;
    review.hidden = false;
    document.getElementById('asset-preview').src = asset.url;
    document.getElementById('asset-preview').alt = asset.alt || options.alt || 'Selected image preview';
    review.elements.alt.value = asset.alt || '';
    review.elements.caption.value = asset.caption || options.caption || '';
    if (options.index !== undefined) review.elements.index.value = String(options.index);
    review.elements.reviewed.checked = false;
    document.getElementById('asset-provenance').textContent = provenanceText(asset);
    review.scrollIntoView({block:'nearest', behavior:'smooth'});
  }
  async function attachSuggestion(asset, slot, button) {
    button.disabled = true;
    try {
      const imported = await prepareCandidate(asset);
      const selectedAsset = {...imported, alt: asset.alt || imported.alt || '', caption: asset.caption || imported.caption || ''};
      if (!selectedAsset.id || selectedAsset.alt.trim().length < 10) throw new Error('This suggestion is missing an attachable asset or descriptive alt text. Use Edit details first.');
      const index = Number(slot.index);
      await api('/attach', {method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({revision,asset_id:selectedAsset.id,index,alt:selectedAsset.alt,caption:selectedAsset.caption,reviewed:true})});
      sessionStorage.setItem('agencyAssetStudioOpen', '1');
      location.reload();
    } finally { button.disabled = false; }
  }
  function renderSuggestions(slots) {
    suggestionResults.replaceChildren();
    if (!slots || !slots.length) { suggestionResults.textContent = 'No image slots need suggestions. Existing reviewed images remain unchanged.'; return; }
    const firstQuery = slots.find(slot => Array.isArray(slot.queries) && slot.queries.length)?.queries[0];
    const searchInput = document.getElementById('asset-search-q');
    if (firstQuery && searchInput && !searchInput.value) searchInput.value = firstQuery;
    slots.forEach(slot => {
      const section = document.createElement('section'); section.className = 'asset-suggestion-slot';
      const heading = document.createElement('h4'); heading.textContent = slot.label || `Image slot ${Number(slot.index) + 1}`; section.append(heading);
      if (slot.reason) { const reason = document.createElement('p'); reason.className = 'subtle'; reason.textContent = slot.reason; section.append(reason); }
      const queries = document.createElement('div'); queries.className = 'asset-query-chips';
      (slot.queries || []).forEach(query => { const chip = document.createElement('button'); chip.type = 'button'; chip.className = 'asset-query-chip'; chip.textContent = query; chip.addEventListener('click', () => { document.getElementById('asset-search-q').value = query; document.getElementById('asset-search-q').focus(); }); queries.append(chip); });
      if (queries.children.length) section.append(queries);
      const candidates = document.createElement('div'); candidates.className = 'asset-suggestion-candidates';
      (slot.candidates || []).forEach(candidate => {
        const card = document.createElement('article'); card.className = 'asset-suggestion-card';
        if (candidate.current) { const current = document.createElement('strong'); current.textContent = 'Currently in this draft'; card.append(current); }
        const image = document.createElement('img'); image.src = candidate.thumbnail_url || candidate.url; image.alt = candidate.alt || 'Suggested image'; image.loading = 'lazy';
        const alt = document.createElement('p'); alt.textContent = `Alt: ${candidate.alt || 'Not provided'}`;
        const caption = document.createElement('p'); caption.textContent = `Caption: ${candidate.caption || 'None'}`;
        const credit = document.createElement('p'); credit.textContent = provenanceText(candidate);
        const source = candidate.source_url || candidate.license_url || candidate.provenance?.source_url || candidate.provenance?.license_url;
        if (source && /^https:\/\//i.test(source)) { const link = document.createElement('a'); link.href = source; link.target = '_blank'; link.rel = 'noopener noreferrer'; link.textContent = 'View source and license'; credit.append(' ', link); }
        const use = document.createElement('button'); use.type = 'button'; use.className = 'btn btn-green'; use.textContent = 'Use this image'; use.title = 'Confirms this image, alt text and rights for this draft. It does not publish.'; use.disabled = (candidate.kind === 'stock' ? !(candidate.provider_id || candidate.id) : !candidate.id) || !(candidate.alt || '').trim() || (candidate.alt || '').trim().length < 10; use.addEventListener('click', () => run(() => attachSuggestion(candidate, slot, use)));
        const edit = document.createElement('button'); edit.type = 'button'; edit.className = 'btn'; edit.textContent = 'Edit details'; edit.addEventListener('click', () => run(async () => { const prepared = await prepareCandidate(candidate); choose({...prepared, alt:candidate.alt || prepared.alt, caption:candidate.caption || prepared.caption}, {index:slot.index}); }));
        card.append(image, alt, caption, credit, use, edit); candidates.append(card);
      });
      if (candidates.children.length) section.append(candidates); else { const empty = document.createElement('p'); empty.textContent = 'No saved candidates yet. Use a query above to search licensed stock.'; section.append(empty); }
      suggestionResults.append(section);
    });
  }
  function renderSuggestionState(data) {
    const active = ['queued', 'running', 'pending'].includes(data.task_status);
    if (data.task_id) { suggestionTask.replaceChildren(); const link = document.createElement('a'); link.href = `/tasks/${data.task_id}`; link.textContent = `Suggestion task #${data.task_id}`; suggestionTask.append(link); }
    if (data.stale) { suggestionStatus.textContent = 'Suggestions are stale for this article revision. Refresh suggestions before using them.'; suggestionRefresh.disabled = false; return; }
    if (Array.isArray(data.slots) && data.slots.length) renderSuggestions(data.slots);
    else if (!active) suggestionResults.textContent = 'No image slots are currently suggested. Refresh suggestions to analyze this article again.';
    suggestionStatus.textContent = active ? 'Suggestions are being prepared…' : data.task_status === 'failed' || data.task_status === 'needs_input' ? (data.error || 'Suggestion task needs review.') : 'Suggestions ready for review.';
    if (active && data.task_id) pollSuggestions(data.task_id);
  }
  async function loadSuggestions() {
    if (suggestionsLoaded) return;
    suggestionsLoaded = true;
    suggestionStatus.textContent = 'Loading saved suggestions…';
    try { renderSuggestionState(await api('/suggestions')); }
    catch (error) { suggestionsLoaded = false; suggestionStatus.textContent = error.message; }
  }
  async function pollSuggestions(taskId) {
    if (suggestionPolling) return;
    suggestionPolling = true;
    suggestionRefresh.disabled = true;
    let attempts = 0;
    const poll = async () => {
      if (++attempts > 30) { suggestionPolling = false; suggestionRefresh.disabled = false; suggestionStatus.textContent = 'Suggestion task is still running. Open its task link to monitor it.'; return; }
      try {
        const current = await api('/suggestions');
        const active = ['queued', 'running', 'pending'].includes(current.task_status);
        if (active) { suggestionStatus.textContent = 'Suggestions are being prepared…'; suggestionPoll = window.setTimeout(poll, 2000); return; }
        suggestionPolling = false; suggestionRefresh.disabled = false;
        if (current.task_status === 'done') { sessionStorage.setItem('agencyAssetStudioOpen', '1'); location.reload(); return; }
        renderSuggestionState(current);
      } catch (error) { suggestionPolling = false; suggestionRefresh.disabled = false; suggestionStatus.textContent = error.message; }
    };
    poll();
  }
  async function refreshSuggestions() {
    if (suggestionPolling) return;
    suggestionRefresh.disabled = true;
    suggestionStatus.textContent = 'Requesting fresh suggestions…';
    try {
      const data = await api('/suggestions', {method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({revision})});
      if (!data.task_id) { suggestionsLoaded = false; suggestionRefresh.disabled = false; return loadSuggestions(); }
      suggestionTask.replaceChildren(); const link = document.createElement('a'); link.href = `/tasks/${data.task_id}`; link.textContent = `Suggestion task #${data.task_id}`; suggestionTask.append(link);
      pollSuggestions(data.task_id);
    } catch (error) { suggestionRefresh.disabled = false; suggestionStatus.textContent = error.message; }
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
  studio.addEventListener('toggle', () => { if (studio.open) loadSuggestions(); });
  suggestionRefresh.addEventListener('click', () => run(refreshSuggestions));
  if (defaultIndex !== undefined && defaultIndex !== null) review.elements.index.value = String(defaultIndex);
  else { const imageOption = [...review.elements.index.options].find(option => option.textContent.includes('Replace planned image')); if (imageOption) review.elements.index.value = imageOption.value; }
  const firstQuery = document.querySelector('.asset-query-chip');
  if (firstQuery) document.getElementById('asset-search-q').value = firstQuery.textContent;
  if (sessionStorage.getItem('agencyAssetStudioOpen') === '1') { sessionStorage.removeItem('agencyAssetStudioOpen'); studio.open = true; }
  document.getElementById('asset-upload').addEventListener('submit', event => {
    event.preventDefault();
    run(async () => { const data = new FormData(event.target); data.set('revision', revision); choose((await api('/upload', {method:'POST',body:data})).asset); });
  });
  review.addEventListener('submit', event => {
    event.preventDefault();
    run(async () => {
      await api('/attach', {method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({revision,asset_id:selected.id,index:Number(review.elements.index.value),alt:review.elements.alt.value,caption:review.elements.caption.value,reviewed:review.elements.reviewed.checked})});
      sessionStorage.setItem('agencyAssetStudioOpen', '1');
      location.reload();
    });
  });
})();
