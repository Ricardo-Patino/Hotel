# services/grr/notification_service.py
from __future__ import annotations

import os, re, smtplib, ssl, unicodedata
from email.message import EmailMessage
from datetime import datetime
from typing import Optional, Tuple, Dict, List, Any

from flask import current_app
from sqlalchemy import text
from extensions import db
from html import escape

# ============================================================
# Utilidades básicas
# ============================================================

_E164_RE = re.compile(r"^\+\d{8,15}$")

def _now() -> datetime:
    return datetime.utcnow()

def _fmt_currency(v, symbol: str = "₡") -> str:
    try:
        num = float(v)
        s = f"{symbol} {num:,.2f}"
        return s.replace(",", "X").replace(".", ",").replace("X", ".")
    except Exception:
        return str(v or "")

def _normalize_cr_phone(raw: Optional[str]) -> Optional[str]:
    if not raw:
        return None
    digits = re.sub(r"\D", "", raw)
    if not digits:
        return None
    if len(digits) == 8:
        return "+506" + digits
    if len(digits) == 11 and digits.startswith("506"):
        return "+" + digits
    if raw.startswith("+") and _E164_RE.match(raw):
        return raw
    maybe = "+" + digits
    return maybe if _E164_RE.match(maybe) else None

def _smtp_cfg() -> dict:
    cfg = (current_app.config if current_app else {}) or {}
    get = lambda k: cfg.get(k) or os.getenv(k)
    host = get("MAIL_SERVER") or os.getenv("SMTP_HOST")
    port = int((get("MAIL_PORT") or os.getenv("SMTP_PORT") or 587) or 587)
    user = get("MAIL_USERNAME") or os.getenv("SMTP_USER")
    pwd  = get("MAIL_PASSWORD") or os.getenv("SMTP_PASS")
    sender = get("MAIL_DEFAULT_SENDER") or os.getenv("SMTP_FROM") or user
    use_tls = str(get("MAIL_USE_TLS") or "1").lower() not in ("0", "false")
    use_ssl = str(get("MAIL_USE_SSL") or "0").lower() in ("1", "true")
    return {"host": host, "port": port, "user": user, "pwd": pwd, "sender": sender, "use_tls": use_tls, "use_ssl": use_ssl}

def _twilio_cfg() -> Tuple[Optional[str], Optional[str], Optional[str], Optional[str]]:
    # (ACCOUNT_SID, AUTH_TOKEN, FROM_NUMBER, MESSAGING_SERVICE_SID)
    return (
        os.getenv("TWILIO_ACCOUNT_SID"),
        os.getenv("TWILIO_AUTH_TOKEN"),
        (os.getenv("TWILIO_FROM") or None),
        (os.getenv("TWILIO_MESSAGING_SID") or None),
    )

# ============================================================
# Trial / GSM-7 helpers
# ============================================================

def _is_twilio_trial() -> bool:
    return str(os.getenv("TWILIO_IS_TRIAL", "0")).lower() in ("1", "true", "yes")

_GSM_REPLACEMENTS = str.maketrans({
    "á":"a","é":"e","í":"i","ó":"o","ú":"u","ü":"u","ñ":"n",
    "Á":"A","É":"E","Í":"I","Ó":"O","Ú":"U","Ü":"U","Ñ":"N",
    "’":"'", "‘":"'", "“":'"', "”":'"', "–":"-", "—":"-",
    "→":"->", "•":"-", "…":"..."
})

def _to_gsm7_approx(s: str) -> str:
    if not s:
        return s
    s = s.translate(_GSM_REPLACEMENTS)
    s = unicodedata.normalize("NFKD", s)
    s = "".join(ch for ch in s if not unicodedata.combining(ch))
    s = "".join(ch if 32 <= ord(ch) <= 126 else " " for ch in s)
    s = " ".join(s.split())
    return s.strip()

def _sms_limit_trial_len() -> int:
    # Conservador: el prefijo “Sent from your Twilio trial account - ” cuenta contra el segmento total.
    default_max = "80" if _is_twilio_trial() else "140"
    return int(os.getenv("TWILIO_TRIAL_MAX_SMS", default_max))

