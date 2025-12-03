# Hotel/Hotel 2/blueprints/sac/routes.py
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Optional, List, Dict, Any, Tuple

from flask import request, jsonify, render_template, session, current_app
from sqlalchemy import text

from extensions import db
from utils.auth import role_required
from . import sac_bp

from models.sac import (
    SACConfig, SACNotifPref, SACOutbox, SACSolicitud,
    SACConversation, SACConversationMsg, SACIncident, SACFeedback
)

# ---------------------------------------------------------------------
# Utilidad de configuración
# ---------------------------------------------------------------------
def cfg(key: str, default: str = "") -> str:
    row = SACConfig.query.get(key)
    return (row.Valor if row else default) or default

# ---------------------------------------------------------------------
# Sesión estandarizada (CLIENTE)
#  Reglas:
#   - user_role == "Cliente"
#   - session["user_id"] = Codigo_Usuario
#   - SIEMPRE mapear a Codigo_Cliente usando Usuario.Codigo_Usuario → Usuario.Codigo_Cliente
#   - NUNCA asumir que user_id es un Codigo_Cliente aunque exista un Cliente con ese mismo ID
# ---------------------------------------------------------------------
def _current_cliente_y_email() -> Tuple[Optional[int], Optional[str]]:
    if session.get("user_role") != "Cliente":
        return None, None

    # Email directo de sesión si está
    email = (session.get("user_email") or None)
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
        row = db.session.execute(
            text("SELECT Codigo_Cliente FROM Usuario WHERE Codigo_Usuario = :u LIMIT 1"),
            {"u": uid}
        ).mappings().first()
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

    params = {"cid": int(cid) if cid else -1, "email": (email or "")}
    lim_sql = f"LIMIT {int(limit)}" if (limit and limit > 0) else ""
    rows = db.session.execute(text(f"""
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
    """), params).mappings().all()
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
        checkout=cfg("checkout_limite", "12:00")
    )

@sac_bp.post("/preferencias", endpoint="preferencias_save")
def preferencias_save():
    cid, email_sesion = _current_cliente_y_email()
    if not cid:
        return jsonify({"ok": False, "error": "No autenticado"}), 401

    data  = request.get_json(silent=True) or request.form or {}
    canal = (data.get("Canal") or "email").strip().lower()
    if canal not in ("email","sms","ambos"):
        canal = "email"

    pref = SACNotifPref.query.get(cid) or SACNotifPref(Codigo_Cliente=cid)
    pref.Canal    = canal
    pref.Email    = (data.get("Email") or email_sesion or "").strip() or None
    pref.Telefono = (data.get("Telefono") or "").strip() or None
    db.session.add(pref); db.session.commit()
    return jsonify({"ok": True})

# ====================== FAQ / HORARIOS ======================
@sac_bp.get("/faq", endpoint="faq_html")
def faq_html():
    return render_template(
        "sac-faq.html",
        checkin_ini=cfg("checkin_inicio", "12:00"),
        checkin_fin=cfg("checkin_fin", "00:00"),
        checkout=cfg("checkout_limite", "12:00")
    )

@sac_bp.get("/horarios")
def horarios_api():
    return jsonify({
        "checkin_inicio": cfg("checkin_inicio", "12:00"),
        "checkin_fin":    cfg("checkin_fin", "00:00"),
        "checkout":       cfg("checkout_limite", "12:00"),
    })

