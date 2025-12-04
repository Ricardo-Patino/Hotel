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

# ---------------------------------------------------------------------
# Blueprint SAC
# ---------------------------------------------------------------------
sac_bp = Blueprint("sac", __name__, url_prefix="/sac")

# ---------------------------------------------------------------------
# Duración de conversación
# ---------------------------------------------------------------------


MAX_CONV_AGE_HOURS = 24


def _get_active_conversation(session_id: str, create_if_missing: bool = True) -> SACConversation | None:
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
        SACConversation.query
        .filter_by(Session_Id=session_id)
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
        elif last_msg_at is not None and last_msg_at < now - timedelta(hours=MAX_CONV_AGE_HOURS):
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
        c
        for c in unicodedata.normalize("NFD", s)
        if unicodedata.category(c) != "Mn"
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
        qn == g
        or qn.startswith(g + " ")
        or (" " + g + " ") in (" " + qn + " ")
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
    rows = db.session.execute(
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
    ).mappings().all()
    return [dict(r) for r in rows]


# ====================== PREFERENCIAS ======================
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
        or len(qn.split()) <= 2
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
    # Session Id desde querystring o header, por si acaso
    sid = request.args.get("session_id") or request.headers.get("X-Session-Id")
    if not sid:
        return jsonify(ok=False, message="session_id requerido"), 400

    conv = _get_active_conversation(sid, create_if_missing=False)
    if not conv:
        # No hay conversación activa para este cliente (o se venció >24h)
        return jsonify(ok=True, conv_id=None, messages=[], last_msg_at=None)

    # Si quieres aplicar la regla de 24h también aquí (por seguridad)
    from datetime import datetime, timedelta
    now = datetime.utcnow()
    if conv.Last_Msg_At and conv.Last_Msg_At < now - timedelta(hours=MAX_CONV_AGE_HOURS):
        return jsonify(ok=True, conv_id=None, messages=[], last_msg_at=None)

    since_raw = request.args.get("since")
    q = (SACConversationMsg.query
         .filter_by(Conv_Id=conv.Id)
         .order_by(SACConversationMsg.Creada_At.asc()))

    if since_raw:
        try:
            # Formato ISO simple "YYYY-MM-DD HH:MM:SS"
            since = datetime.fromisoformat(since_raw)
            q = q.filter(SACConversationMsg.Creada_At > since)
        except ValueError:
            pass  # si viene mal, devolvemos todo

    msgs = []
    last_ts = conv.Last_Msg_At
    for m in q:
        ts = m.Creada_At
        if last_ts is None or ts > last_ts:
            last_ts = ts
        msgs.append({
            "id": m.Id,
            "role": m.Rol,          # 'user' | 'bot' | 'agent'
            "text": m.Texto,
            "created_at": ts.isoformat(sep=" ", timespec="seconds"),
        })

    return jsonify(
        ok=True,
        conv_id=conv.Id,
        messages=msgs,
        last_msg_at=(
            last_ts.isoformat(sep=" ", timespec="seconds")
            if last_ts else None
        ),
        status=getattr(conv, "Status", None),
        needs_agent=getattr(conv, "Needs_Agent", None),
        abierta=conv.Abierta,
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
            SACConversation.query
            .filter_by(Session_Id=sid, Abierta=True)
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
            Conv_Id=conv.Id,
            Rol="bot", 
            Texto="La conversación ha sido derivada a un agente humano de recepción.",
            Creada_At=datetime.utcnow(),
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
        current_app.logger.warning(
            f"[SAC] No se pudo notificar solicitud {s.Id}: {e}"
        )

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

    rows = db.session.execute(
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
    ).mappings().all()

    items = [
        {
            "Id": r["Id"],
            "Codigo_Reserva": r["Codigo_Reserva"],
            "Codigo_Cliente": r["Codigo_Cliente"],
            "Detalle": (r["Clave"] or "")
            + (": " + r["Valor"] if r["Valor"] else ""),
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
        "closed": "Cerrada",
    }
    s = (status or "").lower()
    return mapping.get(s, (status or "Abierta").title())


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

    IMPORTANTE:
    La base puede estar en una versión antigua sin columnas:
      - Status, Needs_Agent, Guest_Name, Guest_Email, Channel,
        Last_Msg_At, Last_Msg_Role.
    Para evitar errores 1054 se inspeccionan las columnas y se
    construye el SELECT dinámicamente, usando constantes NULL
    o valores por defecto cuando la columna no existe.
    """
    cols = _get_conv_columns()

    has_status = "status" in cols
    has_needs_agent = "needs_agent" in cols
    has_guest_name = "guest_name" in cols
    has_guest_email = "guest_email" in cols
    has_channel = "channel" in cols
    has_last_msg_at = "last_msg_at" in cols
    has_last_msg_role = "last_msg_role" in cols

    status_expr = "COALESCE(c.Status,'bot')" if has_status else "'bot'"
    needs_agent_expr = "COALESCE(c.Needs_Agent,0)" if has_needs_agent else "0"
    guest_name_expr = "c.Guest_Name" if has_guest_name else "NULL"
    guest_email_expr = "c.Guest_Email" if has_guest_email else "NULL"
    channel_expr = "c.Channel" if has_channel else "'Web'"
    last_msg_at_expr = "c.Last_Msg_At" if has_last_msg_at else "NULL"
    last_msg_role_expr = "c.Last_Msg_Role" if has_last_msg_role else "NULL"

    order_by_expr = (
        "Needs_Agent DESC, Actualizada_At DESC"
        if has_needs_agent
        else "Actualizada_At DESC"
    )

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
          (
            SELECT m.Texto
            FROM SAC_ConversationMsg m
            WHERE m.Conv_Id = c.Id
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
        status = r.get("Status") or "bot"
        needs_agent = bool(r.get("Needs_Agent"))
        requires_attention = needs_agent or status.lower() == "agent_pending"

        guest_label = (
            r.get("Guest_Name")
            or r.get("Guest_Email")
            or "Visitante web"
        )
        snippet = (r.get("Last_Snippet") or "").replace("\n", " ")
        if len(snippet) > 160:
            snippet = snippet[:157] + "..."

        items.append(
            {
                "id": int(r["Id"]),
                "status": status,
                "status_label": _status_label(status),
                "requires_attention": requires_attention,
                "guest_label": guest_label,
                "last_snippet": snippet,
            }
        )

    return jsonify({"ok": True, "items": items})


@sac_bp.get(
    "/conversations/<int:conversation_id>", endpoint="conversation_detail"
)
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
        SACConversationMsg.query.filter_by(Conv_Id=conversation_id)
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

    # --- NUEVA lógica de estado / atención ---
    status_raw = getattr(conv, "Status", None) or "bot"
    status = str(status_raw).lower()
    needs_agent = bool(getattr(conv, "Needs_Agent", False))
    abierta = bool(getattr(conv, "Abierta", True))

    # Requiere atención si está abierta y:
    #  - tiene Needs_Agent=1, o
    #  - está en estados lógicos orientados a agente.
    requires_attention = abierta and (
        needs_agent or status in ("agent_pending", "agent_active")
    )

    # Un agente puede responder si:
    #  - la conversación está abierta, y
    #  - o bien tiene Needs_Agent=1,
    #  - o bien el Status indica que ya está en flujo de agente.
    can_agent_reply = abierta and (
        needs_agent or status in ("agent_pending", "agent_active")
    )

    conversation_out: Dict[str, Any] = {
        "id": conv.Id,
        "status": status_raw,
        "status_label": _status_label(status_raw),
        "requires_attention": requires_attention,
        "guest_label": getattr(conv, "Guest_Name", None)
        or getattr(conv, "Guest_Email", None)
        or "Visitante web",
        "can_agent_reply": can_agent_reply,
    }

    messages_out: List[Dict[str, Any]] = []
    for m in msgs:
        role_raw = m.Rol or "user"
        role = (role_raw or "").lower()
        content = m.Texto or ""
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

        messages_out.append(
            {
                "id": m.Id,
                "role": out_role,
                "content": content,
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
    Inserta un mensaje con Rol='agent' y actualiza Status/Needs_Agent.
    """
    user_id = session.get("user_id")

    data = request.get_json(silent=True) or {}
    text_msg = (data.get("message") or "").strip()
    if not text_msg:
        return jsonify({"ok": False, "error": "El mensaje no puede estar vacío."}), 400

    conv = SACConversation.query.get(conversation_id)
    if not conv:
        return jsonify({"ok": False, "error": "Conversación no encontrada"}), 404

    now = datetime.utcnow()
    try:
        msg = SACConversationMsg(
            Conv_Id=conv.Id,
            Rol="agent",
            Texto=text_msg,
        )
        # Si el modelo tiene columna Creada_At/Created_At, la asignamos
        if hasattr(msg, "Creada_At"):
            setattr(msg, "Creada_At", now)
        if hasattr(msg, "Created_By_User_Id"):
            setattr(msg, "Created_By_User_Id", user_id)

        db.session.add(msg)

        # Actualizar estado de la conversación
        if hasattr(conv, "Status"):
            conv.Status = "agent_active"
        if hasattr(conv, "Needs_Agent"):
            conv.Needs_Agent = False
        conv.Actualizada_At = now
        if hasattr(conv, "Last_Msg_At"):
            conv.Last_Msg_At = now
        if hasattr(conv, "Last_Msg_Role"):
            conv.Last_Msg_Role = "agent"

        db.session.commit()
    except Exception as e:
        current_app.logger.warning(
            f"[SAC-CONV] No se pudo registrar respuesta de agente: {e}"
        )
        try:
            db.session.rollback()
        except Exception:
            pass
        return jsonify(
            {"ok": False, "error": "Error al registrar la respuesta del agente."}
        ), 500

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

    rows = db.session.execute(
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
    ).mappings().all()

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
        SACConversationMsg.query.filter_by(Conv_Id=conv_id)
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
                "text": m.Texto or "",
                "created_at": created.isoformat()
                if hasattr(created, "isoformat")
                else None,
            }
        )

    meta = {
        "id": conv.Id,
        "cliente_id": getattr(conv, "Codigo_Cliente", None),
        "session_id": getattr(conv, "Session_Id", None),
        "abierta": bool(getattr(conv, "Abierta", True)),
        "actualizada_at": conv.Actualizada_At.isoformat()
        if getattr(conv, "Actualizada_At", None)
        else None,
    }

    return jsonify({"ok": True, "items": items, "meta": meta})


