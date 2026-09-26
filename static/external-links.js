(() => {
  'use strict';
  const selectors = '.article-body a[href], .pipeline-article a[href]';
  const dialog = document.createElement('dialog');
  dialog.className = 'external-link-dialog';
  dialog.setAttribute('aria-labelledby', 'external-link-heading');
  const heading = document.createElement('h2'); heading.id = 'external-link-heading'; heading.textContent = 'Open an external website?';
  const note = document.createElement('p'); note.textContent = 'This link takes you to an independent website. Its content and privacy practices may differ.';
  const destination = document.createElement('p'); destination.className = 'external-link-destination';
  const cancel = document.createElement('button'); cancel.textContent = 'Stay here';
  const proceed = document.createElement('a'); proceed.textContent = 'Continue to website'; proceed.rel = 'noopener noreferrer';
  dialog.append(heading, note, destination, cancel, proceed); document.body.append(dialog);
  cancel.addEventListener('click', () => dialog.close());
  document.querySelectorAll(selectors).forEach(link => {
    let url; try { url = new URL(link.href); } catch { return; }
    if (!['https:', 'http:'].includes(url.protocol) || url.origin === location.origin) return;
    // The application's own product subdomain is not an external publisher.
    if (location.hostname === 'trueapply.in' && url.hostname === 'app.trueapply.in') return;
    link.title = 'External website: ' + url.hostname;
    const label = document.createElement('span'); label.textContent = ' (external site)'; label.className = 'external-link-label';
    link.append(label);
    link.addEventListener('click', event => {
      if (event.ctrlKey || event.metaKey || event.shiftKey || event.altKey) return;
      event.preventDefault(); proceed.href = url.href;
      proceed.target = link.target || '_self'; destination.textContent = url.hostname;
      dialog.showModal(); cancel.focus();
    });
  });
})();