def _truncate_for_trial(s: str) -> str:
    MAX = _sms_limit_trial_len()
    if len(s) <= MAX:
        return s
    return s[:max(0, MAX - 3)] + "..."

def _tel_compact(t: str) -> str:
    return re.sub(r"[^\d+]", "", t or "")

# ============================================================
# Configuración en BD (SAC_Config)
# ============================================================

def _cfg(key: str, default: str = "") -> str:
    try:
        val = db.session.execute(
            text("SELECT Valor FROM SAC_Config WHERE Clave=:k LIMIT 1"),
            {"k": key}
        ).scalar()
        return (val if val not in (None, "") else default)
    except Exception:
        return default

# ============================================================
# Acceso a preferencias / datos de cliente
# ============================================================

def _get_pref(codigo_cliente: Optional[int]) -> Tuple[str, Optional[str], Optional[str]]:
    """
    Devuelve (canal, email_pref, tel_pref). canal ∈ {'email','sms','ambos'} o 'email' por defecto.
    """
    canal = "email"; email = None; tel = None
    if not codigo_cliente:
        return canal, email, tel

    try:
        row = db.session.execute(
            text("""
                SELECT Canal, Email, Telefono
                  FROM SAC_NotifPref
                 WHERE Codigo_Cliente = :c
                 LIMIT 1
            """),
            {"c": int(codigo_cliente)}
        ).mappings().first()
        if row:
            canal = (row.get("Canal") or "email").strip().lower()
            email = (row.get("Email") or None)
            tel   = (row.get("Telefono") or None)
            if canal not in ("email", "sms", "ambos"):
                canal = "email"
    except Exception as e:
        if current_app:
            current_app.logger.warning(f"[NOTIFY] _get_pref fallback por error: {type(e).__name__}: {e}")

    return canal, email, tel

def _get_cliente_contacto(codigo_cliente: Optional[int]) -> Tuple[Optional[str], Optional[str], Optional[str]]:
    """
    Devuelve (nombre, correo, telefono) desde Cliente si existe.
    """
    if not codigo_cliente:
        return None, None, None
    row = db.session.execute(
        text("""
            SELECT Nombre, Correo, Telefono
              FROM Cliente
             WHERE Codigo_Cliente = :c
             LIMIT 1
        """), {"c": int(codigo_cliente)}
    ).mappings().first()
    if not row:
        return None, None, None
    return row.get("Nombre"), row.get("Correo"), row.get("Telefono")

# ============================================================
# Envío bajo nivel
# ============================================================
_URL_RE = re.compile(r'(?P<url>https?://[^\s<>"\'\]\)]+[^\s<>"\'\]\)\.,;:!?\n\r])')

def _linkify_notif_html_text(text: str) -> str:
    text = text or ""
    out = []
    last = 0

    for m in _URL_RE.finditer(text):
        start, end = m.span()
        url = m.group("url")

        if start > last:
            out.append(escape(text[last:start]))

        safe_href = escape(url, quote=True)
        safe_label = escape(url)
        out.append(
            f'<a href="{safe_href}" '
            f'style="color:#1b7a4e; text-decoration:none; font-weight:700; word-break:break-all;">'
            f'{safe_label}</a>'
        )
        last = end

    if last < len(text):
        out.append(escape(text[last:]))

    return "".join(out)



def _notif_text_to_html(body: str) -> str:
    parts = []
    in_list = False

    for raw in (body or "").splitlines():
        line = raw.strip()

        if not line:
            if in_list:
                parts.append("</ul>")
                in_list = False
            parts.append('<div style="height:10px; line-height:10px;">&nbsp;</div>')
            continue

        is_bullet = line.startswith(("•", "-", "–", "—", "*"))

        if is_bullet:
            if not in_list:
                parts.append(
                    '<ul style="margin:0 0 16px; padding-left:20px; color:#334155; font-size:15px; line-height:1.7;">'
                )
                in_list = True

            item_html = _linkify_notif_html_text(line.lstrip("•-–—* ").strip())
            parts.append(f'<li style="margin:0 0 8px;">{item_html}</li>')
        else:
            if in_list:
                parts.append("</ul>")
                in_list = False

            line_html = _linkify_notif_html_text(line)
            parts.append(
                f'<p style="margin:0 0 14px; font-size:15px; line-height:1.7; color:#334155;">{line_html}</p>'
            )

    if in_list:
        parts.append("</ul>")

    return "".join(parts)