# ====================== CHATBOT ======================
@sac_bp.post("/chat/ask")
def chatbot_ask():
    payload = request.get_json(silent=True) or {}
    q   = (payload.get("q") or "").strip()
    ql  = q.lower()
    cid, _ = _current_cliente_y_email()

    faqs = {
        "check-in": f"El check-in es entre {cfg('checkin_inicio','12:00')} y {cfg('checkin_fin','00:00')}.",
        "check in": f"El check-in es entre {cfg('checkin_inicio','12:00')} y {cfg('checkin_fin','00:00')}.",
        "check-out": f"El check-out es hasta las {cfg('checkout_limite','12:00')}.",
        "checkout":  f"El check-out es hasta las {cfg('checkout_limite','12:00')}.",
        "servicios": "Restaurante, bar, piscina 8:00–21:00, Wi-Fi gratis y parqueo sin costo.",
        "piscina":   "Piscina 8:00–21:00 (adultos y niños).",
        "wifi":      "Wi-Fi gratuito en todo el hotel.",
        "estacionamiento": "Parqueo gratuito para huéspedes.",
        "parking":   "Parqueo gratuito para huéspedes."
    }
    answer = next((faqs[k] for k in faqs if k in ql), "")

    if not answer:
        try:
            import os
            from openai import OpenAI
            if os.getenv("OPENAI_API_KEY"):
                client = OpenAI()
                completion = client.chat.completions.create(
                    model=os.getenv("OPENAI_MODEL", "gpt-4o-mini"),
                    messages=[
                        {"role":"system","content":"Eres el asistente del Hotel Villa Grace; responde breve y claro."},
                        {"role":"user","content":q}
                    ],
                    temperature=0.4, max_tokens=120
                )
                answer = (completion.choices[0].message.content or "").strip()
        except Exception as e:
            current_app.logger.info(f"[SAC-CHAT] IA omitida: {e}")

    if not answer:
        answer = "Puedo ayudarte con horarios, servicios, ubicación o tu reserva. ¿Qué deseas saber?"

    # Persistencia de conversación (con cliente)
    sid = request.headers.get("X-Session-Id") or session.get("sid")
    if sid:
        conv = SACConversation.query.filter_by(Session_Id=sid, Abierta=True).first()
        if not conv:
            conv = SACConversation(Session_Id=sid, Abierta=True, Codigo_Cliente=cid)
            db.session.add(conv); db.session.flush()
        # Si la conversación ya existe pero quedó con cliente incorrecto, corregimos
        if cid and conv.Codigo_Cliente != cid:
            conv.Codigo_Cliente = cid
        db.session.add(SACConversationMsg(Conv_Id=conv.Id, Rol="user", Texto=q or "(vacío)"))
        db.session.add(SACConversationMsg(Conv_Id=conv.Id, Rol="bot",  Texto=answer))
        conv.Actualizada_At = datetime.utcnow()
        db.session.commit()

    suggestions = ["Horarios de check-in/out", "Servicios del hotel", "Quiero hablar con un agente"]
    return jsonify({"ok": True, "a": answer, "suggestions": suggestions})

@sac_bp.post("/chat/escalar")
def chatbot_escalar():
    payload = request.get_json(silent=True) or {}
    sid   = payload.get("session_id") or session.get("sid")
    texto = (payload.get("texto") or "Cliente solicita ayuda").strip()

    db.session.add(SACOutbox(
        Canal="email",
        Para=cfg("contacto_recepcion_email", "recepcion@hotel.test"),
        Asunto="[SAC] Solicitud de atención humana",
        Cuerpo=f"Conversación {sid or '-'} pide atención: {texto}",
        Ref_Entidad="Chat", Ref_Id=sid or "-",
        Programado_At=datetime.utcnow()
    ))
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
        checkout=cfg("checkout_limite", "12:00")
    )

@sac_bp.post("/solicitudes")
def registrar_solicitud():
    cid, _ = _current_cliente_y_email()
    if not cid:
        return jsonify({"ok": False, "error": "No autenticado"}), 401

    data = request.get_json(silent=True) or request.form or {}
    rid  = data.get("Codigo_Reserva")
    try:
        rid = int(rid) if rid not in (None, "", "0") else None
    except Exception:
        rid = None

    s = SACSolicitud(
        Codigo_Reserva=rid,
        Codigo_Cliente=cid,
        Clave=(data.get("Clave") or "otra").strip(),
        Valor=((data.get("Valor") or "").strip() or None)
    )
    db.session.add(s); db.session.commit()

    # Confirmación por canal preferido
    try:
        from services.grr.notification_service import NotificationService
        NotificationService().route_and_queue(
            codigo_cliente=cid,
            asunto="Hemos recibido tu solicitud",
            cuerpo=f"Gracias por escribirnos. Id de solicitud: {s.Id}. Nos pondremos en contacto pronto.",
            ref_tipo="SAC_Solicitud", ref_id=str(s.Id)
        )
        db.session.commit()
    except Exception as e:
        current_app.logger.warning(f"[SAC] No se pudo notificar solicitud {s.Id}: {e}")

    return jsonify({"ok": True, "id": s.Id})

