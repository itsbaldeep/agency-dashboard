(function () {
  const root = document.getElementById('marketing-connection-setup');
  const form = document.getElementById('marketing-connection-form');
  const status = document.getElementById('marketing-connection-status');
  if (!root || !form || !status) return;
  const brand = root.dataset.brandId;
  let digest = '';
  const message = (text, error) => { status.textContent = text; status.className = 'subtle mt-2' + (error ? ' error' : ''); };
  const load = () => fetch('/api/brands/' + brand + '/marketing-connection')
    .then(r => r.json().then(body => ({ ok: r.ok, body })))
    .then(({ ok, body }) => {
      if (!ok) return message(body.error || 'Connection setup unavailable', true);
      digest = body.digest || '';
      Object.entries(body.config || {}).forEach(([key, value]) => { if (form.elements[key]) form.elements[key].value = value; });
      message(body.configured ? 'Reference loaded. Saving creates a tracked setup record.' : 'No connection reference saved.');
    }).catch(() => message('Connection setup unavailable', true));
  form.addEventListener('submit', (event) => {
    event.preventDefault();
    const body = Object.fromEntries(new FormData(form).entries());
    body.digest = digest;
    fetch('/api/brands/' + brand + '/marketing-connection', {
      method: 'POST', headers: {'Content-Type': 'application/json', 'Origin': window.location.origin}, body: JSON.stringify(body)
    }).then(r => r.json().then(value => ({ ok: r.ok, value }))).then(({ ok, value }) => {
      if (!ok) return message(value.error || 'Could not save connection reference', true);
      digest = value.digest || '';
      message('Saved as a tracked setup record. No provider call or campaign was sent.');
    }).catch(() => message('Could not save connection reference', true));
  });
  load();
}());