# ====================== INCIDENTES ======================
@sac_bp.get("/incidentes", endpoint="incidentes_html")
@role_required("Recepcionista", "Administrador")
def incidentes_html():
    return render_template("sac-incidentes.html")


@sac_bp.get("/incidentes/data")
@role_required("Recepcionista", "Administrador")
def incidentes_data():
    rows = db.session.execute(
        text(
            """
        SELECT i.Id, i.Codigo_Reserva, i.Codigo_Cliente, i.Tipo, i.Severidad,
               i.Titulo, i.Detalle, i.Estado,
               DATE_FORMAT(i.Creada_At,'%Y-%m-%d %H:%i') AS Fecha
        FROM SAC_Incident i
        ORDER BY i.Creada_At DESC
        LIMIT 300
    """
        )
    ).mappings().all()
    return jsonify({"ok": True, "items": [dict(r) for r in rows]})


@sac_bp.post("/incidentes", endpoint="incidentes_new")
@role_required("Recepcionista", "Administrador")
def incidentes_new():
    data = request.get_json(silent=True) or request.form or {}
    try:
        rid = data.get("Codigo_Reserva")
        rid = int(rid) if rid not in (None, "", "0") else None
    except Exception:
        rid = None

    try:
        cid = data.get("Codigo_Cliente")
        cid = int(cid) if cid not in (None, "", "0") else None
    except Exception:
        cid = None

    if cid is None and rid is not None:
        r = (
            db.session.execute(
                text(
                    "SELECT Codigo_Cliente FROM Reserva "
                    "WHERE Codigo_Reserva=:r LIMIT 1"
                ),
                {"r": rid},
            )
            .mappings()
            .first()
        )
        cid = int(r["Codigo_Cliente"]) if r and r.get("Codigo_Cliente") else None

    i = SACIncident(
        Codigo_Reserva=rid,
        Codigo_Cliente=cid,
        Tipo=(data.get("Tipo") or "INCIDENTE"),
        Severidad=(data.get("Severidad") or "MEDIA"),
        Titulo=(data.get("Titulo") or "Sin título"),
        Detalle=((data.get("Detalle") or "").strip() or None),
    )
    db.session.add(i)
    db.session.commit()
    return jsonify({"ok": True, "id": i.Id})


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
    rows = db.session.execute(
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
    ).mappings().all()
    return jsonify({"ok": True, "items": [dict(r) for r in rows]})