def _wrap_hotel_email_html(subject: str, body: str) -> str:
    hotel_name = _cfg("hotel_nombre", "Hotel Villa Grace")
    hotel_tel  = _cfg("hotel_tel", "+506 2642 0225")
    hotel_dir  = _cfg("hotel_dir", "100 m del Banco Nacional, Cóbano 60111")

    return f"""\
<!doctype html>
<html lang="es">
  <head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>{escape(subject)}</title>
  </head>
  <body style="margin:0; padding:0; background:#f4f7fb; font-family:Arial, Helvetica, sans-serif;">
    <table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" style="background:#f4f7fb;">
      <tr>
        <td align="center" style="padding:28px 16px;">
          <table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" style="max-width:680px;">
            <tr>
              <td style="background:linear-gradient(135deg, #1b7a4e 0%, #155c3b 100%); color:#ffffff; padding:28px 30px; border-radius:22px 22px 0 0;">
                <div style="font-size:28px; font-weight:800;">{hotel_name}</div>
                <div style="font-size:14px; opacity:.92; margin-top:6px;">Tu hogar fuera de casa</div>
              </td>
            </tr>
            <tr>
              <td style="background:#ffffff; padding:30px; border-radius:0 0 22px 22px; box-shadow:0 14px 45px rgba(15,23,42,.08);">
                <h1 style="margin:0 0 18px; font-size:28px; line-height:1.2; color:#0f172a;">{escape(subject)}</h1>
                <div style="background:#f8fafc; border:1px solid #e2e8f0; border-radius:18px; padding:22px;">
                  {_notif_text_to_html(body)}
                </div>
                <div style="margin-top:24px; padding-top:18px; border-top:1px solid #e5e7eb; font-size:14px; line-height:1.7; color:#64748b;">
                  <strong style="color:#334155;">{hotel_name}</strong><br>
                  {hotel_dir}<br>
                  Tel: {hotel_tel}
                </div>
              </td>
            </tr>
          </table>
        </td>
      </tr>
    </table>
  </body>
</html>
"""


def _send_email_smtp(to_email: str, subject: str, body: str, html_body: Optional[str] = None) -> None:
    cfg = _smtp_cfg()
    host, port, user, pwd = cfg["host"], cfg["port"], cfg["user"], cfg["pwd"]
    if not (host and user and pwd):
        raise RuntimeError("SMTP not configured")

    sender = cfg["sender"] or user or "no-reply@hotel.local"

    msg = EmailMessage()
    msg["From"] = sender
    msg["To"] = to_email
    msg["Subject"] = subject
    msg.set_content(body)
    msg.add_alternative(html_body or _wrap_hotel_email_html(subject, body), subtype="html")

    if cfg["use_ssl"]:
        context = ssl.create_default_context()
        with smtplib.SMTP_SSL(host, port, context=context, timeout=30) as s:
            s.login(user, pwd)
            s.send_message(msg)
    else:
        with smtplib.SMTP(host, port, timeout=30) as s:
            if cfg["use_tls"]:
                s.starttls(context=ssl.create_default_context())
            s.login(user, pwd)
            s.send_message(msg)

