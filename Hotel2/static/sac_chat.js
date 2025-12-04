// static/js/sac_chat.js

(function () {
    const messagesEl = document.getElementById("sac-chat-messages");
    const formEl = document.getElementById("sac-chat-form");
    const inputEl = document.getElementById("sac-chat-input");
    const sendBtnEl = document.getElementById("sac-chat-send");

    if (!messagesEl || !formEl || !inputEl || !sendBtnEl) {
        console.warn("[SAC-Chat] Elementos del DOM no encontrados");
        return;
    }

    let sessionId = window.localStorage.getItem("sac_chat_session_id") || null;
    let sending = false;

    function scrollToBottom() {
        messagesEl.scrollTop = messagesEl.scrollHeight;
    }

    function clearEmptyState() {
        const empty = messagesEl.querySelector(".sac-chat-empty");
        if (empty) {
            empty.remove();
        }
    }

    function createMsgRow(role, text, opts) {
        const row = document.createElement("div");
        row.className = "sac-msg-row " + role;

        const bubble = document.createElement("div");
        bubble.className = "sac-msg-bubble " + role;

        if (opts && opts.typing) {
            bubble.setAttribute("data-typing", "1");
            const dots = document.createElement("div");
            dots.className = "sac-typing-dots";
            dots.innerHTML = "<span></span><span></span><span></span>";
            bubble.appendChild(dots);
        } else {
            bubble.textContent = text || "";
        }

        row.appendChild(bubble);
        return row;
    }

    function appendUserMessage(text) {
        clearEmptyState();
        const row = createMsgRow("user", text);
        messagesEl.appendChild(row);
        scrollToBottom();
    }

    function appendBotMessage(text) {
        clearEmptyState();
        const row = createMsgRow("bot", text);
        messagesEl.appendChild(row);
        scrollToBottom();
    }

    function showTyping() {
        clearEmptyState();
        const row = createMsgRow("bot", "", { typing: true });
        messagesEl.appendChild(row);
        scrollToBottom();
        return row;
    }

    function removeTyping(row) {
        if (!row) return;
        const typingBubble = row.querySelector("[data-typing='1']");
        if (!typingBubble) return;
        row.remove();
    }

    async function sendMessage(text) {
        if (!text || !text.trim()) return;
        if (sending) return;
        sending = true;

        try {
            appendUserMessage(text.trim());
            formEl.reset();

            sendBtnEl.disabled = true;
            inputEl.disabled = true;

            const typingRow = showTyping();

            const payload = {
                message: text.trim(),
                session_id: sessionId
            };

            const resp = await fetch("/sac/ask", {
                method: "POST",
                headers: {
                    "Content-Type": "application/json"
                },
                body: JSON.stringify(payload)
            });

            removeTyping(typingRow);

            if (!resp.ok) {
                const txt = await resp.text();
                console.error("[SAC-Chat] Error HTTP:", resp.status, txt);
                appendBotMessage("Ha ocurrido un error al procesar la consulta. Inténtalo de nuevo en unos minutos.");
                return;
            }

            let data;
            try {
                data = await resp.json();
            } catch (err) {
                console.error("[SAC-Chat] Respuesta no es JSON válido:", err);
                appendBotMessage("No se pudo interpretar la respuesta del asistente.");
                return;
            }

            if (data.session_id) {
                sessionId = data.session_id;
                window.localStorage.setItem("sac_chat_session_id", sessionId);
            }

            const answer = (data.answer || data.respuesta || "").trim();
            if (!answer) {
                appendBotMessage("De momento no tengo una respuesta clara en la base de conocimiento para tu consulta.");
            } else {
                appendBotMessage(answer);
            }
        } catch (err) {
            console.error("[SAC-Chat] Excepción al enviar mensaje:", err);
            appendBotMessage("Ha ocurrido un error de comunicación con el servidor.");
        } finally {
            sending = false;
            sendBtnEl.disabled = false;
            inputEl.disabled = false;
            inputEl.focus();
        }
    }

    formEl.addEventListener("submit", function (ev) {
        ev.preventDefault();
        const value = inputEl.value || "";
        if (!value.trim()) {
            return;
        }
        sendMessage(value);
    });

    inputEl.addEventListener("keydown", function (ev) {
        if (ev.key === "Enter" && !ev.shiftKey) {
            ev.preventDefault();
            const value = inputEl.value || "";
            if (!value.trim()) {
                return;
            }
            sendMessage(value);
        }
    });

    // Foco inicial
    setTimeout(function () {
        inputEl.focus();
    }, 200);
})();
