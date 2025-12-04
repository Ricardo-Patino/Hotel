// static/assets/js/sac-widget.js
(function () {
  const widget   = document.getElementById('sac-widget');
  const openBtn  = document.getElementById('sac-open-btn');
  const closeBtn = document.getElementById('sac-close-btn');
  const panel    = document.getElementById('sac-chat-panel');
  const form     = document.getElementById('sac-chat-form');
  const input    = document.getElementById('sac-input');
  const messages = document.getElementById('sac-messages');
  const handoffBox = document.getElementById('sac-handoff-box');
  const handoffYes = document.getElementById('sac-handoff-yes');
  const handoffNo  = document.getElementById('sac-handoff-no');

  if (!widget || !openBtn || !panel || !form || !input || !messages) {
    return;
  }

  // --- Gestión de sesión ---
  function getCookie(name) {
    const match = document.cookie.match(new RegExp('(?:^|; )' + name.replace(/([$?*|{}\(\)\[\]\\\/\+^])/g, '\\$1') + '=([^;]*)'));
    return match ? decodeURIComponent(match[1]) : null;
  }

  function setCookie(name, value, days) {
    let expires = '';
    if (days) {
      const date = new Date();
      date.setTime(date.getTime() + (days * 24 * 60 * 60 * 1000));
      expires = '; expires=' + date.toUTCString();
    }
    document.cookie = name + '=' + encodeURIComponent(value) + expires + '; path=/';
  }

  function createSessionId() {
    return 'vg-' + Math.random().toString(36).substring(2) + Date.now().toString(36);
  }

  let sessionId = localStorage.getItem('vg_session');
  if (!sessionId) {
    sessionId = createSessionId();
    localStorage.setItem('vg_session', sessionId);
  }
  

  let handoffOffered = false;

  // --- UI helpers ---
  function appendMessage(text, role) {
    let cls = 'bot';
    if (role === 'user') cls = 'user';
    else if (role === 'agent') cls = 'bot'; // misma burbuja pero etiqueta diferente
    
    row.className = 'sac-msg-row ' + cls;
    bubble.className = 'sac-msg-bubble ' + cls;
    bubble.innerHTML = (text || '').replace(/\n/g, '<br>');
    row.appendChild(bubble);

    messages.appendChild(row);
    messages.scrollTop = messages.scrollHeight;
  }

  function setHandoffVisible(visible) {
    if (!handoffBox) return;
    handoffBox.classList.toggle('d-none', !visible);
    handoffOffered = visible;
  }

  function setFormDisabled(disabled) {
    input.disabled = disabled;
    form.querySelector('button[type="submit"]').disabled = disabled;
  }

  // --- Abrir/cerrar panel ---
  openBtn.addEventListener('click', () => {
    panel.classList.toggle('d-none');
    if (!panel.classList.contains('d-none')) {
      input.focus();
      // Mensaje inicial solo una vez
      if (!messages.dataset.initialized) {
        appendMessage('Hola, soy el asistente virtual de Hotel Villa Grace. Cuéntame en qué puedo ayudarte.', 'bot');
        messages.dataset.initialized = '1';
      }
    }
  });

  if (closeBtn) {
    closeBtn.addEventListener('click', () => {
      panel.classList.add('d-none');
    });
  }

  // --- Envío de mensaje al bot (/sac/ask) ---
  form.addEventListener('submit', async (ev) => {
    ev.preventDefault();
    const text = (input.value || '').trim();
    if (!text) return;

    appendMessage(text, 'user');
    input.value = '';
    setFormDisabled(true);
    setHandoffVisible(false);

    try {
      const res = await fetch('/sac/ask', {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
          'X-Session-Id': sessionId
        },
        body: JSON.stringify({ q: text })
      });

      if (!res.ok) {
        appendMessage('Lo siento, hubo un problema al procesar tu mensaje. Intenta de nuevo en unos segundos.', 'bot');
        setFormDisabled(false);
        return;
      }

      const data = await res.json();
      const answer = data.answer || 'Lo siento, no pude generar una respuesta en este momento.';
      appendMessage(answer, 'bot');

      if (data.need_handoff) {
        setHandoffVisible(true);
      } else {
        setHandoffVisible(false);
      }

    } catch (e) {
      appendMessage('No he podido comunicarme con el servidor. Por favor revisa tu conexión o intenta nuevamente.', 'bot');
    } finally {
      setFormDisabled(false);
    }
  });

  // --- Handoff Sí / No ---
  if (handoffYes) {
    handoffYes.addEventListener('click', async () => {
      if (!handoffOffered) return;
      setHandoffVisible(false);

      try {
        const res = await fetch('/sac/chat/escalar', {
          method: 'POST',
          headers: {
            'Content-Type': 'application/json',
            'X-Session-Id': sessionId
          },
          body: JSON.stringify({
            session_id: sessionId,
            texto: 'Cliente solicita atención humana desde el chat del sitio web.'
          })
        });
        const data = await res.json();
        if (data && data.ok) {
          appendMessage(
            'De acuerdo, he enviado tu consulta a un agente de recepción. ' +
            'En breve alguien del hotel continuará la atención por los medios habituales.',
            'bot'
          );
        } else {
          appendMessage(
            'Intenté escalar tu consulta, pero hubo un problema. ' +
            'Por favor contáctanos directamente al +506 2642 0225 o por correo.',
            'bot'
          );
        }
      } catch (e) {
        appendMessage(
          'No pude contactar al sistema de recepción en este momento. ' +
          'Por favor llámanos al +506 2642 0225 para ayudarte.',
          'bot'
        );
      }
    });
  }

  if (handoffNo) {
    handoffNo.addEventListener('click', () => {
      if (!handoffOffered) return;
      setHandoffVisible(false);
      appendMessage('Perfecto. Si en algún momento deseas hablar con un agente, solo indícamelo.', 'bot');
    });
  }

})();
