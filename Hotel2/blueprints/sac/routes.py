# Hotel/Hotel 2/blueprints/sac/routes.py
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Optional, List, Dict, Any, Tuple

import os
import json
import pathlib
import hashlib
import unicodedata
import re


from flask import (
    Blueprint,
    request,
    jsonify,
    render_template,
    session,
    current_app,
)
from sqlalchemy import text, inspect

from extensions import db
from utils.auth import role_required

from models.sac import (
    SACConfig,
    SACNotifPref,
    SACOutbox,
    SACSolicitud,
    SACConversation,
    SACConversationMsg,
    SACIncident,
    SACFeedback,
)

from werkzeug.utils import secure_filename
from services.sac.rag import KB_DIR, rebuild_index, search, answer_from_chunks
from collections import Counter

# ---------------------------------------------------------------------
# Blueprint SAC
# ---------------------------------------------------------------------
sac_bp = Blueprint("sac", __name__, url_prefix="/sac")

# ---------------------------------------------------------------------
# Duración de conversación
# ---------------------------------------------------------------------


MAX_CONV_AGE_HOURS = 24


def _get_active_conversation(
    session_id: str, create_if_missing: bool = True
) -> SACConversation | None:
    """
    Devuelve la conversación activa para un session_id.
    - Si la última conversación está cerrada o expirada (>24h) → se ignora.
    - Si no hay conversación válida y create_if_missing=True → crea una nueva.
    Esta versión es tolerante a modelos/bases de datos que NO tengan todas
    las columnas nuevas (Estado, Last_Msg_At, Status, Needs_Agent, etc.).
    """
    if not session_id:
        return None

    now = datetime.utcnow()

    # Última conversación por Session_Id
    conv = (
        SACConversation.query.filter_by(Session_Id=session_id)
        .order_by(SACConversation.Creada_At.desc())
        .first()
    )

    if conv:
        # Algunos campos pueden no existir en el modelo dependiendo de la versión
        abierta = getattr(conv, "Abierta", 1)
        estado = getattr(conv, "Estado", None)
        last_msg_at = getattr(conv, "Last_Msg_At", None)

        # Si está marcada como cerrada (cuando exista Estado) o Abierta=0 → no se reutiliza
        if abierta == 0 or estado == "CERRADA":
            conv = None
        # Si tiene última actividad muy antigua → expirada
        elif last_msg_at is not None and last_msg_at < now - timedelta(
            hours=MAX_CONV_AGE_HOURS
        ):
            conv = None

    if conv is None and create_if_missing:
        # Crear conversación nueva, usando solo los atributos que existan en el modelo
        conv = SACConversation(Session_Id=session_id)

        # Campos base
        if hasattr(conv, "Abierta"):
            conv.Abierta = 1
        if hasattr(conv, "Status"):
            conv.Status = "bot"
        if hasattr(conv, "Needs_Agent"):
            conv.Needs_Agent = 0
        if hasattr(conv, "Channel"):
            conv.Channel = "web"
        if hasattr(conv, "Estado") and getattr(conv, "Estado", None) is None:
            conv.Estado = "NORMAL"

        # Timestamps
        if hasattr(conv, "Creada_At") and getattr(conv, "Creada_At", None) is None:
            conv.Creada_At = now
        if hasattr(conv, "Actualizada_At"):
            conv.Actualizada_At = now
        if hasattr(conv, "Last_Msg_At"):
            conv.Last_Msg_At = now
        if hasattr(conv, "Last_Msg_Role"):
            conv.Last_Msg_Role = "bot"

        db.session.add(conv)
        db.session.commit()

    return conv


# ---------------------------------------------------------------------
# Utilidad de configuración
# ---------------------------------------------------------------------
def cfg(key: str, default: str = "") -> str:
    row = SACConfig.query.get(key)
    return (row.Valor if row else default) or default


# ---------------------------------------------------------------------
# Normalización y detección de small-talk / saludos
# ---------------------------------------------------------------------
def _norm(s: str) -> str:
    s = (s or "").strip().lower()
    s = "".join(
        c for c in unicodedata.normalize("NFD", s) if unicodedata.category(c) != "Mn"
    )
    return re.sub(r"\s+", " ", s)


def _is_greeting(qn: str) -> bool:
    GREET = (
        "hola",
        "buenas",
        "buenos dias",
        "buenas tardes",
        "buenas noches",
        "hi",
        "hello",
        "hey",
        "holi",
        "que tal",
        "qué tal",
        "alo",
        "aloo",
        "aloha",
    )
    return any(
        qn == g or qn.startswith(g + " ") or (" " + g + " ") in (" " + qn + " ")
        for g in GREET
    )


def _is_smalltalk(qn: str) -> bool:
    SMALL = (
        "gracias",
        "ok",
        "vale",
        "de acuerdo",
        "como estas",
        "cómo estás",
        "listo",
        "perfecto",
    )
    return any(qn == s or qn.startswith(s + " ") for s in SMALL)


def _is_help_like(qn: str) -> bool:
    HELP = (
        "ayuda",
        "menu",
        "menú",
        "opciones",
        "que puedes hacer",
        "qué puedes hacer",
        "como me puedes ayudar",
        "cómo me puedes ayudar",
        "que haces",
        "qué haces",
    )
    return any(h in qn for h in HELP)


def _hotel_greeting_answer() -> str:
    ci = cfg("checkin_inicio", "12:00")
    cf = cfg("checkin_fin", "00:00")
    co = cfg("checkout_limite", "12:00")
    return (
        f"¡Bienvenido/a a Hotel Villa Grace! Puedo ayudarle con:\n"
        f"• Reservas, disponibilidad y tarifas.\n"
        f"• Horarios (check-in {ci}–{cf} y check-out hasta {co}).\n"
        f"• Servicios: restaurante, bar, piscina 8:00–21:00, Wi-Fi y parqueo.\n"
        f"• Ubicación y estacionamiento.\n"
        f"• Solicitudes a recepción o incidentes.\n"
        f"Ejemplos: “Tarifa suite 12–14 mayo”, “¿late check-out?”, “política de cancelación”."
    )


def _noinfo_answer() -> str:
    """
    Mensaje estándar cuando el bot NO tiene información confiable en la KB ni reglas.
    No debe inventar nada; únicamente ofrecer transferencia a un agente humano.
    """
    return (
        "Por el momento no encuentro información confiable sobre esa consulta en la "
        "documentación interna del hotel ni en mis datos configurados. Prefiero no "
        "adivinar una respuesta incorrecta.\n\n"
        "¿Desea que transfiera esta conversación a un agente humano de recepción?"
    )


def _looks_like_noinfo(text: str) -> bool:
    """
    Detecta respuestas genéricas de “no tengo información / no hay información”
    para tratarlas como NO_INFO y ofrecer derivar a un agente.
    Se aplica tanto a respuestas base (KB_QA, RAG) como a textos
    ya reescritos por la IA.
    """
    if not text:
        return False

    t = text.lower()
    patterns = [
        # Formas típicas
        "no tengo información",
        "no tengo informacion",
        "no tengo información específica",
        "no tengo informacion especifica",
        "no tengo información sobre",
        "no tengo informacion sobre",
        # No hay datos
        "no hay información",
        "no hay informacion",
        "no encuentro información",
        "no encuentro informacion",
        "no dispongo de información",
        "no dispongo de informacion",
        "no cuento con información",
        "no cuento con informacion",
        "no tengo datos",
        # Formulaciones frecuentes en respuestas “no sé”
        "no se indica",
        "no se encuentra registrada",
        "no se menciona",
        # Muy importante: frases como la que estás viendo ahora
        "no está disponible en nuestra base de conocimiento",
        "no esta disponible en nuestra base de conocimiento",
        "no está disponible en la base de conocimiento",
        "no esta disponible en la base de conocimiento",
    ]
    return any(p in t for p in patterns)


def _default_suggestions(cid: Optional[int]) -> List[str]:
    base: List[str] = [
        "Horarios de check-in/out",
        "Servicios del hotel",
        "Cómo llegar y estacionamiento",
        "Disponibilidad y tarifas",
    ]
    if cid:
        base.insert(0, "Ver mis reservas")
    return base


def _convmsg_fk_attr() -> str:
    return "Conversation_Id" if hasattr(SACConversationMsg, "Conversation_Id") else "Conv_Id"

def _convmsg_text_attr() -> str:
    return "Texto" if hasattr(SACConversationMsg, "Texto") else "Msg_Text"

def _convmsg_query(conv_id: int):
    fk_col = getattr(SACConversationMsg, _convmsg_fk_attr())
    return SACConversationMsg.query.filter(fk_col == conv_id)

def _convmsg_text_value(m) -> str:
    return (getattr(m, "Texto", None) or getattr(m, "Msg_Text", None) or "").strip()

def _clean_fact(v: Optional[str]) -> str:
    s = (v or "").strip()
    s = re.sub(r"\s+", " ", s)
    s = re.sub(r"[.]+$", "", s)
    s = re.sub(r"[,]+$", "", s)
    return s.strip()
# ---------------------------------------------------------------------
# Sesión estandarizada (CLIENTE)
# ---------------------------------------------------------------------
def _current_cliente_y_email() -> Tuple[Optional[int], Optional[str]]:
    """
    Reglas:
      - user_role == "Cliente"
      - session["user_id"] = Codigo_Usuario
      - SIEMPRE mapear a Codigo_Cliente usando Usuario.Codigo_Usuario → Usuario.Codigo_Cliente.
    """
    if session.get("user_role") != "Cliente":
        return None, None

    # Email directo de sesión si está
    email = session.get("user_email") or None
    if isinstance(email, str):
        email = email.strip().lower() or None

    # Cache de cliente si ya se resolvió antes
    if session.get("user_cliente_id"):
        try:
            return int(session["user_cliente_id"]), email
        except Exception:
            pass

    # Mapear SIEMPRE desde Usuario.Codigo_Usuario
    uid = session.get("user_id")
    try:
        uid = int(uid) if uid is not None else None
    except Exception:
        uid = None

    cid: Optional[int] = None
    if uid:
        row = (
            db.session.execute(
                text(
                    "SELECT Codigo_Cliente FROM Usuario "
                    "WHERE Codigo_Usuario = :u LIMIT 1"
                ),
                {"u": uid},
            )
            .mappings()
            .first()
        )
        if row and row.get("Codigo_Cliente"):
            cid = int(row["Codigo_Cliente"])

    # Guarda cacheado para siguientes requests
    if cid:
        session["user_cliente_id"] = cid

    return cid, email


# ---------------------------------------------------------------------
# TODAS las reservas del cliente (por id y/o correo)
# ---------------------------------------------------------------------
def _reservas_del_cliente(limit: Optional[int] = None) -> List[Dict[str, Any]]:
    cid, email = _current_cliente_y_email()
    if not cid and not email:
        return []

    params = {
        "cid": int(cid) if cid else -1,
        "email": (email or ""),
    }
    lim_sql = f"LIMIT {int(limit)}" if (limit and limit > 0) else ""
    rows = (
        db.session.execute(
            text(
                f"""
        SELECT
            r.Codigo_Reserva                  AS Codigo_Reserva,
            r.Fecha_Entrada                   AS Fecha_Entrada,
            r.Fecha_Salida                    AS Fecha_Salida,
            r.Estado                          AS Estado,
            COALESCE(h.Numero_Habitacion,'-') AS Habitacion,
            COALESCE(h.Tipo,'')               AS Tipo
        FROM Reserva r
        LEFT JOIN Habitacion h ON h.Codigo_Habitacion = r.Codigo_Habitacion
        LEFT JOIN Cliente    c ON c.Codigo_Cliente    = r.Codigo_Cliente
        WHERE
              (:cid > 0 AND r.Codigo_Cliente = :cid)
           OR (:email <> '' AND LOWER(COALESCE(c.Correo,'')) = :email)
        ORDER BY r.Fecha_Entrada DESC, r.Codigo_Reserva DESC
        {lim_sql}
    """
            ),
            params,
        )
        .mappings()
        .all()
    )
    return [dict(r) for r in rows]


# ====================== PREFERENCIAS ======================

def _load_active_kb_docs() -> List[Dict[str, Any]]:
    with db.engine.begin() as conn:
        rows = conn.execute(
            text("""
                SELECT Id, Title, Content, Metadata_JSON, Created_At
                  FROM SAC_KB_Doc
                 WHERE Status = 'ACTIVE'
                 ORDER BY Created_At DESC, Id DESC
            """)
        ).mappings().all()
    return [dict(r) for r in rows]


def _extract_label_value(content: str, labels: List[str]) -> Optional[str]:
    if not content:
        return None

    for label in labels:
        # Caso 1: "Etiqueta: valor"
        m = re.search(
            rf"(?im)^{re.escape(label)}\s*:\s*(.+)$",
            content,
        )
        if m:
            val = (m.group(1) or "").strip()
            if val:
                return val

        # Caso 2: "Etiqueta:" en una línea y valor en la siguiente
        m = re.search(
            rf"(?ims)^{re.escape(label)}\s*:\s*\n\s*([^\n]+)",
            content,
        )
        if m:
            val = (m.group(1) or "").strip()
            if val:
                return val

    return None


def _extract_section_bullets(content: str, headings: List[str]) -> List[str]:
    if not content:
        return []

    items: List[str] = []

    for heading in headings:
        m = re.search(
            rf"(?ims)^{re.escape(heading)}\s*:\s*(.*?)(?=^\S.*?:|\Z)",
            content,
        )
        if not m:
            continue

        block = (m.group(1) or "").strip()
        for line in block.splitlines():
            s = line.strip()
            if not s:
                continue
            if s.startswith("-") or s.startswith("•"):
                clean = s.lstrip("-• ").strip()
                clean = clean.rstrip(".").strip()
                if clean:
                    items.append(clean)

    # dedupe preservando orden
    seen = set()
    out = []
    for x in items:
        k = _norm(x)
        if k not in seen:
            seen.add(k)
            out.append(x)
    return out


def _extract_noinfo_message(content: str) -> str:
    if not content:
        return _noinfo_answer()

    m = re.search(
        r'(?is)la respuesta correcta debe ser:\s*["“]?(.+?)["”]?\s*(?:\n|$)',
        content
    )
    if m:
        txt = (m.group(1) or "").strip()
        if txt:
            return txt

    return _noinfo_answer()



def _tokenize_norm(s: str) -> List[str]:
    return [t for t in re.findall(r"[a-z0-9áéíóúñ]+", _norm(s)) if len(t) > 1]


def _extract_kb_sections(content: str) -> Dict[str, Dict[str, Any]]:
    """
    Extrae secciones de documentos con formato:
      Etiqueta: valor
      Etiqueta:
      - item 1
      - item 2

    Funciona para TXT, texto extraído de PDF y DOCX razonablemente estructurados.
    """
    sections: Dict[str, Dict[str, Any]] = {}
    current_key: Optional[str] = None

    for raw_line in (content or "").splitlines():
        line = (raw_line or "").strip()
        if not line:
            continue

        m = re.match(r"^([^:\n]{2,140})\s*:\s*(.*)$", line)
        if m:
            label = (m.group(1) or "").strip()
            value = (m.group(2) or "").strip()

            key = _norm(label)
            bucket = sections.setdefault(
                key,
                {
                    "label": label,
                    "values": [],
                    "items": [],
                    "paragraphs": [],
                },
            )
            current_key = key

            if value:
                clean_value = value.lstrip("-• ").strip()
                if clean_value:
                    bucket["values"].append(clean_value)
            continue

        if current_key is None:
            continue

        bucket = sections[current_key]
        if line.startswith("-") or line.startswith("•"):
            item = line.lstrip("-• ").strip()
            if item:
                bucket["items"].append(item)
        else:
            bucket["paragraphs"].append(line)

    for bucket in sections.values():
        merged_parts = []
        merged_parts.extend(bucket.get("values", []))
        merged_parts.extend(bucket.get("paragraphs", []))
        bucket["value"] = " ".join([p.strip() for p in merged_parts if p and p.strip()]).strip()

    return sections


