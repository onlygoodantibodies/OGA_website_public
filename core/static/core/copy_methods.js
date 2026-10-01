/* "Copy methods" — one handler for every surface that draws the button (the
   gene page, the data portal, the embed card). The paragraph is the button's
   own `data-methods`, composed on the server by
   `pipeline/services/methods_text.py`; this only puts it on the clipboard and
   says whether that worked. Delegated from the document, so buttons drawn
   after load (the portal draws its cards from the API) need no wiring. */
(function () {
  if (window.OGA_COPY_METHODS) return;
  window.OGA_COPY_METHODS = true;

  function viaTextarea(text) {
    var ta = document.createElement('textarea');
    ta.value = text;
    ta.setAttribute('readonly', '');
    ta.style.position = 'fixed';
    ta.style.top = '-1000px';
    document.body.appendChild(ta);
    ta.select();
    var ok = false;
    try { ok = document.execCommand('copy'); } catch (e) { ok = false; }
    document.body.removeChild(ta);
    return ok;
  }

  function say(btn, ok) {
    if (!btn.dataset.label) btn.dataset.label = btn.textContent;
    // A copy that failed must not look like one that worked.
    btn.textContent = ok ? 'Copied' : 'Could not copy';
    btn.classList.toggle('is-copied', ok);
    clearTimeout(btn._ogaTimer);
    btn._ogaTimer = setTimeout(function () {
      btn.textContent = btn.dataset.label;
      btn.classList.remove('is-copied');
    }, 2000);
  }

  document.addEventListener('click', function (event) {
    var btn = event.target.closest && event.target.closest('.copy-methods');
    if (!btn) return;
    event.preventDefault();
    event.stopPropagation();
    var text = btn.getAttribute('data-methods') || '';
    if (!text) return;
    if (navigator.clipboard && window.isSecureContext) {
      navigator.clipboard.writeText(text).then(
        function () { say(btn, true); },
        function () { say(btn, viaTextarea(text)); });
    } else {
      say(btn, viaTextarea(text));
    }
  });
})();