# API JSON para panel
@sac_bp.get("/solicitudes", endpoint="solicitudes_list")
@role_required("Recepcionista", "Administrador")
def solicitudes_list():
    estado = (request.args.get("estado") or "").strip().upper()
    params, where = {}, ""
    if estado:
        where = "WHERE s.Estado = :estado"
        params["estado"] = estado

    rows = db.session.execute(text(f"""
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
    """), params).mappings().all()

    items = [{
        "Id": r["Id"],
        "Codigo_Reserva": r["Codigo_Reserva"],
        "Codigo_Cliente": r["Codigo_Cliente"],
        "Detalle": (r["Clave"] or "") + (": " + r["Valor"] if r["Valor"] else ""),
        "Estado": r["Estado"],
        "Fecha_modificacion": r["Fecha_modificacion"],
        "Cliente": (r["ClienteNombre"] or r["ClienteCorreo"] or str(r["Codigo_Cliente"] or "")) or "-",
    } for r in rows]
    return jsonify({"ok": True, "items": items})

@sac_bp.put("/solicitudes/<int:sid>", endpoint="solicitudes_update")
@role_required("Recepcionista", "Administrador")
def solicitudes_update(sid: int):
    data = request.get_json(silent=True) or {}
    nuevo = (data.get("Estado") or "").strip().upper()
    if nuevo not in ("NUEVA", "EN_PROCESO", "ATENDIDA"):
        return jsonify({"ok": False, "error": "Estado inválido"}), 400
    db.session.execute(text("UPDATE SAC_Solicitud SET Estado=:e WHERE Id=:id"),
                       {"e": nuevo, "id": sid})
    db.session.commit()
    return jsonify({"ok": True})

@sac_bp.get("/panel/solicitudes")
@role_required("Recepcionista", "Administrador")
def panel_solicitudes():
    return render_template("sac-panel-solicitudes.html")

# ====================== CONVERSACIONES ======================
@sac_bp.get("/conversaciones")
@role_required("Recepcionista", "Administrador")
def conversaciones_html():
    return render_template("sac-conversaciones.html")

@sac_bp.get("/conversaciones/data")
@role_required("Recepcionista", "Administrador")
def conversaciones_data():
    q = (request.args.get("q") or "").strip()
    days = int(request.args.get("days") or 30)
    since = datetime.utcnow() - timedelta(days=max(1, min(days, 365)))

    params = {"since": since}
    where = "WHERE c.Actualizada_At >= :since"
    if q:
        where += " AND (COALESCE(cl.Nombre,'') LIKE :q OR COALESCE(cl.Correo,'') LIKE :q OR COALESCE(cl.Telefono,'') LIKE :q)"
        params["q"] = f"%{q}%"

    rows = db.session.execute(text(f"""
        SELECT
          DATE_FORMAT(c.Actualizada_At,'%Y-%m-%d %H:%i') AS Fecha,
          COALESCE(cl.Nombre, CONCAT('Cliente ', c.Codigo_Cliente))        AS Cliente,
          'Web' AS Canal,
          SUBSTRING(
            (SELECT m.Texto FROM SAC_ConversationMsg m WHERE m.Conv_Id=c.Id ORDER BY m.Id DESC LIMIT 1),
            1, 120
          ) AS Asunto,
          CASE WHEN c.Abierta=1 THEN 'Abierta' ELSE 'Cerrada' END AS Estado
        FROM SAC_Conversation c
        LEFT JOIN Cliente cl ON cl.Codigo_Cliente=c.Codigo_Cliente
        {where}
        ORDER BY c.Actualizada_At DESC
        LIMIT 500
    """), params).mappings().all()

    return jsonify({"ok": True, "items": [dict(r) for r in rows]})

