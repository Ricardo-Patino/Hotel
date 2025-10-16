# app.py — Aplicación principal Flask para Hotel_VillaGrace
# Ejecuta:  python "Hotel 2/app.py"

import os
import sys
import smtplib
import ssl
import json
from functools import wraps
from email.message import EmailMessage
from datetime import datetime, date
from pathlib import Path
from typing import Optional
from werkzeug.utils import secure_filename
import reportlab

from sqlalchemy import text, func, inspect
from sqlalchemy.exc import IntegrityError
from itsdangerous import URLSafeTimedSerializer, BadSignature, SignatureExpired

from flask import (
    Flask,
    render_template,
    jsonify,
    request,
    redirect,
    url_for,
    flash,
    session,
    current_app,
    send_file,
    abort,
)

# ---------------------------------------------------------------------------
# Bootstrap de ruta para imports absolutos (extensions, config, blueprints)
# ---------------------------------------------------------------------------
BASE_DIR = Path(__file__).resolve().parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

# .env opcional
try:
    from dotenv import load_dotenv  # type: ignore
    load_dotenv(BASE_DIR / ".env")
except Exception:
    pass

# Imports del proyecto
from services.grr.assignment import auto_assign_for_reserva, reassign_reserva, split_reserva, merge_reserva
from config import Config
from extensions import db, migrate
from models_sql import Usuario, Rol, Habitacion

try:
    from models_sql import Reserva as ReservaModel  # si existiera
except Exception:
    ReservaModel = None

# =========================
# Constantes / Paths
# =========================
STORAGE_DIR = BASE_DIR / "storage"
COMPROBANTES_DIR = STORAGE_DIR / "comprobantes"
COMPROBANTES_DIR.mkdir(parents=True, exist_ok=True)

RECIBOS_DIR = STORAGE_DIR / "recibos"
NOTAS_DIR = STORAGE_DIR / "notas"
RECIBOS_DIR.mkdir(parents=True, exist_ok=True)
NOTAS_DIR.mkdir(parents=True, exist_ok=True)

# GRR-01-004: documentos de huésped (ID/firma)
GUEST_DOCS_DIR = STORAGE_DIR / "guest_docs"
GUEST_DOCS_DIR.mkdir(parents=True, exist_ok=True)
ALLOWED_DOC_EXTS = {"png", "jpg", "jpeg", "pdf"}

# =========================
# Helpers (roles/redirects)
# =========================
DEFAULT_ROLES = ("Administrador", "Recepcionista", "Limpieza", "Cliente")


def _ensure_seed_roles() -> None:
    try:
        existing = {r.Nombre for r in Rol.query.all()}
        for name in DEFAULT_ROLES:
            if name not in existing:
                db.session.add(
                    Rol(Nombre=name, Descripcion=f"Rol {name}", Estado="Activo")
                )
        db.session.commit()
    except Exception:
        db.session.rollback()


def _get_role_by_name(name: str):
    if not name:
        return None
    return Rol.query.filter_by(Nombre=name).first()


def _get_role_name(user: Usuario) -> str:
    try:
        if getattr(user, "rol", None) and getattr(user.rol, "Nombre", None):
            return user.rol.Nombre
    except Exception:
        pass
    try:
        if getattr(user, "Rol_Id", None):
            rol = Rol.query.filter_by(Codigo_Rol=user.Rol_Id).first()
            if rol and rol.Nombre:
                return rol.Nombre
    except Exception:
        pass
    return "Cliente"


def role_redirect_endpoint(role_name: str) -> str:
    mapping = {
        "Administrador": "admin_dashboard_html",
        "Recepcionista": "ops_dashboard_html",
        "Limpieza": "ops_housekeeping_html",
        "Cliente": "portal_dashboard_html",
    }
    endpoint = mapping.get(role_name, "index_html")
    if current_app and endpoint not in current_app.view_functions:
        return "index_html"
    return endpoint


# =========================
# Decoradores de acceso
# =========================
def _user_role() -> str:
    try:
        return (session.get("user_role") or "").strip() or "Cliente"
    except Exception:
        return "Cliente"


def login_required(fn):
    @wraps(fn)
    def wrapper(*args, **kwargs):
        if not session.get("user_id"):
            flash("Inicia sesión para continuar.", "warning")
            return redirect(url_for("login_html", next=request.path))
        return fn(*args, **kwargs)
    return wrapper


def role_required(*roles):
    roles_norm = {r.lower() for r in roles if r}

    def decorator(fn):
        @wraps(fn)
        def wrapper(*args, **kwargs):
            if not session.get("user_id"):
                flash("Inicia sesión para continuar.", "warning")
                return redirect(url_for("login_html", next=request.path))
            current = _user_role().lower()
            if current not in roles_norm:
                if request.path.startswith("/api/"):
                    return jsonify({"ok": False, "error": "forbidden"}), 403
                flash("No tienes permiso para acceder a esta sección.", "danger")
                return redirect(url_for(role_redirect_endpoint(_user_role())))
            return fn(*args, **kwargs)
        return wrapper
    return decorator


# =========================
# Reset Password
# =========================
def _get_serializer(app: Flask) -> URLSafeTimedSerializer:
    return URLSafeTimedSerializer(app.config["SECRET_KEY"], salt="pwd-reset")


def _send_reset_email(app: Flask, to_email: str, reset_url: str) -> None:
    sender = (
        app.config.get("MAIL_DEFAULT_SENDER")
        or app.config.get("MAIL_USERNAME")
        or "no-reply@hotel.local"
    )
    subject = "Restablecimiento de contraseña — Hotel Villa Grace"
    body = (
        "Hola,\n\n"
        "Recibimos una solicitud para restablecer tu contraseña en Hotel Villa Grace.\n"
        f"Para continuar, abre este enlace:\n\n{reset_url}\n\n"
        "Si no fuiste tú, ignora este mensaje. El enlace expira en 1 hora.\n\n"
        "Atentamente,\nHotel Villa Grace"
    )

    host = app.config.get("MAIL_SERVER")
    port = int(app.config.get("MAIL_PORT", 0) or 0)
    user = app.config.get("MAIL_USERNAME")
    pwd = app.config.get("MAIL_PASSWORD")
    use_tls = bool(app.config.get("MAIL_USE_TLS", False))
    use_ssl = bool(app.config.get("MAIL_USE_SSL", False))

    if not (host and port and user and pwd):
        app.logger.warning("[MAIL] Config SMTP incompleta; usando consola.")
        app.logger.info(f"[RESET LINK] Para {to_email}: {reset_url}")
        return

    if not sender or ("@" not in sender):
        sender = user

    try:
        msg = EmailMessage()
        msg["Subject"] = subject
        msg["From"] = sender
        msg["To"] = to_email
        msg.set_content(body)

        if use_ssl:
            context = ssl.create_default_context()
            with smtplib.SMTP_SSL(host, port, context=context, timeout=30) as smtp:
                smtp.login(user, pwd)
                smtp.send_message(msg)
        else:
            with smtplib.SMTP(host, port, timeout=30) as smtp:
                if use_tls:
                    smtp.starttls(context=ssl.create_default_context())
                smtp.login(user, pwd)
                smtp.send_message(msg)

        app.logger.info(
            f"[MAIL SENT] Reset a {to_email} vía {host}:{port} (TLS={use_tls}, SSL={use_ssl})"
        )
    except Exception as e:
        app.logger.error(f"[MAIL ERROR] {type(e).__name__}: {e}")
        app.logger.info(f"[RESET LINK] Para {to_email}: {reset_url}")


# =========================
# Utilidades para reservas
# =========================
def _current_user_email() -> Optional[str]:
    try:
        uid = session.get("user_id")
        if not uid:
            return None
        u = Usuario.query.filter_by(Codigo_Usuario=uid).first()
        return (u.Correo or "").strip().lower() if u else None
    except Exception:
        return None


def current_cliente_id() -> Optional[int]:
    try:
        uid = session.get("user_id")
        if not uid:
            return None
        u = Usuario.query.filter_by(Codigo_Usuario=uid).first()
        return int(u.Codigo_Cliente) if u and u.Codigo_Cliente else None
    except Exception:
        return None


def _get_first_attr(obj, names: list[str]):
    if obj is None:
        return None
    for n in names:
        if hasattr(obj, n):
            return getattr(obj, n)
    try:
        for n in names:
            if isinstance(obj, dict) and n in obj:
                return obj[n]
    except Exception:
        pass
    return None


def _normalize_date_like(v):
    if v is None:
        return None
    try:
        if hasattr(v, "strftime"):
            return v.strftime("%Y-%m-%d")
        s = str(v)
        return s[:10]
    except Exception:
        return str(v)


def _reserva_to_dict(r) -> dict:
    id_val = _get_first_attr(r, ["Codigo_Reserva", "id", "ID", "Reserva_Id"])
    numero = _get_first_attr(r, ["Numero", "numero", "Codigo", "Numero_Comprobante"])
    estado = _get_first_attr(r, ["Estado", "Estatus", "Status"]) or "Confirmada"
    tipo = _get_first_attr(r, ["Tipo", "Habitacion", "RoomType"])
    plan = _get_first_attr(r, ["Plan", "Tarifa", "RatePlan"]) or "Tarifa Flexible"
    canal = _get_first_attr(r, ["Canal", "Source", "Origen"]) or "Web"

    huesp = _get_first_attr(r, ["Huespedes", "Pax", "Huespedes_Count"])
    obs = _get_first_attr(r, ["Observaciones", "Notas", "Comentarios"]) or ""
    usuario = _get_first_attr(
        r, ["Usuario", "Email", "Correo", "Cliente_Email", "email"]
    )

    ci = _normalize_date_like(
        _get_first_attr(r, ["Fecha_Entrada", "Checkin", "CheckIn", "Inicio", "Desde"])
    )
    co = _normalize_date_like(
        _get_first_attr(r, ["Fecha_Salida", "Checkout", "CheckOut", "Fin", "Hasta"])
    )

    monto = _get_first_attr(r, ["Monto_Total", "Total", "Importe", "Total_Monto"]) or 0
    price = _get_first_attr(r, ["Precio_Noche", "Tarifa_Noche", "PriceNight"]) or 0

    created = _get_first_attr(
        r, ["Fecha_Creacion", "Created_At", "Creado", "Fecha_Registro"]
    )
    updated = _get_first_attr(
        r, ["Fecha_Modificacion", "Updated_At", "Modificado", "Fecha_Registro"]
    )

    return {
        "id": id_val,
        "numero": numero or (f"VG-{id_val}" if id_val else None),
        "estado": estado,
        "tipo": tipo,
        "plan": plan,
        "canal": canal,
        "huespedes": str(huesp or ""),
        "observaciones": obs,
        "usuario": usuario or "",
        "checkin": ci,
        "checkout": co,
        "monto": float(monto or 0),
        "precio_noche": float(price or 0),
        "created_at": (
            created.isoformat()
            if hasattr(created, "isoformat")
            else (str(created) if created else None)
        ),
        "updated_at": (
            updated.isoformat()
            if hasattr(updated, "isoformat")
            else (str(updated) if updated else None)
        ),
    }


def _query_user_reservas(
    email: str,
    estado: Optional[str] = None,
    f_ini: Optional[str] = None,
    f_fin: Optional[str] = None,
):
    if not email:
        return []
    params = {"email": (email or "").strip().lower()}
    conds = [
        """
        (
          (C.Correo IS NOT NULL AND LOWER(C.Correo) = :email)
          OR R.Codigo_Cliente IN (
                SELECT COALESCE(U.Codigo_Cliente, -1)
                  FROM Usuario U
                 WHERE LOWER(U.Correo) = :email
             )
        )
    """
    ]
    if estado:
        conds.append("R.Estado = :estado")
        params["estado"] = estado
    if f_ini:
        conds.append("R.Fecha_Entrada >= :fini")
        params["fini"] = f_ini
    if f_fin:
        conds.append("R.Fecha_Salida <= :ffin")
        params["ffin"] = f_fin
    where = " AND ".join(conds)
    stmt = text(
        f"""
        SELECT
          R.Codigo_Reserva,
          R.Numero_Comprobante AS Numero,
          R.Estado,
          R.Canal,
          R.Fecha_Entrada,
          R.Fecha_Salida,
          R.Monto_Total,
          R.Observaciones,
          R.Huespedes,
          H.Precio_Noche,
          H.Tipo,
          R.Fecha_Registro      AS Fecha_Creacion,
          R.Fecha_Registro      AS Fecha_Modificacion,
          C.Correo              AS Usuario
        FROM Reserva R
        JOIN Cliente    C ON C.Codigo_Cliente    = R.Codigo_Cliente
        JOIN Habitacion H ON H.Codigo_Habitacion = R.Codigo_Habitacion
        WHERE {where}
        ORDER BY R.Fecha_Entrada DESC
    """
    )
    rows = db.session.execute(stmt, params).mappings().all()
    return [_reserva_to_dict(r) for r in rows]


