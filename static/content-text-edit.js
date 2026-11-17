(() => {
  const id = JSON.parse(document.getElementById('text-editor-context').textContent).id;
  const url = '/content/' + id + '/edit';
  const form = document.getElementById('text-editor');
  const status = document.getElementById('edit-status');
  const save = document.getElementById('save-edit');
  const preview = document.getElementById('preview-edit');
  let original, digest = null;
  const controls = [];
  const meta = {};
  function message(text) { status.textContent = text; }
  function invalidate() { digest = null; save.disabled = true; }
  function field(parent, label, value, rows = 3) {
    const wrapper = document.createElement('label'); wrapper.style.display = 'block'; wrapper.style.marginTop = '12px';
    wrapper.append(document.createTextNode(label));
    const input = document.createElement('textarea'); input.rows = rows; input.style.width = '100%'; input.value = value || '';
    wrapper.append(input); parent.append(wrapper); return input;
  }
  function payload() {
    const blocks = structuredClone(original.blocks);
    for (const c of controls) blocks[c.index][c.key] = c.list ? c.input.value.split('\n').filter(x => x.trim()) : c.input.value;
    const metadata = {};
    for (const [key, input] of Object.entries(meta)) {
      if (input.value !== original.metadata[key] && (input.value || Object.hasOwn(original.metadata, key))) metadata[key] = input.value;
    }
    return {revision: original.revision, title: document.getElementById('edit-title').value, blocks, metadata, review_note: document.getElementById('edit-note').value};
  }
  async function request(target, data) {
    const response = await fetch(target, {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify(data)});
    const result = await response.json(); if (!response.ok || !result.ok) throw new Error(result.error || 'Unable to save content'); return result;
  }
  form.addEventListener('input', invalidate);
  preview.addEventListener('click', async () => {
    if (!form.reportValidity()) return;
    preview.disabled = true; invalidate(); const data = payload(); const serialized = JSON.stringify(data);
    try {
      const result = await request(url + '/preview', data);
      if (JSON.stringify(payload()) !== serialized) { message('The edit changed during preview. Preview again.'); return; }
      digest = result.preview_digest; save.disabled = false;
      document.getElementById('review-quality').textContent = (result.quality?.findings || []).map(x => x.message).join(' ') || 'Content checks passed. Review the text below.';
      document.getElementById('review-html').innerHTML = result.html || '';
      document.getElementById('review-panel').hidden = false;
      message('Preview ready. Saving records this exact revision without publishing.');
    } catch (error) { message(error.message); } finally { preview.disabled = false; }
  });
  form.addEventListener('submit', async event => {
    event.preventDefault(); if (!digest || !form.reportValidity()) return;
    save.disabled = true;
    try { const result = await request(url, {...payload(), preview_digest:digest}); location.href = '/content/' + id + '/preview'; }
    catch (error) { invalidate(); message(error.message); }
  });
  fetch('/content/' + id + '/edit-data').then(async response => {
    const data = await response.json(); if (!response.ok || !data.ok) throw new Error(data.error || 'Content is not editable');
    original = data; document.getElementById('edit-title').value = data.title;
    for (const [key, label, limit] of [['meta_title','Search title',2000],['meta_description','Search description',2000],['custom_excerpt','Excerpt',300],['target_keyword','Target query',200]]) {
      meta[key] = field(document.getElementById('edit-metadata'), label, data.metadata[key], 2); meta[key].maxLength = limit;
    }
    document.getElementById('blocks-title').textContent = data.status === 'outline' ? 'Outline briefs' : 'Article text';
    data.blocks.forEach((block,index) => {
      const section = document.createElement('section'); section.className = 'card mt-2';
      const heading = document.createElement('h3'); heading.textContent = (index + 1) + '. ' + block.type; section.append(heading);
      const keys = data.status === 'outline' ? ['brief', ...(block.type === 'faq' ? ['answer_pointer'] : [])] : ({intro:['markdown'],prose:['markdown'],heading:['heading'],key_takeaways:['points'],steps:['steps'],faq:['brief','answer']})[block.type] || [];
      if (!keys.length) { const note = document.createElement('p'); note.textContent = 'Preserved. Use the asset or visual editor to change this block.'; section.append(note); }
      for (const key of keys) {
        const list = Array.isArray(block[key]); const input = field(section, key === 'markdown' ? 'Text' : key.replaceAll('_',' '), list ? block[key].join('\n') : block[key], key === 'heading' ? 2 : 5);
        controls.push({index,key,input,list});
      }
      document.getElementById('edit-blocks').append(section);
    });
    form.hidden = false; message('Loaded ' + data.status + '. Changes require a fresh preview.');
  }).catch(error => message(error.message));
})();