def _section_first_value(
    sections: Dict[str, Dict[str, Any]],
    aliases: List[str],
) -> str:
    alias_norms = [_norm(a) for a in aliases]

    # match exacto por alias
    for alias in alias_norms:
        if alias in sections:
            val = _clean_fact(sections[alias].get("value") or "")
            if val:
                return val

    # match parcial por alias
    for alias in alias_norms:
        for key, sec in sections.items():
            if alias == key or alias in key or key in alias:
                val = _clean_fact(sec.get("value") or "")
                if val:
                    return val

    return ""


def _collect_section_items(
    sections: Dict[str, Dict[str, Any]],
    label_aliases: Optional[List[str]] = None,
) -> List[str]:
    out: List[str] = []
    seen = set()

    label_aliases_norm = [_norm(x) for x in (label_aliases or [])]

    for key, sec in sections.items():
        if label_aliases_norm:
            if not any(a == key or a in key or key in a for a in label_aliases_norm):
                continue

        for item in sec.get("items", []) or []:
            clean = _clean_fact(item)
            if clean:
                nk = _norm(clean)
                if nk not in seen:
                    seen.add(nk)
                    out.append(clean)

    return out


def _split_kb_sentences(content: str) -> List[str]:
    parts = re.split(r"(?<=[\.\!\?])\s+|\n+", content or "")
    out = []
    for p in parts:
        s = p.strip(" \t\r\n-•")
        if len(s) >= 3:
            out.append(s)
    return out


def _best_sentence_match(content: str, query: str) -> str:
    q_tokens = set(_tokenize_norm(query))
    if not q_tokens:
        return ""

    best_text = ""
    best_score = 0.0

    for s in _split_kb_sentences(content):
        s_tokens = set(_tokenize_norm(s))
        if not s_tokens:
            continue

        overlap = len(q_tokens & s_tokens)
        if overlap <= 0:
            continue

        score = overlap / max(len(q_tokens), 1)

        if "km" in s.lower() or "metros" in s.lower() or "m " in s.lower():
            score += 0.10

        if score > best_score:
            best_score = score
            best_text = s

    return best_text


def _extract_yes_no_target(qn: str) -> str:
    patterns = [
        r"\btiene(?:n)?\s+(.+?)(?:\?|$)",
        r"\bacepta(?:n)?\s+(.+?)(?:\?|$)",
        r"\bincluye(?:n)?\s+(.+?)(?:\?|$)",
        r"\bofrece(?:n)?\s+(.+?)(?:\?|$)",
        r"\bcuenta con\s+(.+?)(?:\?|$)",
        r"\bhay\s+(.+?)(?:\?|$)",
        r"\bdispone de\s+(.+?)(?:\?|$)",
    ]

    for pat in patterns:
        m = re.search(pat, qn, flags=re.IGNORECASE)
        if m:
            target = (m.group(1) or "").strip()
            target = re.sub(r"^(el|la|los|las|un|una)\s+", "", target, flags=re.IGNORECASE)
            target = re.sub(r"\bdel hotel\b", "", target, flags=re.IGNORECASE).strip()
            target = re.sub(r"\bel hotel\b", "", target, flags=re.IGNORECASE).strip()
            return target.strip("¿?.,;: ")
    return ""


def _match_target_in_items(target: str, items: List[str]) -> str:
    target_norm = _norm(target)
    target_tokens = set(_tokenize_norm(target))

    if not target_norm or not target_tokens:
        return ""

    best_item = ""
    best_score = 0.0

    for item in items:
        item_norm = _norm(item)
        item_tokens = set(_tokenize_norm(item))

        score = 0.0
        if target_norm in item_norm or item_norm in target_norm:
            score = 1.0
        else:
            overlap = len(target_tokens & item_tokens)
            if overlap > 0:
                score = overlap / max(len(target_tokens), 1)

        if score > best_score:
            best_score = score
            best_item = item

    return best_item if best_score >= 0.50 else ""


def _build_summary_from_sections(
    hotel_name: str,
    lema: str,
    ubicacion: str,
    direccion: str,
    habitaciones: str,
    checkin: str,
    checkout: str,
    servicios: List[str],
    cercanias: List[str],
) -> str:
    parts: List[str] = []

    if hotel_name:
        if lema:
            parts.append(f"{hotel_name}. {lema}.")
        else:
            parts.append(f"{hotel_name}.")

    if ubicacion or direccion:
        loc = ", ".join([x for x in [ubicacion, direccion] if x])
        if loc:
            parts.append(f"Está ubicado en {loc}.")

    if habitaciones:
        parts.append(f"Cuenta con {habitaciones}.")

    if servicios:
        parts.append("Ofrece " + ", ".join(servicios[:8]) + ".")

    if checkin or checkout:
        if checkin and checkout:
            parts.append(f"El check-in es {checkin} y el check-out es {checkout}.")
        elif checkin:
            parts.append(f"El horario de check-in es {checkin}.")
        elif checkout:
            parts.append(f"El horario de check-out es {checkout}.")

    if cercanias:
        parts.append("Cerca del hotel están " + ", ".join(cercanias[:4]) + ".")

    return " ".join(parts).strip()


def _ai_backend_available() -> bool:
    return bool((os.getenv("SAC_N8N_WEBHOOK") or "").strip()) or (
        os.getenv("SAC_ENABLE_OLLAMA", "0") in ("1", "true", "True")
    )


def _call_ai_answer_from_context(
    user_q: str,
    hits: List[Dict[str, Any]],
) -> Tuple[Optional[str], Optional[str], float]:
    """
    Usa el backend LLM configurado (n8n u Ollama) para responder SOLO con base
    en el contexto documental recuperado del índice RAG.
    """
    if not hits or not _ai_backend_available():
        return None, None, 0.0

    custom_instructions = cfg("chatbot_system_prompt", "").strip()
    system_prompt = _build_effective_system_prompt(custom_instructions)

    context_blocks = []
    for idx, h in enumerate(hits[:5], start=1):
        txt = (h.get("text") or "").strip()
        if txt:
            context_blocks.append(f"[Fragmento {idx}]\n{txt}")

    context_text = "\n\n".join(context_blocks).strip()
    if not context_text:
        return None, None, 0.0

    noinfo = _noinfo_answer()

    messages = [
        {
            "role": "system",
            "content": (
                system_prompt
                + "\nReglas obligatorias:\n"
                + "1) Responde ÚNICAMENTE con base en el contexto documental proporcionado.\n"
                + "2) No inventes datos, no completes huecos y no uses conocimiento externo.\n"
                + "3) Si la respuesta no está respaldada por el contexto, responde EXACTAMENTE este texto:\n"
                + noinfo
                + "\n4) Si el contexto sí responde la pregunta, contesta breve, clara y natural.\n"
            ),
        },
        {
            "role": "user",
            "content": (
                f"Pregunta del huésped:\n{user_q}\n\n"
                f"Contexto documental recuperado:\n{context_text}"
            ),
        },
    ]

    txt, source, conf = _call_ai_chat(messages, temperature=0.1)
    if not txt:
        return None, None, 0.0

    return txt.strip(), source or "AI_CONTEXT", conf or 0.0


def _build_compact_rag_answer(
    raw_q: str,
    hits: List[Dict[str, Any]],
) -> Optional[Dict[str, Any]]:
    """
    Fallback sin IA:
    intenta devolver la frase más relevante encontrada en los chunks recuperados,
    pero filtra títulos, encabezados y fragmentos demasiado pobres.
    """
    if not hits:
        return None

    best_sentence = ""
    best_score = 0.0
    q_tokens = set(_tokenize_norm(raw_q))

    for h in hits[:5]:
        for s in _split_kb_sentences(h.get("text") or ""):
            st = (s or "").strip()
            s_tokens = set(_tokenize_norm(st))
            if not s_tokens:
                continue

            # Filtro: descartar encabezados muy cortos tipo "Hotel Costa Esmeralda"
            if len(s_tokens) <= 3 and len(st.split()) <= 4 and not any(ch in st for ch in ".:;!?"):
                continue

            overlap = len(q_tokens & s_tokens)
            if overlap <= 0:
                continue

            score = overlap / max(len(q_tokens), 1)

            # Penalizar frases demasiado cortas o vagas
            if len(st) < 18:
                score -= 0.20

            if score > best_score:
                best_score = score
                best_sentence = st

    if not best_sentence or best_score < 0.45:
        return None

    return {
        "answer": best_sentence,
        "confidence": max(0.35, min(0.85, best_score)),
        "source": "KB_DOCS_SENTENCE",
    }
    
    

def _answer_from_uploaded_kb(raw_q: str, qn: str) -> Optional[Dict[str, Any]]:
    """
    Responde de forma dinámica usando los documentos activos de SAC_KB_Doc.
    No hardcodea valores del hotel y soporta cambios de contenido entre archivos.
    """
    docs = _load_active_kb_docs()
    if not docs:
        return None

    contents = [
        str(d.get("Content") or "")
        for d in docs
        if (d.get("Content") or "").strip()
    ]
    if not contents:
        return None

    content = "\n\n".join(contents)
    sections = _extract_kb_sections(content)

    hotel_name = (
        _section_first_value(sections, ["Nombre oficial", "Nombre del hotel", "Nombre"])
        or (docs[0].get("Title") or "").strip()
        or "El hotel"
    )
    lema = _section_first_value(sections, ["Lema", "Slogan", "Claim"])
    ubicacion = _section_first_value(
        sections,
        ["Ubicación", "Ubicacion", "Localización", "Localizacion"]
    )
    direccion = _section_first_value(sections, ["Dirección", "Direccion"])
    telefono = _section_first_value(
        sections,
        ["Teléfono", "Telefono", "WhatsApp", "Número de teléfono", "Numero de telefono"]
    )
    correo = _section_first_value(
        sections,
        ["Correo electrónico", "Correo electronico", "Correo", "Email", "E-mail"]
    )
    habitaciones = _section_first_value(
        sections,
        ["Cantidad de habitaciones", "Habitaciones", "Número de habitaciones", "Numero de habitaciones"]
    )

    checkin_doc = _section_first_value(
        sections,
        ["Horario de check-in", "Horario de check in", "Check-in", "Check in"]
    )
    checkout_doc = _section_first_value(
        sections,
        ["Horario de check-out", "Horario de check out", "Check-out", "Check out"]
    )

    # Valores del documento
    checkin = checkin_doc
    checkout = checkout_doc

    # Fallback de configuración SOLO para respuestas directas de horario,
    # no para contaminar el resumen si el documento ya trae otros datos.
    checkin_cfg = ""
    checkout_cfg = ""

    if not checkin_doc:
        ci = cfg("checkin_inicio", "")
        cf = cfg("checkin_fin", "")
        if ci and cf:
            checkin_cfg = f"De {ci} a {cf}"

    if not checkout_doc:
        co = cfg("checkout_limite", "")
        if co:
            checkout_cfg = f"Hasta las {co}"

    # Variables efectivas para respuestas directas de horario
    effective_checkin = checkin_doc or checkin_cfg
    effective_checkout = checkout_doc or checkout_cfg

    servicios = _collect_section_items(
        sections,
        [
            "Servicios y amenidades",
            "Servicios",
            "Amenidades",
            "Amenidades y servicios",
            "Incluye",
            "Servicios incluidos",
            "Facilidades",
            "Características",
            "Caracteristicas",
        ],
    )

    cercanias = _collect_section_items(
        sections,
        [
            "Puntos de referencia y distancias",
            "Distancias",
            "Lugares cercanos",
            "Ubicación y distancias",
            "Cercanías",
            "Cercanias",
        ],
    )

    no_confirmado = _collect_section_items(
        sections,
        [
            "Información que NO está confirmada",
            "Información que no está confirmada",
            "Informacion que NO esta confirmada",
            "Informacion que no esta confirmada",
            "No confirmado",
            "No confirmada",
        ],
    )

    noinfo_msg = _extract_noinfo_message(content)

    q = qn or _norm(raw_q)
    unknown_blob = " | ".join([_norm(x) for x in no_confirmado])

    # ----------------------------
    # Nombre / lema
    # ----------------------------
    if any(x in q for x in ["nombre oficial", "como se llama", "cómo se llama", "nombre del hotel"]):
        if hotel_name:
            return {
                "answer": hotel_name.rstrip("."),
                "confidence": 0.98,
                "source": "KB_DOCS_STRUCTURED",
            }

    if any(x in q for x in ["lema", "slogan", "claim"]):
        if lema:
            return {
                "answer": lema.rstrip("."),
                "confidence": 0.98,
                "source": "KB_DOCS_STRUCTURED",
            }

    # ----------------------------
    # Ubicación / dirección / contacto
    # ----------------------------
    if "direccion" in q or "dirección" in raw_q.lower():
        if direccion:
            return {
                "answer": f"La dirección de {hotel_name} es {direccion.rstrip('.')}.",
                "confidence": 0.97,
                "source": "KB_DOCS_STRUCTURED",
            }

    if "donde" in q or "dónde" in raw_q.lower() or "ubic" in q:
        loc = ", ".join([x.rstrip(".") for x in [ubicacion, direccion] if x])
        if loc:
            return {
                "answer": f"{hotel_name} está ubicado en {loc}.",
                "confidence": 0.97,
                "source": "KB_DOCS_STRUCTURED",
            }

    if "telefono" in q or "teléfono" in raw_q.lower():
        if telefono:
            return {
                "answer": f"El teléfono de {hotel_name} es {telefono.rstrip('.')}.",
                "confidence": 0.98,
                "source": "KB_DOCS_STRUCTURED",
            }

    if "correo" in q or "email" in q or "e-mail" in q:
        if correo:
            return {
                "answer": f"El correo electrónico de {hotel_name} es {correo.rstrip('.')}.",
                "confidence": 0.98,
                "source": "KB_DOCS_STRUCTURED",
            }

    # ----------------------------
    # Habitaciones y horarios
    # ----------------------------
    if (
        "cuantas habitaciones" in q
        or "cuántas habitaciones" in raw_q.lower()
        or "habitaciones tiene" in q
        or "numero de habitaciones" in q
        or "número de habitaciones" in raw_q.lower()
    ):
        if habitaciones:
            return {
                "answer": f"{hotel_name} cuenta con {habitaciones.rstrip('.')}.",
                "confidence": 0.98,
                "source": "KB_DOCS_STRUCTURED",
            }

    if "check-in" in raw_q.lower() or "check in" in q or ("horario" in q and "entrada" in q):
        if effective_checkin:
            return {
                "answer": f"El horario de check-in es {effective_checkin.rstrip('.')}.",
                "confidence": 0.97,
                "source": "KB_DOCS_STRUCTURED",
            }

    if "check-out" in raw_q.lower() or "check out" in q or ("horario" in q and "salida" in q):
        if effective_checkout:
            return {
                "answer": f"El horario de check-out es {effective_checkout.rstrip('.')}.",
                "confidence": 0.97,
                "source": "KB_DOCS_STRUCTURED",
            }

    if ("horarios" in q or "horario" in q) and ("check" in q or "entrada" in q or "salida" in q):
        if effective_checkin or effective_checkout:
            answer = []
            if effective_checkin:
                answer.append(f"check-in: {effective_checkin.rstrip('.')}")
            if effective_checkout:
                answer.append(f"check-out: {effective_checkout.rstrip('.')}")
            return {
                "answer": "Los horarios son " + " y ".join(answer) + ".",
                "confidence": 0.97,
                "source": "KB_DOCS_STRUCTURED",
            }

    # ----------------------------
    # Preguntas de sí/no sobre servicios o características
    # IMPORTANTE: van ANTES del listado general de servicios
    # ----------------------------
    target = _extract_yes_no_target(q)
    if target:
        all_items = []
        for sec in sections.values():
            all_items.extend(sec.get("items", []) or [])
            if sec.get("value"):
                all_items.append(sec.get("value"))

        matched = _match_target_in_items(target, all_items)
        if matched:
            matched_clean = matched.rstrip(".").strip()
            return {
                "answer": f"Sí. La documentación indica: {matched_clean}.",
                "confidence": 0.95,
                "source": "KB_DOCS_STRUCTURED",
            }

        # Si la pregunta es de sí/no y NO encontramos el servicio en la documentación,
        # no debemos caer a RAG ni devolver títulos o ruido.
        return {
            "answer": noinfo_msg,
            "confidence": 0.90,
            "source": "NO_INFO",
        }

    # ----------------------------
    # Servicios / amenidades (resumen general)
    # ----------------------------
    if (
        "servicio" in q
        or "amenidad" in q
        or "amenidades" in q
        or "que ofrece" in q
        or "qué ofrece" in raw_q.lower()
        or "que tiene el hotel" in q
        or "qué tiene el hotel" in raw_q.lower()
    ):
        if servicios:
            servicios_clean = [s.rstrip(".").strip() for s in servicios[:12] if s.strip()]
            return {
                "answer": f"{hotel_name} ofrece " + ", ".join(servicios_clean) + ".",
                "confidence": 0.96,
                "source": "KB_DOCS_STRUCTURED",
            }

    # ----------------------------
    # Distancias / lugares cercanos
    # ----------------------------
    if (
        "queda cerca" in q
        or "lugares cerca" in q
        or "lugares quedan cerca" in q
        or "que lugares quedan cerca" in q
        or "qué lugares quedan cerca" in raw_q.lower()
        or "cerca del hotel" in q
    ):
        if cercanias:
            cercanias_clean = [c.rstrip(".").strip() for c in cercanias[:8] if c.strip()]
            return {
                "answer": "Cerca del hotel están " + ", ".join(cercanias_clean) + ".",
                "confidence": 0.95,
                "source": "KB_DOCS_STRUCTURED",
            }

    if "distancia" in q or "a que distancia" in q or "a qué distancia" in raw_q.lower():
        best_sentence = _best_sentence_match(content, raw_q)
        if best_sentence:
            return {
                "answer": best_sentence.rstrip(".").strip() + ".",
                "confidence": 0.92,
                "source": "KB_DOCS_STRUCTURED",
            }

    # ----------------------------
    # Resumen
    # ----------------------------
    if "resumen" in q or "resum" in q:
        summary = _build_summary_from_sections(
            hotel_name=hotel_name,
            lema=lema,
            ubicacion=ubicacion,
            direccion=direccion,
            habitaciones=habitaciones,
            checkin=checkin_doc,
            checkout=checkout_doc,
            servicios=servicios,
            cercanias=cercanias,
        )
        if summary:
            return {
                "answer": summary,
                "confidence": 0.95,
                "source": "KB_DOCS_STRUCTURED",
            }

    # ----------------------------
    # Información marcada como no confirmada
    # ----------------------------
    if no_confirmado and target:
        if target and _norm(target) in unknown_blob:
            return {
                "answer": noinfo_msg,
                "confidence": 0.90,
                "source": "NO_INFO",
            }

    return None


