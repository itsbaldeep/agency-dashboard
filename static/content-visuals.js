(() => {
  const config = JSON.parse(document.querySelector('#visual-config').textContent);
  const form = document.querySelector('#visual-form');
  const target = document.querySelector('#visual-target');
  const kind = document.querySelector('#visual-kind');
  const save = document.querySelector('#visual-save');
  const status = document.querySelector('#visual-status');
  let pending = null;
  const value = name => form.elements.namedItem(name).value.trim();
  const lines = name => value(name).split('\n').map(x => x.trim()).filter(Boolean);
  function fields() {
    document.querySelectorAll('[data-visual-kinds]').forEach(el => {el.hidden = !el.dataset.visualKinds.split(' ').includes(kind.value)});
    pending = null; save.disabled = true;
  }
  kind.addEventListener('change', fields);
  form.addEventListener('input', () => {pending = null; save.disabled = true});
  target.addEventListener('change', () => {
    document.querySelector('#visual-remove').hidden = target.value === 'new';
    document.querySelector('#visual-position').disabled = target.value !== 'new';
    if (target.value !== 'new') {
      const v = config.blocks[Number(target.value)];
      for (const name of ['kind','title','caption','before','after','url','alt','credit','credit_url','units']) form.elements.namedItem(name).value = v[name] || '';
      for (const name of ['notes','items']) form.elements.namedItem(name).value = (v[name] || []).join('\n');
      form.elements.namedItem('columns').value = (v.columns || []).join(' | ');
      form.elements.namedItem('rows').value = (v.rows || []).map(row => row.join(' | ')).join('\n');
      form.elements.namedItem('points').value = (v.points || []).map(p => `${p.label} | ${p.value} | ${p.fact_id}`).join('\n');
    }
    fields();
  });
  async function call(path, payload) {
    const response = await fetch(`/content/${config.id}/visuals${path}`, {method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(payload)});
    const result = await response.json();
    if (!response.ok || !result.ok) throw new Error(result.error || 'Visual action failed');
    return result;
  }
  function payload(remove=false) {
    const visual = {kind:kind.value,title:value('title'),caption:value('caption')};
    if (visual.kind === 'annotated_example') Object.assign(visual,{before:value('before'),after:value('after'),notes:lines('notes')});
    if (visual.kind === 'comparison') Object.assign(visual,{columns:value('columns').split('|').map(x=>x.trim()),rows:lines('rows').map(x=>x.split('|').map(c=>c.trim()))});
    if (['flow','checklist'].includes(visual.kind)) visual.items = lines('items');
    if (visual.kind === 'image') {
      Object.assign(visual,{url:value('url'),alt:value('alt'),credit:value('credit')});
      if (value('credit_url')) visual.credit_url = value('credit_url');
    }
    if (['bar_chart','line_chart'].includes(visual.kind)) Object.assign(visual,{units:value('units'),points:lines('points').map(line=>{const [label,number,fact_id]=line.split('|').map(x=>x.trim());return {label,value:number ? Number(number) : null,fact_id}})});
    return {revision:config.revision,action:remove?'remove':target.value==='new'?'add':'replace',index:Number(target.value==='new'?value('index'):target.value),visual};
  }
  async function preview(remove=false) {
    try {
      pending = null; save.disabled = true; status.textContent = 'Validating visual…';
      const data = payload(remove); const result = await call('/preview',data);
      document.querySelector('#visual-preview').innerHTML = result.html;
      pending = {...data,preview_digest:result.preview_digest}; save.disabled = false;
      status.textContent = 'Review the visual, then save it to the draft. Nothing is published.';
    } catch (error) {status.textContent = error.message}
  }
  form.addEventListener('submit', event => {event.preventDefault();preview()});
  document.querySelector('#visual-remove').addEventListener('click',()=>preview(true));
  save.addEventListener('click',async()=>{
    if (!pending) return;
    save.disabled = true;
    try {await call('',pending);location.reload()} catch(error) {status.textContent=error.message;save.disabled=false}
  });
  document.querySelectorAll('[data-visual-undo]').forEach(button=>button.addEventListener('click',async()=>{
    if (!confirm('Restore the draft to immediately before this visual change?')) return;
    try {await call('/undo/'+button.dataset.visualUndo,{revision:config.revision});location.reload()} catch(error) {status.textContent=error.message}
  }));
  fields();
})();