# ====================== KB (documentos + Q/A) ======================
ALLOWED = {".txt", ".md", ".pdf", ".docx", ".html", ".htm"}


@sac_bp.post("/kb/upload")
@role_required("Administrador")
def sac_kb_upload():
    """
    Sube un documento a SAC_KB_Doc y reconstruye el índice vectorial.
    Se usa también desde la consola de KB (y desde el panel de administración del bot).
    """
    f = request.files.get("file")
    title = request.form.get("title") or (f.filename if f else None)
    if not f or not title:
        return jsonify(ok=False, error="Archivo/título faltante"), 400

    ext = pathlib.Path(f.filename).suffix.lower()
    if ext not in ALLOWED:
        return jsonify(ok=False, error=f"Tipo no permitido: {ext}"), 400

    safe = secure_filename(f.filename)
    dest = KB_DIR / safe
    dest.parent.mkdir(parents=True, exist_ok=True)
    f.save(dest)
    h = hashlib.sha256(dest.read_bytes()).hexdigest()
    sz = dest.stat().st_size

    with db.engine.begin() as conn:
        conn.execute(
            text(
                """
          INSERT INTO SAC_KB_Doc (Titulo, FileName, MimeType, Bytes, HashSum, Activo, SubidoPor)
          VALUES (:t,:fn,:mt,:b,:h,1,:u)
        """
            ),
            dict(t=title, fn=safe, mt=ext, b=sz, h=h, u=session.get("user_id")),
        )

    # Reindex rápido: leemos todas las filas activas
    with db.engine.begin() as conn:
        rows = (
            conn.execute(
                text("SELECT Id, FileName FROM SAC_KB_Doc WHERE Activo=1")
            )
            .mappings()
            .all()
        )
    docs, chunks = rebuild_index(db, [dict(r) for r in rows])

    return jsonify(ok=True, docs=docs, chunks=chunks)


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
          SELECT Id, Pregunta, Origen, Estado, Creada_At
          FROM SAC_KB_Unanswered
          WHERE Estado IN ('NUEVA','REVISADA')
          ORDER BY Creada_At DESC
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