def _send_sms_twilio(to_phone: str, body: str) -> str:
    sid, token, from_phone, messaging_sid = _twilio_cfg()
    if not (sid and token and (from_phone or messaging_sid)):
        raise RuntimeError("Twilio not configured")
    try:
        from twilio.rest import Client  # type: ignore
        from twilio.base.exceptions import TwilioRestException  # type: ignore
    except Exception:
        raise RuntimeError("Twilio SDK no instalado. pip install twilio")

    client = Client(sid, token)

    sms = (body or "").replace("\n", " ")
    sms = _to_gsm7_approx(sms)
    sms = _truncate_for_trial(sms)

    kwargs: Dict[str, Any] = {"to": to_phone, "body": sms}
    cb = os.getenv("TWILIO_STATUS_CALLBACK")
    if cb:
        kwargs["status_callback"] = cb
    if messaging_sid:
        kwargs["messaging_service_sid"] = messaging_sid
    else:
        kwargs["from_"] = from_phone

    try:
        msg = client.messages.create(**kwargs)
        if current_app:
            current_app.logger.info(f"[SMS] Twilio sid={msg.sid} len={len(sms)}")
        return msg.sid
    except TwilioRestException as e:
        if "30044" in str(e):
            brand = os.getenv("SMS_BRAND_SHORT", "VG")
            mini = f"{brand} confirmado"
            mini = _to_gsm7_approx(mini)[:20]
            kwargs["body"] = mini
            msg = client.messages.create(**kwargs)
            if current_app:
                current_app.logger.warning(f"[SMS-RETRY-30044] sid={msg.sid} body='{mini}'")
            return msg.sid
        raise

# ============================================================
# Mensajes de reserva (detalle email + SMS compacto)
# ============================================================

def _fetch_room_features(hid: Optional[int]) -> List[str]:
    if not hid:
        return []
    candidates: List[str] = []
    attempts = [
        ("""
            SELECT c.Nombre
              FROM HabitacionCaracteristica hc
              JOIN Caracteristica c ON c.Id = hc.Caracteristica_Id
             WHERE hc.Habitacion_Id = :hid
             ORDER BY c.Nombre
        """, "Nombre"),
        ("""
            SELECT f.Nombre
              FROM Habitacion_Feature hf
              JOIN Feature f ON f.Id = hf.Feature_Id
             WHERE hf.Habitacion_Id = :hid
             ORDER BY f.Nombre
        """, "Nombre"),
        ("""
            SELECT a.Nombre
              FROM HabitacionAtributo ha
              JOIN Atributo a ON a.Id = ha.Atributo_Id
             WHERE ha.Habitacion_Id = :hid
             ORDER BY a.Nombre
        """, "Nombre"),
    ]
    for sql, col in attempts:
        try:
            rows = db.session.execute(text(sql), {"hid": int(hid)}).mappings().all()
            if rows:
                candidates.extend([str(r[col]) for r in rows if r.get(col)])
                break
        except Exception:
            pass
    if not candidates:
        try:
            row = db.session.execute(
                text("""
                    SELECT 
                        COALESCE(Descripcion,'') AS Descripcion,
                        COALESCE(Caracteristicas,'') AS Caracteristicas,
                        COALESCE(Amenities,'') AS Amenities
                    FROM Habitacion
                    WHERE Codigo_Habitacion = :hid
                    LIMIT 1
                """), {"hid": int(hid)}
            ).mappings().first()
            if row:
                blob = " | ".join([row.get("Descripcion",""), row.get("Caracteristicas",""), row.get("Amenities","")])
                parts = [p.strip() for p in re.split(r"[,\n;|/•\-]+", blob) if p.strip()]
                candidates.extend(parts)
        except Exception:
            pass
    norm = []
    seen = set()
    for x in candidates:
        y = " ".join(x.split())
        if not y:
            continue
        key = y.lower()
        if key not in seen:
            seen.add(key); norm.append(y)
    return norm[:12]