@sac_bp.get("/preferencias", endpoint="preferencias_html")
def preferencias_html():
    cid, _ = _current_cliente_y_email()
    pref = SACNotifPref.query.get(int(cid)) if cid else None
    return render_template(
        "sac-preferencias.html",
        pref=pref,
        checkin_ini=cfg("checkin_inicio", "12:00"),
        checkin_fin=cfg("checkin_fin", "00:00"),
        checkout=cfg("checkout_limite", "12:00"),
    )


@sac_bp.post("/preferencias", endpoint="preferencias_save")
def preferencias_save():
    cid, email_sesion = _current_cliente_y_email()
    if not cid:
        return jsonify({"ok": False, "error": "No autenticado"}), 401

    data = request.get_json(silent=True) or request.form or {}
    canal = (data.get("Canal") or "email").strip().lower()
    if canal not in ("email", "sms", "ambos"):
        canal = "email"

    pref = SACNotifPref.query.get(cid) or SACNotifPref(Codigo_Cliente=cid)
    pref.Canal = canal
    pref.Email = (data.get("Email") or email_sesion or "").strip() or None
    pref.Telefono = (data.get("Telefono") or "").strip() or None
    db.session.add(pref)
    db.session.commit()
    return jsonify({"ok": True})


# ====================== FAQ / HORARIOS ======================
@sac_bp.get("/faq", endpoint="faq_html")
def faq_html():
    """
    Página de FAQ y horarios para menú principal.
    Endpoint: 'sac.faq_html' → url_for('sac.faq_html')
    """
    return render_template(
        "sac-faq.html",
        checkin_ini=cfg("checkin_inicio", "12:00"),
        checkin_fin=cfg("checkin_fin", "00:00"),
        checkout=cfg("checkout_limite", "12:00"),
    )


@sac_bp.get("/horarios")
def horarios_api():
    return jsonify(
        {
            "checkin_inicio": cfg("checkin_inicio", "12:00"),
            "checkin_fin": cfg("checkin_fin", "00:00"),
            "checkout": cfg("checkout_limite", "12:00"),
        }
    )


# ====================== CHATBOT (widget embebido simple - legado) ======================
@sac_bp.post("/chat/ask")
def chatbot_ask():
    """
    Endpoint antiguo para el widget ligero.
    Se mantiene simple, sin IA abierta, para respuestas rápidas.
    Parámetros esperados (JSON):
      - q: texto de la pregunta del usuario.
    """
    payload = request.get_json(silent=True) or {}
    q = (payload.get("q") or "").strip()
    qn = _norm(q)
    cid, _ = _current_cliente_y_email()

    # Atender saludos/ayuda/small-talk de forma hotel-céntrica
    if (
        (not qn)
        or _is_greeting(qn)
        or _is_smalltalk(qn)
        or _is_help_like(qn)
    ):
        answer = _hotel_greeting_answer()
        suggestions = _default_suggestions(cid)
    else:
        # Respuestas rápidas por palabra clave (sin IA abierta)
        faqs = {
            "check-in": f"El check-in es entre {cfg('checkin_inicio','12:00')} y {cfg('checkin_fin','00:00')}.",
            "check in": f"El check-in es entre {cfg('checkin_inicio','12:00')} y {cfg('checkin_fin','00:00')}.",
            "check-out": f"El check-out es hasta las {cfg('checkout_limite','12:00')}.",
            "checkout": f"El check-out es hasta las {cfg('checkout_limite','12:00')}.",
            "servicios": "Restaurante, bar, piscina 8:00–21:00, Wi-Fi gratis y parqueo sin costo.",
            "piscina": "Piscina 8:00–21:00 (adultos y niños).",
            "wifi": "Wi-Fi gratuito en todo el hotel.",
            "estacionamiento": "Parqueo gratuito para huéspedes.",
            "parking": "Parqueo gratuito para huéspedes.",
        }
        answer = next((faqs[k] for k in faqs if k in qn), "")
        if not answer:
            answer = _hotel_greeting_answer()
        suggestions = _default_suggestions(cid)

    # Persistencia de conversación básica
    sid = request.headers.get("X-Session-Id") or session.get("sid") or "web"
    try:
        _log_conversation_turn(
            session_id=sid,
            cid=cid,
            user_text=q,
            bot_text=answer,
        )
    except Exception:
        pass

    return jsonify({"ok": True, "a": answer, "suggestions": suggestions})


@sac_bp.get("/chat/history")
def chat_history():
    sid = request.args.get("session_id") or request.headers.get("X-Session-Id")
    if not sid:
        return jsonify(ok=False, message="session_id requerido"), 400

    conv = _get_active_conversation(sid, create_if_missing=False)
    if not conv:
        return jsonify(
            ok=True,
            conv_id=None,
            messages=[],
            last_msg_at=None,
            last_message_id=None,
            status=None,
            needs_agent=None,
            abierta=None,
            handoff_state=None,
            show_handoff_offer=False,
        )

    now = datetime.utcnow()
    if getattr(conv, "Last_Msg_At", None) and conv.Last_Msg_At < now - timedelta(hours=MAX_CONV_AGE_HOURS):
        return jsonify(
            ok=True,
            conv_id=None,
            messages=[],
            last_msg_at=None,
            last_message_id=None,
            status=None,
            needs_agent=None,
            abierta=None,
            handoff_state=None,
            show_handoff_offer=False,
        )

    since_id_raw = request.args.get("since_id")
    since_raw = request.args.get("since")

    q = _convmsg_query(conv.Id).order_by(SACConversationMsg.Id.asc())

    if since_id_raw:
        try:
            since_id = int(since_id_raw)
            if since_id > 0:
                q = q.filter(SACConversationMsg.Id > since_id)
        except Exception:
            pass
    elif since_raw:
        try:
            since = datetime.fromisoformat(since_raw)
            q = q.filter(SACConversationMsg.Creada_At > since)
        except ValueError:
            pass

    msgs = []
    last_ts = getattr(conv, "Last_Msg_At", None)
    last_message_id = 0

    for m in q:
        ts = getattr(m, "Creada_At", None)
        if ts is not None and (last_ts is None or ts > last_ts):
            last_ts = ts

        mid = int(getattr(m, "Id", 0) or 0)
        if mid > last_message_id:
            last_message_id = mid

        msgs.append(
            {
                "id": mid,
                "role": getattr(m, "Rol", None),
                "text": _convmsg_text_value(m),
                "sender_name": _get_message_sender_name(m),
                "created_at": ts.isoformat(sep=" ", timespec="microseconds") if ts else None,
            }
        )

    conv_status = (getattr(conv, "Status", None) or "bot").lower()
    handoff_state = None
    show_handoff_offer = False

    if conv_status == "handoff_offer":
        handoff_state = "OFFER"
        show_handoff_offer = True
    elif conv_status in ("agent_pending", "agent_active"):
        handoff_state = "PENDING"

    return jsonify(
        ok=True,
        conv_id=conv.Id,
        messages=msgs,
        last_msg_at=last_ts.isoformat(sep=" ", timespec="microseconds") if last_ts else None,
        last_message_id=last_message_id or None,
        status=getattr(conv, "Status", None),
        needs_agent=getattr(conv, "Needs_Agent", None),
        abierta=getattr(conv, "Abierta", None),
        handoff_state=handoff_state,
        show_handoff_offer=show_handoff_offer,
    )


@sac_bp.post("/chat/escalar")
def chatbot_escalar():
    payload = request.get_json(silent=True) or {}
    sid = (
        payload.get("session_id")
        or request.headers.get("X-Session-Id")
        or session.get("sid")
    )
    texto = (payload.get("texto") or "Cliente solicita ayuda").strip()

    now = datetime.utcnow()

    # 1) Intentar localizar la conversación abierta por Session_Id
    conv = None
    if sid:
        conv = (
            SACConversation.query.filter_by(Session_Id=sid, Abierta=True)
            .order_by(SACConversation.Id.asc())
            .first()
        )

    if conv:
        # Marcar que requiere agente
        if hasattr(conv, "Status"):
            conv.Status = "agent_pending"
        if hasattr(conv, "Needs_Agent"):
            conv.Needs_Agent = True
        if hasattr(conv, "Channel") and not getattr(conv, "Channel", None):
            conv.Channel = "web"
        conv.Actualizada_At = now
        if hasattr(conv, "Last_Msg_At"):
            conv.Last_Msg_At = now
        if hasattr(conv, "Last_Msg_Role"):
            conv.Last_Msg_Role = "bot"

        # Registrar un mensaje de sistema/bot indicando la derivación
        msg = SACConversationMsg(
            **{
                _convmsg_fk_attr(): conv.Id,
                "Rol": "bot",
                _convmsg_text_attr(): "La conversación ha sido derivada a un agente humano de recepción.",
                "Creada_At": datetime.utcnow(),
            }
        )
        if hasattr(msg, "Creada_At"):
            msg.Creada_At = now
        db.session.add(msg)

    # 2) Seguir encolando el correo como antes
    db.session.add(
        SACOutbox(
            Canal="email",
            Para=cfg("contacto_recepcion_email", "recepcion@hotel.test"),
            Asunto="[SAC] Solicitud de atención humana",
            Cuerpo=f"Conversación {sid or '-'} pide atención: {texto}",
            Ref_Entidad="Chat",
            Ref_Id=sid or "-",
            Programado_At=now,
        )
    )

    db.session.commit()
    return jsonify({"ok": True})



@sac_bp.post("/chat/handoff-decision")
def chat_handoff_decision():
    payload = request.get_json(silent=True) or {}
    decision = (payload.get("decision") or "").strip().lower()

    if decision not in ("accept", "reject"):
        return jsonify({"ok": False, "error": "Decisión inválida."}), 400

    sid = (
        payload.get("session_id")
        or request.headers.get("X-Session-Id")
        or session.get("sid")
    )
    if not sid:
        return jsonify({"ok": False, "error": "session_id requerido"}), 400

    cid, _ = _current_cliente_y_email()
    conv = _get_or_create_conversation(sid, cid)
    if not conv:
        return jsonify({"ok": False, "error": "No se pudo obtener la conversación"}), 500

    current_status = (getattr(conv, "Status", None) or "bot").lower()

    if current_status != "handoff_offer":
        return jsonify(
            {
                "ok": True,
                "status": current_status,
                "handoff_state": None,
                "message": "La conversación ya no tiene una oferta de transferencia pendiente.",
            }
        )

    try:
        if decision == "accept":
            bot_text = (
                "Perfecto, voy a transferir tu consulta a un agente humano de recepción. "
                "En cuanto la revisen, continuarán la conversación por este mismo chat."
            )
            _append_conversation_message(
                conv,
                "bot",
                bot_text,
                sender_name="Bot Hotel Villa Grace",
            )
            _touch_conversation(
                conv,
                status="agent_pending",
                needs_agent=True,
                last_role="bot",
            )
            handoff_state = "PENDING"
        else:
            bot_text = (
                "Entendido. Continuaré atendiéndote por este chat sin transferirte a un asesor."
            )
            _append_conversation_message(
                conv,
                "bot",
                bot_text,
                sender_name="Bot Hotel Villa Grace",
            )
            _touch_conversation(
                conv,
                status="bot",
                needs_agent=False,
                last_role="bot",
            )
            handoff_state = None

        db.session.commit()

        return jsonify(
            {
                "ok": True,
                "status": getattr(conv, "Status", None),
                "handoff_state": handoff_state,
                "message": bot_text,
            }
        )

    except Exception as e:
        current_app.logger.warning(f"[SAC] No se pudo registrar decisión de handoff: {e}")
        try:
            db.session.rollback()
        except Exception:
            pass
        return jsonify({"ok": False, "error": "No se pudo procesar la decisión."}), 500


