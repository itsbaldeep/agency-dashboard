document.addEventListener('DOMContentLoaded', async () => {
  const form = document.getElementById('email-provider-form');
  if (!form) return;
  const status = document.getElementById('email-provider-state');
  const verify = document.getElementById('email-provider-verify');
  const save = form.querySelector('button[type=submit]');
  const url = '/api/brands/' + window.marketingBrand + '/email-provider';
  let digest;
  async function load() {
    const response = await fetch(url);
    const data = await response.json();
    if (!response.ok || !data.ok) throw new Error(data.error || 'Provider connection unavailable');
    digest = data.digest;
    Object.entries(data.config).forEach(([key, value]) => {
      if (form.elements.namedItem(key)) form.elements.namedItem(key).value = value;
    });
    const evidence = data.verification;
    status.textContent = 'Verification: ' + (evidence.status || 'not verified').replaceAll('_', ' ') +
      (evidence.checked_at ? ' · Checked ' + evidence.checked_at : '') +
      (evidence.error ? ' · ' + evidence.error.replaceAll('_', ' ') : '') + '. Sending is not enabled.';
    verify.disabled = !data.config.provider;
    save.disabled = false;
  }
  form.addEventListener('submit', async event => {
    event.preventDefault();
    save.disabled = true;
    try {
      const result = await marketingPost(url, {action: 'save', digest, config: Object.fromEntries(new FormData(form))});
      digest = result.digest;
      await load();
      status.textContent = 'Connection saved. Verify account and sender next. No email sent.';
    } catch (error) { status.textContent = error.message; }
    finally { save.disabled = false; }
  });
  verify.addEventListener('click', async () => {
    verify.disabled = true;
    try {
      const data = await marketingPost(url, {action: 'verify', digest});
      watchMarketingTask(data.task_id);
      status.textContent = 'Read-only verification queued as task #' + data.task_id + '.';
    } catch (error) { status.textContent = error.message; verify.disabled = false; }
  });
  try { await load(); } catch (error) { status.textContent = error.message; }
});