def _compose_reserva_messages(payload: Dict) -> Tuple[str, str, str]:
    """
    Devuelve (subject, email_body, sms).
    - Email: detallado.
    - SMS: SOLO GSM-7, compacto y recortado (Trial seguro).
    """
    rid   = payload.get("rid")
    hid   = payload.get("hid")
    hab   = payload.get("habitacion") or str(hid or "")
    tipo  = payload.get("tipo_habitacion") or ""
    f_in  = str(payload.get("f_entrada") or "")
    f_out = str(payload.get("f_salida") or "")
    total = payload.get("total")
    estado= payload.get("estado") or ""
    nombre= payload.get("cliente_nombre") or "Estimado/a"

    checkin_ini = _cfg("checkin_inicio",  "12:00")
    checkin_fin = _cfg("checkin_fin",     "00:00")
    checkout    = _cfg("checkout_limite", "12:00")

    hotel_nom   = _cfg("hotel_nombre", "Hotel Villa Grace")
    hotel_tel   = _cfg("hotel_tel",    "+506 2642 0225")
    hotel_dir   = _cfg("hotel_dir",    "100 m del Banco Nacional, Cóbano 60111")
    base_url    = _cfg("site_base_url","https://hotelvillagrace.test")
    moneda_sym  = _cfg("moneda_simbolo","₡")

    pol_checkin = _cfg("politicas_checkin",     "Presentar documento de identidad al ingreso.")
    pol_cancel  = _cfg("politicas_cancelacion", "Cancelación hasta 48h antes sin costo.")
    pol_mascotas= _cfg("politicas_mascotas",    "No se permiten mascotas (salvo asistencia).")
    wifi_info   = _cfg("wifi_info",             "Wi-Fi gratuito en todo el hotel.")
    parking     = _cfg("parking_info",          "Parqueo gratuito para huéspedes.")

    features    = _fetch_room_features(hid)
    total_txt   = _fmt_currency(total, symbol=moneda_sym) if total is not None else ""

    subject = f"Confirmación de reserva #{rid} – {hotel_nom}"
    lines: List[str] = [
        f"{nombre},", "",
        f"Gracias por reservar en {hotel_nom}. Detalles:",
        f"• Nº reserva: #{rid}",
        f"• Habitación: {hab}" + (f" ({tipo})" if tipo else ""),
        f"• Entrada: {f_in}  (check-in {checkin_ini}–{checkin_fin})",
        f"• Salida : {f_out} (check-out hasta {checkout})",
        (f"• Total  : {total_txt}" if total_txt else None),
        (f"• Estado : {estado}" if estado else None), ""
    ]
    if features:
        lines.append("Características / amenities:")
        for f in features[:12]:
            lines.append(f"  – {f}")
        lines.append("")
    lines.extend([
        "Información útil:",
        f"  – {wifi_info}",
        f"  – {parking}",
        f"  – Políticas check-in: {pol_checkin}",
        f"  – Políticas cancelación: {pol_cancel}",
        f"  – Políticas mascotas: {pol_mascotas}", "",
        f"Dirección: {hotel_dir}",
        f"Teléfono: {hotel_tel}", "",
        f"Gestiona tu reserva: {base_url}/portal/reservas", "",
        f"{hotel_nom} — \"Tu hogar fuera de casa\"."
    ])
    email_body = "\n".join([x for x in lines if x is not None])

    brand = os.getenv("SMS_BRAND_SHORT", "VG")
    tel_c = _tel_compact(hotel_tel)
    sms_raw = f"{brand} Res#{rid} {hab}{(' '+tipo) if tipo else ''} {f_in}->{f_out} CI {checkin_ini}-{checkin_fin} CO {checkout} Tel {tel_c}"
    sms_gsm = _to_gsm7_approx(sms_raw)
    sms     = _truncate_for_trial(sms_gsm)
    return subject, email_body, sms

def _fetch_reserva_payload(reserva_id: int) -> Dict:
    row = db.session.execute(
        text("""
            SELECT 
                r.Codigo_Reserva         AS rid,
                r.Codigo_Cliente         AS cid,
                r.Codigo_Habitacion      AS hid,
                r.Fecha_Entrada          AS f_entrada,
                r.Fecha_Salida           AS f_salida,
                r.Monto_Total            AS total,
                r.Estado                 AS estado,
                h.Numero_Habitacion      AS habitacion,
                h.Tipo                   AS tipo_habitacion,
                h.Precio_Noche           AS precio_noche,
                c.Nombre                 AS cliente_nombre,
                c.Correo                 AS cliente_email,
                c.Telefono               AS cliente_tel
            FROM Reserva r
            LEFT JOIN Habitacion h ON h.Codigo_Habitacion = r.Codigo_Habitacion
            LEFT JOIN Cliente    c ON c.Codigo_Cliente    = r.Codigo_Cliente
            WHERE r.Codigo_Reserva = :rid
            LIMIT 1
        """), {"rid": int(reserva_id)}
    ).mappings().first()
    return dict(row) if row else {}

# ============================================================
# Servicio público
# ============================================================