# ====================== SOLICITUDES ======================
@sac_bp.get("/solicitudes/nueva")
def solicitud_nueva_html():
    reservas = _reservas_del_cliente(limit=None)
    return render_template(
        "sac-solicitud.html",
        reservas=reservas,
        checkin_ini=cfg("checkin_inicio", "12:00"),
        checkin_fin=cfg("checkin_fin", "00:00"),
        checkout=cfg("checkout_limite", "12:00"),
    )


@sac_bp.post("/solicitudes")
def registrar_solicitud():
    cid, _ = _current_cliente_y_email()
    if not cid:
        return jsonify({"ok": False, "error": "No autenticado"}), 401

    data = request.get_json(silent=True) or request.form or {}
    rid = data.get("Codigo_Reserva")
    try:
        rid = int(rid) if rid not in (None, "", "0") else None
    except Exception:
        rid = None

    s = SACSolicitud(
        Codigo_Reserva=rid,
        Codigo_Cliente=cid,
        Clave=(data.get("Clave") or "otra").strip(),
        Valor=((data.get("Valor") or "").strip() or None),
    )
    db.session.add(s)
    db.session.commit()

    # Confirmación por canal preferido
    try:
        from services.grr.notification_service import NotificationService

        NotificationService().route_and_queue(
            codigo_cliente=cid,
            asunto="Hemos recibido tu solicitud",
            cuerpo=(
                f"Gracias por escribirnos. Id de solicitud: {s.Id}. "
                f"Nos pondremos en contacto pronto."
            ),
            ref_tipo="SAC_Solicitud",
            ref_id=str(s.Id),
        )
        db.session.commit()
    except Exception as e:
        current_app.logger.warning(f"[SAC] No se pudo notificar solicitud {s.Id}: {e}")

    return jsonify({"ok": True, "id": s.Id})


# API JSON para panel de solicitudes
@sac_bp.get("/solicitudes", endpoint="solicitudes_list")
@role_required("Recepcionista", "Administrador")
def solicitudes_list():
    estado = (request.args.get("estado") or "").strip().upper()
    params: Dict[str, Any] = {}
    where = ""
    if estado:
        where = "WHERE s.Estado = :estado"
        params["estado"] = estado

    rows = (
        db.session.execute(
            text(
                f"""
        SELECT
          s.Id, s.Codigo_Reserva, s.Codigo_Cliente, s.Clave, s.Valor, s.Estado,
          DATE_FORMAT(s.Creada_At, '%Y-%m-%d %H:%i') AS Fecha_modificacion,
          COALESCE(c.Nombre,'') AS ClienteNombre,
          COALESCE(c.Correo,'') AS ClienteCorreo
        FROM SAC_Solicitud s
        LEFT JOIN Cliente c ON c.Codigo_Cliente = s.Codigo_Cliente
        {where}
        ORDER BY s.Creada_At DESC
        LIMIT 500
    """
            ),
            params,
        )
        .mappings()
        .all()
    )

    items = [
        {
            "Id": r["Id"],
            "Codigo_Reserva": r["Codigo_Reserva"],
            "Codigo_Cliente": r["Codigo_Cliente"],
            "Detalle": (r["Clave"] or "") + (": " + r["Valor"] if r["Valor"] else ""),
            "Estado": r["Estado"],
            "Fecha_modificacion": r["Fecha_modificacion"],
            "Cliente": (
                r["ClienteNombre"]
                or r["ClienteCorreo"]
                or str(r["Codigo_Cliente"] or "")
            )
            or "-",
        }
        for r in rows
    ]
    return jsonify({"ok": True, "items": items})


@sac_bp.put("/solicitudes/<int:sid>", endpoint="solicitudes_update")
@role_required("Recepcionista", "Administrador")
def solicitudes_update(sid: int):
    data = request.get_json(silent=True) or {}
    nuevo = (data.get("Estado") or "").strip().upper()
    if nuevo not in ("NUEVA", "EN_PROCESO", "ATENDIDA"):
        return jsonify({"ok": False, "error": "Estado inválido"}), 400
    db.session.execute(
        text("UPDATE SAC_Solicitud SET Estado=:e WHERE Id=:id"),
        {"e": nuevo, "id": sid},
    )
    db.session.commit()
    return jsonify({"ok": True})


@sac_bp.get("/panel/solicitudes")
@role_required("Recepcionista", "Administrador")
def panel_solicitudes():
    return render_template("sac-panel-solicitudes.html")


# ====================== CONVERSACIONES – PANEL SEGUIMIENTO NUEVO ======================


@sac_bp.get("/conversaciones")
@role_required("Recepcionista", "Administrador")
def conversaciones_html():
    """
    Panel de seguimiento de conversaciones para recepcionistas/agentes.
    Usa plantilla: sac-conversaciones.html
    """
    return render_template("sac-conversaciones.html")


def _status_label(status: str) -> str:
    mapping = {
        "bot": "Bot",
        "handoff_offer": "Oferta de transferencia",
        "agent_pending": "Pendiente agente",
        "agent_active": "En atención",
        "closed": "Atendida",
    }
    s = (status or "").lower()
    return mapping.get(s, (status or "Abierta").title())


def _status_variant(status: str) -> str:
    s = (status or "").lower()
    if s == "closed":
        return "success"
    if s in ("agent_pending", "handoff_offer"):
        return "danger"
    if s == "agent_active":
        return "primary"
    if s == "bot":
        return "light"
    return "secondary"


# Cache sencillo de columnas reales de SAC_Conversation para evitar errores 1054
_SAC_CONV_COLUMNS_CACHE = None


def _get_conv_columns() -> set:
    global _SAC_CONV_COLUMNS_CACHE
    if _SAC_CONV_COLUMNS_CACHE is None:
        try:
            inspector = inspect(db.engine)
            cols = inspector.get_columns("SAC_Conversation")
            _SAC_CONV_COLUMNS_CACHE = {c.get("name", "").lower() for c in cols}
        except Exception as e:
            _SAC_CONV_COLUMNS_CACHE = set()
            try:
                current_app.logger.warning(
                    f"[SAC] No se pudo inspeccionar SAC_Conversation: {e}"
                )
            except Exception:
                pass
    return _SAC_CONV_COLUMNS_CACHE


def _conv_has(col_name: str) -> bool:
    return col_name.lower() in _get_conv_columns()


@sac_bp.get("/conversations/list", endpoint="conversations_list")
@role_required("Recepcionista", "Administrador")
def conversations_list():
    """
    Listado resumido de conversaciones (lado izquierdo del panel).
    Compatible con esquemas antiguos/nuevos.
    """
    cols = _get_conv_columns()

    has_status = "status" in cols
    has_needs_agent = "needs_agent" in cols
    has_guest_name = "guest_name" in cols
    has_guest_email = "guest_email" in cols
    has_channel = "channel" in cols
    has_last_msg_at = "last_msg_at" in cols
    has_last_msg_role = "last_msg_role" in cols
    has_abierta = "abierta" in cols
    has_estado = "estado" in cols

    status_expr = "COALESCE(c.Status,'bot')" if has_status else "'bot'"
    needs_agent_expr = "COALESCE(c.Needs_Agent,0)" if has_needs_agent else "0"
    guest_name_expr = "c.Guest_Name" if has_guest_name else "NULL"
    guest_email_expr = "c.Guest_Email" if has_guest_email else "NULL"
    channel_expr = "c.Channel" if has_channel else "'Web'"
    last_msg_at_expr = "c.Last_Msg_At" if has_last_msg_at else "NULL"
    last_msg_role_expr = "c.Last_Msg_Role" if has_last_msg_role else "NULL"
    abierta_expr = "COALESCE(c.Abierta,1)" if has_abierta else "1"
    estado_expr = "COALESCE(c.Estado,'')" if has_estado else "''"

    order_by_expr = (
        "COALESCE(c.Needs_Agent,0) DESC, c.Actualizada_At DESC"
        if has_needs_agent
        else "c.Actualizada_At DESC"
    )

    text_col = _convmsg_text_attr()
    fk_col = _convmsg_fk_attr()

    sql = text(
        f"""
        SELECT
          c.Id,
          c.Session_Id,
          {status_expr}      AS Status,
          {needs_agent_expr} AS Needs_Agent,
          {guest_name_expr}  AS Guest_Name,
          {guest_email_expr} AS Guest_Email,
          {channel_expr}     AS Channel,
          c.Actualizada_At,
          {last_msg_at_expr}   AS Last_Msg_At,
          {last_msg_role_expr} AS Last_Msg_Role,
          {abierta_expr}       AS Abierta,
          {estado_expr}        AS Estado,
          (
            SELECT m.{text_col}
            FROM SAC_ConversationMsg m
            WHERE m.{fk_col} = c.Id
            ORDER BY m.Id DESC
            LIMIT 1
          ) AS Last_Snippet
        FROM SAC_Conversation c
        ORDER BY {order_by_expr}
        LIMIT 100
        """
    )

    rows = db.session.execute(sql).mappings().all()

    items: List[Dict[str, Any]] = []
    for r in rows:
        status = (r.get("Status") or "bot")
        status_norm = str(status).lower()
        needs_agent = bool(r.get("Needs_Agent"))
        abierta = bool(r.get("Abierta"))
        estado_legacy = str(r.get("Estado") or "").upper()

        is_closed = (not abierta) or status_norm == "closed" or estado_legacy == "CERRADA"
        requires_attention = (not is_closed) and (
            needs_agent or status_norm in ("agent_pending", "agent_active")
        )

        guest_label = r.get("Guest_Name") or r.get("Guest_Email") or "Visitante web"
        snippet = (r.get("Last_Snippet") or "").replace("\n", " ")
        if len(snippet) > 160:
            snippet = snippet[:157] + "..."

        effective_status = "closed" if is_closed else status_norm

        items.append(
            {
                "id": int(r["Id"]),
                "status": effective_status,
                "status_label": _status_label(effective_status),
                "status_variant": _status_variant(effective_status),
                "requires_attention": requires_attention,
                "guest_label": guest_label,
                "last_snippet": snippet,
            }
        )

    return jsonify({"ok": True, "items": items})


@sac_bp.get("/conversations/<int:conversation_id>", endpoint="conversation_detail")
@role_required("Recepcionista", "Administrador")
def conversation_detail(conversation_id: int):
    """
    Detalle completo de una conversación, con mensajes.
    Estructura esperada por sac-conversaciones.html.

    Ajuste: se considera tanto Status como Needs_Agent y Abierta para determinar
    si un agente puede responder o no.
    """
    conv = SACConversation.query.get(conversation_id)
    if not conv:
        return jsonify({"ok": False, "error": "Conversación no encontrada"}), 404

    msgs = (
        _convmsg_query(conversation_id)
        .order_by(SACConversationMsg.Id.asc())
        .all()
    )

    def fmt_ts(dt: Any) -> str:
        if dt is None:
            return ""
        if isinstance(dt, str):
            return dt[:16]
        try:
            return dt.strftime("%Y-%m-%d %H:%M")
        except Exception:
            return str(dt)

    # --- lógica de estado / atención ---
    status_raw = getattr(conv, "Status", None) or "bot"
    status = str(status_raw).lower()
    needs_agent = bool(getattr(conv, "Needs_Agent", False))
    abierta = bool(getattr(conv, "Abierta", True))
    estado_legacy = str(getattr(conv, "Estado", None) or "").upper()

    is_closed = (not abierta) or status == "closed" or estado_legacy == "CERRADA"

    requires_attention = (not is_closed) and (
        needs_agent or status in ("agent_pending", "agent_active")
    )

    can_agent_reply = (not is_closed) and (
        needs_agent or status in ("agent_pending", "agent_active")
    )

    conversation_out: Dict[str, Any] = {
        "id": conv.Id,
        "status": status_raw,
        "status_label": _status_label("closed" if is_closed else status_raw),
        "status_variant": _status_variant("closed" if is_closed else status_raw),
        "requires_attention": requires_attention,
        "guest_label": getattr(conv, "Guest_Name", None)
        or getattr(conv, "Guest_Email", None)
        or "Visitante web",
        "can_agent_reply": can_agent_reply,
        "is_closed": is_closed,
    }

    messages_out: List[Dict[str, Any]] = []
    for m in msgs:
        role_raw = m.Rol or "user"
        role = (role_raw or "").lower()
        content = _convmsg_text_value(m)
        created = getattr(m, "Creada_At", None)

        # Normalizamos roles a los usados por el front:
        #  - "user"   → user
        #  - "agent"  → agent
        #  - "bot"    → bot
        #  - otros    → system
        if role == "user":
            out_role = "user"
        elif role == "agent":
            out_role = "agent"
        elif role == "bot":
            out_role = "bot"
        else:
            out_role = "system"

        sender_name = _extract_sender_name(m)
        if not sender_name:
            if out_role == "agent":
                sender_name = "Asesor Hotel Villa Grace"
            elif out_role == "bot":
                sender_name = "Bot Hotel Villa Grace"
            elif out_role == "user":
                sender_name = "Cliente"
            else:
                sender_name = "Sistema"

        messages_out.append(
            {
                "id": m.Id,
                "role": out_role,
                "content": content,
                "sender_name": sender_name,
                "created_at": created,
                "created_at_human": fmt_ts(created),
            }
        )

    return jsonify(
        {"ok": True, "conversation": conversation_out, "messages": messages_out}
    )


@sac_bp.post(
    "/conversations/<int:conversation_id>/agent-reply",
    endpoint="conversation_agent_reply",
)
@role_required("Recepcionista", "Administrador")
def conversation_agent_reply(conversation_id: int):
    """
    Permite que un agente humano continúe la conversación desde el panel.
    """
    user_id = session.get("user_id")
    agent_name = (
        session.get("user_name")
        or session.get("user_email")
        or "Asesor Hotel Villa Grace"
    )

    data = request.get_json(silent=True) or {}
    text_msg = (data.get("message") or "").strip()
    if not text_msg:
        return jsonify({"ok": False, "error": "El mensaje no puede estar vacío."}), 400

    conv = SACConversation.query.get(conversation_id)
    if not conv:
        return jsonify({"ok": False, "error": "Conversación no encontrada"}), 404

    try:
        _append_conversation_message(
            conv,
            "agent",
            text_msg,
            sender_name=agent_name,
            created_by_user_id=user_id,
        )

        _touch_conversation(
            conv,
            status="agent_active",
            needs_agent=True,
            last_role="agent",
        )

        db.session.commit()

    except Exception as e:
        current_app.logger.warning(
            f"[SAC-CONV] No se pudo registrar respuesta de agente: {e}"
        )
        try:
            db.session.rollback()
        except Exception:
            pass
        return (
            jsonify(
                {"ok": False, "error": "Error al registrar la respuesta del agente."}
            ),
            500,
        )

    return jsonify({"ok": True})




