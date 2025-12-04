// static/js/sac_kb_console.js

(function () {
    const tbody = document.getElementById("sac-kb-tbody");
    const form = document.getElementById("sac-kb-upload-form");
    const uploadBtn = document.getElementById("kb-upload-btn");
    const statusEl = document.getElementById("kb-status");

    if (!tbody || !form || !uploadBtn || !statusEl) {
        console.warn("[SAC-KB] Elementos del DOM no encontrados en la consola de KB.");
        return;
    }

    function setStatus(message, isError) {
        statusEl.textContent = message || "";
        statusEl.classList.remove("ok", "err");
        if (!message) return;
        statusEl.classList.add(isError ? "err" : "ok");
    }

    function formatDateIso(isoStr) {
        if (!isoStr) return "";
        try {
            const d = new Date(isoStr);
            if (Number.isNaN(d.getTime())) return isoStr;
            // Devuelve YYYY-MM-DD HH:MM
            const pad = (n) => String(n).padStart(2, "0");
            return (
                d.getFullYear() +
                "-" +
                pad(d.getMonth() + 1) +
                "-" +
                pad(d.getDate()) +
                " " +
                pad(d.getHours()) +
                ":" +
                pad(d.getMinutes())
            );
        } catch {
            return isoStr;
        }
    }

    function renderEmpty(message) {
        tbody.innerHTML = "";
        const tr = document.createElement("tr");
        const td = document.createElement("td");
        td.colSpan = 7;
        const div = document.createElement("div");
        div.className = "sac-kb-empty";
        div.textContent = message;
        td.appendChild(div);
        tr.appendChild(td);
        tbody.appendChild(tr);
    }

    function renderDocs(items) {
        tbody.innerHTML = "";

        if (!items || items.length === 0) {
            renderEmpty("No hay documentos cargados aún.");
            return;
        }

        items.forEach((doc) => {
            const tr = document.createElement("tr");

            const tdId = document.createElement("td");
            tdId.textContent = doc.id != null ? String(doc.id) : "";
            tr.appendChild(tdId);

            const tdTitle = document.createElement("td");
            tdTitle.textContent = doc.title || "";
            tr.appendChild(tdTitle);

            const tdFile = document.createElement("td");
            tdFile.textContent = doc.filename || "";
            tr.appendChild(tdFile);

            const tdType = document.createElement("td");
            tdType.textContent = doc.source_type || "";
            tr.appendChild(tdType);

            const tdLang = document.createElement("td");
            tdLang.textContent = doc.lang || "";
            tr.appendChild(tdLang);

            const tdStatus = document.createElement("td");
            const span = document.createElement("span");
            if (doc.is_active === false) {
                span.className = "sac-badge-inactive";
                span.textContent = "Inactivo";
            } else {
                span.className = "sac-badge-active";
                span.textContent = "Activo";
            }
            tdStatus.appendChild(span);
            tr.appendChild(tdStatus);

            const tdCreated = document.createElement("td");
            tdCreated.textContent = formatDateIso(doc.created_at);
            tr.appendChild(tdCreated);

            tbody.appendChild(tr);
        });
    }

    async function loadDocs() {
        renderEmpty("Cargando documentos de la base de conocimiento...");
        try {
            const resp = await fetch("/sac/kb/docs", {
                method: "GET",
                headers: {
                    "Accept": "application/json"
                }
            });

            if (!resp.ok) {
                const txt = await resp.text();
                console.error("[SAC-KB] Error HTTP al cargar docs:", resp.status, txt);
                renderEmpty("No se pudieron cargar los documentos (error de servidor).");
                return;
            }

            const data = await resp.json();
            if (!data.ok) {
                console.error("[SAC-KB] Respuesta no OK:", data);
                renderEmpty("No se pudieron cargar los documentos (respuesta inválida).");
                return;
            }

            renderDocs(data.items || []);
        } catch (err) {
            console.error("[SAC-KB] Excepción al cargar documentos:", err);
            renderEmpty("No se pudieron cargar los documentos (error de comunicación).");
        }
    }

    async function uploadDoc(ev) {
        ev.preventDefault();

        const fileInput = document.getElementById("kb-file");
        const titleInput = document.getElementById("kb-title");
        const typeInput = document.getElementById("kb-source-type");
        const langInput = document.getElementById("kb-lang");

        const file = fileInput && fileInput.files && fileInput.files[0];
        if (!file) {
            setStatus("Debes seleccionar un archivo.", true);
            return;
        }
        if (!typeInput.value) {
            setStatus("Debes seleccionar un tipo de fuente.", true);
            return;
        }

        const formData = new FormData();
        formData.append("file", file);
        if (titleInput && titleInput.value) {
            formData.append("title", titleInput.value);
        }
        formData.append("source_type", typeInput.value);
        if (langInput && langInput.value) {
            formData.append("lang", langInput.value);
        }

        uploadBtn.disabled = true;
        setStatus("Subiendo e indexando documento...", false);

        try {
            // Ajustar esta URL si tu endpoint de ingesta tiene otro path
            const resp = await fetch("/sac/kb/teach", {
                method: "POST",
                body: formData
            });

            if (!resp.ok) {
                const txt = await resp.text();
                console.error("[SAC-KB] Error HTTP al subir doc:", resp.status, txt);
                setStatus("Error al subir o indexar el documento.", true);
                return;
            }

            let data;
            try {
                data = await resp.json();
            } catch (e) {
                console.error("[SAC-KB] Respuesta no es JSON:", e);
                setStatus("Se subió el archivo, pero no se pudo interpretar la respuesta del servidor.", true);
                return;
            }

            if (!data.ok) {
                console.error("[SAC-KB] Respuesta no OK al subir doc:", data);
                const msg = data.error || "El backend reportó un error al indexar el documento.";
                setStatus(msg, true);
                return;
            }

            setStatus("Documento cargado e indexado correctamente.", false);
            // Limpia el formulario y recarga la tabla
            form.reset();
            await loadDocs();
        } catch (err) {
            console.error("[SAC-KB] Excepción al subir documento:", err);
            setStatus("Error de comunicación con el servidor al subir el documento.", true);
        } finally {
            uploadBtn.disabled = false;
        }
    }

    form.addEventListener("submit", uploadDoc);

    // Carga inicial de documentos
    loadDocs();
})();