def _get_reserva_by_id(reserva_id: int):
    row = (
        db.session.execute(
            text(
                """
        SELECT
          R.Codigo_Reserva,
          R.Numero_Comprobante AS Numero,
          R.Estado,
          R.Canal,
          R.Fecha_Entrada,
          R.Fecha_Salida,
          R.Monto_Total,
          R.Observaciones,
          R.Huespedes,
          H.Precio_Noche,
          H.Tipo,
          R.Codigo_Cliente,
          R.Codigo_Habitacion,
          R.Codigo_Funcionario,
          R.Fecha_Registro      AS Fecha_Creacion,
          R.Fecha_Registro      AS Fecha_Modificacion,
          C.Correo              AS Usuario
        FROM Reserva R
        JOIN Cliente    C ON C.Codigo_Cliente    = R.Codigo_Cliente
        JOIN Habitacion H ON H.Codigo_Habitacion = R.Codigo_Habitacion
        WHERE R.Codigo_Reserva = :id
        LIMIT 1
    """
            ),
            {"id": reserva_id},
        )
        .mappings()
        .first()
    )
    return row


def _reserva_belongs_to_email(reserva_id: int, email: str) -> bool:
    if not email:
        return False
    row = db.session.execute(
        text(
            """
        SELECT 1
          FROM Reserva R
          JOIN Cliente C ON C.Codigo_Cliente = R.Codigo_Cliente
         WHERE R.Codigo_Reserva = :id
           AND (
                 (C.Correo IS NOT NULL AND LOWER(C.Correo) = :email)
                 OR R.Codigo_Cliente IN (
                        SELECT COALESCE(U.Codigo_Cliente, -1)
                          FROM Usuario U
                         WHERE LOWER(U.Correo) = :email
                    )
               )
         LIMIT 1
    """
        ),
        {"id": reserva_id, "email": (email or "").strip().lower()},
    ).first()
    return bool(row)


def _validate_no_overbooking(ci: str, co: str, rooms: int = 1) -> tuple[bool, str]:
    with current_app.test_client() as c:
        resp = c.get(url_for("api_availability", checkin=ci, checkout=co, rooms=rooms))
        data = resp.get_json() if resp.is_json else {}
    if not data or not data.get("ok"):
        return False, "No se pudo validar disponibilidad."
    if not data.get("available"):
        return False, data.get("message") or "Sin cupo para ese rango."
    return True, ""


def ensure_cliente_for_email(
    nombre: str, correo: str, telefono: Optional[str] = None
) -> Optional[int]:
    if not correo:
        return None
    correo = correo.strip().lower()
    row = db.session.execute(
        text("SELECT Codigo_Cliente FROM Cliente WHERE LOWER(Correo)=:e LIMIT 1"),
        {"e": correo},
    ).first()
    if row:
        return row[0]
    nombre = (nombre or "").strip() or "Cliente Web"
    partes = nombre.split(" ", 1)
    nom = partes[0][:50]
    ape = (partes[1] if len(partes) > 1 else "").strip()[:50]
    tel = (telefono or "").strip()[:20] or "00000000"

    db.session.execute(
        text(
            """
        INSERT INTO Cliente (Cedula, Nombre, Apellido, Telefono, Correo, Fecha_Nacimiento)
        VALUES (0, :n, :a, :t, :e, '1990-01-01')
    """
        ),
        {"n": nom, "a": ape, "t": tel, "e": correo},
    )
    db.session.commit()

    row2 = db.session.execute(
        text("SELECT Codigo_Cliente FROM Cliente WHERE LOWER(Correo)=:e LIMIT 1"),
        {"e": correo},
    ).first()
    return row2[0] if row2 else None


# =========================
# PDF / KPIs / Auditoría (GRR-01-002)
# =========================

def _make_unique_number(reserva_id: int, fecha_entrada: str) -> str:
    """VG-YYYYMMDD-XXXX"""
    ymd = (
        fecha_entrada.replace("-", "")[:8]
        if fecha_entrada
        else datetime.utcnow().strftime("%Y%m%d")
    )
    return f"VG-{ymd}-{int(reserva_id):04d}"


def _update_kpis(monto: float, fecha_entrada: str):
    """
    Upsert en KPI_Stats para day/week/month.
    Clave day:  YYYY-MM-DD
          week: ISO 'YYYY-Www'
          month: YYYY-MM
    """
    try:
        dt = datetime.strptime(fecha_entrada[:10], "%Y-%m-%d")
    except Exception:
        dt = datetime.utcnow()
    key_day = dt.strftime("%Y-%m-%d")
    key_mon = dt.strftime("%Y-%m")
    isoy, isow, _ = dt.isocalendar()
    key_week = f"{isoy}-W{isow:02d}"

    for periodo, clave in (("day", key_day), ("week", key_week), ("month", key_mon)):
        db.session.execute(
            text(
                """
            INSERT INTO KPI_Stats (Periodo, Clave, Total_Reservas, Total_Monto)
            VALUES (:p, :c, 1, :m)
            ON DUPLICATE KEY UPDATE
              Total_Reservas = Total_Reservas + 1,
              Total_Monto    = Total_Monto + :m
        """
            ),
            {"p": periodo, "c": clave, "m": float(monto or 0)},
        )
    db.session.commit()


def _audit_log(
    usuario: Optional[str],
    accion: str,
    detalles: dict,
    entidad_id: Optional[str] = None
):
    """
    Inserta en Audit_Log. Si se recibe entidad_id y no está en 'detalles',
    se inyecta para trazabilidad.
    """
    try:
        payload = dict(detalles or {})
        if entidad_id and "entidad_id" not in payload:
            payload["entidad_id"] = str(entidad_id)

        db.session.execute(
            text(
                """
            INSERT INTO Audit_Log (Usuario, Accion, Detalles)
            VALUES (:u, :a, :d)
        """
            ),
            {
                "u": (usuario or ""),
                "a": accion,
                "d": json.dumps(payload, ensure_ascii=False),
            },
        )
        db.session.commit()
    except Exception as e:
        current_app.logger.warning(f"[AUDIT] No se pudo registrar: {e}")
        db.session.rollback()


def _write_minimal_pdf(path: Path, title: str, lines: list[str]) -> None:
    """
    Genera un PDF válido sin dependencias externas (texto simple).
    Si reportlab está disponible, la usa automáticamente.
    """
    try:
        # Intentar con reportlab si está instalado
        from reportlab.lib.pagesizes import LETTER  # type: ignore
        from reportlab.pdfgen import canvas  # type: ignore

        c = canvas.Canvas(str(path), pagesize=LETTER)
        width, height = LETTER
        y = height - 72
        c.setFont("Helvetica-Bold", 14)
        c.drawString(72, y, title)
        y -= 24
        c.setFont("Helvetica", 11)
        for ln in lines:
            if y < 72:
                c.showPage()
                y = height - 72
                c.setFont("Helvetica", 11)
            c.drawString(72, y, ln)
            y -= 16
        c.showPage()
        c.save()
        return
    except Exception:
        pass

    # Fallback mínimo (sin reportlab)
    text_lines = [title, ""] + lines
    content = ""
    y = 750
    for ln in text_lines:
        ln = ln.replace("(", r"\(").replace(")", r"\)")
        content += f"BT /F1 12 Tf 72 {y} Td ({ln}) Tj ET\n"
        y -= 16
        if y < 72:
            break

    objects = []
    offsets = []

    def _add(obj):
        pos = sum(len(o) if isinstance(o, bytes) else len(o.encode("latin-1")) for o in objects)
        offsets.append(pos)
        objects.append(obj)

    _add("1 0 obj << /Type /Catalog /Pages 2 0 R >> endobj\n")
    _add("2 0 obj << /Type /Pages /Kids [3 0 R] /Count 1 >> endobj\n")
    _add("3 0 obj << /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 4 0 R /Resources << /Font << /F1 5 0 R >> >> >> endobj\n")
    stream = content.encode("latin-1", "ignore")
    _add(
        f"4 0 obj << /Length {len(stream)} >> stream\n".encode("latin-1")
        + stream
        + b"\nendstream\nendobj\n"
    )
    _add("5 0 obj << /Type /Font /Subtype /Type1 /BaseFont /Helvetica >> endobj\n")

    with open(path, "wb") as f:
        f.write(b"%PDF-1.4\n")
        cursor = f.tell()
        for obj in objects:
            if isinstance(obj, bytes):
                f.write(obj); cursor += len(obj)
            else:
                data = obj.encode("latin-1"); f.write(data); cursor += len(data)
        xref_pos = cursor
        f.write(b"xref\n")
        f.write(f"0 {len(objects)+1}\n".encode("latin-1"))
        f.write(b"0000000000 65535 f \n")
        base = 0
        for off in offsets:
            f.write(f"{base+off:010d} 00000 n \n".encode("latin-1"))
        f.write(b"trailer\n")
        f.write(f"<< /Size {len(objects)+1} /Root 1 0 R >>\n".encode("latin-1"))
        f.write(b"startxref\n")
        f.write(f"{xref_pos}\n".encode("latin-1"))
        f.write(b"%%EOF")


def _create_comprobante_pdf(reserva: dict) -> Path:
    """
    Crea el PDF de comprobante y devuelve la ruta.
    También inserta registros en Documento y ReservaDocumento.
    """
    numero = (
        reserva.get("Numero")
        or reserva.get("Numero_Comprobante")
        or reserva.get("numero")
    )
    rid = int(reserva.get("Codigo_Reserva") or reserva.get("id"))
    cliente_email = reserva.get("Usuario", "")
    checkin = reserva.get("Fecha_Entrada") or reserva.get("checkin")
    checkout = reserva.get("Fecha_Salida") or reserva.get("checkout")
    tipo = reserva.get("Tipo") or "Habitación"
    monto = float(reserva.get("Monto_Total") or reserva.get("monto") or 0.0)

    filename = f"{numero}.pdf"
    out_path = COMPROBANTES_DIR / filename

    title = "Comprobante de Reserva — Hotel Villa Grace"
    lines = [
        f"Número:        {numero}",
        f"Reserva ID:    {rid}",
        f"Cliente:       {cliente_email or '-'}",
        f"Check-in:      {checkin}",
        f"Check-out:     {checkout}",
        f"Habitación:    {tipo}",
        f"Monto total:   ₡ {monto:,.2f}".replace(",", "X").replace(".", ",").replace("X", "."),
        f"Canal:         {reserva.get('Canal') or reserva.get('canal') or 'Web'}",
        f"Estado:        {reserva.get('Estado') or reserva.get('estado') or 'Confirmada'}",
        "",
        "Gracias por su preferencia.",
    ]
    _write_minimal_pdf(out_path, title, lines)

    # Registrar en tablas Documento / ReservaDocumento (si existen)
    try:
        res = db.session.execute(
            text(
                """
            INSERT INTO Documento (Tipo, Ruta, MimeType, TamanoBytes)
            VALUES ('Comprobante','/storage/comprobantes/:fn','application/pdf', :sz)
        """
            ).bindparams(fn=filename, sz=out_path.stat().st_size)
        )
        doc_id = res.lastrowid

        db.session.execute(
            text(
                """
            INSERT INTO ReservaDocumento (Codigo_Reserva, Documento_Id)
            VALUES (:r, :d)
        """
            ),
            {"r": rid, "d": doc_id},
        )
        db.session.commit()
    except Exception as e:
        current_app.logger.warning(f"[COMPROBANTE] No se pudo registrar documento: {e}")
        db.session.rollback()

    return out_path