@sac_bp.post(
    "/conversations/<int:conversation_id>/close",
    endpoint="conversation_close",
)
@role_required("Recepcionista", "Administrador")
def conversation_close(conversation_id: int):
    """
    Cierra la conversación únicamente cuando el asesor lo marca manualmente.
    """
    conv = SACConversation.query.get(conversation_id)
    if not conv:
        return jsonify({"ok": False, "error": "Conversación no encontrada"}), 404

    agent_name = (
        session.get("user_name")
        or session.get("user_email")
        or "Asesor Hotel Villa Grace"
    )

    now = datetime.utcnow()
    try:
        close_note = SACConversationMsg(
            **{
                _convmsg_fk_attr(): conv.Id,
                "Rol": "agent",
                _convmsg_text_attr(): f"Conversación marcada como atendida por {agent_name}.",
            }
        )
        if hasattr(close_note, "Creada_At"):
            setattr(close_note, "Creada_At", now)
        _set_msg_meta(close_note, {"sender_name": agent_name})
        db.session.add(close_note)

        if hasattr(conv, "Status"):
            conv.Status = "closed"
        if hasattr(conv, "Needs_Agent"):
            conv.Needs_Agent = False
        if hasattr(conv, "Estado") and getattr(conv, "Estado", None) is not None:
            conv.Estado = "CERRADA"
        if hasattr(conv, "Abierta"):
            conv.Abierta = 0
        if hasattr(conv, "Actualizada_At"):
            conv.Actualizada_At = now
        if hasattr(conv, "Last_Msg_At"):
            conv.Last_Msg_At = now
        if hasattr(conv, "Last_Msg_Role"):
            conv.Last_Msg_Role = "agent"

        db.session.commit()
    except Exception as e:
        current_app.logger.warning(f"[SAC-CONV] No se pudo cerrar conversación: {e}")
        try:
            db.session.rollback()
        except Exception:
            pass
        return jsonify({"ok": False, "error": "No se pudo cerrar la conversación."}), 500

    return jsonify({"ok": True})


# ====================== CONVERSACIONES – PANEL LEGACY ======================
@sac_bp.get("/conversaciones/data")
@role_required("Recepcionista", "Administrador")
def conversaciones_data():
    q = (request.args.get("q") or "").strip()
    days = int(request.args.get("days") or 30)
    since = datetime.utcnow() - timedelta(days=max(1, min(days, 365)))

    params: Dict[str, Any] = {"since": since}
    where = "WHERE c.Actualizada_At >= :since"
    if q:
        where += (
            " AND (COALESCE(cl.Nombre,'') LIKE :q "
            "OR COALESCE(cl.Correo,'') LIKE :q "
            "OR COALESCE(cl.Telefono,'') LIKE :q)"
        )
        params["q"] = f"%{q}%"

    rows = (
        db.session.execute(
            text(
                f"""
        SELECT
          c.Id AS Id,
          DATE_FORMAT(c.Actualizada_At,'%Y-%m-%d %H:%i') AS Fecha,
          COALESCE(cl.Nombre, CONCAT('Cliente ', c.Codigo_Cliente)) AS Cliente,
          'Web' AS Canal,
          SUBSTRING(
            (SELECT m.Texto
               FROM SAC_ConversationMsg m
              WHERE m.Conv_Id=c.Id
              ORDER BY m.Id DESC
              LIMIT 1),
            1, 120
          ) AS Asunto,
          CASE WHEN c.Abierta=1 THEN 'Abierta' ELSE 'Cerrada' END AS Estado
        FROM SAC_Conversation c
        LEFT JOIN Cliente cl ON cl.Codigo_Cliente=c.Codigo_Cliente
        {where}
        ORDER BY c.Actualizada_At DESC
        LIMIT 500
    """
            ),
            params,
        )
        .mappings()
        .all()
    )

    return jsonify({"ok": True, "items": [dict(r) for r in rows]})


@sac_bp.get("/conversaciones/<int:conv_id>/messages")
@role_required("Recepcionista", "Administrador")
def conversaciones_messages(conv_id: int):
    """
    Devuelve el hilo completo de mensajes de una conversación (legacy).
    """
    conv = SACConversation.query.get(conv_id)
    if not conv:
        return jsonify({"ok": False, "error": "Conversación no encontrada"}), 404

    msgs = (
        _convmsg_query(conv_id)
        .order_by(SACConversationMsg.Id.asc())
        .all()
    )

    items: List[Dict[str, Any]] = []
    for m in msgs:
        created = getattr(m, "Creada_At", None)
        items.append(
            {
                "id": m.Id,
                "role": m.Rol,
                "text": _convmsg_text_value(m),
                "created_at": (
                    created.isoformat() if hasattr(created, "isoformat") else None
                ),
            }
        )

    meta = {
        "id": conv.Id,
        "cliente_id": getattr(conv, "Codigo_Cliente", None),
        "session_id": getattr(conv, "Session_Id", None),
        "abierta": bool(getattr(conv, "Abierta", True)),
        "actualizada_at": (
            conv.Actualizada_At.isoformat()
            if getattr(conv, "Actualizada_At", None)
            else None
        ),
    }

    return jsonify({"ok": True, "items": items, "meta": meta})


#  INCIDENTES
@sac_bp.get("/incidentes", endpoint="incidentes_html")
@role_required("Recepcionista", "Administrador")
def incidentes_html():
    return render_template("sac-incidentes.html")


# @sac_bp.get("/incidentes/data")
# @role_required("Recepcionista", "Administrador")
# def incidentes_data():
#    rows = db.session.execute(
#        text(
#           """
#        SELECT i.Id, i.Codigo_Reserva, i.Codigo_Cliente, i.Tipo, i.Severidad,
#              i.Titulo, i.Detalle, i.Estado,
#              DATE_FORMAT(i.Creada_At,'%Y-%m-%d %H:%i') AS Fecha
#       FROM SAC_Incident i
#       ORDER BY i.Creada_At DESC
#       LIMIT 300
#   """
#       )
#   ).mappings().all()
#   return jsonify({"ok": True, "items": [dict(r) for r in rows]})


@sac_bp.get("/incidentes/data", endpoint="incidentes_data")
@role_required("Recepcionista", "Administrador")
def incidentes_data():
    tipo = (request.args.get("tipo") or "").strip().upper()
    estado = (request.args.get("estado") or "").strip().upper()

    try:
        page = int(request.args.get("page") or 1)
    except Exception:
        page = 1
    if page < 1:
        page = 1

    try:
        page_size = int(request.args.get("page_size") or 20)
    except Exception:
        page_size = 20

    if page_size < 5:
        page_size = 5
    if page_size > 100:
        page_size = 100

    valid_tipo = {"INCIDENTE", "COMENTARIO"}
    valid_estado = {"ABIERTA", "EN_PROCESO", "CERRADA"}

    where = []
    params = {}

    if tipo in valid_tipo:
        where.append("i.Tipo = :tipo")
        params["tipo"] = tipo

    if estado in valid_estado:
        where.append("i.Estado = :estado")
        params["estado"] = estado

    where_sql = ("WHERE " + " AND ".join(where)) if where else ""

    offset = (page - 1) * page_size

    rows = (
        db.session.execute(
            text(
                f"""
        SELECT i.Id, i.Codigo_Reserva, i.Codigo_Cliente,
               i.Reportado_Por, i.Asignado_A,
               i.Tipo, i.Severidad, i.Titulo, i.Detalle, i.Estado,
               DATE_FORMAT(i.Creado_At,'%Y-%m-%d %H:%i') AS Fecha
        FROM sac_incident i
        {where_sql}
        ORDER BY i.Creado_At DESC
        LIMIT :lim OFFSET :off
        """
            ),
            {**params, "lim": page_size + 1, "off": offset},
        )
        .mappings()
        .all()
    )

    items = [dict(r) for r in rows]
    has_next = len(items) > page_size
    if has_next:
        items = items[:page_size]

    return jsonify({"ok": True, "items": items, "has_next": has_next})


from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

@sac_bp.post("/incidentes", endpoint="incidentes_new")
def incidentes_new():
    """
    Crea un incidente SAC.
    Fix: valida Codigo_Reserva contra la tabla reserva/Reserva para evitar FK 1452.
    Devuelve SIEMPRE JSON manejable por el front.
    """
    try:
        data = request.get_json(force=True) or {}
    except Exception:
        return jsonify(ok=False, error="Payload inválido."), 200

    # --------- Lectura de campos ----------
    rid_raw = (data.get("Codigo_Reserva") or "").strip()
    cid_raw = (data.get("Codigo_Cliente") or "").strip()

    reportado = (data.get("Reportado_Por") or "").strip()
    asignado = (data.get("Asignado_A") or "").strip()
    tipo = (data.get("Tipo") or "INCIDENTE").strip().upper()
    sev = (data.get("Severidad") or "MEDIA").strip().upper()
    titulo = (data.get("Titulo") or "").strip()
    detalle = (data.get("Detalle") or "").strip()
    estado = (data.get("Estado") or "ABIERTA").strip().upper()

    # --------- Normalización numérica ----------
    rid = None
    if rid_raw:
        try:
            rid = int(rid_raw)
        except ValueError:
            return jsonify(ok=False, error="Código de reserva inválido (debe ser numérico)."), 200

    cid = None
    if cid_raw:
        try:
            cid = int(cid_raw)
        except ValueError:
            return jsonify(ok=False, error="Código de cliente inválido (debe ser numérico)."), 200

    # --------- Validaciones de catálogo ----------
    valid_tipo = {"INCIDENTE", "QUEJA", "CONSULTA"}
    valid_sev = {"BAJA", "MEDIA", "ALTA"}
    valid_estado = {"ABIERTA", "EN_PROCESO", "RESUELTA", "CERRADA"}

    if tipo not in valid_tipo:
        return jsonify(ok=False, error=f"Tipo inválido. Use: {', '.join(sorted(valid_tipo))}."), 200
    if sev not in valid_sev:
        return jsonify(ok=False, error=f"Severidad inválida. Use: {', '.join(sorted(valid_sev))}."), 200
    if estado not in valid_estado:
        return jsonify(ok=False, error=f"Estado inválido. Use: {', '.join(sorted(valid_estado))}."), 200

    if not titulo:
        return jsonify(ok=False, error="El título es requerido."), 200
    if not detalle:
        return jsonify(ok=False, error="El detalle es requerido."), 200

    # --------- Validación FK: reserva debe existir si se provee ----------
    if rid is not None:
        try:
            with db.engine.begin() as conn:
                # detectar nombre real por portabilidad
                res_tbl = conn.execute(text("""
                    SELECT table_name
                    FROM information_schema.tables
                    WHERE table_schema = DATABASE()
                      AND LOWER(table_name) = 'reserva'
                    LIMIT 1
                """)).scalar()

                if not res_tbl:
                    # si no existe tabla reserva, no asociar (evita 500)
                    return jsonify(ok=False, error="No existe tabla 'reserva' en este schema. No se puede asociar el incidente a una reserva."), 200

                exists = conn.execute(
                    text(f"SELECT 1 FROM `{res_tbl}` WHERE Codigo_Reserva = :rid LIMIT 1"),
                    {"rid": rid}
                ).scalar()

                if not exists:
                    return jsonify(
                        ok=False,
                        error=f"El código de reserva {rid} no existe. Dejá el campo vacío si el incidente no está asociado a una reserva."
                    ), 200
        except Exception:
            current_app.logger.exception("Error validando Codigo_Reserva contra reserva")
            return jsonify(ok=False, error="No se pudo validar el código de reserva. Intenta nuevamente."), 200

    # --------- Insert ----------
    try:
        inc = SACIncident(
            Codigo_Reserva=rid,
            Codigo_Cliente=cid,
            Reportado_Por=reportado or None,
            Asignado_A=asignado or None,
            Tipo=tipo,
            Severidad=sev,
            Titulo=titulo,
            Detalle=detalle,
            Estado=estado,
        )
        db.session.add(inc)
        db.session.commit()
        return jsonify(ok=True, id=getattr(inc, "Id", None)), 200

    except IntegrityError as e:
        db.session.rollback()
        # Mensaje específico si vuelve a caer por FK
        msg = str(getattr(e, "orig", e))
        if "FK_SAC_Incident_Reserva" in msg or "foreign key constraint fails" in msg.lower():
            return jsonify(ok=False, error="La reserva indicada no existe o no es válida. Dejá el código vacío o usa uno existente."), 200
        return jsonify(ok=False, error="No se pudo guardar el incidente por una restricción de base de datos."), 200

    except Exception:
        db.session.rollback()
        current_app.logger.exception("Error guardando incidente SAC")
        return jsonify(ok=False, error="No se pudo guardar. Intenta nuevamente."), 200



@sac_bp.put("/incidentes/<int:iid>", endpoint="incidentes_update")
@role_required("Recepcionista", "Administrador")
def incidentes_update(iid: int):
    data = request.get_json(silent=True) or {}

    valid_estado = {"ABIERTA", "EN_PROCESO", "CERRADA"}
    valid_asignado = {
        "Recepcionista",
        "Mantenimiento",
        "Limpieza",
        "Administración",
        "Otro",
    }

    sets = []
    params = {"id": iid}

    # Solo actualizar si la llave viene en el JSON
    if "Estado" in data:
        nuevo_estado = (data.get("Estado") or "").strip().upper()
        if nuevo_estado and nuevo_estado not in valid_estado:
            return jsonify({"ok": False, "error": "Estado inválido"}), 400
        sets.append("Estado = :estado")
        params["estado"] = (
            nuevo_estado  # permite '' si quisieras, pero normalmente vendrá válido
        )

    if "Asignado_A" in data:
        asignado = (data.get("Asignado_A") or "").strip()
        if asignado and asignado not in valid_asignado:
            return jsonify({"ok": False, "error": "Asignado inválido"}), 400
        sets.append("Asignado_A = :asignado")
        params["asignado"] = asignado or None

    if "Titulo" in data:
        titulo = (data.get("Titulo") or "").strip()
        if not titulo:
            return jsonify({"ok": False, "error": "Título requerido"}), 400
        sets.append("Titulo = :titulo")
        params["titulo"] = titulo

    if not sets:
        return jsonify({"ok": True})

    db.session.execute(
        text(f"UPDATE sac_incident SET {', '.join(sets)} WHERE Id = :id"),
        params,
    )
    db.session.commit()
    return jsonify({"ok": True})


@sac_bp.get("/incidentes/<int:inc_id>/comentarios", endpoint="incidentes_comments_list")
@role_required("Recepcionista", "Administrador")
def incidentes_comments_list(inc_id: int):
    rows = (
        db.session.execute(
            text(
                """
            SELECT c.Id, c.Incident_Id, c.Autor, c.Comentario,
                   DATE_FORMAT(c.Creada_At,'%Y-%m-%d %H:%i') AS Fecha
            FROM sac_incident_comment c
            WHERE c.Incident_Id = :id
            ORDER BY c.Creada_At DESC
            LIMIT 200
        """
            ),
            {"id": inc_id},
        )
        .mappings()
        .all()
    )

    return jsonify({"ok": True, "items": [dict(r) for r in rows]})


@sac_bp.post("/incidentes/<int:inc_id>/comentarios", endpoint="incidentes_comments_new")
@role_required("Recepcionista", "Administrador")
def incidentes_comments_new(inc_id: int):
    data = request.get_json(silent=True) or request.form or {}
    comentario = (data.get("Comentario") or "").strip()

    if not comentario:
        return jsonify({"ok": False, "error": "Comentario requerido."}), 400

    autor = (
        session.get("user_name") or session.get("user_email") or ""
    ).strip() or None

    db.session.execute(
        text(
            """
            INSERT INTO sac_incident_comment (Incident_Id, Autor, Comentario)
            VALUES (:id, :autor, :comentario)
        """
        ),
        {"id": inc_id, "autor": autor, "comentario": comentario},
    )
    db.session.commit()
    return jsonify({"ok": True})


# ====================== INDICADORES ======================
@sac_bp.get("/indicadores", endpoint="indicadores_html")
@role_required("Administrador")
def indicadores_html():
    # Se renderiza vacío; el front hace fetch a /indicadores/kpis y /indicadores/flujo
    return render_template("sac-indicadores.html")


