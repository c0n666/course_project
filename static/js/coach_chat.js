/* Coach chat: sends messages with fetch and appends the replies without a reload.
   Without JS the form still posts to /coach/chat and the page reloads with the answer. */
(function () {
  'use strict';
  const form = document.getElementById('chatForm');
  const input = document.getElementById('chatInput');
  const log = document.getElementById('chatLog');
  const latest = document.getElementById('latest');
  const suggestions = document.getElementById('chatSuggestions');
  if (!form || !input) return;
  const sendBtn = form.querySelector('button[type="submit"]');

  function scrollToEnd(smooth = true) {
    latest.scrollIntoView({ behavior: smooth ? 'smooth' : 'auto', block: 'end' });
  }

  function bubble(templateId) {
    const node = document.getElementById(templateId).content.firstElementChild.cloneNode(true);
    log.insertBefore(node, latest);
    return node;
  }

  function autosize() {
    input.style.height = 'auto';
    input.style.height = Math.min(input.scrollHeight, 160) + 'px';
    input.style.overflowY = input.scrollHeight > 160 ? 'auto' : 'hidden';  // no scrollbar until it's needed
  }

  async function send(text) {
    const message = text.trim();
    if (!message || sendBtn.disabled) return;
    if (!navigator.onLine) {
      App.toast("You're offline. Reconnect to talk to the coach.", 'error');
      return;
    }
    if (suggestions) suggestions.hidden = true;

    const mine = bubble('bubbleMe');
    mine.textContent = message;  // user text is never parsed as HTML
    const typing = bubble('bubbleTyping');
    input.value = '';
    autosize();
    input.disabled = sendBtn.disabled = true;
    scrollToEnd();

    try {
      const body = await App.post(form.action, { message });
      bubble('bubbleAi').innerHTML = body.reply.html;  // server-rendered and escaped (coach_markup)
    } catch (err) {
      mine.remove();
      input.value = message;  // keep what they wrote so they can retry
      autosize();
      App.toast(err.message || 'The coach could not answer. Please try again.', 'error');
    } finally {
      typing.remove();
      input.disabled = sendBtn.disabled = false;
      input.focus();
      scrollToEnd();
    }
  }

  form.addEventListener('submit', (e) => {
    e.preventDefault();  // runs before the document-level spinner handler, which then skips
    send(input.value);
  });

  input.addEventListener('keydown', (e) => {
    // Enter sends, Shift+Enter adds a line (not while composing with an IME).
    if (e.key === 'Enter' && !e.shiftKey && !e.isComposing) {
      e.preventDefault();
      send(input.value);
    }
  });
  input.addEventListener('input', autosize);

  suggestions?.addEventListener('click', (e) => {
    const chip = e.target.closest('[data-suggestion]');
    if (chip) send(chip.dataset.suggestion);
  });

  autosize();
  scrollToEnd(false);
})();
