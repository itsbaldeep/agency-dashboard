/* Structured rules belong to the draft; recipients stay at their source. */
document.addEventListener('DOMContentLoaded', async () => {
  const form = document.getElementById('campaign-policy');
  if (!form) return;
  const status = document.getElementById('campaign-policy-status');
  const url = '/api/brands/' + window.marketingBrand + '/work-items/' + workId + '/campaign';
  let revision;
  function updateCategory() {
    const transactional = form.elements.category.value === 'transactional';
    Array.from(form.elements.play.options).forEach(option => {
      option.disabled = transactional ? option.value !== 'service_event' : option.value === 'service_event';
    });
    if (transactional) form.elements.play.value = 'service_event';
    else if (form.elements.play.value === 'service_event') form.elements.play.value = 'inactivity';
    form.elements.service_event.required = transactional;
    form.elements.service_event.disabled = !transactional;
    if (!transactional) form.elements.service_event.value = '';
    form.elements.inactivity_days.min = form.elements.play.value === 'inactivity' ? 7 : 0;
    if (Number(form.elements.inactivity_days.value) < Number(form.elements.inactivity_days.min)) {
      form.elements.inactivity_days.value = form.elements.inactivity_days.min;
    }
  }
  form.elements.category.addEventListener('change', updateCategory);
  form.elements.play.addEventListener('change', updateCategory);
  try {
    const response = await fetch(url);
    const data = await response.json();
    if (!response.ok || !data.ok) throw new Error(data.error || 'Campaign rules unavailable');
    revision = data.revision;
    Object.entries(data.policy).forEach(([key, value]) => {
      if (form.elements.namedItem(key)) form.elements.namedItem(key).value = value;
    });
    updateCategory();
    const list = document.getElementById('campaign-blockers');
    list.replaceChildren();
    data.blockers.forEach(message => {
      const item = document.createElement('li');
      item.textContent = message;
      list.appendChild(item);
    });
    form.querySelector('button').disabled = false;
  } catch (error) {
    status.textContent = error.message;
  }
  form.addEventListener('submit', async event => {
    event.preventDefault();
    if (!revision) return;
    const policy = Object.fromEntries(new FormData(form));
    ['inactivity_days', 'cooldown_hours', 'max_per_7_days'].forEach(key => { policy[key] = Number(policy[key]); });
    const button = form.querySelector('button');
    button.disabled = true;
    try {
      await marketingPost(url, {revision, policy});
      window.location.reload();
    } catch (error) {
      status.textContent = error.message;
      button.disabled = false;
    }
  });
});