@sac_bp.get("/kb/docs")
@role_required("Administrador")
def sac_kb_docs():
    """
    Devuelve un listado ligero de documentos de KB en formato JSON
    para alimentar la tabla de la consola de KB.
    """
    with db.engine.begin() as conn:
        rows = (
            conn.execute(
                text(
                    """
          SELECT Id, Titulo, FileName, Bytes, SubidoEn, Activo
          FROM SAC_KB_Doc
          ORDER BY SubidoEn DESC
          LIMIT 200
        """
                )
            )
            .mappings()
            .all()
        )

    items: List[Dict[str, Any]] = []
    for r in rows:
        items.append(
            {
                "id": int(r["Id"]),
                "title": (r["Titulo"] or "").strip(),
                "filename": r["FileName"],
                "source_type": None,  # se puede ampliar en DB si lo requieres
                "lang": None,
                "is_active": bool(r["Activo"]),
                "created_at": r["SubidoEn"].isoformat()
                if r.get("SubidoEn")
                else None,
                "size_bytes": int(r["Bytes"] or 0),
            }
        )

    return jsonify({"ok": True, "items": items})


@sac_bp.post("/kb/teach")
@role_required("Administrador")
def sac_kb_teach():
    """
    Alta/actualización de pares Pregunta-Respuesta manuales (FAQ estructurado).
    No se usa para subir archivos, solo Q/A.

    Espera JSON:
      - pregunta: str
      - respuesta: str
    """
    data = request.get_json(silent=True) or {}
    q = (data.get("pregunta") or "").strip()
    a = (data.get("respuesta") or "").strip()
    if not q or not a:
        return jsonify(ok=False, error="Pregunta/Respuesta requeridas"), 400

    with db.engine.begin() as conn:
        conn.execute(
            text(
                """
          INSERT INTO SAC_KB_QA (Pregunta, Respuesta, Activo, CreadoPor)
          VALUES (:q,:a,1,:u)
          ON DUPLICATE KEY UPDATE Respuesta=VALUES(Respuesta), Activo=1
        """
            ),
            dict(q=q, a=a, u=session.get("user_id")),
        )
        # marcar preguntas abiertas similares como respondidas
        conn.execute(
            text(
                """
          UPDATE SAC_KB_Unanswered
             SET Estado='RESPONDIDA', Respondida_At=NOW()
           WHERE Estado!='RESPONDIDA'
             AND Pregunta LIKE :likeq
        """
            ),
            dict(likeq=f"%{q[:80]}%"),
        )
    return jsonify(ok=True)