@sac_bp.get("/indicadores/kpis")
@role_required("Administrador")
def indicadores_kpis():
    k = (
        db.session.execute(
            text(
                """
        SELECT 
          COUNT(*) AS tickets,
          SUM(CASE WHEN Estado='ATENDIDA' THEN 1 ELSE 0 END) AS atendidas
        FROM SAC_Solicitud
    """
            )
        )
        .mappings()
        .first()
        or {}
    )
    nps = (
        db.session.execute(text("SELECT AVG(NPS) AS nps FROM SAC_Feedback"))
        .mappings()
        .first()
        or {}
    )
    return jsonify(
        {
            "ok": True,
            "tickets": int(k.get("tickets") or 0),
            "atendidas": int(k.get("atendidas") or 0),
            "nps": round(nps.get("nps") or 0, 2),
        }
    )


@sac_bp.get("/indicadores/flujo")
@role_required("Administrador")
def indicadores_flujo():
    rows = (
        db.session.execute(
            text(
                """
        SELECT
          DATE_FORMAT(Programado_At,'%Y-%m-%d %H:%i') AS Fecha,
          Canal, Estado,
          COALESCE(Ref_Entidad,'-') AS Ref_Entidad,
          COALESCE(Ref_Id,'-')      AS Ref_Id
        FROM SAC_Outbox
        ORDER BY Programado_At DESC
        LIMIT 200
    """
            )
        )
        .mappings()
        .all()
    )
    return jsonify({"ok": True, "items": [dict(r) for r in rows]})


# ====================== KB (documentos + Q/A) ======================
ALLOWED = {".txt", ".md", ".pdf", ".docx", ".html", ".htm"}





@sac_bp.get("/kb/debug_search")
@role_required("Administrador")
def sac_kb_debug_search():
    """
    Endpoint de depuración para verificar que el índice RAG
    está leyendo correctamente los documentos de la KB.

    Uso:
      GET /sac/kb/debug_search?q=horario%20de%20la%20piscina
    """
    q = (request.args.get("q") or "").strip()
    if not q:
        return jsonify({"ok": False, "error": "Parámetro q requerido (q)"}), 400

    # Reutilizamos directamente las funciones de RAG, sin duplicar lógica
    hits = search(q, topk=5)
    ans = answer_from_chunks(q, hits)

    return jsonify(
        {
            "ok": True,
            "query": q,
            "hits": hits,
            "rag_answer": ans,
        }
    )


@sac_bp.get("/kb")
@role_required("Administrador")
def sac_kb_list():
    """
    Vista legacy de KB (sac-kb.html), con lista de documentos y preguntas no respondidas.
    """
    with db.engine.begin() as conn:
        docs = (
            conn.execute(
                text(
                    """
          SELECT Id, Titulo, FileName, Bytes, SubidoEn, Activo, Chunks
          FROM SAC_KB_Doc ORDER BY SubidoEn DESC
        """
                )
            )
            .mappings()
            .all()
        )
        gaps = (
            conn.execute(
                text(
                    """
              SELECT Id, Question, Status, Created_At, Reviewed_At, Resolution_Note
              FROM SAC_KB_Unanswered
              WHERE Status IN ('Pendiente','Clasificada')
              ORDER BY Created_At DESC
            """
                )
            )
            .mappings()
            .all()
        )
    return render_template("sac-kb.html", docs=docs, gaps=gaps)


# ---- Nueva consola ligera de KB (UI con tabla + upload) ----
@sac_bp.get("/kb/console")
@role_required("Administrador")
def sac_kb_console():
    """
    Consola simplificada de administración de la base de conocimiento SAC.
    Usa sac/kb_console.html y los endpoints:
      - /sac/kb/docs  (JSON)
      - /sac/kb/upload (POST, ya implementado)
    """
    return render_template("sac/kb_console.html")


import json
import hashlib
from datetime import datetime
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

def _kb_extract_text(file_path: str, ext: str) -> str:
    """
    Extrae texto para poblar SAC_KB_Doc.Content (NOT NULL).
    Implementación básica (TXT/MD/HTML/PDF). Si no se logra, devuelve placeholder.
    """
    ext = (ext or "").lower()

    try:
        if ext in {".txt", ".md", ".csv", ".log", ".json", ".xml", ".html", ".htm"}:
            raw = pathlib.Path(file_path).read_bytes()
            return raw.decode("utf-8", errors="ignore").strip()

        if ext == ".pdf":
            try:
                from PyPDF2 import PdfReader
                reader = PdfReader(file_path)
                parts = []
                for p in reader.pages:
                    parts.append((p.extract_text() or "").strip())
                txt = "\n\n".join([t for t in parts if t])
                return txt.strip()
            except Exception:
                # si PyPDF2 no está o el PDF no permite extracción
                return ""

        # (Opcional) DOCX si lo ocupás:
        if ext == ".docx":
            try:
                import docx
                d = docx.Document(file_path)
                txt = "\n".join([p.text for p in d.paragraphs if p.text])
                return txt.strip()
            except Exception:
                return ""

    except Exception:
        return ""

    return ""


@sac_bp.post("/kb/upload", endpoint="sac_kb_upload")
@role_required("Administrador")
def sac_kb_upload():
    try:
        f = request.files.get("file")
        title = (request.form.get("title") or "").strip()

        if not f:
            return jsonify(ok=False, error="No se recibió archivo."), 400

        original_name = (f.filename or "documento").strip()
        if not title:
            title = original_name

        os.makedirs(KB_DIR, exist_ok=True)

        ext = os.path.splitext(original_name)[1].lower()

        blob = f.read()
        size_bytes = len(blob)
        sha = hashlib.sha256(blob).hexdigest()

        safe_orig = secure_filename(original_name)
        stored_name = f"{sha[:12]}_{safe_orig}"
        dest = os.path.join(KB_DIR, stored_name)

        with open(dest, "wb") as out:
            out.write(blob)

        content = _kb_extract_text(dest, ext)
        if not content:
            content = f"[Contenido no extraíble automáticamente] Archivo: {original_name} ({ext or 'sin extensión'})"

        meta = {
            "original_name": original_name,
            "stored_name": stored_name,
            "ext": ext,
            "size_bytes": size_bytes,
            "sha256": sha,
            "stored_path": dest,
            "uploaded_by": session.get("user_id"),
        }

        with db.engine.begin() as conn:
            existing = conn.execute(
                text("""
                    SELECT Id
                      FROM SAC_KB_Doc
                     WHERE Content_Hash = :hash
                     LIMIT 1
                """),
                {"hash": sha},
            ).scalar()

            if existing:
                return jsonify(ok=False, error="Este documento ya existe en la base de conocimiento."), 200

            res = conn.execute(
                text("""
                    INSERT INTO SAC_KB_Doc
                        (Source, Title, Content, Lang, Metadata_JSON, Content_Hash, Status)
                    VALUES
                        ('FILE', :title, :content, :lang, :meta, :hash, 'ACTIVE')
                """),
                {
                    "title": title,
                    "content": content,
                    "lang": "es",
                    "meta": json.dumps(meta, ensure_ascii=False),
                    "hash": sha,
                },
            )

            doc_id = getattr(res, "lastrowid", None)

            rows = conn.execute(
                text("""
                    SELECT
                      Id AS Id,
                      JSON_UNQUOTE(JSON_EXTRACT(Metadata_JSON, '$.stored_name')) AS FileName
                    FROM SAC_KB_Doc
                    WHERE Status = 'ACTIVE'
                    ORDER BY Created_At DESC
                """)
            ).mappings().all()

        try:
            rebuild_index(db, rows)
        except Exception:
            current_app.logger.exception("KB: fallo en rebuild_index")

        return jsonify(
            ok=True,
            id=doc_id,
            message="Documento subido e indexado correctamente."
        ), 200

    except Exception:
        current_app.logger.exception("KB upload error")
        return jsonify(ok=False, error="Error al subir el documento a la base de conocimiento."), 500


@sac_bp.get("/kb/docs", endpoint="sac_kb_docs")
@role_required("Administrador")
def sac_kb_docs():
    """
    Devuelve docs para /sac/chat/admin.
    El frontend espera `items`, así que devolvemos `items` y también `docs`
    para mantener compatibilidad.
    """
    try:
        with db.engine.begin() as conn:
            rows = conn.execute(
                text("""
                    SELECT
                      Id AS id,
                      Title AS title,
                      JSON_UNQUOTE(JSON_EXTRACT(Metadata_JSON, '$.original_name')) AS filename,
                      CAST(JSON_UNQUOTE(JSON_EXTRACT(Metadata_JSON, '$.size_bytes')) AS UNSIGNED) AS size_bytes,
                      Created_At AS created_at
                    FROM SAC_KB_Doc
                    WHERE Status = 'ACTIVE'
                    ORDER BY Created_At DESC
                    LIMIT 200
                """)
            ).mappings().all()

        items = []
        for r in rows:
            created = r.get("created_at")
            items.append(
                {
                    "id": int(r.get("id")) if r.get("id") is not None else None,
                    "title": r.get("title") or "",
                    "filename": r.get("filename") or "",
                    "size_bytes": int(r.get("size_bytes") or 0),
                    "created_at": created.isoformat() if hasattr(created, "isoformat") else (str(created) if created else None),
                }
            )

        return jsonify(ok=True, items=items, docs=items), 200

    except Exception:
        current_app.logger.exception("KB docs error")
        return jsonify(ok=False, items=[], docs=[], error="No se pudo cargar la lista de documentos."), 500


@sac_bp.delete("/kb/docs/<int:doc_id>", endpoint="sac_kb_delete")
@role_required("Administrador")
def sac_kb_delete(doc_id: int):
    try:
        meta = {}
        stored_name = None

        with db.engine.begin() as conn:
            row = conn.execute(
                text("""
                    SELECT Id, Metadata_JSON
                      FROM SAC_KB_Doc
                     WHERE Id = :id
                     LIMIT 1
                """),
                {"id": doc_id},
            ).mappings().first()

            if not row:
                return jsonify(ok=False, error="Documento no encontrado."), 404

            try:
                meta = json.loads(row.get("Metadata_JSON") or "{}")
            except Exception:
                meta = {}

            stored_name = (meta.get("stored_name") or "").strip() or None

            conn.execute(
                text("DELETE FROM SAC_KB_Doc WHERE Id = :id"),
                {"id": doc_id},
            )

            remaining = conn.execute(
                text("""
                    SELECT
                      Id AS Id,
                      JSON_UNQUOTE(JSON_EXTRACT(Metadata_JSON, '$.stored_name')) AS FileName
                    FROM SAC_KB_Doc
                    WHERE Status = 'ACTIVE'
                    ORDER BY Created_At DESC
                """)
            ).mappings().all()

        if stored_name:
            try:
                path = pathlib.Path(KB_DIR) / stored_name
                if path.exists():
                    path.unlink()
            except Exception:
                current_app.logger.warning("No se pudo eliminar el archivo físico de KB.", exc_info=True)

        try:
            rebuild_index(db, remaining)
        except Exception:
            current_app.logger.exception("KB: fallo al reconstruir índice tras eliminar documento")

        return jsonify(ok=True), 200

    except Exception:
        current_app.logger.exception("KB delete error")
        return jsonify(ok=False, error="No se pudo eliminar el documento."), 500

@sac_bp.post("/kb/teach")
@role_required("Administrador")
def sac_kb_teach():
    """
    Alta/actualización manual de preguntas y respuestas en la KB.
    Sigue disponible, pero ya no es obligatoria para que el bot funcione.
    """
    data = request.get_json(silent=True) or request.form or {}
    q = (data.get("pregunta") or data.get("question") or "").strip()
    a = (data.get("respuesta") or data.get("answer") or "").strip()

    if not q or not a:
        return jsonify(ok=False, error="Pregunta y respuesta son requeridas."), 400

    try:
        with db.engine.begin() as conn:
            existing = conn.execute(
                text("""
                    SELECT Id
                      FROM SAC_KB_QA
                     WHERE LOWER(Question) = LOWER(:q)
                     LIMIT 1
                """),
                {"q": q},
            ).mappings().first()

            if existing:
                qa_id = int(existing["Id"])
                conn.execute(
                    text("""
                        UPDATE SAC_KB_QA
                           SET Answer = :a,
                               Status = 'ACTIVE',
                               Updated_At = NOW()
                         WHERE Id = :id
                    """),
                    {"a": a, "id": qa_id},
                )
            else:
                res = conn.execute(
                    text("""
                        INSERT INTO SAC_KB_QA (Question, Answer, Status)
                        VALUES (:q, :a, 'ACTIVE')
                    """),
                    {"q": q, "a": a},
                )
                qa_id = int(getattr(res, "lastrowid", 0) or 0)

            try:
                conn.execute(
                    text("""
                        UPDATE SAC_KB_Unanswered
                           SET Status = 'Resuelta',
                               Reviewed_At = NOW(),
                               Resolution_Note = 'Respondida desde /sac/kb/teach'
                         WHERE Status <> 'Resuelta'
                           AND Question LIKE :likeq
                    """),
                    {
                        "likeq": f"%{q[:80]}%",
                    },
                )
            except Exception:
                pass

        return jsonify(ok=True, id=qa_id), 200

    except Exception:
        current_app.logger.exception("KB teach error")
        return jsonify(ok=False, error="No se pudo guardar la pregunta/respuesta."), 500


# ====================== Panel Administración Chatbot ======================
@sac_bp.get("/chat/admin", endpoint="sac_chat_admin")
@role_required("Administrador")
def sac_chat_admin():
    prompt_current = cfg("chatbot_system_prompt", "")

    popular_questions: List[Dict[str, Any]] = []

    try:
        text_col = getattr(SACConversationMsg, _convmsg_text_attr())

        rows = (
            db.session.query(text_col.label("pregunta"))
            .filter(SACConversationMsg.Rol == "user")
            .filter(text_col.isnot(None))
            .filter(text_col != "")
            .order_by(SACConversationMsg.Id.desc())
            .limit(2000)
            .all()
        )

        counter = Counter()
        for row in rows:
            txt = (getattr(row, "pregunta", None) or "").strip()
            qn = _norm(txt)
            if not qn:
                continue
            if _is_greeting(qn) or _is_smalltalk(qn) or _is_help_like(qn):
                continue
            if len(qn) < 4:
                continue
            counter[qn] += 1

        for question, count in counter.most_common(30):
            popular_questions.append({"text": question, "count": int(count)})

    except Exception as e:
        current_app.logger.warning(f"[SAC] No se pudieron calcular preguntas populares: {e}")

    kb_stats = {"docs": 0, "unanswered": 0}
    with db.engine.begin() as conn:
        kb_stats["docs"] = int(
            conn.execute(
                text("SELECT COUNT(*) FROM SAC_KB_Doc WHERE Status='ACTIVE'")
            ).scalar() or 0
        )

        try:
            kb_stats["unanswered"] = int(
                conn.execute(
                    text("""
                        SELECT COUNT(*)
                          FROM SAC_KB_Unanswered
                         WHERE Status IN ('Pendiente','Clasificada')
                    """)
                ).scalar() or 0
            )
        except Exception:
            kb_stats["unanswered"] = 0

    return render_template(
        "sac/chat_admin.html",
        prompt_current=prompt_current,
        popular_questions=popular_questions,
        kb_stats=kb_stats,
    )


def _build_effective_system_prompt(custom_instructions: str) -> str:
    """
    Construye el prompt efectivo que ve la IA: base + instrucciones personalizadas.
    """
    custom_instructions = (custom_instructions or "").strip()
    if not custom_instructions:
        return BASE_SYSTEM_PROMPT
    return (
        BASE_SYSTEM_PROMPT
        + "\nInstrucciones adicionales específicas del Hotel Villa Grace:\n"
        + custom_instructions
        + "\n"
    )


