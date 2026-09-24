"""Browser behavior and responsive layout for the fleet conversation panel."""

CONVERSATION_CSS = """
#conversation-panel{position:fixed;right:0;top:0;bottom:0;width:min(460px,100vw);
  box-sizing:border-box;overflow-y:auto;background:#0f172a;border-left:1px solid #334155;
  box-shadow:-12px 0 40px #0005;padding:1.25rem;z-index:50}
#conversation-panel header{flex-direction:row;align-items:flex-start}
#conversation-panel h2{margin:0;overflow-wrap:anywhere}
#conversation-panel header p{margin:0}#conversation-panel p{overflow-wrap:anywhere}
#conversation-panel button{margin:.35rem .25rem .35rem 0}
#conversation-panel textarea{width:100%;box-sizing:border-box}
#conversation-panel label{display:block;margin:.7rem 0}
.conversation-status{padding:1rem 0;border-bottom:1px solid #334155}
.conversation-status small{color:#94a3b8}.voice-warning{color:#fbbf24;white-space:pre-wrap}
.conversation-history{max-height:32vh;overflow:auto;margin:1rem 0}
.conversation-message{border-bottom:1px solid #334155;padding:.5rem 0}
.conversation-message p{white-space:pre-wrap;margin:.3rem 0}
.conversation-message strong{color:#7dd3fc;font-size:.8rem}
.conversation-tests{margin-top:1rem;border-top:1px solid #334155}
#voice-preview audio{width:100%}
@media(min-width:1000px){body:has(#conversation-panel){padding-right:490px}}
"""

CONVERSATION_SCRIPT = """
(() => {
  let opener = null, audioUrl = null, previewRequest = null;
  function releasePreview() {
    if (previewRequest) previewRequest.abort();
    previewRequest = null;
    const audio = document.querySelector('#voice-preview audio');
    if (audio) audio.pause();
    if (audioUrl) URL.revokeObjectURL(audioUrl);
    audioUrl = null;
  }
  function cancelPanelRequests() {
    document.querySelectorAll(
      '#conversation-panel form, #conversation-panel button, #conversation-live')
      .forEach(el => htmx.trigger(el, 'htmx:abort'));
    releasePreview();
  }
  function closePanel() {
    cancelPanelRequests();
    document.getElementById('conversation-host').replaceChildren();
    if (opener && opener.isConnected) opener.focus();
  }
  // A browser agent running in the panel stops when the panel is replaced,
  // so relaunching it just shows it and opening anything else asks first.
  function runningAgent() {
    const panel = document.querySelector('#conversation-panel[data-running-agent]');
    return panel ? panel.dataset.runningAgent || 'this agent' : '';
  }
  document.body.addEventListener('htmx:beforeRequest', event => {
    if (event.detail.elt.matches('[data-conversation-open]')) {
      const elt = event.detail.elt, running = runningAgent();
      const url = new URL(elt.getAttribute('hx-get') || '', location.href);
      const launching = url.pathname === '/dashboard/page-agent/run' && (elt.matches('form')
        ? String(new FormData(elt).get('name') || '').trim() : url.searchParams.get('name'));
      if (running && launching === running) {
        event.preventDefault();
        document.getElementById('conversation-panel').focus();
        return;
      }
      if (running && !confirm('Stop ' + running + '? It is running in the side panel.')) {
        event.preventDefault();
        return;
      }
      opener = event.detail.elt;
      cancelPanelRequests();
    }
    if (event.detail.target.id === 'conversation-live') {
      event.detail.target.dataset.detailState = JSON.stringify(
        Array.from(event.detail.target.querySelectorAll('[data-live-detail]'))
          .map(el => [el.dataset.liveDetail, el.open]));
      const history = document.querySelector('.conversation-history');
      if (history) {
        event.detail.target.dataset.scroll = history.scrollTop;
        event.detail.target.dataset.bottom =
          history.scrollHeight - history.scrollTop - history.clientHeight < 30 ? '1' : '0';
      }
    }
  });
  document.body.addEventListener('htmx:afterSwap', event => {
    if (event.detail.target.id === 'conversation-host') {
      document.getElementById('conversation-panel')?.focus();
    }
    if (event.detail.target.id === 'conversation-live') {
      for (const [key, open] of JSON.parse(event.detail.target.dataset.detailState || '[]')) {
        const detail = event.detail.target.querySelector('[data-live-detail="' + key + '"]');
        if (detail) detail.open = open;
      }
      const history = document.querySelector('.conversation-history');
      if (history) history.scrollTop = event.detail.target.dataset.bottom === '0'
        ? Number(event.detail.target.dataset.scroll || 0) : history.scrollHeight;
    }
  });
  document.addEventListener('keydown', event => {
    if (event.key === 'Escape' && document.getElementById('conversation-panel')
        && !runningAgent()) closePanel();
  });
  document.addEventListener('click', async event => {
    if (event.target.closest('[data-close-conversation]')) { closePanel(); return; }
    // Moving a running agent to its own window. Removing the panel's frame
    // sends its goodbye now, before the window has even loaded; a goodbye
    // landing after the window registered would take the moved agent offline.
    const move = event.target.closest('[data-own-window-move]');
    if (move) {
      event.preventDefault();
      closePanel();
      window.open(move.href, '_blank', 'noopener');
      return;
    }
    const ownWindow = event.target.closest('[data-own-window]');
    if (ownWindow) {
      const form = ownWindow.closest('form');
      if (!form.reportValidity()) return;
      const params = new URLSearchParams();
      for (const [key, value] of new FormData(form)) if (value) params.set(key, value);
      window.open('/dashboard/page-agent?' + params.toString(), '_blank', 'noopener');
      return;
    }
    const button = event.target.closest('[data-voice-preview]');
    if (!button) return;
    releasePreview();
    const controller = new AbortController();
    previewRequest = controller;
    const result = document.getElementById('voice-preview');
    result.textContent = 'Preparing persona voice for this browser…';
    button.disabled = true;
    try {
      const response = await fetch(button.dataset.voicePreview, {
        method: 'POST', headers: {'X-Requested-With': 'XMLHttpRequest'}, signal: controller.signal
      });
      if (!response.ok) throw new Error('Voice preview failed. Check access and persona settings.');
      const blob = await response.blob();
      if (controller.signal.aborted || !result.isConnected) return;
      audioUrl = URL.createObjectURL(blob);
      const audio = document.createElement('audio');
      audio.controls = true; audio.src = audioUrl;
      const caption = document.createElement('p');
      caption.textContent = response.headers.get('X-Voice-Notice') ||
        'Persona voice · playback in this browser only';
      result.replaceChildren(caption, audio);
      try { await audio.play(); }
      catch (_) { caption.textContent += ' · Press play to enable sound.'; }
    } catch (error) {
      if (error.name !== 'AbortError') result.textContent = error.message;
    } finally { button.disabled = false; }
  });
})();
"""