# ====================== Panel Administración Chatbot ======================
@sac_bp.get("/chat/admin", endpoint="sac_chat_admin")
@role_required("Administrador")
def sac_chat_admin():
    """
    Panel de administración del chatbot:
      - Edición de prompt (instrucciones del bot).
      - Upload de documentos (PDF, etc.) para la KB.
      - Vista rápida de preguntas frecuentes/tendencias.
    """
    prompt_current = cfg("chatbot_system_prompt", "")

    # Preguntas frecuentes (simple: agrupación por texto de usuario)
    popular_questions: List[Dict[str, Any]] = []
    with db.engine.begin() as conn:
        rows = (
            conn.execute(
                text(
                    """
            SELECT
              TRIM(LOWER(Texto)) AS pregunta,
              COUNT(*) AS veces
            FROM SAC_ConversationMsg
            WHERE Rol='user'
              AND Texto IS NOT NULL
              AND Texto <> ''
            GROUP BY TRIM(LOWER(Texto))
            ORDER BY veces DESC
            LIMIT 30
        """
                )
            )
            .mappings()
            .all()
        )
    for r in rows:
        popular_questions.append(
            {"text": r["pregunta"], "count": int(r["veces"] or 0)}
        )

    kb_stats = {"docs": 0, "unanswered": 0}
    with db.engine.begin() as conn:
        kb_stats["docs"] = int(
            conn.execute(text("SELECT COUNT(*) FROM SAC_KB_Doc")).scalar() or 0
        )
        kb_stats["unanswered"] = int(
            conn.execute(
                text(
                    "SELECT COUNT(*) FROM SAC_KB_Unanswered "
                    "WHERE Estado IN ('NUEVA','REVISADA')"
                )
            ).scalar()
            or 0
        )

    return render_template(
        "sac/chat_admin.html",
        prompt_current=prompt_current,
        popular_questions=popular_questions,
        kb_stats=kb_stats,
    )