@sac_bp.get("/chat/config")
@role_required("Administrador")
def sac_chat_config_get():
    """
    Devuelve:
      - custom_prompt: texto guardado en SAC_Config.
      - effective_prompt: prompt base + custom.
      - ollama_enabled / n8n_webhook_configured: flags para badges del panel.
    """
    custom_prompt = cfg("chatbot_system_prompt", "")
    effective_prompt = _build_effective_system_prompt(custom_prompt)

    ollama_enabled = os.getenv("SAC_ENABLE_OLLAMA", "0") in ("1", "true", "True")
    n8n_configured = bool(os.getenv("SAC_N8N_WEBHOOK", "").strip())

    return jsonify(
        {
            "ok": True,
            "custom_prompt": custom_prompt,
            "effective_prompt": effective_prompt,
            "ollama_enabled": ollama_enabled,
            "n8n_webhook_configured": n8n_configured,
        }
    )


@sac_bp.post("/chat/config")
@role_required("Administrador")
def sac_chat_config_save():
    """
    Guarda o limpia el prompt personalizado del chatbot.
    JSON esperado:
      - { "clear": true }   → elimina/custom vacío → se usa solo BASE_SYSTEM_PROMPT.
      - { "prompt": "texto" } → guarda texto como custom_prompt.
    """
    data = request.get_json(silent=True) or {}
    clear = bool(data.get("clear"))
    new_prompt = (data.get("prompt") or "").strip()

    if clear:
        row = SACConfig.query.get("chatbot_system_prompt")
        if row:
            row.Valor = ""
            db.session.add(row)
            db.session.commit()
        return jsonify({"ok": True, "cleared": True})

    # Guardar prompt no vacío
    if not new_prompt:
        return jsonify({"ok": False, "error": "Prompt vacío"}), 400

    row = SACConfig.query.get("chatbot_system_prompt")
    if not row:
        row = SACConfig(Clave="chatbot_system_prompt", Valor=new_prompt)
    else:
        row.Valor = new_prompt
    db.session.add(row)
    db.session.commit()
    return jsonify({"ok": True, "cleared": False})


# --- Endpoint previo de guardado de prompt (compatibilidad) ---
@sac_bp.post("/chat/admin/prompt")
@role_required("Administrador")
def sac_chat_admin_save_prompt():
    """
    Compatibilidad con versiones anteriores que hacían POST aquí.
    Redirige la lógica a /sac/chat/config.
    """
    data = request.get_json(silent=True) or request.form or {}
    new_prompt = (data.get("prompt") or "").strip()
    if not new_prompt:
        return jsonify({"ok": False, "error": "Prompt vacío"}), 400

    row = SACConfig.query.get("chatbot_system_prompt")
    if not row:
        row = SACConfig(Clave="chatbot_system_prompt", Valor=new_prompt)
    else:
        row.Valor = new_prompt
    db.session.add(row)
    db.session.commit()
    return jsonify({"ok": True})


# ---- Estadísticas de preguntas para chat_admin.html ----
@sac_bp.get("/chat/stats")
@role_required("Administrador")
def sac_chat_stats():
    """
    Devuelve tendencias reales del chatbot, excluyendo saludos y small-talk.
    Compatible con Texto / Msg_Text.
    """
    try:
        max_rows = int(request.args.get("max_rows") or 2000)
    except Exception:
        max_rows = 2000

    try:
        limit = int(request.args.get("limit") or 30)
    except Exception:
        limit = 30

    max_rows = max(100, min(max_rows, 10000))
    limit = max(5, min(limit, 100))

    try:
        text_col = getattr(SACConversationMsg, _convmsg_text_attr())

        rows = (
            db.session.query(text_col.label("texto"))
            .filter(SACConversationMsg.Rol == "user")
            .filter(text_col.isnot(None))
            .filter(text_col != "")
            .order_by(SACConversationMsg.Id.desc())
            .limit(max_rows)
            .all()
        )

        counter = Counter()
        total_counted = 0

        for row in rows:
            txt = (getattr(row, "texto", None) or "").strip()
            qn = _norm(txt)
            if not qn:
                continue
            if _is_greeting(qn) or _is_smalltalk(qn) or _is_help_like(qn):
                continue
            if len(qn) < 4:
                continue

            total_counted += 1
            counter[qn] += 1

        items = [
            {"question": question, "count": count}
            for question, count in counter.most_common(limit)
        ]

        return jsonify({"ok": True, "items": items, "total_counted": total_counted})

    except Exception as e:
        current_app.logger.warning(f"[SAC] Error calculando estadísticas del chat: {e}")
        return jsonify(
            {"ok": False, "items": [], "total_counted": 0, "error": "No se pudieron calcular las estadísticas."}
        ), 500


# ====================== Conversación + IA (n8n / Ollama) ======================
def _get_conversation_history(session_id: str, limit: int = 6) -> List[Dict[str, str]]:
    """
    Recupera las últimas N interacciones de SAC_ConversationMsg
    para alimentar al modelo de IA con contexto.
    """
    if not session_id:
        return []

    # Usar la misma lógica de conversación activa (24h, abierta, etc.)
    conv = _get_active_conversation(session_id, create_if_missing=False)
    if not conv:
        return []

    msgs = (
        _convmsg_query(conv.Id)
        .order_by(SACConversationMsg.Id.asc())
        .all()
    )

    history: List[Dict[str, str]] = []
    for m in msgs[-limit:]:
        rol = (m.Rol or "").lower()
        if rol == "user":
            role = "user"
        else:
            # 'bot', 'agent', etc. → assistant
            role = "assistant"
        history.append({"role": role, "content": _convmsg_text_value(m)})
    return history


def _get_or_create_conversation(
    session_id: str, cid: Optional[int]
) -> Optional[SACConversation]:
    """
    Envuelve _get_active_conversation para que toda la lógica de
    conversación (incluyendo límite de 24h) sea consistente.
    """
    if not session_id:
        return None

    # Reutiliza la lógica de 24 horas y estado
    conv = _get_active_conversation(session_id, create_if_missing=True)
    if not conv:
        return None

    # Asegurar que el Código_Cliente quede asociado si lo conocemos
    if cid and getattr(conv, "Codigo_Cliente", None) != cid:
        conv.Codigo_Cliente = cid
        try:
            db.session.commit()
        except Exception:
            db.session.rollback()

    return conv


def _msg_meta_to_dict(raw: Any) -> Dict[str, Any]:
    if raw is None:
        return {}
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str):
        try:
            data = json.loads(raw)
            return data if isinstance(data, dict) else {}
        except Exception:
            return {}
    return {}


def _message_default_sender_name(role: str) -> str:
    r = (role or "").lower()
    if r == "agent":
        return "Asesor Hotel Villa Grace"
    if r == "bot":
        return "Bot Hotel Villa Grace"
    if r == "user":
        return "Cliente"
    return "Sistema"


def _set_message_sender_name(
    msg: SACConversationMsg,
    sender_name: Optional[str] = None,
    extra_meta: Optional[Dict[str, Any]] = None,
) -> None:
    if not hasattr(msg, "Meta_JSON"):
        return

    meta = _msg_meta_to_dict(getattr(msg, "Meta_JSON", None))
    meta["sender_name"] = (sender_name or _message_default_sender_name(getattr(msg, "Rol", ""))).strip()

    if extra_meta:
        for k, v in extra_meta.items():
            meta[k] = v

    setattr(msg, "Meta_JSON", json.dumps(meta, ensure_ascii=False))


def _get_message_sender_name(msg: SACConversationMsg) -> str:
    meta = _msg_meta_to_dict(getattr(msg, "Meta_JSON", None))
    sender = (meta.get("sender_name") or "").strip()
    if sender:
        return sender

    return _message_default_sender_name(getattr(msg, "Rol", ""))


def _append_conversation_message(
    conv: SACConversation,
    role: str,
    text_msg: str,
    *,
    sender_name: Optional[str] = None,
    created_by_user_id: Optional[int] = None,
) -> SACConversationMsg:
    now = datetime.utcnow()

    msg = SACConversationMsg(
        **{
            _convmsg_fk_attr(): conv.Id,
            "Rol": role,
            _convmsg_text_attr(): (text_msg or "").strip(),
        }
    )

    if hasattr(msg, "Creada_At"):
        setattr(msg, "Creada_At", now)

    if created_by_user_id is not None and hasattr(msg, "Created_By_User_Id"):
        setattr(msg, "Created_By_User_Id", created_by_user_id)

    _set_message_sender_name(msg, sender_name=sender_name)

    db.session.add(msg)
    return msg


def _touch_conversation(
    conv: SACConversation,
    *,
    status: Optional[str] = None,
    needs_agent: Optional[bool] = None,
    last_role: Optional[str] = None,
) -> None:
    now = datetime.utcnow()

    if hasattr(conv, "Actualizada_At"):
        conv.Actualizada_At = now
    if hasattr(conv, "Last_Msg_At"):
        conv.Last_Msg_At = now
    if last_role is not None and hasattr(conv, "Last_Msg_Role"):
        conv.Last_Msg_Role = last_role
    if status is not None and hasattr(conv, "Status"):
        conv.Status = status
    if needs_agent is not None and hasattr(conv, "Needs_Agent"):
        conv.Needs_Agent = bool(needs_agent)

def _safe_meta_json(value: Any) -> Dict[str, Any]:
    if value is None:
        return {}
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
            return parsed if isinstance(parsed, dict) else {}
        except Exception:
            return {}
    return {}


def _sender_name_for_role(role: str) -> str:
    role = (role or "").lower()
    if role == "agent":
        return (
            session.get("user_name")
            or session.get("user_email")
            or "Asesor Hotel Villa Grace"
        )
    if role == "bot":
        return "Bot Hotel Villa Grace"
    if role == "user":
        return "Cliente"
    return "Sistema"


def _set_msg_meta(msg: SACConversationMsg, meta: Dict[str, Any]) -> None:
    if hasattr(msg, "Meta_JSON"):
        try:
            setattr(msg, "Meta_JSON", json.dumps(meta, ensure_ascii=False))
        except Exception:
            setattr(msg, "Meta_JSON", "{}")


def _extract_sender_name(msg: SACConversationMsg) -> Optional[str]:
    meta = _safe_meta_json(getattr(msg, "Meta_JSON", None))
    sender_name = (meta.get("sender_name") or "").strip()
    return sender_name or None


def _log_single_conversation_message(
    conv: SACConversation,
    role: str,
    text_msg: str,
    *,
    status: Optional[str] = None,
    needs_agent: Optional[bool] = None,
    sender_name: Optional[str] = None,
) -> None:
    now = datetime.utcnow()

    msg = SACConversationMsg(
        **{
            _convmsg_fk_attr(): conv.Id,
            "Rol": role,
            _convmsg_text_attr(): (text_msg or "").strip(),
        }
    )
    if hasattr(msg, "Creada_At"):
        setattr(msg, "Creada_At", now)

    _set_msg_meta(
        msg,
        {
            "sender_name": sender_name or _sender_name_for_role(role),
        },
    )

    db.session.add(msg)

    if hasattr(conv, "Actualizada_At"):
        conv.Actualizada_At = now
    if hasattr(conv, "Last_Msg_At"):
        conv.Last_Msg_At = now
    if hasattr(conv, "Last_Msg_Role"):
        conv.Last_Msg_Role = role
    if status is not None and hasattr(conv, "Status"):
        conv.Status = status
    if needs_agent is not None and hasattr(conv, "Needs_Agent"):
        conv.Needs_Agent = bool(needs_agent)


def _log_conversation_turn(
    session_id: str,
    cid: Optional[int],
    user_text: str,
    bot_text: str,
    status: Optional[str] = None,
    needs_agent: Optional[bool] = None,
    last_role: str = "bot",
) -> None:
    """
    Registra el turno (usuario/bot) en SAC_Conversation y SAC_ConversationMsg.
    """
    if not session_id:
        return

    try:
        conv = _get_or_create_conversation(session_id, cid)
        if not conv:
            return

        assistant_role = last_role if last_role in ("bot", "agent") else "bot"
        assistant_sender = (
            session.get("user_name")
            or session.get("user_email")
            or _message_default_sender_name(assistant_role)
        ) if assistant_role == "agent" else _message_default_sender_name("bot")

        _append_conversation_message(
            conv,
            "user",
            user_text or "(vacío)",
            sender_name="Cliente",
        )

        _append_conversation_message(
            conv,
            assistant_role,
            bot_text or "",
            sender_name=assistant_sender,
        )

        _touch_conversation(
            conv,
            status=status,
            needs_agent=needs_agent,
            last_role=assistant_role,
        )

        db.session.commit()

    except Exception as e:
        current_app.logger.warning(f"[SAC-CONV] No se pudo registrar conversación: {e}")
        try:
            db.session.rollback()
        except Exception:
            pass


def _call_ai_chat(
    messages: List[Dict[str, str]], temperature: float = 0.4
) -> Tuple[Optional[str], Optional[str], float]:
    """
    Llama a un motor de IA conversacional:
      1) Si SAC_N8N_WEBHOOK está definido, llama a ese webhook (n8n).
      2) Si no, y SAC_ENABLE_OLLAMA=1, llama a Ollama local.
      3) Si nada está configurado, devuelve (None, None, 0.0).

    IMPORTANTE: Estos motores se usan solo para REFORMULAR respuestas base
    ya calculadas por el sistema del hotel. No deben inventar nuevos datos.
    """
    # 1) n8n
    n8n_url = (os.getenv("SAC_N8N_WEBHOOK") or "").strip()
    if n8n_url:
        try:
            import requests  # type: ignore

            resp = requests.post(
                n8n_url,
                json={"messages": messages, "temperature": temperature},
                timeout=int(os.getenv("SAC_N8N_TIMEOUT", "25")),
            )
            if resp.ok:
                js = resp.json()
                txt = (
                    js.get("answer") or js.get("output") or js.get("message") or ""
                ).strip()
                if txt:
                    conf = float(js.get("confidence") or 0.7)
                    return txt, "AI_N8N", conf
        except Exception as e:
            current_app.logger.warning(f"[SAC-AI] Error llamando a n8n: {e}")

    # 2) Ollama local (chat API)
    if os.getenv("SAC_ENABLE_OLLAMA", "0") in ("1", "true", "True"):
        ollama_url = (
            os.getenv("OLLAMA_URL") or "http://localhost:11434/api/chat"
        ).strip()
        model = (os.getenv("OLLAMA_MODEL") or "llama3.2").strip()
        try:
            import requests  # type: ignore

            payload = {
                "model": model,
                "messages": messages,
                "stream": False,
                "options": {"temperature": temperature},
            }
            resp = requests.post(ollama_url, json=payload, timeout=30)
            if resp.ok:
                js = resp.json()
                msg = js.get("message") or {}
                txt = (msg.get("content") or "").strip()
                if txt:
                    return txt, "AI_OLLAMA", 0.65
        except Exception as e:
            current_app.logger.warning(f"[SAC-AI] Error llamando a Ollama: {e}")

    # 3) Sin IA configurada
    return None, None, 0.0

def _ai_rephrase_enabled() -> bool:
    return os.getenv("SAC_ENABLE_AI_REPHRASE", "0") in ("1", "true", "True")