# ====================== INCIDENTES ======================
@sac_bp.get("/incidentes", endpoint="incidentes_html")
@role_required("Recepcionista", "Administrador")
def incidentes_html():
    return render_template("sac-incidentes.html")

@sac_bp.get("/incidentes/data")
@role_required("Recepcionista", "Administrador")
def incidentes_data():
    rows = db.session.execute(text("""
        SELECT i.Id, i.Codigo_Reserva, i.Codigo_Cliente, i.Tipo, i.Severidad,
               i.Titulo, i.Detalle, i.Estado,
               DATE_FORMAT(i.Creada_At,'%Y-%m-%d %H:%i') AS Fecha
        FROM SAC_Incident i
        ORDER BY i.Creada_At DESC
        LIMIT 300
    """)).mappings().all()
    return jsonify({"ok": True, "items": [dict(r) for r in rows]})

@sac_bp.post("/incidentes", endpoint="incidentes_new")
@role_required("Recepcionista", "Administrador")
def incidentes_new():
    data = request.get_json(silent=True) or request.form or {}
    try:
        rid = data.get("Codigo_Reserva")
        rid = int(rid) if rid not in (None,"","0") else None
    except Exception:
        rid = None

    try:
        cid = data.get("Codigo_Cliente")
        cid = int(cid) if cid not in (None,"","0") else None
    except Exception:
        cid = None

    if cid is None and rid is not None:
        r = db.session.execute(text("SELECT Codigo_Cliente FROM Reserva WHERE Codigo_Reserva=:r LIMIT 1"),
                               {"r": rid}).mappings().first()
        cid = int(r["Codigo_Cliente"]) if r and r.get("Codigo_Cliente") else None

    i = SACIncident(
        Codigo_Reserva=rid,
        Codigo_Cliente=cid,
        Tipo=(data.get("Tipo") or "INCIDENTE"),
        Severidad=(data.get("Severidad") or "MEDIA"),
        Titulo=(data.get("Titulo") or "Sin título"),
        Detalle=((data.get("Detalle") or "").strip() or None)
    )
    db.session.add(i); db.session.commit()
    return jsonify({"ok": True, "id": i.Id})

# ====================== INDICADORES ======================
@sac_bp.get("/indicadores", endpoint="indicadores_html")
@role_required("Administrador")
def indicadores_html():
    # Se renderiza vacío; el front hace fetch a /kpis y /flujo
    return render_template("sac-indicadores.html")

@sac_bp.get("/indicadores/kpis")
@role_required("Administrador")
def indicadores_kpis():
    k = db.session.execute(text("""
        SELECT 
          COUNT(*) AS tickets,
          SUM(CASE WHEN Estado='ATENDIDA' THEN 1 ELSE 0 END) AS atendidas
        FROM SAC_Solicitud
    """)).mappings().first() or {}
    nps = db.session.execute(text("SELECT AVG(NPS) AS nps FROM SAC_Feedback")).mappings().first() or {}
    return jsonify({"ok": True,
                    "tickets": int(k.get("tickets") or 0),
                    "atendidas": int(k.get("atendidas") or 0),
                    "nps": round(nps.get("nps") or 0, 2)})

@sac_bp.get("/indicadores/flujo")
@role_required("Administrador")
def indicadores_flujo():
    rows = db.session.execute(text("""
        SELECT
          DATE_FORMAT(Programado_At,'%Y-%m-%d %H:%i') AS Fecha,
          Canal, Estado,
          COALESCE(Ref_Entidad,'-') AS Ref_Entidad,
          COALESCE(Ref_Id,'-')      AS Ref_Id
        FROM SAC_Outbox
        ORDER BY Programado_At DESC
        LIMIT 200
    """)).mappings().all()
    return jsonify({"ok": True, "items": [dict(r) for r in rows]})