# ---- Configuración JSON del chatbot para chat_admin.html ----
BASE_SYSTEM_PROMPT = (
    "Eres el asistente virtual del Hotel Villa Grace en Cóbano, Puntarenas, Costa Rica.\n"
    "Debes responder en español latino, tono cordial y profesional.\n"
    "Se te entrega una 'respuesta base' generada por los sistemas del hotel; "
    "tu tarea es reformularla o resumirla para que suene natural, no repetitiva, "
    "y coherente con la conversación reciente.\n\n"
    "Reglas IMPORTANTES:\n"
    "1. NO agregues datos nuevos (números, horarios, políticas, precios, direcciones, "
    "   nombres de contacto, características específicas) que no aparezcan explícitamente "
    "   en la 'respuesta base' o en el propio mensaje del huésped.\n"
    "2. Si la respuesta base indica que no hay información suficiente o que el bot no sabe, "
    "   respeta ese mensaje y NO intentes completar con suposiciones.\n"
    "3. Si el huésped solo saluda o dice algo como 'hola', 'buenas' o 'gracias', "
    "   responde en 1–2 frases muy cortas, sin listas de viñetas.\n"
    "4. Si el huésped se queja de que repites o dice 'no contestes lo mismo', "
    "   cambia el enfoque: reconoce brevemente que ya le explicaste y pide "
    "   más detalle o da un ejemplo distinto, sin inventar datos.\n"
    "5. Evita repetir literalmente grandes fragmentos de texto; puedes condensarlos.\n"
    "6. Solo usa listas con viñetas si el huésped explícitamente pide 'lista', 'puntos', etc.\n"
    "7. Mantén la información factual de la respuesta base, pero adapta el estilo.\n"
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
    Devuelve las preguntas más frecuentes del chatbot.
    Parámetros:
      - max_rows: máximo de mensajes a analizar (no estrictamente usado como sample).
      - limit: máximo de filas en el resultado.
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

    # Total de mensajes de usuario
    total = (
        db.session.execute(
            text(
                """
        SELECT COUNT(*) AS n
        FROM SAC_ConversationMsg
        WHERE Rol='user'
          AND Texto IS NOT NULL
          AND Texto <> ''
    """
            )
        )
        .mappings()
        .first()
        or {}
    )
    total_counted = int(total.get("n") or 0)

    # Preguntas normalizadas
    rows = (
        db.session.execute(
            text(
                """
        SELECT
          TRIM(LOWER(Texto)) AS pregunta,
          COUNT(*) AS veces
        FROM (
          SELECT Texto
          FROM SAC_ConversationMsg
          WHERE Rol='user'
            AND Texto IS NOT NULL
            AND Texto <> ''
          ORDER BY Id DESC
          LIMIT :max_rows
        ) t
        GROUP BY TRIM(LOWER(Texto))
        HAVING LENGTH(pregunta) >= 4
        ORDER BY veces DESC
        LIMIT :limit
    """
            ),
            {"max_rows": max_rows, "limit": limit},
        )
        .mappings()
        .all()
    )

    items = [
        {"question": r["pregunta"], "count": int(r["veces"] or 0)} for r in rows
    ]
    return jsonify({"ok": True, "items": items, "total_counted": total_counted})


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
        SACConversationMsg.query.filter_by(Conv_Id=conv.Id)
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
        history.append({"role": role, "content": m.Texto or ""})
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
    Si no existe conversación abierta para la sesión, la crea.

    Parámetros adicionales:
      - status: nuevo estado lógico de la conversación (bot, handoff_offer, agent_pending, agent_active, closed).
      - needs_agent: flag para marcar si requiere atención humana en el panel.
      - last_role: rol del último mensaje ('bot', 'agent', etc.) para meta.
    """
    if not session_id:
        return

    try:
        conv = _get_or_create_conversation(session_id, cid)
        if not conv:
            return

        now = datetime.utcnow()

        # Mensaje de usuario
        msg_user = SACConversationMsg(
            Conv_Id=conv.Id,
            Rol="user",
            Texto=user_text or "(vacío)",
        )
        if hasattr(msg_user, "Creada_At"):
            setattr(msg_user, "Creada_At", now)
        db.session.add(msg_user)

        # Mensaje del bot/assistant
        msg_bot = SACConversationMsg(
            Conv_Id=conv.Id,
            Rol=last_role if last_role != "user" else "bot",
            Texto=bot_text or "",
        )
        if hasattr(msg_bot, "Creada_At"):
            setattr(msg_bot, "Creada_At", now)
        db.session.add(msg_bot)

        # Actualizar metadatos de conversación
        conv.Actualizada_At = now
        if hasattr(conv, "Last_Msg_At"):
            setattr(conv, "Last_Msg_At", now)
        if hasattr(conv, "Last_Msg_Role"):
            setattr(conv, "Last_Msg_Role", last_role)
        if status is not None and hasattr(conv, "Status"):
            setattr(conv, "Status", status)
        if needs_agent is not None and hasattr(conv, "Needs_Agent"):
            setattr(conv, "Needs_Agent", bool(needs_agent))

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
                    js.get("answer")
                    or js.get("output")
                    or js.get("message")
                    or ""
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


# ====================== Endpoint público de QA/RAG ======================
@sac_bp.post("/ask")
def sac_ask():
    """
    Endpoint principal de QA:
      1) Small-talk / saludos → respuesta hotel-céntrica.
      2) Búsqueda en Q/A manual (FULLTEXT, luego LIKE).
      3) Búsqueda semántica en documentos (RAG).
      4) Opcionalmente, restaurantes cercanos (OSM o configuración).
      5) Si NO hay información confiable, NO inventar nada:
         - Responder que no hay datos.
         - Sugerir transferencia a un agente humano.
      6) Registrar pregunta no contestada en SAC_KB_Unanswered.
      7) Pasar la respuesta base por una capa de IA conversacional (n8n/Ollama)
         SOLO para reformular, sin agregar datos.
      8) Integrar con SAC_Conversation para poder escalar a agentes humanos,
         manteniendo estados: bot, handoff_offer, agent_pending, agent_active.
    """
    data = request.get_json(silent=True) or {}
    raw_q = (data.get("q") or "").strip()
    qn = _norm(raw_q)
    sess = (
        request.headers.get("X-Session-Id")
        or request.cookies.get("vg_session")
        or "web"
    )
    cid, _ = _current_cliente_y_email()

    # Cargar/crear conversación actual para conocer estado
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
        "handoff_state": None,  # 'OFFER' | 'PENDING'
    }

    conv_status = current_status or "bot"
    conv_needs_agent = current_needs_agent

    # ---------- Estado ya escalado a agente ----------
    if conv_status in ("agent_pending", "agent_active"):
        # Si el agente aún no ha respondido, seguimos mostrando el estado pendiente.
        msgs = SACConversationMsg.query.filter_by(Conv_Id=conv.Id).order_by(SACConversationMsg.Id.asc()).all()
        # Si ya existe un mensaje de Rol='agent' → mostrar ese texto y reactivar el bot.
        last_agent_msg = next((m for m in reversed(msgs) if m.Rol == "agent"), None)
        if last_agent_msg:
            result["answer"] = last_agent_msg.Texto
            result["source"] = "AGENT_REPLY"
            result["confidence"] = 1.0
            conv_status = "bot"
            conv_needs_agent = False
        else:
            result["answer"] = (
                "Tu consulta ha sido transferida a un agente humano. "
                "En cuanto revise el mensaje, continuará la conversación aquí mismo."
            )
            result["source"] = "HANDOFF_PENDING"
            result["need_handoff"] = True
            result["handoff_state"] = "PENDING"
    
        _log_conversation_turn(
            session_id=sess,
            cid=cid,
            user_text=raw_q,
            bot_text=result["answer"],
            status=conv_status,
            needs_agent=conv_needs_agent,
            last_role="bot",
        )
        db.session.commit()
        return jsonify(result)
    

    # ---------- Usuario responde a una oferta de transferencia ----------
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
                    "confidence": 0.0,
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
        else:
            # El usuario no aceptó explícitamente; seguimos con flujo normal de bot.
            conv_status = "bot"
            conv_needs_agent = False

    # ---------- 1) Small-talk / saludos / muy corto ----------
    if (
        (not qn)
        or _is_greeting(qn)
        or _is_smalltalk(qn)
        or _is_help_like(qn)
        or len(qn.split()) <= 2
    ):
        result["answer"] = _hotel_greeting_answer()
        result["confidence"] = 0.45
        result["source"] = "SMALL_TALK"
    else:
        # ---------- 2) Q/A manual FULLTEXT ----------
        ft_best = None
        try:
            with db.engine.begin() as conn:
                ft_best = (
                    conn.execute(
                        text(
                            """
                    SELECT Id, Pregunta, Respuesta,
                           MATCH(Pregunta, Respuesta)
                           AGAINST(:q IN NATURAL LANGUAGE MODE) AS score
                      FROM SAC_KB_QA
                     WHERE Activo=1
                     ORDER BY score DESC
                     LIMIT 1
                """
                        ),
                        {"q": raw_q},
                    )
                    .mappings()
                    .first()
                )
        except Exception:
            ft_best = None

        if ft_best and ft_best.get("score") and float(ft_best["score"]) >= 1.0:
            result["answer"] = ft_best["Respuesta"]
            result["confidence"] = min(0.99, 0.5 + float(ft_best["score"]) / 10.0)
            result["source"] = "KB_QA_FT"
            result["qa_id"] = int(ft_best["Id"])
        else:
            # ---------- 3) Q/A manual LIKE ----------
            with db.engine.begin() as conn:
                like_row = (
                    conn.execute(
                        text(
                            """
                    SELECT Id, Pregunta, Respuesta
                      FROM SAC_KB_QA
                     WHERE Activo=1
                       AND (
                           LOWER(Pregunta)  LIKE LOWER(:likeq)
                        OR LOWER(Respuesta) LIKE LOWER(:likeq)
                       )
                     ORDER BY CHAR_LENGTH(Pregunta) ASC
                     LIMIT 1
                """
                        ),
                        {"likeq": f"%{raw_q}%"},
                    )
                    .mappings()
                    .first()
                )

            if like_row:
                result["answer"] = like_row["Respuesta"]
                result["confidence"] = 0.60
                result["source"] = "KB_QA_LIKE"
                result["qa_id"] = int(like_row["Id"])
            else:
                # ---------- 4) RAG sobre documentos (KB_DOCS) ----------
                try:
                    hits = search(raw_q, topk=5)
                    ans = answer_from_chunks(raw_q, hits)
                    # Se asume que answer_from_chunks ya está instruido
                    # para NO inventar más allá del contexto.
                    if ans.get("ok") and float(ans.get("confidence", 0.0)) >= 0.28:
                        # ans ya incluye 'answer', 'confidence', etc.
                        result.update(ans)
                        result["source"] = "KB_DOCS"
                        result["suggestions"] = suggestions
                except Exception as e:
                    current_app.logger.warning(f"[SAC] RAG error: {e}")

                # ---------- 5) Restaurantes cercanos ----------
                if not result["answer"]:
                    if any(
                        x in qn
                        for x in [
                            "restaurante",
                            "comer",
                            "cenar",
                            "restaurant",
                            "almorzar",
                            "food",
                        ]
                    ):
                        # 5.1 OSM
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
                            current_app.logger.warning(
                                f"[SAC] Error consultando OSM: {e}"
                            )

                        # 5.2 Fallback: lista en SAC_Config
                        if not result["answer"]:
                            with db.engine.begin() as conn:
                                cfg_val = conn.execute(
                                    text(
                                        "SELECT Valor FROM SAC_Config "
                                        "WHERE Clave='nearby_restaurants'"
                                    )
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

                # ---------- 6) Registrar no contestadas + política NO-HALU ----------
                if not result["answer"]:
                    # No hay respuesta confiable → registrar gap y ofrecer agente
                    with db.engine.begin() as conn:
                        conn.execute(
                            text(
                                """
                        INSERT INTO SAC_KB_Unanswered (Pregunta, Detalle, Origen, Session_Id, Cliente_Id)
                        VALUES (:p, :d, 'WEB', :s, :c)
                    """
                            ),
                            {
                                "p": raw_q,
                                "d": json.dumps({"ua": "web"}),
                                "s": sess,
                                "c": cid,
                            },
                        )
                    result["answer"] = _noinfo_answer()
                    result["confidence"] = 0.0
                    result["source"] = "NO_INFO"
                    result["need_handoff"] = True
                    result["handoff_state"] = "OFFER"
                    conv_status = "handoff_offer"
                    conv_needs_agent = False

    # ---------- 7) Capa de IA conversacional (n8n / Ollama) ----------
    base_answer = (result.get("answer") or "").strip()
    base_source = (result.get("source") or "") or None

    # Nunca usar IA para reescribir cuando:
    #   - No hay información (NO_INFO) y se está ofreciendo agente.
    #   - Podría inducir a inventar datos en un caso 'sin respuesta'.
    skip_ai = (result.get("source") in ("NO_INFO",) or bool(result.get("need_handoff")))

    if base_answer and not skip_ai:
        try:
            history = _get_conversation_history(sess, limit=6)
            ai_answer, ai_source, ai_conf = _call_ai_rephrase(
                history=history,
                user_q=raw_q,
                base_answer=base_answer,
                base_source=base_source,
            )
            if ai_answer:
                # Sustituimos solo el texto y ajustamos confianza / fuente
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
            current_app.logger.warning(
                f"[SAC-AI] Error al reescribir respuesta: {e}"
            )

    # ---------- 8) Registrar conversación (último paso) ----------
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
        # ya se registró en el log interno dentro de _log_conversation_turn
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