# =========================
# FACTORY PRINCIPAL
# =========================
def create_app() -> Flask:
    app = Flask(
        __name__,
        template_folder=str(BASE_DIR / "templates"),
        static_folder=str(BASE_DIR / "static"),
    )
    app.config.from_object(Config)

    db.init_app(app)
    migrate.init_app(app, db)

    with app.app_context():
        try:
            _ensure_seed_roles()
        except Exception:
            pass

    # Blueprint de FAC (cierre de caja)
    from blueprints.fin_cash import fin_cash_bp
    app.register_blueprint(fin_cash_bp)

    # Blueprint de GRR (creación de reservas)
    from blueprints.grr.routes import grr_bp
    app.register_blueprint(grr_bp, url_prefix="/grr")

    # ------------------------- Helpers para GRR-01-003 -------------------------
    def _extraer_reserva_id_de_response(resp) -> Optional[int]:
        data = None
        try:
            data = resp.get_json(silent=True)
        except Exception:
            pass
        if not isinstance(data, dict):
            return None
        for k in ("Codigo_Reserva", "reserva_id", "id", "CodigoReserva", "Codigo", "ReservaId"):
            v = data.get(k)
            try:
                return int(v)
            except Exception:
                pass
        comp = data.get("Numero_Comprobante") or data.get("numero_comprobante")
        if comp:
            rid = db.session.execute(
                text("SELECT Codigo_Reserva FROM Reserva WHERE Numero_Comprobante=:n LIMIT 1"),
                {"n": comp},
            ).scalar()
            if rid:
                return int(rid)
        return None

    def _reserva_min(rid: int) -> Optional[dict]:
        row = db.session.execute(text("""
          SELECT
            R.Codigo_Reserva,
            R.Codigo_Cliente,
            R.Codigo_Habitacion,
            R.Fecha_Entrada,
            R.Fecha_Salida,
            R.Monto_Total,
            R.Estado,
            C.Correo AS ClienteCorreo
          FROM Reserva R
          JOIN Cliente C ON C.Codigo_Cliente = R.Codigo_Cliente
          WHERE R.Codigo_Reserva = :rid
          LIMIT 1
        """), {"rid": rid}).mappings().first()
        return dict(row) if row else None

    def _kpi_touch(fecha_reserva: str, monto_total: float):
        _update_kpis(float(monto_total or 0), str(fecha_reserva or ""))

    def _tabla_existe(nombre: str) -> bool:
        try:
            insp = inspect(db.engine)
            return insp.has_table(nombre)
        except Exception:
            exists = db.session.execute(
                text("SELECT COUNT(*) FROM INFORMATION_SCHEMA.TABLES WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME=:t"),
                {"t": nombre}
            ).scalar()
            return bool(exists)

    def _asegurar_auto_asignacion(reserva_id: int):
        """
        Ejecuta auto-asignación si NO hay estancias aún.
        No rompe el flujo si algo falla.
        """
        try:
            hay_estancias = False
            if _tabla_existe("ReservaEstancia"):
                hay_estancias = bool(
                    db.session.execute(
                        text("SELECT 1 FROM ReservaEstancia WHERE Codigo_Reserva=:r LIMIT 1"),
                        {"r": reserva_id}
                    ).first()
                )
            if hay_estancias:
                return
        except Exception:
            pass

        try:
            auto_assign_for_reserva(reserva_id, preferir_tipo=True, allow_split=True)
        except Exception as e:
            current_app.logger.warning(f"[ASSIGN] auto_assign_for_reserva falló R={reserva_id}: {e}")

    # ---------------------- Hook global GRR-01-002 -----------------------------
    @app.after_request
    def grr_after_request(response):
        """
        Post-respuesta:
          - Auditoría de operaciones de reserva
          - Actualización de KPI (day/week/month) para Confirmadas
          - Auto-asignación óptima (GRR-01-003) para Confirmada/Pendiente
        """
        try:
            status = response.status_code
            method = request.method.upper()
            path = (request.path or "").lower()

            if status >= 400 or method not in ("POST", "PUT", "PATCH", "DELETE"):
                return response

            es_endpoint_reserva = any(
                s in path for s in ("/api/reservas", "/grr/reservas", "/reservas")
            )
            if not es_endpoint_reserva:
                return response

            rid = _extraer_reserva_id_de_response(response)

            if method == "DELETE" and rid:
                _audit_log(
                    _current_user_email(),
                    "reserva.eliminada",
                    {"motivo": "delete-endpoint"},
                    entidad_id=str(rid),
                )
                db.session.commit()
                return response

            if not rid:
                return response

            r = _reserva_min(rid)
            if not r:
                return response

            if method == "POST":
                _audit_log(
                    _current_user_email(),
                    "reserva.creada",
                    {
                        "Codigo_Reserva": r["Codigo_Reserva"],
                        "ClienteCorreo": r["ClienteCorreo"],
                        "Fecha_Entrada": str(r["Fecha_Entrada"]),
                        "Fecha_Salida": str(r["Fecha_Salida"]),
                        "Estado": r["Estado"],
                        "Monto_Total": float(r["Monto_Total"]),
                    },
                    entidad_id=str(r["Codigo_Reserva"]),
                )
            elif method in ("PUT", "PATCH"):
                _audit_log(
                    _current_user_email(),
                    "reserva.actualizada",
                    {"Codigo_Reserva": r["Codigo_Reserva"], "Estado": r["Estado"]},
                    entidad_id=str(r["Codigo_Reserva"]),
                )

            if r["Estado"] == "Confirmada":
                _kpi_touch(str(r["Fecha_Entrada"]), float(r["Monto_Total"]))

            if r["Estado"] in ("Confirmada", "Pendiente"):
                try:
                    _asegurar_auto_asignacion(int(rid))
                except Exception as e:
                    current_app.logger.warning(f"[ASSIGN] error auto R={rid}: {e}")

            db.session.commit()

        except Exception as e:
            try:
                db.session.rollback()
            except Exception:
                pass
            current_app.logger.warning(f"[AFTER] error en grr_after_request: {e}")

        return response

    # ---------------------- Helpers de sesión para plantillas ----------------------
    @app.context_processor
    def inject_session_flags():
        def is_logged_in():
            return bool(session.get("user_id"))
        return {
            "is_logged_in": is_logged_in,
            "current_user_name": session.get("user_name"),
            "current_user_role": session.get("user_role"),
        }

    # ---------------------- Proteger rutas de reserva si no hay sesión -------------
    PROTECTED_BOOKING_PATHS = {
        "/booking", "/booking.html",
        "/booking-search", "/booking-search.html",
        "/booking-results", "/booking-results.html",
        "/booking-details", "/booking-details.html",
        "/booking-checkout", "/booking-checkout.html",
        "/booking-confirmation", "/booking-confirmation.html",
    }

    @app.before_request
    def require_login_for_booking():
        path = request.path
        if path in PROTECTED_BOOKING_PATHS and not session.get("user_id"):
            next_url = request.full_path if request.query_string else request.path
            flash("Inicia sesión para continuar con tu reserva.", "warning")
            return redirect(url_for("login_html", next=next_url))

    # ---------------------- RUTAS ESTÁTICAS / PÚBLICAS ----------------------
    @app.errorhandler(404)
    def not_found(e):
        return render_template("404.html"), 404

    @app.route("/")
    @app.route("/index.html")
    def index_html():
        return render_template("index.html")

    @app.route("/contact.html")
    def contact_html():
        return render_template("contact.html")

    @app.route("/about.html")
    def about_html():
        return render_template("about.html")

    @app.route("/booking.html")
    def booking_html():
        return redirect(url_for("booking_search"))

    # ---------------------- Portal / Ops / Admin (protegidas por rol) ------------
    @app.route("/portal-dashboard.html")
    @role_required("Cliente")
    def portal_dashboard_html():
        return render_template("portal-dashboard.html")

    @app.route("/portal-perfil.html")
    @role_required("Cliente")
    def portal_perfil_html():
        return render_template("portal-perfil.html")

    @app.route("/portal-preferencias.html")
    @role_required("Cliente")
    def portal_preferencias_html():
        return render_template("portal-preferencias.html")

    @app.route("/portal-reservas.html")
    @role_required("Cliente")
    def portal_reservas_html():
        return render_template("portal-reservas.html")

    @app.route("/portal-reserva-detalle.html")
    @app.route("/portal-reserva-detalle")
    @role_required("Cliente")
    def portal_reserva_detalle_html():
        return render_template("portal-reserva-detalle.html")

    @app.route("/ops-dashboard.html")
    @role_required("Administrador", "Recepcionista")
    def ops_dashboard_html():
        return render_template("ops-dashboard.html")

    @app.route("/ops-housekeeping.html")
    @role_required("Administrador", "Limpieza")
    def ops_housekeeping_html():
        return render_template("ops-housekeeping.html")

    @app.route("/admin-dashboard.html")
    @role_required("Administrador")
    def admin_dashboard_html():
        return render_template("admin-dashboard.html")

    # === OPS: tablero de estados por habitación (tablero en tiempo real) ===
    @app.route("/ops-rooms-status.html")
    @role_required("Administrador", "Recepcionista")
    def ops_rooms_status_html():
        return render_template("ops-rooms-status.html")

    # === ADMIN/OPS: vista de calendario por habitación ===
    @app.route("/admin-calendario.html")
    @role_required("Administrador", "Recepcionista")
    def admin_calendario_html():
        return render_template("admin-calendario.html")

    # ---------------------- API Disponibilidad --------------------------
    @app.get("/api/availability")
    def api_availability():
        from datetime import datetime as dt

        checkin = (request.args.get("checkin") or "").strip()
        checkout = (request.args.get("checkout") or "").strip()
        guests = int((request.args.get("guests") or "1") or 1)
        rooms_req = int((request.args.get("rooms") or "1") or 1)

        try:
            ci = dt.strptime(checkin, "%Y-%m-%d").date()
            co = dt.strptime(checkout, "%Y-%m-%d").date()
        except Exception:
            return jsonify({"ok": False, "available": False, "message": "Fechas inválidas"}), 400

        today = date.today()
        if ci < today or co <= ci:
            return jsonify({"ok": True, "available": False, "message": "Rango de fechas no válido"}), 200

        try:
            total_rooms = db.session.query(Habitacion).count()
            if not total_rooms:
                total_rooms = 10
        except Exception:
            total_rooms = 10

        available_rooms = total_rooms
        nights = (co - ci).days
        is_available = available_rooms >= rooms_req

        return jsonify({
            "ok": True,
            "available": bool(is_available),
            "nights": nights,
            "rooms_available": available_rooms,
            "rooms_requested": rooms_req,
            "guests": guests,
            "checkin": checkin,
            "checkout": checkout,
            "message": ("Disponibilidad confirmada" if is_available else "Sin cupo para ese rango"),
        }), 200

    @app.get("/booking/search")
    def booking_search_alias():
        return redirect(url_for("api_availability", **request.args))

    # === API: estado actual de todas las habitaciones (para el tablero) ===
    @app.get("/api/rooms/status")
    @role_required("Administrador", "Recepcionista")
    def api_rooms_status():
        try:
            rows = db.session.query(Habitacion).order_by(Habitacion.Numero_Habitacion.asc()).all()
            data = [{
                "id": h.Codigo_Habitacion,
                "numero": h.Numero_Habitacion,
                "tipo": h.Tipo,
                "estado": h.Estado,  # Disponible, Ocupada, Limpieza, Mantenimiento
                "precio": float(h.Precio_Noche) if h.Precio_Noche is not None else None,
            } for h in rows]
            return jsonify({"ok": True, "items": data})
        except Exception as e:
            current_app.logger.exception("rooms/status error: %s", e)
            return jsonify({"ok": False, "error": str(e)}), 500

    # === API: calendario por habitación (reservas y tareas relevantes) ===
    @app.get("/api/rooms/<int:room_id>/calendar")
    @role_required("Administrador", "Recepcionista")
    def api_room_calendar(room_id: int):
        reservas = (
            db.session.execute(
                text(
                    """
            SELECT Codigo_Reserva AS id,
                   Fecha_Entrada  AS start,
                   Fecha_Salida   AS end,
                   Estado
              FROM Reserva
             WHERE Codigo_Habitacion = :rid
             ORDER BY Fecha_Entrada
        """
                ),
                {"rid": room_id},
            ).mappings().all()
        )

        events = []
        for r in reservas:
            events.append(
                {
                    "id": int(r["id"]),
                    "title": f"Reserva #{int(r['id'])}",
                    "start": r["start"].isoformat() if hasattr(r["start"], "isoformat") else str(r["start"]),
                    "end": r["end"].isoformat() if hasattr(r["end"], "isoformat") else str(r["end"]),
                    "type": "reserva",
                    "status": r["Estado"],
                }
            )
        return jsonify({"ok": True, "events": events})

    # === API: listar tareas de limpieza (para panel de limpieza) ===
    @app.get("/api/housekeeping/tasks")
    @role_required("Administrador", "Limpieza")
    def api_hk_list():
        estado = request.args.get("estado")  # Pendiente | En proceso | Terminado | (None=Todos)
        q = (
            db.session.execute(
                text(
                    """
            SELECT Id, Habitacion_Id, Estado, Fecha_Creacion, Fecha_Cierre, Observaciones
              FROM HousekeepingTask
             WHERE (:e IS NULL OR Estado = :e)
             ORDER BY CASE Estado
                        WHEN 'Pendiente'   THEN 1
                        WHEN 'En proceso'  THEN 2
                        WHEN 'Terminado'   THEN 3
                        ELSE 4
                      END, Id DESC
        """
                ),
                {"e": estado},
            ).mappings().all()
        )

        items = []
        for t in q:
            items.append(
                {
                    "id": t["Id"],
                    "habitacion_id": t["Habitacion_Id"],
                    "estado": t["Estado"],
                    "fecha": t["Fecha_Creacion"],
                    "cierre": t["Fecha_Cierre"],
                    "observaciones": t["Observaciones"],
                }
            )
        return jsonify({"ok": True, "items": items})

    # === API: actualizar estado / agregar insumos / cerrar tarea ===
    @app.post("/api/housekeeping/tasks/<int:task_id>/update")
    @role_required("Administrador", "Limpieza")
    def api_hk_update(task_id: int):
        nuevo_estado = (request.json or {}).get("estado")
        observaciones = (request.json or {}).get("observaciones")

        db.session.execute(
            text(
                """
            UPDATE HousekeepingTask
               SET Estado = COALESCE(:estado, Estado),
                   Observaciones = COALESCE(:obs, Observaciones),
                   Fecha_Cierre = CASE WHEN :estado = 'Terminado' AND Fecha_Cierre IS NULL
                                       THEN NOW() ELSE Fecha_Cierre END
             WHERE Id = :id
        """
            ),
            {"estado": nuevo_estado, "obs": observaciones, "id": task_id},
        )
        db.session.commit()
        return jsonify({"ok": True})

    # === ADMIN: CRUD Villas/Casas ===
    @app.route("/admin-villas.html")
    @role_required("Administrador")
    def admin_villas_html():
        return render_template("admin-villas.html")

    @app.get("/api/villas")
    @role_required("Administrador")
    def api_villas_list():
        rows = (
            db.session.execute(
                text(
                    """
            SELECT Id, Nombre, Capacidad, TarifaBase, Estado
              FROM Villa
             ORDER BY Nombre
        """
                )
            ).mappings().all()
        )
        return jsonify({"ok": True, "items": [dict(r) for r in rows]})

    @app.post("/api/villas")
    @role_required("Administrador")
    def api_villas_create():
        p = request.json or {}
        db.session.execute(
            text(
                """
            INSERT INTO Villa (Nombre, Capacidad, TarifaBase, Estado)
            VALUES (:n, :c, :t, 'Activo')
        """
            ),
            {"n": p.get("nombre"), "c": p.get("capacidad"), "t": p.get("tarifa")},
        )
        db.session.commit()
        return jsonify({"ok": True})

    @app.post("/api/villas/<int:villa_id>")
    @role_required("Administrador")
    def api_villas_update(villa_id: int):
        p = request.json or {}
        db.session.execute(
            text(
                """
            UPDATE Villa
               SET Nombre = COALESCE(:n, Nombre),
                   Capacidad = COALESCE(:c, Capacidad),
                   TarifaBase = COALESCE(:t, TarifaBase),
                   Estado = COALESCE(:e, Estado)
             WHERE Id = :id
        """
            ),
            {"id": villa_id, "n": p.get("nombre"), "c": p.get("capacidad"), "t": p.get("tarifa"), "e": p.get("estado")},
        )
        db.session.commit()
        return jsonify({"ok": True})

    @app.delete("/api/villas/<int:villa_id>")
    @role_required("Administrador")
    def api_villas_delete(villa_id: int):
        db.session.execute(text("DELETE FROM Villa WHERE Id = :id"), {"id": villa_id})
        db.session.commit()
        return jsonify({"ok": True})

    # === API: crear solicitud de mantenimiento desde una habitación ===
    @app.post("/api/rooms/<int:room_id>/maintenance")
    @role_required("Administrador", "Recepcionista", "Limpieza")
    def api_room_maintenance_create(room_id: int):
        p = request.json or {}
        db.session.execute(
            text(
                """
            INSERT INTO MaintenanceRequest (Habitacion_Id, Titulo, Descripcion, Estado, Fecha_Creacion)
            VALUES (:rid, :tit, :des, 'Pendiente', NOW())
        """
            ),
            {"rid": room_id, "tit": p.get("titulo"), "des": p.get("descripcion")},
        )
        db.session.commit()
        return jsonify({"ok": True})

    # ---------------------- Búsqueda / resultados reserva ----------------------
    @app.route("/booking-search", methods=["GET", "POST"])
    @app.route("/booking-search.html", methods=["GET", "POST"])
    def booking_search():
        if request.method == "POST":
            checkin = request.form.get("checkin")
            checkout = request.form.get("checkout")
            guests = request.form.get("guests", "1")
            rooms = request.form.get("rooms", "1")
            return redirect(
                url_for(
                    "booking_results",
                    checkin=checkin,
                    checkout=checkout,
                    guests=guests,
                    rooms=rooms,
                )
            )
        return render_template("booking-search.html")

    @app.route("/booking-results", methods=["GET"])
    @app.route("/booking-results.html", methods=["GET"])
    def booking_results():
        with app.test_client() as c:
            resp = c.get(url_for("api_availability", **request.args))
            data = resp.get_json() if resp.is_json else {"ok": False}
        return render_template("booking-results.html", availability=data)

    # ---------------------- Detalle / Checkout / Confirmación ------------------
    @app.route("/booking-details", methods=["GET", "POST"])
    @app.route("/booking-details.html", methods=["GET", "POST"])
    def booking_details_html():
        params = {
            "checkin": request.args.get("checkin"),
            "checkout": request.args.get("checkout"),
            "adults": request.args.get("adults", "2"),
            "children": request.args.get("children", "0"),
            "room": request.args.get("room", "1"),
            "price": request.args.get("price"),
        }
        return render_template("booking-details.html", **params)

    @app.route("/booking-checkout", methods=["GET", "POST"])
    @app.route("/booking-checkout.html", methods=["GET", "POST"])
    def booking_checkout_html():
        ctx = {
            "checkin": request.values.get("checkin"),
            "checkout": request.values.get("checkout"),
            "adults": request.values.get("adults"),
            "children": request.values.get("children"),
            "room": request.values.get("room"),
            "price": request.values.get("price"),
            "full_name": request.values.get("full_name"),
            "email": request.values.get("email"),
            "phone": request.values.get("phone"),
        }
        return render_template("booking-checkout.html", **ctx)

    @app.route("/booking-confirmation", methods=["GET"])
    @app.route("/booking-confirmation.html", methods=["GET"])
    def booking_confirmation_html():
        data = {
            "checkin": request.args.get("checkin"),
            "checkout": request.args.get("checkout"),
            "adults": request.args.get("adults"),
            "children": request.args.get("children"),
            "room": request.args.get("room"),
            "price": request.args.get("price"),
            "reservation_code": request.args.get(
                "code", "VG-" + datetime.now().strftime("%Y%m%d-%H%M%S")
            ),
        }

        # --- NUEVO (GRR-01-004): recordatorio del documento con el que se creó el perfil
        identity_notice = None
        try:
            uid = session.get("user_id")
            doc_value = None
            doc_label = None
            if uid:
                u = Usuario.query.filter_by(Codigo_Usuario=uid).first()
                if u and getattr(u, "Cedula_Pasaporte", None):
                    doc_value = u.Cedula_Pasaporte
                    doc_label = "pasaporte" if not str(doc_value).isdigit() else "cédula"
                elif u and getattr(u, "Codigo_Cliente", None):
                    row = db.session.execute(
                        text("SELECT Cedula FROM Cliente WHERE Codigo_Cliente=:cid LIMIT 1"),
                        {"cid": u.Codigo_Cliente}
                    ).first()
                    if row and row[0]:
                        doc_value = row[0]
                        doc_label = "cédula"
            if data.get("checkin") and doc_value:
                identity_notice = (
                    f"El día {data['checkin']} debe presentar el {doc_label} "
                    f"{doc_value} con el que creó su perfil."
                )
        except Exception:
            pass

        return render_template("booking-confirmation.html", identity_notice=identity_notice, **data)

    # ---------------------- API Portal Reservas (solo Cliente) ----------------------
    @app.route("/api/portal/reservas", methods=["GET"])
    @role_required("Cliente")
    def api_portal_reservas_list():
        if not session.get("user_id"):
            return jsonify({"ok": False, "error": "auth_required"}), 401

        email = _current_user_email()
        if not email:
            current_app.logger.warning(
                "[PORTAL] Sin email asociado a user_id=%s", session.get("user_id")
            )
            return jsonify({"ok": True, "items": []})

        estado = request.args.get("estado") or None
        fini = request.args.get("fini") or None
        ffin = request.args.get("ffin") or None

        try:
            reservas = _query_user_reservas(email, estado, fini, ffin)
            current_app.logger.info(
                "[PORTAL] user=%s, cli_id=%s -> %d reservas",
                email,
                current_cliente_id(),
                len(reservas),
            )
            return jsonify({"ok": True, "items": reservas})
        except Exception as e:
            current_app.logger.exception(
                "[PORTAL] Error listando reservas para %s: %s", email, e
            )
            return jsonify({"ok": True, "items": [], "warning": "no_data"}), 200

    @app.route("/api/portal/reservas/<int:reserva_id>", methods=["GET"])
    @role_required("Cliente")
    def api_portal_reserva_get(reserva_id: int):
        if not session.get("user_id"):
            return jsonify({"ok": False, "error": "auth_required"}), 401
        email = _current_user_email()
        if not _reserva_belongs_to_email(reserva_id, email or ""):
            return jsonify({"ok": False, "error": "forbidden"}), 403
        r = _get_reserva_by_id(reserva_id)
        if not r:
            return jsonify({"ok": False, "error": "not_found"}), 404
        return jsonify({"ok": True, "item": _reserva_to_dict(r)})

    @app.route("/api/portal/reservas/<int:reserva_id>", methods=["PUT", "PATCH"])
    @role_required("Cliente")
    def api_portal_reserva_update(reserva_id: int):
        if not session.get("user_id"):
            return jsonify({"ok": False, "error": "auth_required"}), 401
        email = _current_user_email()
        if not _reserva_belongs_to_email(reserva_id, email or ""):
            return jsonify({"ok": False, "error": "forbidden"}), 403

        r = _get_reserva_by_id(reserva_id)
        if not r:
            return jsonify({"ok": False, "error": "not_found"}), 404

        payload = request.get_json(silent=True) or {}

        def _norm(v):
            if not v:
                return v
            return str(v)[:10]

        new_ci = _norm(payload.get("checkin")) or _normalize_date_like(r.get("Fecha_Entrada"))
        new_co = _norm(payload.get("checkout")) or _normalize_date_like(r.get("Fecha_Salida"))
        new_obs = payload.get("observaciones", None)
        new_h = payload.get("huespedes", None)

        ok, msg = _validate_no_overbooking(str(new_ci), str(new_co))
        if not ok:
            return jsonify({"ok": False, "error": "availability", "message": msg}), 409

        sql = text(
            """
            UPDATE Reserva
               SET Fecha_Entrada = :ci,
                   Fecha_Salida  = :co,
                   Observaciones = COALESCE(:obs, Observaciones),
                   Huespedes     = COALESCE(:h, Huespedes)
             WHERE Codigo_Reserva = :id
        """
        )
        db.session.execute(
            sql,
            {"ci": new_ci, "co": new_co, "obs": new_obs, "h": str(new_h) if new_h is not None else None, "id": reserva_id},
        )
        db.session.commit()
        r2 = _get_reserva_by_id(reserva_id)
        return jsonify({"ok": True, "item": _reserva_to_dict(r2)})

    @app.route("/api/portal/reservas/<int:reserva_id>", methods=["DELETE"])
    @role_required("Cliente")
    def api_portal_reserva_cancel(reserva_id: int):
        if not session.get("user_id"):
            return jsonify({"ok": False, "error": "auth_required"}), 401
        email = _current_user_email()
        if not _reserva_belongs_to_email(reserva_id, email or ""):
            return jsonify({"ok": False, "error": "forbidden"}), 403

        db.session.execute(
            text("UPDATE Reserva SET Estado='Cancelada' WHERE Codigo_Reserva = :id"),
            {"id": reserva_id},
        )
        db.session.commit()
        return jsonify({"ok": True})

    @app.route("/api/portal/reservas/<int:reserva_id>/export", methods=["GET"])
    @role_required("Cliente")
    def api_portal_reserva_export(reserva_id: int):
        if not session.get("user_id"):
            return jsonify({"ok": False, "error": "auth_required"}), 401
        email = _current_user_email()
        if not _reserva_belongs_to_email(reserva_id, email or ""):
            return jsonify({"ok": False, "error": "forbidden"}), 403

        r = _get_reserva_by_id(reserva_id)
        if not r:
            return jsonify({"ok": False, "error": "not_found"}), 404

        data = _reserva_to_dict(r)
        return jsonify(
            {
                "ok": True,
                "comprobante": {
                    "numero": data.get("numero") or f"VG-{data.get('id')}",
                    "fecha": datetime.utcnow().isoformat() + "Z",
                    "cliente": email,
                    "reserva": data,
                    "emisor": {"hotel": "Hotel Villa Grace", "canal": data.get("canal", "Web")},
                },
            }
        )

    # ---------------------- RUTA descarga comprobante ----------------------
    @app.route("/api/reservas/<int:reserva_id>/comprobante", methods=["GET"])
    @role_required("Cliente", "Administrador", "Recepcionista")
    def api_reserva_comprobante(reserva_id: int):
        r = _get_reserva_by_id(reserva_id)
        if not r:
            return jsonify({"ok": False, "error": "not_found"}), 404

        if _user_role().lower() == "cliente":
            email = _current_user_email()
            if not _reserva_belongs_to_email(reserva_id, email or ""):
                return jsonify({"ok": False, "error": "forbidden"}), 403

        numero = r.get("Numero") or _make_unique_number(
            reserva_id, _normalize_date_like(r.get("Fecha_Entrada")) or ""
        )
        pdf_path = COMPROBANTES_DIR / f"{numero}.pdf"
        if not pdf_path.exists():
            _create_comprobante_pdf(dict(r))
        if not pdf_path.exists():
            return jsonify({"ok": False, "error": "not_available"}), 404
        return send_file(str(pdf_path), as_attachment=True, download_name=f"{numero}.pdf")

    # ---------------------- RUTA de HABITACIONES DINÁMICAS (opcional) ----------------------
    @app.route("/rooms.html")
    def rooms_html():
        try:
            habitaciones = Habitacion.query.order_by(Habitacion.Numero_Habitacion.asc()).all()
            print(f"[DEBUG] Se cargaron {len(habitaciones)} habitaciones desde la BD.")
        except Exception as e:
            print(f"[ERROR] Al cargar habitaciones: {e}")
            habitaciones = []
        return render_template("rooms.html", habitaciones=habitaciones)

    @app.route("/test-db")
    def test_db():
        try:
            habitaciones = Habitacion.query.limit(5).all()
            data = [
                {
                    "Codigo_Habitacion": h.Codigo_Habitacion,
                    "Numero_Habitacion": h.Numero_Habitacion,
                    "Tipo": h.Tipo,
                    "Precio_Noche": float(h.Precio_Noche),
                    "Estado": h.Estado,
                }
                for h in habitaciones
            ]
            return jsonify({"ok": True, "habitaciones": data})
        except Exception as e:
            return jsonify({"ok": False, "error": str(e)})

    # ---------------------- KPIs para dashboards ----------------------
    @app.get("/api/kpi/summary")
    @role_required("Administrador", "Recepcionista")
    def api_kpi_summary():
        def _fetch(periodo, key_fmt_sql):
            row = db.session.execute(
                text(
                    f"""
                SELECT Total_Reservas, Total_Monto
                  FROM KPI_Stats
                 WHERE Periodo = :p AND Clave = {key_fmt_sql}
                 LIMIT 1
            """
                ),
                {"p": periodo},
            ).first()
            return {
                "reservas": int(row[0]) if row else 0,
                "monto": float(row[1]) if row else 0.0,
            }

        return jsonify(
            {
                "ok": True,
                "day": _fetch("day", "DATE_FORMAT(CURDATE(), '%Y-%m-%d')"),
                "week": _fetch("week", "DATE_FORMAT(CURDATE(), '%x-W%v')"),
                "month": _fetch("month", "DATE_FORMAT(CURDATE(), '%Y-%m')"),
            }
        )

    # ---------------------- Auditoría reciente ----------------------
    @app.get("/api/audit/recent")
    @role_required("Administrador")
    def api_audit_recent():
        rows = (
            db.session.execute(
                text(
                    """
            SELECT Id, Fecha, Usuario, Accion, Detalles
              FROM Audit_Log
             ORDER BY Id DESC
             LIMIT 50
        """
                )
            ).mappings().all()
        )
        items = []
        for r in rows:
            try:
                det = json.loads(r["Detalles"]) if r["Detalles"] else {}
            except Exception:
                det = {"raw": r["Detalles"]}
            items.append(
                {
                    "id": r["Id"],
                    "fecha": r["Fecha"].isoformat() if hasattr(r["Fecha"], "isoformat") else str(r["Fecha"]),
                    "usuario": r["Usuario"],
                    "accion": r["Accion"],
                    "detalles": det,
                }
            )
        return jsonify({"ok": True, "items": items})

    # ---------------------- Recuperar contraseña ----------------------
    @app.route("/forgot-password", methods=["GET", "POST"])
    def forgot_password():
        if request.method == "POST":
            email = (request.form.get("email") or "").strip().lower()
            generic_msg = "Si el correo existe, te hemos enviado un enlace para restablecer tu contraseña."
            if not email or "@" not in email:
                flash(generic_msg, "info")
                return render_template("forgot-password.html")

            user = Usuario.query.filter(func.lower(Usuario.Correo) == email).first()
            s = _get_serializer(app)
            token = s.dumps({"email": email})
            reset_url = url_for("reset_password", token=token, _external=True)

            if user:
                try:
                    _send_reset_email(app, email, reset_url)
                except Exception as e:
                    app.logger.warning(f"Error enviando reset: {e}")

            flash(generic_msg, "info")
            return render_template("forgot-password.html")
        return render_template("forgot-password.html")

    @app.route("/reset-password/<token>", methods=["GET", "POST"])
    def reset_password(token: str):
        s = _get_serializer(app)
        max_age = 3600  # 1 hora
        try:
            data = s.loads(token, max_age=max_age)
            email = (data or {}).get("email")
        except SignatureExpired:
            flash("El enlace expiró. Solicita uno nuevo.", "warning")
            return redirect(url_for("forgot_password"))
        except BadSignature:
            flash("Enlace inválido. Solicita uno nuevo.", "danger")
            return redirect(url_for("forgot_password"))

        user = Usuario.query.filter(func.lower(Usuario.Correo) == (email or "").lower()).first()
        if not user:
            flash("El enlace no es válido o expiró.", "danger")
            return redirect(url_for("forgot_password"))

        if request.method == "POST":
            pwd1 = (request.form.get("password") or "").strip()
            pwd2 = (request.form.get("confirm_password") or "").strip()

            if len(pwd1) < 8:
                flash("La contraseña debe tener al menos 8 caracteres.", "warning")
                return render_template("reset-password.html", token=token, email=email)
            if pwd1 != pwd2:
                flash("Las contraseñas no coinciden.", "warning")
                return render_template("reset-password.html", token=token, email=email)

            user.set_password(pwd1)
            db.session.commit()
            if session.get("user_id") == user.Codigo_Usuario:
                session.clear()

            flash("Tu contraseña fue actualizada. Ya puedes iniciar sesión.", "success")
            return redirect(url_for("login_html"))

        return render_template("reset-password.html", token=token, email=email)

    # ---------------------- LOGIN / LOGOUT ----------------------
    @app.route("/login.html", methods=["GET", "POST"])
    def login_html():
        if request.method == "POST":
            identifier = (request.form.get("email") or "").strip().lower()
            password = (request.form.get("password") or "").strip()

        # Find by email, otherwise try by document
            user = None
            if identifier:
                user = Usuario.query.filter(func.lower(Usuario.Correo) == identifier).first()

            if not user:
                ced = (request.form.get("email") or "").strip()
                if ced:
                    user = Usuario.query.filter_by(Cedula_Pasaporte=ced).first()

            if not user:
                flash("Credenciales inválidas o usuario inactivo.", "danger")
                return render_template("login.html")

            if user.Estado != "Activo":
                flash("Usuario inactivo.", "danger")
                return render_template("login.html")

            ok = False
            try:
                ok = bool(user.check_password(password))
            except Exception:
                ok = False

            if not ok:
                legacy_plain = None
                for col in ("Contrasena", "Password", "Password_Plain", "Pwd"):
                    if hasattr(user, col):
                        legacy_plain = getattr(user, col)
                        break
                if legacy_plain and str(legacy_plain) == password:
                    try:
                        user.set_password(password)
                        try:
                            for col in ("Contrasena", "Password", "Password_Plain", "Pwd"):
                                if hasattr(user, col):
                                    setattr(user, col, None)
                        except Exception:
                            pass
                        db.session.commit()
                    except Exception:
                        db.session.rollback()
                    ok = True

            if not ok:
                flash("Credenciales inválidas.", "danger")
                return render_template("login.html")

            try:
                if not getattr(user, "Codigo_Cliente", None):
                    cid = ensure_cliente_for_email(user.Nombre, user.Correo, getattr(user, "Telefono", None))
                    if cid:
                        user.Codigo_Cliente = cid
                        db.session.commit()
            except Exception:
                db.session.rollback()

            role_name = _get_role_name(user)
            if role_name not in DEFAULT_ROLES:
                role_name = "Cliente"

            session["user_id"] = user.Codigo_Usuario
            session["user_name"] = user.Nombre
            session["user_role"] = role_name

            nxt = request.args.get("next")
            if nxt:
                return redirect(nxt)

            flash(f"Bienvenido/a {user.Nombre}.", "success")
            return redirect(url_for(role_redirect_endpoint(role_name)))

        return render_template("login.html")

    @app.route("/logout")
    def logout():
        session.clear()
        flash("Sesión cerrada correctamente.", "info")
        return redirect(url_for("index_html"))

    # ---------------------- REGISTRO ----------------------
    @app.route("/register", methods=["GET", "POST"])
    def register():
        if request.method == "POST":
            full_name = (request.form.get("full_name") or "").strip()
            national_id = (request.form.get("national_id") or "").strip()
            email = (request.form.get("email") or "").strip().lower()
            phone = (request.form.get("phone") or "").strip()
            password = (request.form.get("password") or "").strip()
            confirm = (request.form.get("confirm_password") or "").strip()

            if not full_name or not email or not phone or not password:
                flash("Por favor complete todos los campos obligatorios.", "warning")
                return render_template("register.html")
            if password != confirm:
                flash("Las contraseñas no coinciden.", "warning")
                return render_template("register.html")
            if "@" not in email or "." not in email:
                flash("Correo electrónico inválido.", "warning")
                return render_template("register.html")
            if len(password) < 8:
                flash("La contraseña debe tener al menos 8 caracteres.", "warning")
                return render_template("register.html")

            if Usuario.query.filter(func.lower(Usuario.Correo) == email).first():
                flash("El correo ya está registrado.", "danger")
                return render_template("register.html")

            if national_id:
                if Usuario.query.filter_by(Cedula_Pasaporte=national_id).first():
                    flash("La cédula/pasaporte ya está registrada.", "danger")
                    return render_template("register.html")
            else:
                national_id = None

            role = _get_role_by_name("Cliente")
            if not role:
                _ensure_seed_roles()
                role = _get_role_by_name("Cliente")

            u = Usuario(
                Nombre=full_name,
                Cedula_Pasaporte=national_id,
                Correo=email,
                Telefono=phone,
                Rol_Id=role.Codigo_Rol if role else None,
                Estado="Activo",
            )
            u.set_password(password)
            db.session.add(u)

            try:
                cliente_id = ensure_cliente_for_email(full_name, email, phone)
                if cliente_id:
                    u.Codigo_Cliente = cliente_id
                db.session.commit()
            except IntegrityError:
                db.session.rollback()
                flash("Ya existe un usuario con ese correo o cédula/pasaporte.", "danger")
                return render_template("register.html")
            except Exception as e:
                db.session.rollback()
                flash("No se pudo completar el registro. Inténtalo de nuevo.", "danger")
                current_app.logger.exception(f"[REGISTER] Error creando usuario: {e}")
                return render_template("register.html")

            flash("Registro exitoso. Ya puedes iniciar sesión.", "success")
            return redirect(url_for("login_html"))

        return render_template("register.html")

    @app.route("/register.html", methods=["GET", "POST"])
    def register_html():
        return register()

    # ========= GRR-01-004 — MODELOS / HELPERS / RUTAS =============
    class GuestCheckin(db.Model):
        __tablename__ = "guest_checkin"
        id = db.Column(db.Integer, primary_key=True)
        reserva_id = db.Column(db.Integer, nullable=False, index=True)
        cliente_id = db.Column(db.Integer, index=True)
        recep_user_id = db.Column(db.Integer, nullable=False)
        doc_type = db.Column(db.String(20))
        doc_number = db.Column(db.String(64), index=True, nullable=False)
        doc_image_path = db.Column(db.String(255))
        signature_path = db.Column(db.String(255))
        key_activated = db.Column(db.Boolean, default=False)
        created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
        meta = db.Column(db.JSON)

    class KeyActivation(db.Model):
        __tablename__ = "key_activation"
        id = db.Column(db.Integer, primary_key=True)
        reserva_id = db.Column(db.Integer, nullable=False, index=True)
        habitacion_id = db.Column(db.Integer, index=True)
        cliente_id = db.Column(db.Integer, index=True)
        activated_at = db.Column(db.DateTime, default=datetime.utcnow)
        status = db.Column(db.Enum("activated", "failed"), default="activated")
        meta = db.Column(db.JSON)

    def allowed_guest_file(filename: str) -> bool:
        return "." in (filename or "") and filename.rsplit(".", 1)[1].lower() in ALLOWED_DOC_EXTS

    def _normalize_docnum(v: str) -> str:
        return (v or "").strip()

    def _activate_e_key(habitacion_id: Optional[int], reserva_id: int, cliente_id: Optional[int]) -> None:
        """
        Stub de activación de llave electrónica
        """
        try:
            ka = KeyActivation(
                reserva_id=reserva_id,
                habitacion_id=int(habitacion_id) if habitacion_id else None,
                cliente_id=int(cliente_id) if cliente_id else None,
                activated_at=datetime.utcnow(),
                status="activated",
                meta={"provider": "stub", "note": "Simulación de activación"}
            )
            db.session.add(ka)
            db.session.flush()
            _audit_log(
                _current_user_email(),
                "checkin.key_activated",
                {"reserva_id": reserva_id, "habitacion_id": habitacion_id, "key_activation_id": ka.id}
            )
            db.session.commit()
        except Exception as e:
            current_app.logger.warning(f"[KEY] No se pudo registrar activación: {e}")
            db.session.rollback()

    def _reservas_by_doc_when(doc_number: str, when_iso: Optional[str] = None):
        """
        Devuelve reservas cuya Cedula (Cliente) o Cedula_Pasaporte (Usuario vinculado)
        coincide con 'doc_number' y donde 'when_iso' está entre Entrada y Salida (rango estancia).
        Si when_iso es None, usa la fecha de HOY.
        """
        doc = _normalize_docnum(doc_number)
        when = (when_iso or date.today().isoformat())
        rows = db.session.execute(text("""
            SELECT
                R.Codigo_Reserva           AS reserva_id,
                R.Codigo_Cliente           AS cliente_id,
                R.Codigo_Habitacion        AS habitacion_id,
                R.Estado                   AS estado,
                R.Fecha_Entrada            AS checkin,
                R.Fecha_Salida             AS checkout,
                R.Numero_Comprobante       AS numero,
                C.Cedula                   AS cliente_doc,
                C.Correo                   AS cliente_email,
                C.Nombre                   AS nombre,
                C.Apellido                 AS apellido,
                H.Numero_Habitacion        AS habitacion_num
            FROM Reserva R
            JOIN Cliente C ON C.Codigo_Cliente = R.Codigo_Cliente
            LEFT JOIN Usuario U ON U.Codigo_Cliente = C.Codigo_Cliente
            LEFT JOIN Habitacion H ON H.Codigo_Habitacion = R.Codigo_Habitacion
            WHERE DATE(R.Fecha_Entrada) <= DATE(:w)
              AND DATE(:w) <= DATE(R.Fecha_Salida)
              AND R.Estado IN ('Confirmada', 'Pendiente', 'En Casa')
              AND ( C.Cedula = :doc OR U.Cedula_Pasaporte = :doc )
            ORDER BY R.Fecha_Entrada ASC, R.Codigo_Reserva ASC
        """), {"doc": doc, "w": when}).mappings().all()
        return [dict(r) for r in rows]

    # UI de recepción (simple)
    @app.route("/ops-checkin.html", methods=["GET"])
    @role_required("Administrador", "Recepcionista")
    def ops_checkin_html():
        return render_template("ops-checkin.html")

    # ------------ ENDPOINTS COMPATIBLES CON EL FRONT (ops/*) ----------------

    @app.get("/api/ops/checkin/search")
    @role_required("Administrador", "Recepcionista")
    def api_ops_checkin_search():
        """
        Front de recepción: busca reservas por documento.
        Parámetros: ?doc=########  &when=YYYY-MM-DD (opcional, default: hoy)
        """
        doc = (request.args.get("doc") or "").strip()
        when = (request.args.get("when") or date.today().isoformat()).strip()
        if not doc:
            return jsonify({"ok": False, "error": "doc_required"}), 400

        rows = _reservas_by_doc_when(doc, when)
        items = [{
            "id": int(r["reserva_id"]),
            "numero": r.get("numero") or _make_unique_number(int(r["reserva_id"]), str(r.get("checkin") or "")),
            "huesped": f"{(r.get('nombre') or '').strip()} {(r.get('apellido') or '').strip()}".strip() or (r.get("cliente_email") or ""),
            "checkin": str(r["checkin"])[:10],
            "checkout": str(r["checkout"])[:10],
            "habitacion": r.get("habitacion_num") or r.get("habitacion_id"),
            "estado": r.get("estado") or "Confirmada",
        } for r in rows]
        return jsonify({"ok": True, "items": items})

    @app.post("/api/ops/checkin/complete")
    @role_required("Administrador", "Recepcionista")
    def api_ops_checkin_complete():
        """
        Marca check-in para una reserva.
        Firma e imagen del documento son **opcionales**.
        Acepta JSON o multipart/form-data.
        """
        reserva_id = None
        documento = None
        firma_b64 = None
        docfile = None

        if request.content_type and request.content_type.startswith("multipart/form-data"):
            reserva_id = request.form.get("reserva_id")
            documento = (request.form.get("documento") or "").strip()
            firma_b64 = request.form.get("firma_base64")
            docfile = request.files.get("docfile")
        else:
            p = request.get_json(silent=True) or {}
            reserva_id = p.get("reserva_id")
            documento = (p.get("documento") or "").strip()
            firma_b64 = p.get("guardar_firma")

        try:
            reserva_id = int(reserva_id)
        except Exception:
            return jsonify({"ok": False, "error": "reserva_id_invalid"}), 400
        if not documento:
            return jsonify({"ok": False, "error": "documento_required"}), 400

        # Cargar reserva + cliente
        r = db.session.execute(text("""
            SELECT R.Codigo_Reserva, R.Codigo_Cliente, R.Estado,
                   C.Cedula AS CedulaCliente, C.Nombre, C.Apellido,
                   R.Codigo_Habitacion
              FROM Reserva R
              JOIN Cliente C ON C.Codigo_Cliente = R.Codigo_Cliente
             WHERE R.Codigo_Reserva = :rid
             LIMIT 1
        """), {"rid": reserva_id}).mappings().first()
        if not r:
            return jsonify({"ok": False, "error": "reserva_not_found"}), 404

        # Validar documento contra perfil (Cliente o Usuario vinculado)
        doc_ok = False
        if r.get("CedulaCliente") is not None and str(r["CedulaCliente"]) == documento:
            doc_ok = True
        else:
            row_u = db.session.execute(text("""
                SELECT 1 FROM Usuario
                 WHERE Codigo_Cliente = :cid
                   AND Cedula_Pasaporte = :doc
                 LIMIT 1
            """), {"cid": r["Codigo_Cliente"], "doc": documento}).first()
            doc_ok = bool(row_u)
        if not doc_ok:
            return jsonify({"ok": False, "error": "documento_mismatch",
                            "message": "El documento no coincide con el perfil que realizó la reserva."}), 409

        # Guardar firma (opcional, base64)
        saved_files = []
        if firma_b64:
            try:
                import base64
                raw = firma_b64.split(",")[-1]
                data = base64.b64decode(raw)
                fpath = GUEST_DOCS_DIR / f"firma-R{reserva_id}-{datetime.utcnow().strftime('%Y%m%d%H%M%S')}.png"
                with open(fpath, "wb") as f:
                    f.write(data)
                saved_files.append(("GuestSignature", fpath))
            except Exception as e:
                current_app.logger.warning(f"[CHECKIN] No se pudo guardar firma: {e}")

        # Guardar imagen/PDF del documento (opcional)
        if docfile:
            try:
                fn = secure_filename(docfile.filename or f"doc-R{reserva_id}.bin")
                fpath = GUEST_DOCS_DIR / f"{datetime.utcnow():%Y%m%d%H%M%S}-{fn}"
                docfile.save(str(fpath))
                saved_files.append(("GuestDoc", fpath))
            except Exception as e:
                current_app.logger.warning(f"[CHECKIN] No se pudo guardar doc: {e}")

        # Relacionar en tablas Documento/ReservaDocumento si existen
        for tipo, pth in saved_files:
            try:
                res = db.session.execute(
                    text("INSERT INTO Documento (Tipo, Ruta, MimeType, TamanoBytes) VALUES (:t,:r,:m,:s)"),
                    {"t": tipo, "r": f"/storage/guest_docs/{Path(pth).name}",
                     "m": "image/png" if pth.suffix.lower()==".png" else "application/octet-stream",
                     "s": Path(pth).stat().st_size}
                )
                doc_id = res.lastrowid
                db.session.execute(
                    text("INSERT INTO ReservaDocumento (Codigo_Reserva, Documento_Id) VALUES (:r,:d)"),
                    {"r": reserva_id, "d": doc_id}
                )
                db.session.commit()
            except Exception as e:
                db.session.rollback()
                current_app.logger.warning(f"[CHECKIN] No se pudo asociar documento: {e}")

        # Marcar la reserva como 'En Casa' (o el estado que uses para check-in)
        try:
            db.session.execute(
                text("UPDATE Reserva SET Estado = 'En Casa' WHERE Codigo_Reserva = :r"),
                {"r": reserva_id}
            )
            db.session.commit()
        except Exception:
            db.session.rollback()

        # Activación de llave (opcional)
        try:
            _activate_e_key(r.get("Codigo_Habitacion"), reserva_id, r.get("Codigo_Cliente"))
        except Exception:
            pass

        _audit_log(_current_user_email(), "checkin.completed",
                   {"reserva_id": reserva_id, "documento": documento}, entidad_id=str(reserva_id))

        return jsonify({"ok": True, "reserva_id": reserva_id})

    @app.post("/api/ops/walkin")
    @role_required("Administrador", "Recepcionista")
    def api_ops_walkin():
        """
        Crea una estancia sin reserva previa para un documento dado y fechas.
        Intenta auto-asignar habitación.
        """
        p = request.get_json(silent=True) or {}
        doc = (p.get("documento") or "").strip()
        name = (p.get("nombre") or "").strip() or "Walk-in"
        ci = (p.get("checkin") or "").strip()
        co = (p.get("checkout") or "").strip()
        if not doc or not ci or not co:
            return jsonify({"ok": False, "error": "missing_params"}), 400

        # Asegurar/obtener Cliente por documento
        cli = db.session.execute(text("""
            SELECT Codigo_Cliente, Correo, Nombre, Apellido FROM Cliente
             WHERE CAST(Cedula AS CHAR) = :d
             LIMIT 1
        """), {"d": doc}).mappings().first()
        if not cli:
            # crear cliente mínimo
            nom, ape = (name.split(" ",1)+[""])[:2]
            res = db.session.execute(text("""
                INSERT INTO Cliente (Cedula, Nombre, Apellido, Telefono, Correo, Fecha_Nacimiento)
                VALUES (:ced,:n,:a,'',NULL,'1990-01-01')
            """), {"ced": doc, "n": nom[:50], "a": ape[:50]})
            db.session.commit()
            cid = res.lastrowid
        else:
            cid = int(cli["Codigo_Cliente"])

        # Crear reserva mínima
        rres = db.session.execute(text("""
            INSERT INTO Reserva (Codigo_Cliente, Codigo_Habitacion, Fecha_Entrada, Fecha_Salida,
                                 Estado, Canal, Monto_Total, Huespedes, Observaciones, Fecha_Registro)
            VALUES (:c, NULL, :ci, :co, 'Pendiente', 'FrontDesk', 0, '1', 'Walk-in creado en recepción', NOW())
        """), {"c": cid, "ci": ci, "co": co})
        db.session.commit()
        rid = int(rres.lastrowid)

        # Crear número/comprobante si aplica
        numero = _make_unique_number(rid, ci)
        try:
            db.session.execute(text("UPDATE Reserva SET Numero_Comprobante=:n WHERE Codigo_Reserva=:r"),
                               {"n": numero, "r": rid})
            db.session.commit()
        except Exception:
            db.session.rollback()

        # Auto-asignar
        try:
            auto_assign_for_reserva(rid, preferir_tipo=True, allow_split=False)
        except Exception as e:
            current_app.logger.warning(f"[WALKIN] auto-assign fallo: {e}")

        info = db.session.execute(text("""
            SELECT R.Codigo_Reserva, R.Numero_Comprobante AS Numero,
                   H.Numero_Habitacion
              FROM Reserva R
              LEFT JOIN Habitacion H ON H.Codigo_Habitacion = R.Codigo_Habitacion
             WHERE R.Codigo_Reserva = :r
             LIMIT 1
        """), {"r": rid}).mappings().first()

        _audit_log(_current_user_email(), "walkin.created", {"reserva_id": rid, "documento": doc}, entidad_id=str(rid))

        return jsonify({"ok": True, "reserva_id": rid, "numero": info.get("Numero") if info else numero,
                        "habitacion": (info.get("Numero_Habitacion") if info else None)})

    # --------- Endpoints anteriores mantenidos (compatibilidad) ---------

    def _reservas_hoy_por_documento(doc_number: str):
        return _reservas_by_doc_when(doc_number, date.today().isoformat())

    @app.get("/api/checkin/find")
    @role_required("Administrador", "Recepcionista")
    def api_checkin_find():
        doc = _normalize_docnum(request.args.get("doc"))
        if not doc:
            return jsonify({"ok": False, "error": "doc_required"}), 400
        matches = _reservas_hoy_por_documento(doc)
        if not matches:
            return jsonify({"ok": True, "items": [], "message": "No hay reservas para hoy con ese documento."})
        return jsonify({"ok": True, "items": matches})

    @app.post("/api/checkin")
    @role_required("Administrador", "Recepcionista")
    def api_checkin_confirm():
        """
        Wrapper: usa la lógica de /api/ops/checkin/complete
        """
        # Adaptar nombres y redirigir internamente
        if request.content_type and request.content_type.startswith("multipart/form-data"):
            form = request.form.copy()
            # map: doc_number -> documento
            if form.get("doc_number") and not form.get("documento"):
                form = form.to_dict(flat=True)
                form["documento"] = form.pop("doc_number")
            # enviar a función objetivo
            with app.test_request_context(
                "/api/ops/checkin/complete",
                method="POST",
                data=request.form,
                content_type=request.content_type,
                environ_base=request.environ
            ):
                return api_ops_checkin_complete()
        else:
            p = request.get_json(silent=True) or {}
            if p.get("doc_number") and not p.get("documento"):
                p["documento"] = p["doc_number"]
            with app.test_request_context("/api/ops/checkin/complete", method="POST", json=p):
                return api_ops_checkin_complete()

    # ========= FIN-INV-01 — Gestión de Facturas =========
    def allowed_file(filename):
        return "." in filename and filename.rsplit(".", 1)[1].lower() in {"png", "jpg", "jpeg", "pdf"}

    class FinInvoice(db.Model):
        __tablename__ = "fin_invoices"
        id_factura = db.Column(db.Integer, primary_key=True)
        numero = db.Column(db.String(50), unique=True, nullable=False)  # Ej: VG-20251011-0001
        cliente_nombre = db.Column(db.String(120))
        cliente_email = db.Column(db.String(120), index=True)
        moneda = db.Column(db.String(10), default="CRC")
        monto_total = db.Column(db.Numeric(12, 2), nullable=False)
        descripcion = db.Column(db.Text)
        archivo_path = db.Column(db.String(255))  # PDF subido (opcional)
        id_reserva = db.Column(db.Integer)  # vínculo opcional con reserva
        id_usuario = db.Column(db.Integer, nullable=False)  # quien la emitió
        fecha_emision = db.Column(db.DateTime, default=datetime.utcnow)
        estado = db.Column(db.Enum("Borrador", "Emitida", "Pagada", "Anulada"), default="Emitida")

    INVOICE_UPLOAD_FOLDER = os.path.join(BASE_DIR, "static", "uploads", "invoices")
    os.makedirs(INVOICE_UPLOAD_FOLDER, exist_ok=True)

    @app.route("/fin-invoices.html", methods=["GET"])
    @login_required
    @role_required("Administrador", "Recepcionista")
    def fin_invoices_html():
        facturas = FinInvoice.query.order_by(FinInvoice.fecha_emision.desc()).limit(200).all()
        return render_template("fin-invoices.html", facturas=facturas)

    @app.route("/fin/invoices/nuevo", methods=["POST"])
    @login_required
    @role_required("Administrador", "Recepcionista")
    def fin_invoice_nuevo():
        numero = (request.form.get("numero") or "").strip()
        cliente_nombre = (request.form.get("cliente_nombre") or "").strip()
        cliente_email = (request.form.get("cliente_email") or "").strip().lower()
        moneda = (request.form.get("moneda") or "CRC").strip()[:10]
        monto_total = request.form.get("monto_total")
        descripcion = (request.form.get("descripcion") or "").strip()
        id_reserva = request.form.get("id_reserva")

        if not numero or not monto_total:
            flash("Debe indicar número de factura y monto.", "warning")
            return redirect(url_for("fin_invoices_html"))

        if FinInvoice.query.filter_by(numero=numero).first():
            flash("El número de factura ya existe.", "danger")
            return redirect(url_for("fin_invoices_html"))

        archivo = request.files.get("archivo")
        filename = None
        if archivo and allowed_file(archivo.filename):
            filename = secure_filename(archivo.filename)
            save_path = os.path.join(INVOICE_UPLOAD_FOLDER, filename)
            archivo.save(save_path)

        factura = FinInvoice(
            numero=numero,
            cliente_nombre=cliente_nombre or None,
            cliente_email=cliente_email or None,
            moneda=moneda or "CRC",
            monto_total=monto_total,
            descripcion=descripcion or None,
            archivo_path=filename,
            id_reserva=int(id_reserva) if id_reserva else None,
            id_usuario=session["user_id"],
            estado="Emitida",
        )
        db.session.add(factura)
        db.session.commit()

        flash("Factura registrada correctamente.", "success")
        return redirect(url_for("fin_invoices_html"))

    @app.route("/fin/invoices/<int:id>/pagar", methods=["POST"])
    @login_required
    @role_required("Administrador", "Recepcionista")
    def fin_invoice_pagar(id):
        factura = FinInvoice.query.get_or_404(id)
        if factura.estado not in ("Emitida", "Borrador"):
            flash("Solo se pueden marcar como pagadas las facturas emitidas o en borrador.", "warning")
            return redirect(url_for("fin_invoices_html"))
        factura.estado = "Pagada"
        db.session.commit()
        flash("Factura marcada como Pagada.", "success")
        return redirect(url_for("fin_invoices_html"))

    @app.route("/fin/invoices/<int:id>/anular", methods=["POST"])
    @login_required
    @role_required("Administrador")
    def fin_invoice_anular(id):
        factura = FinInvoice.query.get_or_404(id)
        if factura.estado == "Anulada":
            flash("La factura ya está anulada.", "info")
            return redirect(url_for("fin_invoices_html"))
        factura.estado = "Anulada"
        db.session.commit()
        flash("Factura anulada.", "info")
        return redirect(url_for("fin_invoices_html"))

    # ====== FAC-07-002: Libro Mayor / POS ======
    class FinLedgerTx(db.Model):
        __tablename__ = "fin_ledger_tx"
        id_tx = db.Column(db.Integer, primary_key=True)
        external_id = db.Column(db.String(64), unique=True, nullable=False)
        source = db.Column(db.Enum("POS", "BACKOFFICE"), default="POS", nullable=False)
        reserva_id = db.Column(db.Integer)
        currency = db.Column(db.String(10), default="CRC", nullable=False)
        total = db.Column(db.Numeric(14, 2), default=0, nullable=False)
        status = db.Column(db.Enum("posted", "voided"), default="posted", nullable=False)
        created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
        meta = db.Column(db.JSON)
        lines = db.relationship("FinLedgerLine", backref="tx", cascade="all, delete-orphan")

    class FinLedgerLine(db.Model):
        __tablename__ = "fin_ledger_line"
        id_line = db.Column(db.Integer, primary_key=True)
        id_tx = db.Column(db.Integer, db.ForeignKey("fin_ledger_tx.id_tx"), nullable=False)
        line_no = db.Column(db.Integer, nullable=False)
        account = db.Column(db.String(64), nullable=False)
        debit = db.Column(db.Numeric(14, 2), default=0, nullable=False)
        credit = db.Column(db.Numeric(14, 2), default=0, nullable=False)
        description = db.Column(db.String(255))

    @app.post("/api/pos/tx")
    def api_pos_tx():
        api_key = request.headers.get("X-POS-KEY")
        if not api_key or api_key != current_app.config.get("POS_API_KEY", "dev-pos-key"):
            return jsonify({"ok": False, "error": "unauthorized"}), 401

        payload = request.get_json(silent=True) or {}
        ext_id = (payload.get("external_id") or "").strip()
        if not ext_id:
            return jsonify({"ok": False, "error": "external_id_required"}), 400

        if FinLedgerTx.query.filter_by(external_id=ext_id).first():
            return jsonify({"ok": True, "duplicate": True})

        reserva_id = payload.get("reserva_id")
        currency = (payload.get("currency") or "CRC")[:10]
        total = float(payload.get("total") or 0)
        lines_in = payload.get("lines") or []
        meta = payload.get("meta") or {}

        if not lines_in:
            return jsonify({"ok": False, "error": "lines_required"}), 400

        tx = FinLedgerTx(
            external_id=ext_id,
            source="POS",
            reserva_id=int(reserva_id) if reserva_id else None,
            currency=currency,
            total=total,
            status="posted",
            meta=meta,
        )
        db.session.add(tx)
        db.session.flush()

        total_debit = 0.0
        total_credit = 0.0
        for ln in lines_in:
            line = FinLedgerLine(
                id_tx=tx.id_tx,
                line_no=int(ln.get("line_no") or 0),
                account=(ln.get("account") or "").strip()[:64] or "UNDEF",
                debit=float(ln.get("debit") or 0),
                credit=float(ln.get("credit") or 0),
                description=(ln.get("description") or "").strip()[:255] or None,
            )
            total_debit += float(line.debit or 0)
            total_credit += float(line.credit or 0)
            db.session.add(line)

        if round(total_debit - total_credit, 2) != 0.00:
            current_app.logger.warning(f"[POS] Descuadre {ext_id}: debit={total_debit} credit={total_credit}")

        if tx.reserva_id:
            db.session.execute(
                text("UPDATE Reserva SET Monto_Pagado = Monto_Pagado + :p WHERE Codigo_Reserva = :r"),
                {"p": total_debit, "r": tx.reserva_id},
            )

        db.session.commit()
        return jsonify({"ok": True, "id_tx": tx.id_tx, "posted_debit": total_debit, "posted_credit": total_credit})

    def _apply_pos_tx_to_reserva(reserva_id: int, total: float, external_id: str):
        try:
            db.session.execute(
                text(
                    """
                UPDATE Reserva
                   SET Estado = CASE WHEN Estado='Pendiente' THEN 'Confirmada' ELSE Estado END,
                       Canal  = 'POS'
                 WHERE Codigo_Reserva = :rid
                 LIMIT 1
            """
                ),
                {"rid": reserva_id},
            )
            db.session.commit()
        except Exception as e:
            current_app.logger.warning(f"[POS][RESERVA] No se pudo actualizar R={reserva_id}: {e}")
            db.session.rollback()

        try:
            _audit_log(None, "pos.tx.posted", {"reserva_id": reserva_id, "external_id": external_id, "total": float(total)})
        except Exception:
            pass

    @app.post("/api/pos/ledger")
    def api_pos_ledger():
        api_key = request.headers.get("X-Api-Key") or request.headers.get("Authorization", "").replace("Bearer ", "")
        expected = app.config.get("POS_API_KEY") or os.getenv("POS_API_KEY") or "dev-pos-key"
        if not api_key or api_key != expected:
            return jsonify({"ok": False, "error": "unauthorized"}), 401

        payload = request.get_json(silent=True) or {}
        external_id = (payload.get("external_id") or "").strip()
        reserva_id = payload.get("reserva_id")
        currency = (payload.get("currency") or "CRC").strip()[:10]
        lines = payload.get("lines") or []
        meta = payload.get("meta") or {}

        if not external_id or not isinstance(lines, list) or not lines:
            return jsonify({"ok": False, "error": "invalid_payload"}), 400

        existing = db.session.query(FinLedgerTx).filter_by(external_id=external_id).first()
        if existing:
            return jsonify({"ok": True, "id_tx": existing.id_tx, "status": "already_posted"})

        total_debit = sum(float(x.get("debit") or 0) for x in lines)
        total_credit = sum(float(x.get("credit") or 0) for x in lines)
        if round(total_debit - total_credit, 2) != 0.00:
            return jsonify({"ok": False, "error": "unbalanced_entry", "debit": total_debit, "credit": total_credit}), 422

        total = max(total_debit, total_credit)

        tx = FinLedgerTx(
            external_id=external_id,
            source="POS",
            reserva_id=int(reserva_id) if reserva_id else None,
            currency=currency,
            total=total,
            status="posted",
            meta=meta,
        )
        db.session.add(tx)
        db.session.flush()

        for idx, ln in enumerate(lines, start=1):
            db.session.add(
                FinLedgerLine(
                    id_tx=tx.id_tx,
                    line_no=idx,
                    account=(ln.get("account") or "").strip()[:64] or "UNASSIGNED",
                    debit=float(ln.get("debit") or 0),
                    credit=float(ln.get("credit") or 0),
                    description=(ln.get("description") or "")[:255] or None,
                )
            )

        db.session.commit()

        if reserva_id:
            try:
                _apply_pos_tx_to_reserva(int(reserva_id), total, external_id)
            except Exception as e:
                current_app.logger.warning(f"[POS] apply reserva failed: {e}")

        return jsonify({"ok": True, "id_tx": tx.id_tx})

    # ========= HU 3 para finanzas: Recibos / Notas =========
    class FinReceipt(db.Model):
        __tablename__ = "fin_receipts"
        id_receipt = db.Column(db.Integer, primary_key=True)
        numero = db.Column(db.String(40), unique=True, nullable=False)
        reserva_id = db.Column(db.Integer)
        invoice_id = db.Column(db.Integer)
        tx_id = db.Column(db.Integer)
        metodo = db.Column(db.String(30), default="Tarjeta", nullable=False)
        currency = db.Column(db.String(10), default="CRC", nullable=False)
        monto = db.Column(db.Numeric(14, 2), nullable=False)
        emitido_por = db.Column(db.Integer, nullable=False)
        creado_en = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
        estado = db.Column(db.Enum("Emitido", "Anulado"), default="Emitido", nullable=False)

    class FinNote(db.Model):
        __tablename__ = "fin_notes"
        id_note = db.Column(db.Integer, primary_key=True)
        numero = db.Column(db.String(40), unique=True, nullable=False)
        tipo = db.Column(db.Enum("Credito", "Debito"), nullable=False)
        ref_invoice = db.Column(db.Integer)
        ref_reserva = db.Column(db.Integer)
        currency = db.Column(db.String(10), default="CRC", nullable=False)
        monto_abs = db.Column(db.Numeric(14, 2), nullable=False)
        motivo = db.Column(db.String(255))
        emitido_por = db.Column(db.Integer, nullable=False)
        creado_en = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
        estado = db.Column(db.Enum("Emitida", "Anulada"), default="Emitida", nullable=False)

    def _make_seq(prefix: str, nid: int) -> str:
        return f"{prefix}-{datetime.utcnow():%Y%m%d}-{nid:04d}"

    _BRAND_NAME = "Hotel Villa Grace"

    def _asset_logo_path() -> Optional[str]:
        for rel in [
            "static/assets/img/favicon.png",
            "static/assets/img/apple-touch-icon.png",
            "static/assets/img/logo.png",
        ]:
            p = BASE_DIR / rel
            if p.exists():
                return str(p)
        return None

    def _format_money_crc(v: float, currency: str = "CRC") -> str:
        s = f"{float(v):,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")
        if (currency or "CRC").upper() == "USD":
            return f"$ {s}"
        return f"₡ {s}"

    def _draw_header(canvas, title: str):
        from reportlab.lib.pagesizes import LETTER
        width, height = LETTER
        logo = _asset_logo_path()

        canvas.setFillColorRGB(0.09, 0.33, 0.25)
        canvas.rect(0, height - 60, width, 60, stroke=0, fill=1)

        x = 36
        y = height - 54
        if logo:
            try:
                canvas.drawImage(logo, x, y - 24, width=24, height=24, preserveAspectRatio=True, mask="auto")
                x += 32
            except Exception:
                pass

        canvas.setFillColorRGB(1, 1, 1)
        canvas.setFont("Helvetica-Bold", 14)
        canvas.drawString(x, height - 40, _BRAND_NAME)

        canvas.setFont("Helvetica-Bold", 12)
        canvas.drawRightString(width - 36, height - 40, title)

    def _kv_table(canvas, rows, y_start, title=None):
        from reportlab.lib.pagesizes import LETTER
        width, height = LETTER
        left = 48
        right = width - 48
        y = y_start

        if title:
            canvas.setFont("Helvetica-Bold", 11)
            canvas.setFillColorRGB(0.09, 0.33, 0.25)
            canvas.drawString(left, y, title)
            y -= 10

        canvas.setFillColorRGB(0, 0, 0)
        canvas.setFont("Helvetica", 10)
        for k, v in rows:
            if y < 90:
                canvas.showPage()
                _draw_header(canvas, title or "")
                y = height - 100
                canvas.setFont("Helvetica", 10)
            canvas.setFillColorRGB(0.15, 0.15, 0.15)
            canvas.drawString(left, y, str(k))
            canvas.setFillColorRGB(0, 0, 0)
            canvas.drawRightString(right, y, str(v))
            y -= 16

        canvas.setStrokeColorRGB(0.85, 0.85, 0.85)
        canvas.line(left, y, right, y)
        y -= 10
        return y

    def _footer(canvas, note=""):
        from reportlab.lib.pagesizes import LETTER
        width, height = LETTER
        canvas.setFont("Helvetica-Oblique", 9)
        canvas.setFillColorRGB(0.35, 0.35, 0.35)
        canvas.drawString(48, 60, note or "Gracias por su preferencia.")

    def _create_receipt_pdf(recibo: FinReceipt) -> Path:
        numero = recibo.numero
        filename = f"{numero}.pdf"
        out = RECIBOS_DIR / filename
        try:
            from reportlab.lib.pagesizes import LETTER
            from reportlab.pdfgen import canvas as _cv

            c = _cv.Canvas(str(out), pagesize=LETTER)
            _draw_header(c, "Recibo de pago")

            width, height = LETTER
            y = height - 100

            c.setFillColorRGB(0.96, 0.98, 0.97)
            c.roundRect(36, y - 50, width - 72, 50, 8, stroke=0, fill=1)
            c.setFillColorRGB(0.09, 0.33, 0.25)
            c.setFont("Helvetica-Bold", 12)
            c.drawString(48, y - 20, f"Número: {numero}")
            c.setFont("Helvetica", 10)
            c.setFillColorRGB(0.2, 0.2, 0.2)
            c.drawRightString(width - 48, y - 20, recibo.creado_en.strftime("%Y-%m-%d %H:%M"))

            y -= 70

            rows = [
                ("Método", recibo.metodo),
                ("Moneda", recibo.currency),
                ("Monto", _format_money_crc(recibo.monto, recibo.currency)),
                ("Reserva ID", recibo.reserva_id or "-"),
                ("Factura ID", recibo.invoice_id or "-"),
                ("Tx POS ID", recibo.tx_id or "-"),
            ]
            _kv_table(c, rows, y, title="Detalle del pago")

            _footer(c, "Gracias por su pago.")
            c.showPage()
            c.save()
            return out
        except Exception:
            lines = [
                "COMPROBANTE DE PAGO",
                "",
                f"Número:        {numero}",
                f"Fecha:         {recibo.creado_en:%Y-%m-%d %H:%M}",
                f"Método:        {recibo.metodo}",
                f"Moneda:        {recibo.currency}",
                f"Monto:         {_format_money_crc(recibo.monto, recibo.currency)}",
                f"Reserva ID:    {recibo.reserva_id or '-'}",
                f"Factura ID:    {recibo.invoice_id or '-'}",
                f"Tx POS ID:     {recibo.tx_id or '-'}",
                "",
                "Gracias por su pago.",
            ]
            _write_minimal_pdf(out, f"{_BRAND_NAME}  Recibo", lines)
            return out

    def _create_note_pdf(nota: FinNote) -> Path:
        numero = nota.numero
        filename = f"{numero}.pdf"
        out = NOTAS_DIR / filename
        signo = "-" if nota.tipo == "Credito" else "+"
        try:
            from reportlab.lib.pagesizes import LETTER
            from reportlab.pdfgen import canvas as _cv

            c = _cv.Canvas(str(out), pagesize=LETTER)
            title = f"Nota de {'Crédito' if nota.tipo=='Credito' else 'Débito'}"
            _draw_header(c, title)

            width, height = LETTER
            y = height - 100

            c.setFillColorRGB(0.98, 0.98, 0.98)
            c.roundRect(36, y - 50, width - 72, 50, 8, stroke=0, fill=1)
            c.setFillColorRGB(0.09, 0.33, 0.25)
            c.setFont("Helvetica-Bold", 12)
            c.drawString(48, y - 20, f"Número: {numero}")
            c.setFont("Helvetica", 10)
            c.setFillColorRGB(0.2, 0.2, 0.2)
            c.drawRightString(width - 48, y - 20, nota.creado_en.strftime("%Y-%m-%d %H:%M"))

            y -= 70

            rows = [
                ("Tipo", "Crédito" if nota.tipo == "Credito" else "Débito"),
                ("Moneda", nota.currency),
                ("Importe", f"{signo}{_format_money_crc(nota.monto_abs, nota.currency)}"),
                ("Reserva ID", nota.ref_reserva or "-"),
                ("Factura ID", nota.ref_invoice or "-"),
                ("Motivo", nota.motivo or "-"),
            ]
            _kv_table(c, rows, y, title="Detalle")

            _footer(c, "Documento generado digitalmente.")
            c.showPage()
            c.save()
            return out
        except Exception:
            lines = [
                f"NOTA DE {nota.tipo.upper()}",
                "",
                f"Número:        {numero}",
                f"Fecha:         {nota.creado_en:%Y-%m-%d %H:%M}",
                f"Moneda:        {nota.currency}",
                f"Importe:       {signo}{_format_money_crc(nota.monto_abs, nota.currency)}",
                f"Aplica a Res.: {nota.ref_reserva or '-'}   Fact.: {nota.ref_invoice or '-'}",
                f"Motivo:        {nota.motivo or '-'}",
            ]
            _write_minimal_pdf(out, f"{_BRAND_NAME} — Nota", lines)
            return out

    @app.post("/api/fin/receipts")
    @login_required
    @role_required("Administrador", "Recepcionista")
    def api_fin_receipts_new():
        payload = request.get_json(silent=True) or {}
        reserva_id = payload.get("reserva_id")
        invoice_id = payload.get("invoice_id")
        tx_id = payload.get("tx_id")
        metodo = (payload.get("metodo") or "Tarjeta")[:30]
        currency = (payload.get("currency") or "CRC")[:10]
        monto = float(payload.get("monto") or 0)

        if monto <= 0:
            return jsonify({"ok": False, "error": "monto_invalid"}), 400

        rec = FinReceipt(
            numero="PENDING",
            reserva_id=int(reserva_id) if reserva_id else None,
            invoice_id=int(invoice_id) if invoice_id else None,
            tx_id=int(tx_id) if tx_id else None,
            metodo=metodo,
            currency=currency,
            monto=monto,
            emitido_por=session["user_id"],
        )
        db.session.add(rec)
        db.session.flush()
        rec.numero = _make_seq("RC", rec.id_receipt)

        if rec.reserva_id:
            db.session.execute(
                text(
                    """
                UPDATE Reserva
                   SET Monto_Pagado = Monto_Pagado + :p,
                       Fecha_Ultimo_Pago = NOW(),
                       Estado = CASE
                                 WHEN Estado='Pendiente' AND (Monto_Pagado + :p) >= Monto_Total
                                 THEN 'Confirmada' ELSE Estado
                               END
                 WHERE Codigo_Reserva = :r
            """
                ),
                {"p": monto, "r": rec.reserva_id},
            )

        db.session.commit()

        _create_receipt_pdf(rec)
        return jsonify(
            {"ok": True, "id_receipt": rec.id_receipt, "numero": rec.numero, "pdf": f"/api/fin/receipts/{rec.id_receipt}/pdf"}
        )

    @app.get("/api/fin/receipts/<int:rid>/pdf")
    @login_required
    @role_required("Administrador", "Recepcionista", "Cliente")
    def api_fin_receipt_pdf(rid: int):
        rec = FinReceipt.query.get_or_404(rid)
        path = RECIBOS_DIR / f"{rec.numero}.pdf"
        if not path.exists():
            _create_receipt_pdf(rec)
        return send_file(str(path), as_attachment=True, download_name=f"{rec.numero}.pdf")

    @app.post("/api/fin/notes")
    @login_required
    @role_required("Administrador", "Recepcionista")
    def api_fin_notes_new():
        payload = request.get_json(silent=True) or {}
        tipo = (payload.get("tipo") or "").capitalize()
        if tipo not in ("Credito", "Debito"):
            return jsonify({"ok": False, "error": "tipo_invalid"}), 400

        ref_invoice = payload.get("invoice_id")
        ref_reserva = payload.get("reserva_id")
        currency = (payload.get("currency") or "CRC")[:10]
        monto_abs = float(payload.get("monto_abs") or 0)
        motivo = (payload.get("motivo") or "").strip()[:255]

        if monto_abs <= 0:
            return jsonify({"ok": False, "error": "monto_invalid"}), 400

        note = FinNote(
            numero="PENDING",
            tipo=tipo,
            ref_invoice=int(ref_invoice) if ref_invoice else None,
            ref_reserva=int(ref_reserva) if ref_reserva else None,
            currency=currency,
            monto_abs=monto_abs,
            motivo=motivo,
            emitido_por=session["user_id"],
        )
        db.session.add(note)
        db.session.flush()
        prefix = "NC" if tipo == "Credito" else "ND"
        note.numero = _make_seq(prefix, note.id_note)
        db.session.commit()

        _create_note_pdf(note)
        return jsonify({"ok": True, "id_note": note.id_note, "numero": note.numero, "pdf": f"/api/fin/notes/{note.id_note}/pdf"})

    @app.get("/api/fin/notes/<int:nid>/pdf")
    @login_required
    @role_required("Administrador", "Recepcionista", "Cliente")
    def api_fin_note_pdf(nid: int):
        note = FinNote.query.get_or_404(nid)
        path = NOTAS_DIR / f"{note.numero}.pdf"
        if not path.exists():
            _create_note_pdf(note)
        return send_file(str(path), as_attachment=True, download_name=f"{note.numero}.pdf")

    return app


# =========================
# EJECUCIÓN
# =========================
if __name__ == "__main__":
    app = create_app()
    try:
        print(
            f"DB -> {os.getenv('DB_USER','root')} @ {os.getenv('DB_HOST','127.0.0.1')} : {os.getenv('DB_PORT','3306')} / {os.getenv('DB_NAME','Hotel_VillaGrace')}"
        )
        print(f"Comprobantes -> {COMPROBANTES_DIR}")
        print(f"Guest Docs -> {GUEST_DOCS_DIR}")
    except Exception:
        pass
    app.run(host="0.0.0.0", port=5000, debug=True)
