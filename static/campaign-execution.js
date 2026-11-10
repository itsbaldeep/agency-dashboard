document.addEventListener('DOMContentLoaded', async () => {
  const form = document.getElementById('campaign-review-form');
  if (!form) return;
  const url = '/api/brands/' + window.marketingBrand + '/work-items/' + workId + '/campaign-execution';
  const status = document.getElementById('campaign-execution-status');
  const previewButton = document.getElementById('campaign-preview');
  const dialog = document.getElementById('campaign-approval-dialog');
  const confirm = document.getElementById('campaign-approval-confirm');
  const approve = document.getElementById('campaign-approval-submit');
  let revision, review;
  const localDate = new Date(Date.now() + 15 * 60000);
  localDate.setMinutes(localDate.getMinutes() - localDate.getTimezoneOffset());
  form.elements.send_at.value = localDate.toISOString().slice(0,16);
  async function action(payload) {
    const result = await marketingPost(url, payload);
    if (result.task_id) watchMarketingTask(result.task_id);
    else window.location.reload();
  }
  try {
    const response = await fetch(url);
    const data = await response.json();
    if (!response.ok || !data.ok) throw new Error(data.error || 'Campaign execution unavailable');
    revision = data.revision;
    previewButton.disabled = !data.configured;
    form.querySelector('button').disabled = !data.configured || !data.reviewed || !data.preview;
    const evidence = document.getElementById('campaign-preview-evidence');
    evidence.textContent = !data.configured ? 'Connect a brand-owned campaign source in Email setup first.' :
      data.preview ? 'Preview: ' + data.preview.eligible_count + ' eligible, ' + data.preview.suppressed_count + ' excluded. Expires ' + data.preview.expires_at + '. Revision ' + data.preview.revision + '.' : 'No audience preview collected. Nothing is scheduled.';
    const rows = document.getElementById('campaign-runs');
    if (!data.runs.length) {
      const tr = document.createElement('tr'); const cell = document.createElement('td'); cell.colSpan=4; cell.textContent='No approved delivery runs.'; tr.appendChild(cell); rows.appendChild(tr);
    }
    data.runs.forEach(run => {
      const tr = document.createElement('tr');
      ['#'+run.id, run.state, run.send_at].forEach(value => { const cell=document.createElement('td'); cell.textContent=value; tr.appendChild(cell); });
      const cell = document.createElement('td');
      if (run.task_id) { const link=document.createElement('a'); link.className='btn btn-xs'; link.href='/tasks/'+run.task_id; link.textContent='Task'; cell.appendChild(link); }
      const cancelable = ['approved','queued'].includes(run.state);
      if (cancelable || ['dispatching','accepted','partial','uncertain'].includes(run.state)) {
        const button=document.createElement('button'); button.className='btn btn-xs'; button.textContent=cancelable?'Cancel':'Check source receipt';
        button.addEventListener('click', async()=>{button.disabled=true;try{await action({action:cancelable?'cancel':'receipt',run_id:run.id});}catch(error){status.textContent=error.message;button.disabled=false;}}); cell.appendChild(button);
      }
      tr.appendChild(cell); rows.appendChild(tr);
    });
  } catch (error) { status.textContent=error.message; }
  previewButton.addEventListener('click', async()=>{previewButton.disabled=true;try{await action({action:'preview',revision});}catch(error){status.textContent=error.message;previewButton.disabled=false;}});
  form.addEventListener('submit', async event => {
    event.preventDefault();
    try {
      const sendAt = new Date(form.elements.send_at.value).toISOString();
      const result = await marketingPost(url, {action:'review',revision,send_at:sendAt});
      review = result.review;
      document.getElementById('campaign-approval-details').textContent = review.message.subject + '\n\n' + review.message.body + '\n\nAudience: ' + review.eligible_count + ' eligible\nCategory: ' + review.policy.category + '\nPlay: ' + review.policy.play + '\nRules: ' + JSON.stringify(review.policy) + '\nDelivery: ' + review.send_at + '\nAudience digest: ' + review.audience_digest + '\nApproval digest: ' + review.approval_digest;
      confirm.checked=false; approve.disabled=true; dialog.showModal();
    } catch (error) { status.textContent=error.message; }
  });
  confirm.addEventListener('change', ()=>{approve.disabled=!confirm.checked;});
  document.getElementById('campaign-approval-close').addEventListener('click', ()=>dialog.close());
  approve.addEventListener('click', async()=>{
    if (!confirm.checked || !review) return;
    approve.disabled=true;
    try { await action({action:'approve',revision,send_at:review.send_at,approval_digest:review.approval_digest}); }
    catch (error) { dialog.close();status.textContent=error.message; }
  });
});