def _call_ai_rephrase(
    history: List[Dict[str, str]],
    user_q: str,
    base_answer: str,
    base_source: Optional[str],
) -> Tuple[Optional[str], Optional[str], float]:
    """
    Usa la IA para adaptar la 'base_answer' al contexto de la conversación.
    - NO debe inventar nuevos datos (horarios, precios, políticas, direcciones, etc.).
    - Solo puede reformular texto existente en la respuesta base o en lo dicho por el huésped.
    - Para saludos/agradecimientos, responde corto, sin listas.
    """
    base_source = base_source or "UNKNOWN"
    custom_instructions = cfg("chatbot_system_prompt", "").strip()
    system_prompt = _build_effective_system_prompt(custom_instructions)

    messages: List[Dict[str, str]] = [{"role": "system", "content": system_prompt}]
    # Historial reciente
    for m in history[-6:]:
        messages.append(m)

    # Mensaje actual del usuario
    messages.append(
        {
            "role": "user",
            "content": f"Mensaje del huésped: {user_q or '(vacío)'}",
        }
    )

    # Respuesta base que debe adaptarse
    messages.append(
        {
            "role": "assistant",
            "content": (
                f"Respuesta base del sistema (fuente: {base_source}):\n"
                f"{base_answer}"
            ),
        }
    )

    # Instrucción final explícita
    messages.append(
        {
            "role": "user",
            "content": (
                "Por favor responde al huésped con una sola respuesta adaptada, "
                "natural y NO repetitiva, siguiendo las reglas anteriores y "
                "sin inventar datos nuevos."
            ),
        }
    )

    return _call_ai_chat(messages, temperature=0.4)


def user_accepts_handoff(text: str) -> bool:
    """
    Heurística sencilla para detectar que el huésped acepta
    ser atendido por un agente humano.
    """
    if not text:
        return False
    t = text.lower()
    opciones_si = [
        "si",
        "sí",
        "claro",
        "por favor",
        "de acuerdo",
        "esta bien",
        "está bien",
        "ok",
        "dale",
        "perfecto",
        "porfa",
    ]
    return any(p in t for p in opciones_si)


def user_rejects_handoff(text: str) -> bool:
    """
    Heurística sencilla para detectar que el huésped rechaza
    ser atendido por un agente humano.
    """
    if not text:
        return False

    t = text.lower().strip()
    opciones_no = [
        "no",
        "no gracias",
        "mejor no",
        "prefiero no",
        "continuar con el bot",
        "seguir con el bot",
        "no deseo",
    ]
    return any(t == p or t.startswith(p + " ") for p in opciones_no)

# ====================== Endpoint público de QA/RAG ======================
@sac_bp.post("/ask")
def sac_ask():
    data = request.get_json(silent=True) or {}
    raw_q = (data.get("q") or "").strip()
    qn = _norm(raw_q)
    sess = (
        request.headers.get("X-Session-Id")
        or request.cookies.get("vg_session")
        or "web"
    )
    cid, _ = _current_cliente_y_email()

    conv = _get_or_create_conversation(sess, cid)
    current_status = (getattr(conv, "Status", None) or "bot").lower() if conv else "bot"
    current_needs_agent = bool(getattr(conv, "Needs_Agent", False)) if conv else False

    suggestions = _default_suggestions(cid)
    result: Dict[str, Any] = {
        "ok": True,
        "answer": "",
        "confidence": 0.0,
        "source": None,
        "suggestions": suggestions,
        "need_handoff": False,
        "handoff_state": None,
    }

    conv_status = current_status or "bot"
    conv_needs_agent = current_needs_agent

    # Conversación ya escalada:
    # registrar SIEMPRE el nuevo mensaje del usuario para que el asesor lo vea.
    if conv_status in ("agent_pending", "agent_active"):
        try:
            _append_conversation_message(
                conv,
                "user",
                raw_q,
                sender_name="Cliente",
            )
            _touch_conversation(
                conv,
                status=conv_status,
                needs_agent=True,
                last_role="user",
            )
            db.session.commit()
        except Exception as e:
            current_app.logger.warning(
                f"[SAC] No se pudo registrar mensaje del cliente en conversación escalada: {e}"
            )
            try:
                db.session.rollback()
            except Exception:
                pass

        if conv_status == "agent_pending":
            result["answer"] = (
                "Tu consulta fue transferida a un agente humano de recepción. "
                "Tu mensaje ya quedó registrado y el asesor lo verá en este mismo chat."
            )
            result["source"] = "HANDOFF_PENDING"
        else:
            result["answer"] = (
                "Tu conversación está siendo atendida por recepción. "
                "Tu mensaje ya quedó registrado y el asesor continuará por este mismo chat."
            )
            result["source"] = "AGENT_ACTIVE"

        result["confidence"] = 1.0
        result["need_handoff"] = True
        result["handoff_state"] = "PENDING"
        return jsonify(result)

    # Oferta de transferencia pendiente:
    # NO se debe perder ni desaparecer hasta que el usuario decida sí o no.
    if conv_status == "handoff_offer":
        if user_accepts_handoff(qn):
            transfer_msg = (
                "Perfecto, voy a transferir tu consulta a un agente humano de recepción. "
                "En cuanto la revisen, continuarán la conversación por este mismo chat."
            )
            result.update(
                {
                    "answer": transfer_msg,
                    "source": "HANDOFF_ACCEPTED",
                    "confidence": 1.0,
                    "need_handoff": True,
                    "handoff_state": "PENDING",
                }
            )
            conv_status = "agent_pending"
            conv_needs_agent = True
            _log_conversation_turn(
                session_id=sess,
                cid=cid,
                user_text=raw_q,
                bot_text=transfer_msg,
                status=conv_status,
                needs_agent=conv_needs_agent,
                last_role="bot",
            )
            return jsonify(result)

        if user_rejects_handoff(qn):
            decline_msg = (
                "Entendido. Continuaré atendiéndote por este chat sin transferirte a un asesor."
            )
            result.update(
                {
                    "answer": decline_msg,
                    "source": "HANDOFF_REJECTED",
                    "confidence": 1.0,
                    "need_handoff": False,
                    "handoff_state": None,
                }
            )
            conv_status = "bot"
            conv_needs_agent = False
            _log_conversation_turn(
                session_id=sess,
                cid=cid,
                user_text=raw_q,
                bot_text=decline_msg,
                status=conv_status,
                needs_agent=conv_needs_agent,
                last_role="bot",
            )
            return jsonify(result)

        result["answer"] = (
            "Tienes una solicitud de transferencia pendiente. "
            "Por favor selecciona Sí o No en el recuadro para decidir si deseas ser atendido por un asesor."
        )
        result["source"] = "HANDOFF_DECISION_PENDING"
        result["confidence"] = 1.0
        result["need_handoff"] = True
        result["handoff_state"] = "OFFER"
        return jsonify(result)

    # 1) Small-talk / saludos
    # IMPORTANTE:
    # ya NO tratamos automáticamente cualquier consulta corta como small-talk,
    # porque "wifi", "parqueo", "karaoke", etc. son consultas válidas.
    if (
        (not qn)
        or _is_greeting(qn)
        or _is_smalltalk(qn)
        or _is_help_like(qn)
    ):
        result["answer"] = _hotel_greeting_answer()
        result["confidence"] = 0.45
        result["source"] = "SMALL_TALK"
    else:
        # 2) Respuesta determinística desde documentos subidos/config
        structured = _answer_from_uploaded_kb(raw_q, qn)
        if structured:
            result.update(structured)
            if result.get("source") == "NO_INFO":
                result["need_handoff"] = True
                result["handoff_state"] = "OFFER"
                conv_status = "handoff_offer"
                conv_needs_agent = False

        # 3) Q/A manual solo si todavía no hay respuesta
        # FIX:
        # - ya no tomamos la primera fila de SAC_KB_QA como fallback ciego
        # - solo usamos KB_QA si la similitud con la pregunta es razonable
        if not result["answer"]:
            try:
                with db.engine.begin() as conn:
                    ft_candidates = conn.execute(
                        text("""
                            SELECT Id, Question, Answer
                              FROM SAC_KB_QA
                             WHERE Status='ACTIVE'
                             ORDER BY Updated_At DESC, Created_At DESC
                             LIMIT 100
                        """)
                    ).mappings().all()
            except Exception:
                ft_candidates = []

            if ft_candidates:
                q_tokens = set(_tokenize_norm(raw_q))
                best_like = None
                best_score = 0.0
                raw_q_norm = _norm(raw_q)

                for row in ft_candidates:
                    qq = _norm(row.get("Question") or "")
                    aa = _norm(row.get("Answer") or "")
                    qq_tokens = set(_tokenize_norm(qq))

                    overlap = len(q_tokens & qq_tokens)
                    score = overlap / max(len(q_tokens), 1) if q_tokens else 0.0

                    if raw_q_norm and (raw_q_norm in qq or qq in raw_q_norm):
                        score = max(score, 0.95)
                    elif raw_q_norm and raw_q_norm in aa:
                        score = max(score, 0.65)

                    if score > best_score:
                        best_score = score
                        best_like = row

                if best_like and best_score >= 0.55:
                    result["answer"] = best_like["Answer"]
                    result["confidence"] = round(best_score, 2)
                    result["source"] = "KB_QA"
                    result["qa_id"] = int(best_like["Id"])

        # 4) RAG solo si todavía no hay respuesta
        # Primero intentamos recuperación real del índice.
        hits: List[Dict[str, Any]] = []
        if not result["answer"]:
            try:
                hits = search(raw_q, topk=6)
            except Exception as e:
                current_app.logger.warning(f"[SAC] RAG search error: {e}")
                hits = []

        # 4.a) Si hay backend de IA configurado, responder con el prompt efectivo
        # usando SOLO contexto documental.
        if not result["answer"] and hits:
            try:
                ai_answer, ai_source, ai_conf = _call_ai_answer_from_context(raw_q, hits)
                if ai_answer:
                    result["answer"] = ai_answer
                    result["confidence"] = max(0.60, float(ai_conf or 0.60))
                    result["source"] = ai_source or "AI_CONTEXT"
            except Exception as e:
                current_app.logger.warning(f"[SAC] AI context answer error: {e}")

        # 4.b) Fallback sin IA: devolver la frase documental más relevante
        if not result["answer"] and hits:
            try:
                compact = _build_compact_rag_answer(raw_q, hits)
                if compact:
                    result.update(compact)
            except Exception as e:
                current_app.logger.warning(f"[SAC] Compact RAG answer error: {e}")

        # 5) Restaurantes cercanos solo para preguntas explícitas de cercanía,
        # NO para preguntas tipo '¿el hotel tiene restaurante?'
        if not result["answer"]:
            nearby_restaurant_query = any(
                x in qn
                for x in [
                    "restaurantes cerca",
                    "restaurant cerca",
                    "donde comer cerca",
                    "dónde comer cerca",
                    "lugares para comer cerca",
                    "opciones para comer cerca",
                ]
            )

            if nearby_restaurant_query:
                try:
                    if os.getenv("OSM_ENABLE", "0") == "1":
                        import requests  # type: ignore

                        lat = float(os.getenv("HOTEL_LAT", "9.585"))
                        lon = float(os.getenv("HOTEL_LON", "-85.103"))
                        r = requests.get(
                            "https://nominatim.openstreetmap.org/search",
                            params={
                                "q": "restaurant",
                                "format": "json",
                                "limit": "5",
                                "viewbox": f"{lon-0.05},{lat+0.05},{lon+0.05},{lat-0.05}",
                            },
                            headers={"User-Agent": "VillaGraceBot/1.0"},
                            timeout=20,
                        )
                        js = r.json()
                        if js:
                            lines = [
                                f"- {it.get('display_name','').split(',')[0]}"
                                for it in js[:5]
                            ]
                            result["answer"] = (
                                "Algunas opciones cercanas que aparecen en mapas públicos:\n"
                                + "\n".join(lines)
                            )
                            result["confidence"] = 0.40
                            result["source"] = "OSM"
                except Exception as e:
                    current_app.logger.warning(f"[SAC] Error consultando OSM: {e}")

                if not result["answer"]:
                    with db.engine.begin() as conn:
                        cfg_val = conn.execute(
                            text("SELECT Valor FROM SAC_Config WHERE Clave='nearby_restaurants'")
                        ).scalar()
                    if cfg_val:
                        try:
                            items = json.loads(cfg_val)
                            result["answer"] = (
                                "Opciones cercanas registradas por el hotel:\n"
                                + "\n".join(f"- {x}" for x in items[:5])
                            )
                            result["confidence"] = 0.35
                            result["source"] = "CONFIG"
                        except Exception:
                            pass

        # 6) No info si todavía no hay respuesta
        if not result["answer"]:
            try:
                with db.engine.begin() as conn:
                    conn.execute(
                        text("""
                            INSERT INTO SAC_KB_Unanswered (Question, Asked_By, Context_JSON, Status)
                            VALUES (:p, :asked_by, :ctx, 'Pendiente')
                        """),
                        {
                            "p": raw_q,
                            "asked_by": str(cid) if cid else sess,
                            "ctx": json.dumps(
                                {"channel": "web", "session_id": sess, "cliente_id": cid},
                                ensure_ascii=False,
                            ),
                        },
                    )
            except Exception:
                current_app.logger.warning("[SAC] No se pudo registrar pregunta no contestada.", exc_info=True)

            result["answer"] = _noinfo_answer()
            result["confidence"] = 0.0
            result["source"] = "NO_INFO"
            result["need_handoff"] = True
            result["handoff_state"] = "OFFER"
            conv_status = "handoff_offer"
            conv_needs_agent = False

    # 7) Capa IA solo si está habilitada y no hay handoff
    base_answer = (result.get("answer") or "").strip()
    base_source = (result.get("source") or "") or None
    skip_ai = result.get("source") in ("NO_INFO",) or bool(result.get("need_handoff"))

    if base_answer and not skip_ai and _ai_rephrase_enabled():
        try:
            history = _get_conversation_history(sess, limit=6)
            ai_answer, ai_source, ai_conf = _call_ai_rephrase(
                history=history,
                user_q=raw_q,
                base_answer=base_answer,
                base_source=base_source,
            )
            if ai_answer:
                result["answer"] = ai_answer
                if ai_source:
                    result["source"] = ai_source
                if ai_conf:
                    try:
                        base_conf = float(result.get("confidence") or 0.0)
                    except Exception:
                        base_conf = 0.0
                    result["confidence"] = max(base_conf, ai_conf)
        except Exception as e:
            current_app.logger.warning(f"[SAC-AI] Error al reescribir respuesta: {e}")

    # 8) Normalización final de no-info
    final_answer = (result.get("answer") or "").strip()
    if (
        final_answer
        and _looks_like_noinfo(final_answer)
        and result.get("source") != "NO_INFO"
    ):
        result["answer"] = _noinfo_answer()
        result["confidence"] = 0.0
        result["source"] = "NO_INFO"
        result["need_handoff"] = True
        result["handoff_state"] = "OFFER"
        conv_status = "handoff_offer"
        conv_needs_agent = False

    try:
        _log_conversation_turn(
            session_id=sess,
            cid=cid,
            user_text=raw_q,
            bot_text=result.get("answer") or base_answer or "",
            status=conv_status,
            needs_agent=conv_needs_agent,
            last_role="bot",
        )
    except Exception:
        pass

    return jsonify(result)


# --- OUTBOX RUN (manual/admin) ---
@sac_bp.post("/outbox/run")
@role_required("Administrador")
def outbox_run():
    """
    Ejecuta el worker de Outbox manualmente (para pruebas / administración).
    """
    from services.sac.outbox_worker import process_outbox

    n = process_outbox(batch=int((request.args.get("n") or 50)))
    return jsonify({"ok": True, "processed": n})


# --- Página de prueba de chat SAC/RAG ---
@sac_bp.get("/chat")
def sac_chat_page():
    """
    Página sencilla para probar el widget de chat SAC/RAG.
    """
    return render_template("sac/chat_page.html")