class NotificationService:
    """
    Enrutador de notificaciones:
    - route_and_queue: genérico (usa preferencias del cliente si hay).
    - send_confirmation: plantilla simple (compatibilidad).
    - send_reserva_details / send_reserva_confirmation: confirmación con payload de reserva.
    """

    def __init__(self, *_, **__):
        self.db = db  # compatibilidad

    @staticmethod
    def _resolve_kwargs(kwargs: Dict) -> Dict:
        return {
            "cliente_id": kwargs.get("cliente_id") or kwargs.get("codigo_cliente") or kwargs.get("Codigo_Cliente"),
            "email": kwargs.get("email") or kwargs.get("to_email") or kwargs.get("email_fallback") or kwargs.get("correo"),
            "phone": kwargs.get("phone") or kwargs.get("to_phone") or kwargs.get("tel_fallback") or kwargs.get("telefono"),
            "subject": kwargs.get("subject") or kwargs.get("asunto"),
            "body": kwargs.get("body") or kwargs.get("message") or kwargs.get("cuerpo"),
            "html": kwargs.get("html") or kwargs.get("html_body") or kwargs.get("cuerpo_html"),
            "sms": kwargs.get("sms") or kwargs.get("sms_body"),
            "ref_entidad": kwargs.get("ref_entidad") or kwargs.get("ref_tipo") or "SAC",
            "ref_id": kwargs.get("ref_id") or kwargs.get("refid") or "-",
        }

    def route_and_queue(self, **kwargs) -> Dict[str, object]:
        """
        Envía inmediatamente por e-mail y/o SMS según preferencia del cliente.
        Retorna dict: {ok, canal, email, tel, sms_sid}.
        """
        p = self._resolve_kwargs(kwargs)
        cid = p["cliente_id"]
        subject = (p["subject"] or "Notificación Hotel Villa Grace")
        body    = (p["body"] or "Gracias por contactarnos.")

        # Preferencias del cliente
        canal_pref, email_pref, tel_pref = _get_pref(cid)

        # Contactos finales: preferencia > fallback argumento > Cliente
        _, email_cli, tel_cli = _get_cliente_contacto(cid)
        to_email = (email_pref or p["email"] or email_cli or None)
        raw_tel  = (tel_pref   or p["phone"] or tel_cli or None)
        to_phone = _normalize_cr_phone(raw_tel)

        sent_email = False
        sent_sms   = False
        last_sid   = None

        # Email
        if canal_pref in ("email", "ambos"):
            if to_email:
                try:
                    html_body = p.get("html") or _wrap_hotel_email_html(subject, body)
                    _send_email_smtp(to_email, subject, body, html_body)
                    sent_email = True
                except Exception as e:
                    if current_app:
                        current_app.logger.warning(f"[NOTIFY] Email fallo -> {type(e).__name__}: {e}")
            else:
                if current_app:
                    current_app.logger.warning("[NOTIFY] Email omitido: no hay correo destino")

        # SMS
        if canal_pref in ("sms", "ambos"):
            if to_phone:
                try:
                    sms_body = p.get("sms") or body or subject
                    sms_body = sms_body.replace("\n", " ")
                    sms_body = _to_gsm7_approx(sms_body)
                    sms_body = _truncate_for_trial(sms_body)
                    last_sid = _send_sms_twilio(to_phone, sms_body)
                    sent_sms = True
                except Exception as e:
                    if current_app:
                        current_app.logger.warning(f"[NOTIFY] SMS fallo -> {type(e).__name__}: {e}")
            else:
                if current_app:
                    current_app.logger.warning("[NOTIFY] SMS omitido: teléfono inválido o ausente")

        ok = bool(sent_email or sent_sms)
        if current_app:
            current_app.logger.info(
                f"[NOTIFY] canal={canal_pref} email={to_email} sms={to_phone} "
                f"ok_email={sent_email} ok_sms={sent_sms} sid={last_sid}"
            )
        return {"ok": ok, "canal": canal_pref, "email": to_email, "tel": to_phone, "sms_sid": last_sid}

    # Plantilla simple para compatibilidad con ReservationService
    def send_confirmation(self, correo: str | None, telefono: str | None, numero: str) -> None:
        subject = f"Confirmación de reserva {numero} – Hotel Villa Grace"
        body = (
            f"¡Gracias por su reserva {numero} en Hotel Villa Grace!\n"
            f"Ante cualquier consulta, responda a este mensaje."
        )
        self.route_and_queue(email=correo, phone=telefono, subject=subject, body=body)

    # Conveniencia: enviar detalles de reserva por canales (respeta preferencias)
    def send_reserva_details(self, reserva_id: int) -> Dict[str, object]:
        payload = _fetch_reserva_payload(reserva_id)
        if not payload:
            return {"ok": False, "error": "reserva_not_found"}

        subject, email_body, sms_body = _compose_reserva_messages(payload)
        cid = payload.get("cid")
        return self.route_and_queue(
            cliente_id=cid,
            subject=subject,
            body=email_body,
            sms=sms_body,
            ref_entidad="Reserva",
            ref_id=str(reserva_id),
        )
        
        
    def send_reserva_pending(self, reserva_id: int) -> Dict[str, object]:
        """
        Notificación: Reserva creada pero pendiente de confirmación (SINPE).
        Respeta preferencias del cliente (/sac/preferencias) vía route_and_queue.
        """
        payload = _fetch_reserva_payload(int(reserva_id))
        if not payload:
            return {"ok": False, "error": "not_found"}
    
        rid   = payload.get("rid")
        cid   = payload.get("cid")
        f_in  = str(payload.get("f_entrada") or "")
        f_out = str(payload.get("f_salida") or "")
        total = payload.get("total")
    
        hotel_nom  = _cfg("hotel_nombre", "Hotel Villa Grace")
        hotel_tel  = _cfg("hotel_tel", "+506 2642 0225")
        base_url   = _cfg("site_base_url", "https://hotelvillagrace.test")
        moneda_sym = _cfg("moneda_simbolo", "₡")
    
        # Config SINPE (si no existen, el email igual sale sin ese detalle)
        sinpe_num = _cfg("sinpe_mobile", "")
        sinpe_ben = _cfg("sinpe_beneficiary", hotel_nom)
    
        total_txt = _fmt_currency(total, symbol=moneda_sym) if total is not None else ""
    
        subject = f"Reserva pendiente de confirmación #{rid} – {hotel_nom}"
    
        lines = [
            (payload.get("cliente_nombre") or "Estimado/a") + ",",
            "",
            "Hemos registrado tu solicitud de reserva, pero está pendiente de confirmación.",
            "La confirmación se realizará cuando el hotel valide el pago por SINPE.",
            "",
            f"• Nº reserva: #{rid}",
            f"• Entrada: {f_in}",
            f"• Salida : {f_out}",
            (f"• Total  : {total_txt}" if total_txt else None),
            "",
        ]
        if sinpe_num:
            lines.extend([
                "Pago por SINPE Móvil:",
                f"• Número: {sinpe_num}",
                f"• Beneficiario: {sinpe_ben}",
                "",
                "Recomendación: en el detalle del SINPE indica tu correo y fechas para facilitar la verificación.",
                "",
            ])
    
        lines.extend([
            f"Teléfono: {hotel_tel}",
            f"Portal del huésped: {base_url}/portal/reservas",
            "",
            f"{hotel_nom} — \"Tu hogar fuera de casa\".",
        ])
    
        body = "\n".join([x for x in lines if x is not None])
    
        # SMS compacto (GSM-7)
        sms_raw = f"VG Reserva #{rid} PENDIENTE. {f_in}->{f_out}. Total {total_txt}. Se confirma al validar SINPE. Tel {hotel_tel}"
        sms = _truncate_for_trial(_to_gsm7_approx(sms_raw))
    
        return self.route_and_queue(
            cliente_id=cid,
            email=payload.get("cliente_email"),
            phone=payload.get("cliente_tel"),
            subject=subject,
            body=body,
            sms=sms,
            ref_entidad="RESERVA",
            ref_id=str(rid),
        )
    

    # *** Alias de compatibilidad esperado por app.py ***
    # app.py invoca ns.send_reserva_confirmation(rid); mapeamos a send_reserva_details.
    def send_reserva_confirmation(self, reserva_id: int) -> Dict[str, object]:
        return self.send_reserva_details(reserva_id)
