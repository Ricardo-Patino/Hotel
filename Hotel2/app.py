from __future__ import annotations
# app.py — Aplicación principal Flask para Hotel_VillaGrace
# Ejecuta:  python "Hotel 2/app.py"

import os
import sys
import smtplib
import ssl
import json
import csv
from functools import wraps
from email.message import EmailMessage
from datetime import datetime, date
from pathlib import Path
from typing import Optional
from werkzeug.utils import secure_filename
import reportlab  # noqa
import re

from functools import lru_cache
from sqlalchemy import text, func, inspect
from sqlalchemy import text as _text
from sqlalchemy.exc import IntegrityError
from itsdangerous import URLSafeTimedSerializer, BadSignature, SignatureExpired
from werkzeug.security import check_password_hash, generate_password_hash
from datetime import datetime, date
from flask import session, render_template
from models_sql import Usuario
from blueprints.fin_kpi import fin_kpi_bp

from blueprints.pos import pos_bp
from blueprints.fin_kpi import fin_kpi_bp
from blueprints.fin_periods import fin_periods_bp

from datetime import date
from typing import Tuple

from flask import current_app
from sqlalchemy import text

from extensions import db


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

# .env opcionalno
from dotenv import load_dotenv  # type: ignore
load_dotenv(BASE_DIR / ".env")

# Imports del proyecto
from services.grr.assignment import auto_assign_for_reserva, reassign_reserva, split_reserva, merge_reserva  # noqa
from config import Config
from extensions import db, migrate

from extensions import db
from models_sql import Habitacion

from models.hrm import Funcionario, FuncionarioHistorial
import models_sql


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

# Exportaciones (historial PDF/Excel)
EXPORTS_DIR = STORAGE_DIR / "exports"
EXPORTS_DIR.mkdir(parents=True, exist_ok=True)

# Facturas (archivos PDF u otros adjuntos de factura)
INVOICE_UPLOAD_FOLDER = STORAGE_DIR / "invoices"
INVOICE_UPLOAD_FOLDER.mkdir(parents=True, exist_ok=True)



RECIBOS_DIR = STORAGE_DIR / "recibos"
NOTAS_DIR = STORAGE_DIR / "notas"
RECIBOS_DIR.mkdir(parents=True, exist_ok=True)
NOTAS_DIR.mkdir(parents=True, exist_ok=True)

# GRR-01-004: documentos de huésped (ID/firma)
GUEST_DOCS_DIR = STORAGE_DIR / "guest_docs"
GUEST_DOCS_DIR.mkdir(parents=True, exist_ok=True)
ALLOWED_DOC_EXTS = {"png", "jpg", "jpeg", "pdf"}

# --- Impuestos / Reglas de cálculo para Check-out (ajustables por config/env) ---
DEFAULT_IVA = float(os.getenv("IVA_RATE", "0.13"))              # 13% CR por defecto
APPLY_TAX_ON_CONSUMOS = os.getenv("APPLY_TAX_ON_CONSUMOS", "1") in ("1", "true", "True")
ROOM_TOTAL_INCLUDES_TAX = os.getenv("ROOM_TOTAL_INCLUDES_TAX", "1") in ("1", "true", "True")

# =========================
# Helpers (roles/redirects)
# =========================
DEFAULT_ROLES = ("Administrador", "Recepcionista", "Limpieza", "Cliente")

# === Exponer modelos usados en el resto del archivo ===
# (mantén también "import models_sql" como lo dejaste)
Usuario = models_sql.Usuario
Rol = models_sql.Rol
Habitacion = models_sql.Habitacion  # OJO: sin tilde



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
    if getattr(user, "rol", None) and getattr(user.rol, "Nombre", None):
        return user.rol.Nombre
    if getattr(user, "Rol_Id", None):
        rol = Rol.query.filter_by(Codigo_Rol=user.Rol_Id).first()
        if rol and rol.Nombre:
            return rol.Nombre
    
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


# ===== Helpers de validación/normalización (Registro) =====

_JUNK_SEQ = {"0000","1111","2222","3333","4444","5555","6666","7777","8888","9999"}

def _strip_accents(s: str) -> str:
    try:
        import unicodedata
        return ''.join(c for c in unicodedata.normalize('NFD', s) if unicodedata.category(c) != 'Mn')
    except Exception:
        return s

def _is_repeated(s: str, min_len: int = 4) -> bool:
    if not s: return False
    if len(s) < min_len: return False
    return all(ch == s[0] for ch in s)

def _is_sequential_digits(s: str, min_len: int = 4) -> bool:
    digs = ''.join(ch for ch in s if ch.isdigit())
    if len(digs) < min_len: return False
    asc = '0123456789'
    desc = '9876543210'
    return digs in asc or digs in desc

def _validate_name(n: str) -> bool:
    if not n: return False
    n = n.strip()
    if len(n) < 2: return False
    base = _strip_accents(n).replace(" ", "")
    if not base.isalpha(): return False
    if _is_repeated(base): return False
    return True

def _normalize_cr_phone(raw: str) -> str | None:
    if not raw: return None
    s = (raw or '').strip().replace(' ', '')
    if s.startswith('+506'): s = s[4:]
    if s.startswith('506'):  s = s[3:]
    digits = ''.join(ch for ch in s if ch.isdigit())
    if len(digits) != 8: return None
    if _is_repeated(digits) or _is_sequential_digits(digits): return None
    return f'+506{digits}'

def _is_valid_email(e: str) -> bool:
    if not e: return False
    e = e.strip().lower()
    import re as _re
    if not _re.match(r'^[^\s@]+@[^\s@]+\.[^\s@]{2,}$', e): return False
    compact = ''.join(ch for ch in e if ch.isalnum())
    if _is_repeated(compact): return False
    return True

def _normalize_document(raw: str) -> str | None:
    """Acepta: cédula CR (9 dígitos, opcional guiones), DIMEX 11-12 dígitos, pasaporte 6-20 alfanum.
       Devuelve normalizado (solo dígitos para cédula/DIMEX o el alfanum original para pasaporte)."""
    if not raw: return None
    v = (raw or '').strip()
    import re as _re
    if _re.search(r'[A-Za-z]', v):
        # pasaporte
        if _re.fullmatch(r'[A-Za-z0-9]{6,20}', v) and not _is_repeated(v):
            return v.upper()
        return None
    digits = ''.join(ch for ch in v if ch.isdigit())
    if len(digits) == 9 and not _is_repeated(digits) and not _is_sequential_digits(digits):
        return digits  # cédula
    if len(digits) in (11, 12) and not _is_repeated(digits) and not _is_sequential_digits(digits):
        return digits  # DIMEX
    # permitir formato 1-2345-6789 ya cubierto por 9 dígitos arriba
    return None

def _is_strong_password(p: str, email: str) -> bool:
    if not p or len(p) < 8: return False
    import re as _re
    if not _re.search(r'[a-z]', p): return False
    if not _re.search(r'[A-Z]', p): return False
    if not _re.search(r'[0-9]', p): return False
    if _is_repeated(p) or _is_sequential_digits(p): return False
    user = (email or '').split('@')[0].lower()
    if user and user in p.lower(): return False
    return True

def _parse_date(d):
    if isinstance(d, datetime):
        return d.date()
    if isinstance(d, date):
        return d
    if not d:
        return None
    s = str(d)[:10]  # "YYYY-MM-DD"
    for fmt in ("%Y-%m-%d", "%Y/%m/%d", "%d/%m/%Y"):
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            pass
    return None  # si no se pudo parsear

def _nights(checkin, checkout):
    ci = _parse_date(checkin)
    co = _parse_date(checkout)
    if not ci or not co:
        return 0
    return max((co - ci).days, 0)

# =========================
# Decoradores de acceso
# =========================
def _user_role() -> str:
    try:
        return (session.get("user_role") or "").strip() or "Cliente"
    except Exception:
        return "Cliente"


def _is_api_request() -> bool:
    try:
        return (request.path or "").startswith("/api/")
    except Exception:
        return False

def login_required(fn):
    @wraps(fn)
    def wrapper(*args, **kwargs):
        if not session.get("user_id"):
            if _is_api_request():
                return jsonify({"ok": False, "error": "unauthorized", "message": "No autenticado."}), 401
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
                if _is_api_request():
                    return jsonify({"ok": False, "error": "unauthorized", "message": "No autenticado."}), 401
                flash("Inicia sesión para continuar.", "warning")
                return redirect(url_for("login_html", next=request.path))

            current = _user_role().lower()
            if current not in roles_norm:
                if _is_api_request():
                    return jsonify({"ok": False, "error": "forbidden", "message": "No autorizado."}), 403
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

# === NUEVO: Helpers para edición de reserva (GRR-01-011) ===

def _room_is_available_for(habitacion_id: int, ci: str, co: str, exclude_reserva_id: Optional[int] = None) -> tuple[bool, str]:
    """
    Valida que la MISMA habitación esté libre en [ci, co) excluyendo la propia reserva.
    """
    try:
        cond_excl = ""
        params = {"h": int(habitacion_id), "ci": ci, "co": co}
        if exclude_reserva_id:
            cond_excl = " AND r.Codigo_Reserva <> :rid "
            params["rid"] = int(exclude_reserva_id)

        row = db.session.execute(
            text(f"""
                SELECT 1
                  FROM Reserva r
                 WHERE r.Codigo_Habitacion = :h
                   AND r.Estado IN ('Confirmada','Pendiente')
                   AND DATE(r.Fecha_Entrada) < DATE(:co)
                   AND DATE(r.Fecha_Salida)  > DATE(:ci)
                   {cond_excl}
                 LIMIT 1
            """), params
        ).first()
        return (row is None), ("" if row is None else "Solape con otra reserva.")
    except Exception as e:
        current_app.logger.warning(f"[room availability] {e}")
        return False, "Error validando disponibilidad."

def _get_room_info(hid: int) -> dict:
    """
    Devuelve info de habitación (precio, capacidad, tipo) con tolerancia a columnas faltantes.
    """
    try:
        cols = db.session.execute(
            text("""
                SELECT COLUMN_NAME
                  FROM information_schema.COLUMNS
                 WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME='Habitacion'
            """)
        ).mappings().all()
        names = {c["COLUMN_NAME"] for c in cols}
        cap_col = "Capacidad" if "Capacidad" in names else None
        tipo_col = "Tipo" if "Tipo" in names else None

        sel = ["Precio_Noche"]
        if cap_col: sel.append(cap_col)
        if tipo_col: sel.append(tipo_col)

        row = db.session.execute(
            text(f"""
                SELECT {", ".join(sel)}
                  FROM Habitacion
                 WHERE Codigo_Habitacion = :h
                 LIMIT 1
            """),
            {"h": int(hid)}
        ).first()
        if not row: return {"price": 0.0, "capacity": None, "tipo": None}

        d = {"price": float(row[0] or 0.0)}
        idx = 1
        if cap_col:
            try: d["capacity"] = int(row[idx] or 0)
            except: d["capacity"] = None
            idx += 1
        else:
            d["capacity"] = None
        if tipo_col:
            d["tipo"] = row[idx]
        else:
            d["tipo"] = None
        return d
    except Exception as e:
        current_app.logger.warning(f"[room info] {e}")
        return {"price": 0.0, "capacity": None, "tipo": None}

def _calc_total(price_per_night: float, nights: int, guests: int,
                iva_rate: float = DEFAULT_IVA, include_tax: bool = ROOM_TOTAL_INCLUDES_TAX) -> float:
    """
    Mantiene la misma lógica usada en el flujo anónimo: precio * noches * huéspedes (+ IVA si corresponde).
    """
    base = float(price_per_night or 0.0) * int(max(1, nights)) * int(max(1, guests))
    total = base * (1.0 + float(iva_rate or 0.0)) if include_tax else base
    return round(float(total), 2)

def _create_recibo_pdf(reserva: dict, delta: float, pay_method: str = "tarjeta", reference: Optional[str] = None) -> Optional[Path]:
    """
    Genera un PDF simple de recibo por diferencia (delta > 0) y lo registra en Documento/ReservaDocumento si existen.
    """
    try:
        numero = reserva.get("Numero") or reserva.get("Numero_Comprobante") or reserva.get("numero") or f"VG-{reserva.get('id')}"
        rid = int(reserva.get("Codigo_Reserva") or reserva.get("id"))
        fname = f"REC-{numero}.pdf"
        out_path = RECIBOS_DIR / fname
        lines = [
            f"Recibo de pago por cambios de reserva",
            f"Reserva:      {numero}",
            f"Importe:      ₡ {float(delta):,.2f}".replace(",", "X").replace(".", ",").replace("X", "."),
            f"Método:       {pay_method}",
            f"Referencia:   {reference or '-'}",
        ]
        _write_minimal_pdf(out_path, "Recibo — Hotel Villa Grace", lines)

        # Registrar en tablas (si existen)
        try:
            ruta = f"/storage/recibos/{fname}"
            res = db.session.execute(
                text("""
                    INSERT INTO Documento (Tipo, Ruta, MimeType, TamanoBytes)
                    VALUES ('Recibo', :ruta, 'application/pdf', :sz)
                """),
                {"ruta": ruta, "sz": out_path.stat().st_size}
            )
            doc_id = res.lastrowid
            db.session.execute(
                text("""INSERT INTO ReservaDocumento (Codigo_Reserva, Documento_Id) VALUES (:r, :d)"""),
                {"r": rid, "d": doc_id}
            )
            db.session.commit()
        except Exception as e:
            current_app.logger.warning(f"[RECIBO] No se pudo registrar documento: {e}")
            db.session.rollback()

        return out_path
    except Exception as e:
        current_app.logger.warning(f"[RECIBO] {e}")
        return None



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



def es_recepcionista(usuario: Usuario | None) -> bool:
    if not usuario:
        return False
    rol = (getattr(usuario, "Rol", None) or "").strip().lower()
    if rol == "recepcionista":
        return True
    # Alternativas si usas Rol_Id u otra tabla de roles
    rol_id = getattr(usuario, "Rol_Id", None)
    if rol_id in (2, ):  # ajusta el ID real de Recepcionista
        return True
    return False

# ==== Helpers específicos para Editar Reserva (cotizar / aplicar) ====

def _room_is_free(room_id: int, ci: str, co: str, exclude_reserva_id: Optional[int] = None) -> bool:
    """
    La reserva editada debe conservar la MISMA habitación libre en [ci, co).
    Evita solapes con Confirmada/Pendiente excluyendo la propia.
    """
    params = {"rid": int(room_id), "ci": ci, "co": co}
    extra = ""
    if exclude_reserva_id:
        extra = "AND r.Codigo_Reserva <> :ex"
        params["ex"] = int(exclude_reserva_id)

    row = db.session.execute(text(f"""
        SELECT 1
          FROM Reserva r
         WHERE r.Codigo_Habitacion = :rid
           AND r.Estado IN ('Confirmada','Pendiente')
           AND DATE(r.Fecha_Entrada) < DATE(:co)
           AND DATE(r.Fecha_Salida)  > DATE(:ci)
           {extra}
         LIMIT 1
    """), params).first()
    return not bool(row)


def _habitacion_info(room_id) -> dict | None:
    """
    Devuelve info flexible de la habitación sin asumir columnas opcionales.
    Evita referenciar columnas inexistentes en el SELECT.
    """
    try:
        row = db.session.execute(
            text("SELECT * FROM Habitacion WHERE Codigo_Habitacion = :id LIMIT 1"),
            {"id": room_id}
        ).mappings().first()
        if not row:
            return None

        def pick(*keys):
            for k in keys:
                if k in row and row[k] is not None:
                    return row[k]
            return None

        # Precio por noche
        price_raw = pick("Precio_Noche","precio_noche","Precio","Tarifa","tarifa","price")
        from decimal import Decimal
        try:
            price = Decimal(str(price_raw)) if price_raw is not None else Decimal("0")
        except Exception:
            price = Decimal("0")

        # Capacidad (si no existe, queda en None y se omite validación)
        cap = pick("Capacidad","capacidad","Pax_Max","pax_max")
        try:
            capacity = int(cap) if cap is not None else None
        except Exception:
            capacity = None

        tipo = pick("Tipo","tipo","Categoria","categoria")

        return {
            "price": price,
            "capacity": capacity,
            "tipo": tipo,
            "raw": dict(row),
        }
    except Exception as e:
        app.logger.error(f"[ROOM] _habitacion_info error: {e}", exc_info=True)
        return {"price": 0, "capacity": None, "tipo": None}





def _calc_total_with_policy(price_night: float, nights: int, pax: int) -> float:
    """
    Misma política que /api/reservas/anon: total = precio_noche * noches * pax * (1 + IVA)
    """
    iva = float(DEFAULT_IVA or 0.0)
    nights = max(1, int(nights or 1))
    pax = max(1, int(pax or 1))
    base = float(price_night or 0.0) * nights * pax
    return round(base * (1.0 + iva), 2)


def _parse_ymd(s: str):
    from datetime import datetime as _dt
    return _dt.strptime(str(s)[:10], "%Y-%m-%d").date()



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


def _validate_no_overbooking(ci: str, co: str, rooms: int = 1, tipo: Optional[str] = None, guests: Optional[int] = None) -> tuple[bool, str]:
    qs = {"checkin": ci, "checkout": co, "rooms": rooms}
    if guests is not None:
        qs["guests"] = int(guests)
    if tipo:
        qs["tipo"] = tipo
    with current_app.test_client() as c:
        resp = c.get(url_for("api_availability", **qs))
        data = resp.get_json() if resp.is_json else {}
    if not data or not data.get("ok"):
        return False, "No se pudo validar disponibilidad."
    if not data.get("available"):
        return False, data.get("message") or "Sin cupo para ese rango."
    return True, ""



def ensure_cliente_for_email(
    nombre: str,
    correo: str,
    telefono: Optional[str] = None,
    doc_num: Optional[str] = None
) -> Optional[int]:
    """
    Asegura un Cliente por correo y sincroniza Cedula cuando venga doc_num.
    - No hace commit() aquí: el commit lo maneja el caller (registro).
    - Cedula se guarda como string (cédula/DIMEX numérica o pasaporte alfanum).
    """
    if not correo:
        return None

    correo = (correo or "").strip().lower()
    tel = (telefono or "").strip()[:20] if telefono else ""
    if not tel:
        tel = "00000000"

    # Normalizar documento con la misma lógica del registro
    doc_norm = None
    if doc_num:
        try:
            doc_norm = _normalize_document(doc_num)
        except Exception:
            doc_norm = (doc_num or "").strip() or None

    # ¿Existe Cliente por correo?
    row = db.session.execute(
        text("SELECT Codigo_Cliente, Cedula FROM Cliente WHERE LOWER(Correo)=:e LIMIT 1"),
        {"e": correo},
    ).first()

    # Parsear nombre/apellido desde "nombre completo"
    nombre = (nombre or "").strip() or "Cliente Web"
    partes = nombre.split(" ", 1)
    nom = partes[0][:50]
    ape = (partes[1] if len(partes) > 1 else "").strip()[:50]

    if row:
        cid = int(row[0])
        ced_actual = row[1]

        # Solo sobrescribimos Cedula si:
        # - viene doc_norm
        # - y en Cliente está vacía/NULL/0
        set_ced = ""
        params = {"id": cid, "n": nom, "a": ape, "t": tel, "e": correo}

        if doc_norm and (ced_actual is None or str(ced_actual).strip() in ("", "0", "000000000")):
            set_ced = ", Cedula = :ced"
            params["ced"] = doc_norm

        db.session.execute(
            text(f"""
                UPDATE Cliente
                   SET Nombre = :n,
                       Apellido = :a,
                       Telefono = COALESCE(NULLIF(:t,''), Telefono),
                       Correo = :e
                       {set_ced}
                 WHERE Codigo_Cliente = :id
            """),
            params
        )
        return cid

    # Si no existe Cliente, crear uno
    # Cedula: si no hay doc_norm, insert NULL (la columna ya la hicimos NULL en SQL)
    db.session.execute(
        text("""
            INSERT INTO Cliente (Cedula, Nombre, Apellido, Telefono, Correo, Fecha_Nacimiento)
            VALUES (:ced, :n, :a, :t, :e, '1990-01-01')
        """),
        {"ced": doc_norm, "n": nom, "a": ape, "t": tel, "e": correo},
    )

    new_id = db.session.execute(
        text("SELECT Codigo_Cliente FROM Cliente WHERE LOWER(Correo)=:e LIMIT 1"),
        {"e": correo},
    ).scalar()

    return int(new_id) if new_id else None



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


def _update_kpis(monto_total: float, fecha_entrada: str, noches: int = 1, iva_rate: float = DEFAULT_IVA):
    """
    Upsert en KPI_Stats para day/week/month.
    - Total_Reservas: +1
    - Total_Monto:    +monto_total (con impuesto, como antes)
    - Revenue_SinImpuesto: + (monto_total / (1+IVA))
    - Total_Noches:   + noches
    """
    try:
        from datetime import datetime as _dt
        dt = _dt.strptime((fecha_entrada or "")[:10], "%Y-%m-%d")
    except Exception:
        from datetime import datetime as _dt
        dt = _dt.utcnow()

    key_day  = dt.strftime("%Y-%m-%d")
    key_mon  = dt.strftime("%Y-%m")
    isoy, isow, _ = dt.isocalendar()
    key_week = f"{isoy}-W{isow:02d}"

    rev_sin_iva = float(monto_total or 0) / (1.0 + float(iva_rate or 0.0))
    n = int(noches or 1)

    for periodo, clave in (("day", key_day), ("week", key_week), ("month", key_mon)):
        db.session.execute(
            text("""
                INSERT INTO KPI_Stats (Periodo, Clave, Total_Reservas, Total_Monto, Revenue_SinImpuesto, Total_Noches)
                VALUES (:p, :c, 1, :m, :r, :n)
                ON DUPLICATE KEY UPDATE
                  Total_Reservas = Total_Reservas + 1,
                  Total_Monto    = Total_Monto + :m,
                  Revenue_SinImpuesto = Revenue_SinImpuesto + :r,
                  Total_Noches   = Total_Noches + :n
            """),
            {"p": periodo, "c": clave, "m": float(monto_total or 0), "r": rev_sin_iva, "n": n}
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
    También inserta/relaciona registros en Documento y ReservaDocumento.
    Diseño: estético y acorde al hotel (usa reportlab si está disponible).
    """
    from datetime import datetime
    import math

    def _to_iso_date(v) -> str:
        if not v:
            return ""
        if hasattr(v, "strftime"):
            return v.strftime("%Y-%m-%d")
        s = str(v)
        return s[:10]

    def _fmt_crc(n: float) -> str:
        s = f"{float(n or 0.0):,.2f}"
        s = s.replace(",", "X").replace(".", ",").replace("X", ".")
        return f"₡ {s}"

    # Datos base
    rid = int(reserva.get("Codigo_Reserva") or reserva.get("id") or 0)
    if not rid:
        raise ValueError("Reserva inválida para generar comprobante (sin Codigo_Reserva/id).")

    checkin = _to_iso_date(reserva.get("Fecha_Entrada") or reserva.get("checkin"))
    checkout = _to_iso_date(reserva.get("Fecha_Salida") or reserva.get("checkout"))
    estado = (reserva.get("Estado") or reserva.get("estado") or "").strip() or "Confirmada"
    canal = (reserva.get("Canal") or reserva.get("canal") or "Web").strip()
    monto = float(reserva.get("Monto_Total") or reserva.get("monto") or 0.0)

    # Intentar enriquecer: huésped + habitación desde DB (sin depender del dict)
    cliente_nombre = ""
    cliente_apellido = ""
    cliente_tel = ""
    cliente_email = str(reserva.get("Usuario") or reserva.get("email") or "").strip()
    hab_numero = ""
    hab_tipo = str(reserva.get("Tipo") or "Habitación").strip()

    try:
        row = db.session.execute(
            text(
                """
                SELECT
                  C.Nombre AS Cliente_Nombre,
                  C.Apellido AS Cliente_Apellido,
                  C.Telefono AS Cliente_Telefono,
                  C.Correo AS Cliente_Correo,
                  H.Numero_Habitacion AS Habitacion_Numero,
                  H.Tipo AS Habitacion_Tipo
                FROM Reserva R
                JOIN Cliente C ON C.Codigo_Cliente = R.Codigo_Cliente
                JOIN Habitacion H ON H.Codigo_Habitacion = R.Codigo_Habitacion
                WHERE R.Codigo_Reserva = :id
                LIMIT 1
                """
            ),
            {"id": rid},
        ).mappings().first()

        if row:
            cliente_nombre = str(row.get("Cliente_Nombre") or "").strip()
            cliente_apellido = str(row.get("Cliente_Apellido") or "").strip()
            cliente_tel = str(row.get("Cliente_Telefono") or "").strip()
            if not cliente_email:
                cliente_email = str(row.get("Cliente_Correo") or "").strip()
            hab_numero = str(row.get("Habitacion_Numero") or "").strip()
            if row.get("Habitacion_Tipo"):
                hab_tipo = str(row.get("Habitacion_Tipo")).strip()
    except Exception:
        # No bloquear el PDF si el enriquecimiento falla
        pass

    cliente_full = (f"{cliente_nombre} {cliente_apellido}").strip() or (cliente_email or "-")

    # Número comprobante: si no existe, generarlo y persistirlo
    numero = reserva.get("Numero") or reserva.get("Numero_Comprobante") or reserva.get("numero")
    numero = str(numero).strip() if numero is not None else ""
    if not numero or numero.lower() == "none":
        numero = _make_unique_number(rid, checkin or datetime.now().strftime("%Y-%m-%d"))
        try:
            db.session.execute(
                text(
                    """
                    UPDATE Reserva
                    SET Numero_Comprobante = COALESCE(Numero_Comprobante, :n)
                    WHERE Codigo_Reserva = :r
                    """
                ),
                {"n": numero, "r": rid},
            )
            db.session.commit()
        except Exception:
            db.session.rollback()

    COMPROBANTES_DIR.mkdir(parents=True, exist_ok=True)
    filename = f"{numero}.pdf"
    out_path = COMPROBANTES_DIR / filename

    # Cálculo de noches (si se puede)
    noches = ""
    try:
        if checkin and checkout:
            d1 = datetime.strptime(checkin, "%Y-%m-%d")
            d2 = datetime.strptime(checkout, "%Y-%m-%d")
            n = max(1, int(round((d2 - d1).total_seconds() / 86400.0)))
            noches = str(n)
    except Exception:
        noches = ""

    # =========================
    # PDF ESTÉTICO (reportlab)
    # =========================
    try:
        from reportlab.lib.pagesizes import LETTER  # type: ignore
        from reportlab.pdfgen import canvas  # type: ignore
        from reportlab.lib import colors  # type: ignore
        from reportlab.lib.units import inch  # type: ignore

        W, H = LETTER
        c = canvas.Canvas(str(out_path), pagesize=LETTER)

        primary = colors.HexColor("#1b7a4e")   # verde corporativo
        dark = colors.HexColor("#1f2937")
        muted = colors.HexColor("#6b7280")
        panel = colors.HexColor("#f3f4f6")
        white = colors.white

        # Header
        c.setFillColor(primary)
        c.rect(0, H - 1.05 * inch, W, 1.05 * inch, stroke=0, fill=1)

        c.setFillColor(white)
        c.setFont("Helvetica-Bold", 18)
        c.drawString(0.75 * inch, H - 0.70 * inch, "Hotel Villa Grace")
        c.setFont("Helvetica", 10)
        c.drawString(0.75 * inch, H - 0.93 * inch, "Tu hogar fuera de casa")

        # Datos hotel (derecha)
        c.setFont("Helvetica", 8.5)
        c.drawRightString(W - 0.75 * inch, H - 0.62 * inch, "Cóbano, Puntarenas, Costa Rica")
        c.drawRightString(W - 0.75 * inch, H - 0.80 * inch, "Tel: +506 2642 0225")
        c.drawRightString(W - 0.75 * inch, H - 0.98 * inch, "Email: hotelvillagrace@gmail.com")

        # Título
        y = H - 1.35 * inch
        c.setFillColor(dark)
        c.setFont("Helvetica-Bold", 16)
        c.drawString(0.75 * inch, y, "Comprobante de Reserva")
        c.setStrokeColor(colors.HexColor("#e5e7eb"))
        c.setLineWidth(1)
        c.line(0.75 * inch, y - 10, W - 0.75 * inch, y - 10)

        # Panel principal
        panel_x = 0.75 * inch
        panel_w = W - 1.5 * inch
        panel_h = 4.05 * inch
        panel_y = y - 0.25 * inch - panel_h

        c.setFillColor(panel)
        c.roundRect(panel_x, panel_y, panel_w, panel_h, 12, stroke=0, fill=1)

        # helper para filas
        def draw_kv(x, y, k, v):
            c.setFillColor(muted)
            c.setFont("Helvetica", 9)
            c.drawString(x, y, k)
            c.setFillColor(dark)
            c.setFont("Helvetica-Bold", 11)
            c.drawString(x, y - 14, v if v else "-")
            return y - 34

        # Columna izquierda
        left_x = panel_x + 0.35 * inch
        top_y = panel_y + panel_h - 0.45 * inch

        yy = top_y
        yy = draw_kv(left_x, yy, "Número de comprobante", numero)
        yy = draw_kv(left_x, yy, "Reserva ID", str(rid))
        yy = draw_kv(left_x, yy, "Estado", estado)
        yy = draw_kv(left_x, yy, "Canal", canal)

        # Columna derecha
        right_x = panel_x + panel_w/2 + 0.15 * inch
        yy2 = top_y
        yy2 = draw_kv(right_x, yy2, "Check-in", checkin)
        yy2 = draw_kv(right_x, yy2, "Check-out", checkout)
        if noches:
            yy2 = draw_kv(right_x, yy2, "Noches", noches)
        yy2 = draw_kv(right_x, yy2, "Habitación", f"{hab_tipo}" + (f" (#{hab_numero})" if hab_numero else ""))

        # Bloque huésped + total (debajo del panel)
        y2 = panel_y - 0.45 * inch
        c.setFillColor(dark)
        c.setFont("Helvetica-Bold", 11)
        c.drawString(0.75 * inch, y2, "Huésped:")
        c.setFont("Helvetica", 11)
        c.drawString(1.55 * inch, y2, cliente_full)

        if cliente_email:
            c.setFillColor(muted)
            c.setFont("Helvetica", 9)
            c.drawString(0.75 * inch, y2 - 16, f"Correo: {cliente_email}")
        if cliente_tel:
            c.setFillColor(muted)
            c.setFont("Helvetica", 9)
            c.drawString(0.75 * inch, y2 - 30, f"Teléfono: {cliente_tel}")

        # Total destacado (derecha)
        c.setFillColor(primary)
        box_w = 2.65 * inch
        box_h = 0.75 * inch
        box_x = W - 0.75 * inch - box_w
        box_y = y2 - 0.55 * inch
        c.roundRect(box_x, box_y, box_w, box_h, 12, stroke=0, fill=1)
        c.setFillColor(white)
        c.setFont("Helvetica", 9.5)
        c.drawString(box_x + 0.25 * inch, box_y + box_h - 0.28 * inch, "Total pagado / total de reserva")
        c.setFont("Helvetica-Bold", 14)
        c.drawString(box_x + 0.25 * inch, box_y + 0.22 * inch, _fmt_crc(monto))

        # Pie
        c.setFillColor(muted)
        c.setFont("Helvetica", 8.5)
        c.drawString(0.75 * inch, 0.70 * inch, f"Emitido: {datetime.now().strftime('%Y-%m-%d %H:%M')}")
        c.drawRightString(W - 0.75 * inch, 0.70 * inch, "Este documento corresponde únicamente a la reserva indicada.")
        c.setStrokeColor(colors.HexColor("#e5e7eb"))
        c.line(0.75 * inch, 0.62 * inch, W - 0.75 * inch, 0.62 * inch)

        c.save()

    except Exception:
        # Fallback (si reportlab no está disponible por alguna razón)
        title = "Comprobante de Reserva — Hotel Villa Grace"
        lines = [
            f"Número:        {numero}",
            f"Reserva ID:    {rid}",
            f"Cliente:       {cliente_full or '-'}",
            f"Check-in:      {checkin}",
            f"Check-out:     {checkout}",
            f"Habitación:    {hab_tipo}" + (f" (#{hab_numero})" if hab_numero else ""),
            f"Monto total:   {_fmt_crc(monto)}",
            f"Canal:         {canal}",
            f"Estado:        {estado}",
            "",
            "Gracias por su preferencia.",
        ]
        _write_minimal_pdf(out_path, title, lines)

    # =========================
    # Registrar Documento / ReservaDocumento
    # =========================
    try:
        ruta = f"/storage/comprobantes/{filename}"

        # 1) Documento (evitar duplicados por ruta)
        doc_id = db.session.execute(
            text("SELECT Id FROM Documento WHERE Ruta=:ruta LIMIT 1"),
            {"ruta": ruta},
        ).scalar()

        if not doc_id:
            res = db.session.execute(
                text(
                    """
                    INSERT INTO Documento (Tipo, Ruta, MimeType, TamanoBytes)
                    VALUES ('Comprobante', :ruta, 'application/pdf', :sz)
                    """
                ),
                {"ruta": ruta, "sz": out_path.stat().st_size},
            )
            doc_id = res.lastrowid

        # 2) Relación con reserva (evitar duplicados)
        exists_rel = db.session.execute(
            text(
                """
                SELECT 1
                FROM ReservaDocumento
                WHERE Codigo_Reserva=:r AND Documento_Id=:d
                LIMIT 1
                """
            ),
            {"r": rid, "d": int(doc_id)},
        ).scalar()

        if not exists_rel:
            db.session.execute(
                text(
                    """
                    INSERT INTO ReservaDocumento (Codigo_Reserva, Documento_Id)
                    VALUES (:r, :d)
                    """
                ),
                {"r": rid, "d": int(doc_id)},
            )

        db.session.commit()

    except Exception as e:
        current_app.logger.warning(f"[COMPROBANTE] No se pudo registrar documento: {e}")
        db.session.rollback()

    return out_path



def _reserva_basic_row(r: dict) -> dict:
    """Normaliza una reserva a columnas comunes para exportar."""
    d = _reserva_to_dict(r)
    return {
        "numero": d.get("numero") or f"VG-{d.get('id')}",
        "checkin": d.get("checkin"),
        "checkout": d.get("checkout"),
        "estado": d.get("estado"),
        "tipo": d.get("tipo") or "-",
        "plan": d.get("plan") or "-",
        "canal": d.get("canal") or "-",
        "huespedes": d.get("huespedes") or "0",
        "monto": f"{float(d.get('monto') or 0.0):.2f}",
    }

def _create_reservas_pdf(items: list[dict], title: str, filename: str) -> Path:
    """Genera un PDF simple con el listado de reservas del usuario."""
    out_path = EXPORTS_DIR / filename
    lines = []
    header = "Núm.;Check-in;Check-out;Estado;Tipo;Plan;Canal;Huésp.;Total"
    lines.append(header)
    for r in items:
        row = _reserva_basic_row(r)
        lines.append(
            f"{row['numero']};{row['checkin']};{row['checkout']};{row['estado']};"
            f"{row['tipo']};{row['plan']};{row['canal']};{row['huespedes']};₡ {row['monto']}"
        )
    _write_minimal_pdf(out_path, title, lines)
    return out_path

def _create_reservas_csv(items: list[dict], filename: str) -> Path:
    """
    Genera un CSV (compatible con Excel) con el listado de reservas.
    Usamos CSV para evitar dependencias adicionales (.xlsx). Excel lo abre sin problema.
    """
    out_path = EXPORTS_DIR / filename
    cols = ["numero", "checkin", "checkout", "estado", "tipo", "plan", "canal", "huespedes", "monto"]
    with open(out_path, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f, delimiter=",")
        w.writerow(["Numero", "Check-in", "Check-out", "Estado", "Tipo", "Plan", "Canal", "Huespedes", "Total_CRC"])
        for it in items:
            row = _reserva_basic_row(it)
            w.writerow([row[c] for c in cols])
    return out_path



# =========================
# FACTORY PRINCIPAL
# =========================
def create_app() -> Flask:
    app = Flask(
        __name__,
    )

    app.config.from_object(Config)

    db.init_app(app)
    migrate.init_app(app, db)

    with app.app_context():
        try:
            _ensure_seed_roles()
        except Exception:
            pass



    # Blueprint de registro de KPIs contables:
    app.register_blueprint(fin_kpi_bp)
    
    # Blueprint de cierres mensuales
    from blueprints.fin_periods import fin_periods_bp
    app.register_blueprint(fin_periods_bp)

    # Blueprint de FAC-07-005 (reportes contables)
    from blueprints.fin_reports import fin_reports_bp
    app.register_blueprint(fin_reports_bp)

    # Blueprint de FAC (cierre de caja)
    from blueprints.fin_cash import fin_cash_bp
    app.register_blueprint(fin_cash_bp)

    # Blueprint de GRR (creación de reservas)
    from blueprints.grr.routes import grr_bp
    app.register_blueprint(grr_bp, url_prefix="/grr")

    # Blueprint de Inventario (INV-07)
    from blueprints.inv import inv_bp
    app.register_blueprint(inv_bp, url_prefix="/inv")

    # Blueprint de Operaciones
    from blueprints.admin import admin_bp 
    app.register_blueprint(admin_bp)

    from blueprints.mnt import mnt_bp  # <-- IMPORTA
    app.register_blueprint(mnt_bp)     # <-- REGISTRA (después de inv_bp / admin_bp)

    # === Punto de Venta (POS) ===
    app.register_blueprint(pos_bp)

    #Bluprint de HRM
    from blueprints.hrm import hrm_bp
    app.register_blueprint(hrm_bp)

    # === Eventos (EVT) ===
    from blueprints.evt import evt_bp
    app.register_blueprint(evt_bp)

    # === AREP (Analítica y Reportes) ===
    from blueprints.arep import arep_bp
    app.register_blueprint(arep_bp)
    
    # === SAC (Atención al Cliente y Comunicación) ===
    from blueprints.sac.routes import sac_bp
    app.register_blueprint(sac_bp)

    # === FAC (008) ===

    from blueprints.fin_recurring import fin_recurring_bp
    app.register_blueprint(fin_recurring_bp)

    # ============ACE=============
    from blueprints.ace import ace_bp
    app.register_blueprint(ace_bp, url_prefix='/ace')

    from blueprints.rooms import rooms_bp
    app.register_blueprint(rooms_bp)
    

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
        
    def _col_exists(table_name: str, column_name: str) -> bool:
        try:
            row = db.session.execute(
                text("""
                    SELECT 1
                      FROM information_schema.COLUMNS
                     WHERE TABLE_SCHEMA = DATABASE()
                       AND TABLE_NAME = :t
                       AND COLUMN_NAME = :c
                     LIMIT 1
                """),
                {"t": table_name, "c": column_name}
            ).first()
            return bool(row)
        except Exception:
            return False
    
        
        
    def _col_nullable(table: str, column: str) -> bool:
        try:
            row = db.session.execute(
                text("""
                    SELECT CASE WHEN IS_NULLABLE='YES' THEN 1 ELSE 0 END
                      FROM INFORMATION_SCHEMA.COLUMNS
                     WHERE TABLE_SCHEMA = DATABASE()
                       AND TABLE_NAME = :t
                       AND COLUMN_NAME = :c
                     LIMIT 1
                """),
                {"t": table, "c": column}
            ).scalar()
            return bool(row)
        except Exception:
            return True  # si no podemos saberlo, asumimos que sí permite NULL


    # --- Helpers seguros para castear valores de dict/JSON ---
    def _to_int(v, default=None):
        try:
            return int(v)
        except Exception:
            return default

    def _to_float(v, default=None):
        try:
            return float(v)
        except Exception:
            return default


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
          - GRR-01-009: Envío de confirmación automática (email/SMS) al crear reserva (flujo logueado)
        """
        try:
            status = response.status_code
            method = request.method.upper()
            path = (request.path or "").lower()
    
            # Solo en respuestas exitosas de escritura
            if status >= 400 or method not in ("POST", "PUT", "PATCH", "DELETE"):
                return response
    
            # Solo si el endpoint es de reservas
            es_endpoint_reserva = any(
                s in path for s in ("/api/reservas", "/grr/reservas", "/reservas")
            )
            if not es_endpoint_reserva:
                return response
    
            # Intentar extraer el id de la reserva de la respuesta
            rid = _extraer_reserva_id_de_response(response)
    
            # Auditoría de DELETE y salir
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
    
            # Cargar datos mínimos de la reserva
            r = _reserva_min(rid)
            if not r:
                return response
    
            # Auditoría de creación / actualización
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
    
                # === GRR-01-009: Confirmación automática (email/SMS) ===
                # Evitar doble envío en el flujo público sin sesión (/api/reservas/anon),
                # ya que ese endpoint realiza su propio correo de confirmación.
                try:
                    # Notificación SOLO para creación de reserva desde checkout (GRR), no en /api/reservas/anon
                    if "/api/reservas/anon" not in request.path:
                        try:
                            r = _get_reserva_by_id(int(rid)) or {}
                            est = (r.get("Estado") or "").strip()
                    
                            if est == "Confirmada":
                                _notify_reserva_success(int(rid))
                            elif est == "Pendiente":
                                _notify_reserva_pending(int(rid))
                        except Exception:
                            pass
                    
                    
                except Exception as e:
                    current_app.logger.warning(f"[GRR-01-009] Notificación omitida: {e}")
    
            elif method in ("PUT", "PATCH"):
                _audit_log(
                    _current_user_email(),
                    "reserva.actualizada",
                    {"Codigo_Reserva": r["Codigo_Reserva"], "Estado": r["Estado"]},
                    entidad_id=str(r["Codigo_Reserva"]),
                )
    
            # KPI solo cuando queda confirmada
            if r["Estado"] == "Confirmada":
                _kpi_touch(str(r["Fecha_Entrada"]), float(r["Monto_Total"]))
    
            # Auto-asignación para Confirmada/Pendiente
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
        # ---------------------- Helpers de sesión para plantillas ----------------------
    @app.context_processor
    def inject_session_flags():
        def is_logged_in():
            return bool(session.get("user_id"))

        # Paso 1 (SINPE): datos para mostrar en plantillas (booking-checkout.html, etc.)
        # Se leen de variables de entorno (.env o sistema):
        #  - SINPE_MOBILE / SINPE_NUMERO / SINPE_PHONE
        #  - SINPE_BENEFICIARIO / SINPE_NOMBRE
        sinpe_mobile = (
            os.getenv("SINPE_MOBILE")
            or os.getenv("SINPE_NUMERO")
            or os.getenv("SINPE_PHONE")
            or ""
        )
        sinpe_beneficiary = (
            os.getenv("SINPE_BENEFICIARIO")
            or os.getenv("SINPE_NOMBRE")
            or "Hotel Villa Grace"
        )

        return {
            "is_logged_in": is_logged_in,
            "current_user_name": session.get("user_name"),
            "current_user_role": session.get("user_role"),
            # Paso 1 (SINPE)
            "sinpe_mobile": sinpe_mobile,
            "sinpe_beneficiary": sinpe_beneficiary,
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
        """Landing público del hotel + widget de tipo de cambio."""
        fx_usd_crc = None
        fx_date = None

        try:
            # Tipo de cambio USD → moneda base (normalmente CRC)
            hoy = date.today()
            fx = get_fx_rate_for_date("USD", hoy)

            if fx is not None:
                fx_usd_crc = fx          # número que usas en el widget
                fx_date = hoy.strftime("%d/%m/%Y")  # fecha que se muestra debajo
        except Exception as exc:
            # Si hay cualquier error, simplemente mostramos el mensaje de “no disponible”
            app.logger.warning(
                "No se pudo obtener el tipo de cambio USD/CRC para index: %s", exc
            )

        return render_template(
            "index.html",
            fx_usd_crc=fx_usd_crc,
            fx_date=fx_date,
        )


    @app.route("/contact.html")
    def contact_html():
        return render_template("contact.html")

    @app.route("/about.html")
    def about_html():
        return render_template("about.html")

    @app.route("/booking.html")
    def booking_html():
        return redirect(url_for("booking_search"))
    
    # =========================
    # GRR-01-007: Reserva sin iniciar sesión (UI públicas)
    # =========================
    @app.route("/reserva-sin-sesion")
    def anon_reserva_html():
        # Pantalla pública (no está en PROTECTED_BOOKING_PATHS)
        return render_template("anon-reserva.html")

    @app.route("/reserva-sin-sesion/exito")
    def anon_reserva_exito_html():
        return render_template("anon-reserva-exito.html")
    
    @app.get("/reserva-sin-sesion")
    def anon_reserva_page():
        uid = session.get("user_id")
        u = Usuario.query.filter_by(Codigo_Usuario=uid).first() if uid else None
        return render_template("anon-reserva.html", is_recepcionista=es_recepcionista(u))
    


    # ---------------------- Portal / Ops / Admin (protegidas por rol) ------------
    @app.route("/portal-dashboard.html")
    @role_required("Cliente")
    def portal_dashboard_html():
        return render_template("portal-dashboard.html")

    @app.route("/admin-users.html")
    @role_required("Administrador", "Recepcionista")
    def admin_users_html():
        return render_template("admin-users.html")


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
        uid = session.get("user_id")
        if not uid:
            # Si por alguna razón llega aquí sin sesión, redirige a login (consistente con el resto)
            return redirect(url_for("login_html", next=request.path))
    
        current_user = {
            "id": int(uid),
            "is_authenticated": True
        }
    
        # Pasá explícitamente lo que el template use (idealmente current_user)
        return render_template(
            "portal-reservas.html",
            user_id=int(uid),
            current_user=current_user
        )
    

    @app.route("/portal-reserva-detalle.html")
    @app.route("/portal-reserva-detalle")
    @role_required("Cliente")
    def portal_reserva_detalle_html():
        return render_template("portal-reserva-detalle.html")

    @app.route("/ops-dashboard.html")
    @role_required("Administrador", "Recepcionista")
    def ops_dashboard_html():
        pending_approvals_count = 0
        try:
            pending_approvals_count = int(
                db.session.execute(
                    text("SELECT COUNT(*) FROM Reserva WHERE Estado='Pendiente'")
                ).scalar() or 0
            )
        except Exception as e:
            try:
                current_app.logger.warning(f"[OPS] No se pudo calcular pendientes: {e}")
            except Exception:
                pass
            pending_approvals_count = 0

        return render_template(
            "ops-dashboard.html",
            pending_approvals_count=pending_approvals_count
        )
        
    
    @app.route("/ops-approvals.html")
    @role_required("Administrador", "Recepcionista")
    def ops_approvals_html():
        return render_template("ops-approvals.html")



    @app.route("/ops-housekeeping.html")
    @role_required("Administrador", "Limpieza")
    def ops_housekeeping_html():
        return render_template("ops-housekeeping.html")

    @app.route("/admin-dashboard.html")
    @role_required("Administrador")
    def admin_dashboard_html():
        return render_template("admin-dashboard.html")

    @app.route("/admin-rooms.html")
    @role_required("Administrador")
    def admin_rooms_html():
        return render_template("admin-rooms.html")
    
    @app.route("/admin-taxes.html")
    @role_required("Administrador")
    def admin_taxes_html():
        return render_template("admin-taxes.html")

    @app.route("/admin-rates.html")
    @role_required("Administrador")
    def admin_rates_html():
        return render_template("admin-rates.html")
    
    @app.route("/admin-channels.html")
    @role_required("Administrador")
    def admin_channels_html():
        return render_template("admin-channels.html")

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
    
        # ---------------------- OPS/ADMIN: Gestión de Cupones ----------------------
    @app.get("/api/coupons")
    @role_required("Administrador", "Recepcionista")
    def api_coupons_list():
        rows = db.session.execute(text("""
            SELECT Codigo, Tipo, Valor, Valido_Desde, Valido_Hasta, Max_Usos, Usos, Activo
              FROM Coupon
             ORDER BY Codigo ASC
        """)).mappings().all()
        return jsonify({"ok": True, "items": [dict(r) for r in rows]})

    @app.post("/api/coupons")
    @role_required("Administrador", "Recepcionista")
    def api_coupons_create():
        p = request.get_json(silent=True) or {}
        codigo = (p.get("codigo") or "").strip().upper()
        tipo   = (p.get("tipo") or "porcentaje").strip()         # 'porcentaje' | 'monto' | 'corporativo'
        valor  = float(p.get("valor") or 0)
        vd     = (p.get("valido_desde") or None)
        vh     = (p.get("valido_hasta") or None)
        max_usos = int(p.get("max_usos") or 0)

        if not codigo or valor <= 0:
            return jsonify({"ok": False, "msg": "Código y valor son obligatorios."}), 400

        db.session.execute(text("""
            INSERT INTO Coupon (Codigo, Tipo, Valor, Valido_Desde, Valido_Hasta, Max_Usos, Usos, Activo)
            VALUES (:c, :t, :v, :vd, :vh, :m, 0, 1)
            ON DUPLICATE KEY UPDATE
                Tipo=:t, Valor=:v, Valido_Desde=:vd, Valido_Hasta=:vh, Max_Usos=:m, Activo=1
        """), {"c": codigo, "t": tipo, "v": valor, "vd": vd, "vh": vh, "m": max_usos})
        db.session.commit()
        return jsonify({"ok": True, "codigo": codigo})

    @app.post("/api/coupons/<string:codigo>/toggle")
    @role_required("Administrador", "Recepcionista")
    def api_coupons_toggle(codigo: str):
        db.session.execute(text("""
            UPDATE Coupon SET Activo = CASE WHEN Activo=1 THEN 0 ELSE 1 END WHERE Codigo=:c
        """), {"c": codigo})
        db.session.commit()
        return jsonify({"ok": True})
    
    # app.py — debajo de api_coupons_toggle
    @app.delete("/api/coupons/<string:codigo>")
    @role_required("Administrador", "Recepcionista")
    def api_coupons_delete(codigo: str):
        db.session.execute(text("DELETE FROM Coupon WHERE Codigo = :c"), {"c": codigo})
        db.session.commit()
        return jsonify({"ok": True})


    
    @app.route("/ops-walkin.html")
    @role_required("Administrador", "Recepcionista")
    def ops_walkin_html():
        return render_template("ops-walkin.html")
    
    # app.py — sección de vistas HTML de OPS/ADMIN
    @app.route("/ops/coupons")
    @role_required("Administrador", "Recepcionista")
    def ops_coupons_html():
        return render_template("ops-coupons.html")


    # === GRR — Preview de cupón (no persiste) ===
    @app.get("/grr/coupons/preview")
    def grr_coupon_preview():
        code = (request.args.get("code") or "").strip().upper()
        try:
            subtotal = float(request.args.get("subtotal") or 0)
        except Exception:
            subtotal = 0.0
    
        row = db.session.execute(text("""
            SELECT Codigo, Tipo, Valor, Valido_Desde, Valido_Hasta, Max_Usos, Usos, Activo
              FROM Coupon
             WHERE Codigo = :c
             LIMIT 1
        """), {"c": code}).mappings().first()
    
        if not row or not row["Activo"]:
            return jsonify({"ok": False, "msg": "Cupón inválido o inactivo."}), 200
    
        # vigencia y usos
        today = date.today()
        vd, vh = row["Valido_Desde"], row["Valido_Hasta"]
        if (vd and str(vd)[:10] > str(today)) or (vh and str(vh)[:10] < str(today)):
            return jsonify({"ok": False, "msg": "Fuera de vigencia."}), 200
        if row["Max_Usos"] and row["Usos"] is not None and row["Usos"] >= row["Max_Usos"]:
            return jsonify({"ok": False, "msg": "Cupón agotado."}), 200
    
        amount = 0.0
        if row["Tipo"] == "porcentaje":
            amount = round(subtotal * (float(row["Valor"] or 0) / 100.0), 2)
        elif row["Tipo"] in ("monto", "monto_fijo"):
            amount = float(row["Valor"] or 0)
    
        return jsonify({
            "ok": True,
            "type": row["Tipo"],
            "amount": amount,
            "msg": f"Descuento: ₡ {amount:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")
        })
    
    
    # === GRR — Aplicar cupón a una reserva (persiste) ===
    @app.post("/grr/reservas/<int:reserva_id>/apply-coupon")
    @role_required("Cliente","Administrador","Recepcionista")
    def grr_apply_coupon(reserva_id: int):
        p = request.get_json(silent=True) or {}
        code = (p.get("codigo") or p.get("code") or "").strip().upper()
        if not code:
            return jsonify({"ok": False, "msg": "Código requerido."}), 400
    
        # Cargar reserva
        r = _get_reserva_by_id(reserva_id)
        if not r:
            return jsonify({"ok": False, "msg": "Reserva no encontrada."}), 404
    
        # Si es cliente, verificar pertenencia
        if _user_role().lower() == "cliente":
            if not _reserva_belongs_to_email(reserva_id, _current_user_email() or ""):
                return jsonify({"ok": False, "msg": "No autorizado."}), 403
    
        # Cargar cupón
        row = db.session.execute(text("""
            SELECT Codigo, Tipo, Valor, Valido_Desde, Valido_Hasta, Max_Usos, Usos, Activo
              FROM Coupon WHERE Codigo=:c LIMIT 1
        """), {"c": code}).mappings().first()
        if not row or not row["Activo"]:
            return jsonify({"ok": False, "msg": "Cupón inválido o inactivo."}), 200
    
        today = date.today()
        vd, vh = row["Valido_Desde"], row["Valido_Hasta"]
        if (vd and str(vd)[:10] > str(today)) or (vh and str(vh)[:10] < str(today)):
            return jsonify({"ok": False, "msg": "Fuera de vigencia."}), 200
        if row["Max_Usos"] and row["Usos"] is not None and row["Usos"] >= row["Max_Usos"]:
            return jsonify({"ok": False, "msg": "Cupón agotado."}), 200
    
        total_actual = float(r.get("Monto_Total") or 0.0)
        if row["Tipo"] == "porcentaje":
            descuento = round(total_actual * (float(row["Valor"] or 0)/100.0), 2)
        else:
            descuento = float(row["Valor"] or 0)
    
        nuevo_total = max(0.0, round(total_actual - descuento, 2))
    
        # Persistir cambios
        db.session.execute(
            text("UPDATE Reserva SET Monto_Total=:t, Observaciones = CONCAT(COALESCE(Observaciones,''),' | CUPON ', :c) WHERE Codigo_Reserva=:r"),
            {"t": nuevo_total, "c": code, "r": reserva_id}
        )
        db.session.execute(text("UPDATE Coupon SET Usos = COALESCE(Usos,0)+1 WHERE Codigo=:c"), {"c": code})
        db.session.commit()
    
        return jsonify({"ok": True, "monto": nuevo_total, "descuento": descuento, "codigo": code})
    
    # ---------------------- API Disponibilidad --------------------------
    @app.get("/api/availability")
    def api_availability():
        from datetime import datetime as dt
        from math import ceil
    
        checkin = (request.args.get("checkin") or "").strip()
        checkout = (request.args.get("checkout") or "").strip()
        guests = int((request.args.get("guests") or 1) or 1)
        rooms_req = int((request.args.get("rooms") or 1) or 1)
        tipo = (request.args.get("tipo") or request.args.get("type") or "").strip()
    
        # Validar fechas
        try:
            ci = dt.strptime(checkin, "%Y-%m-%d").date()
            co = dt.strptime(checkout, "%Y-%m-%d").date()
        except Exception:
            return jsonify({"ok": False, "available": False, "message": "Fechas inválidas"}), 400
    
        today = date.today()
        if ci < today or co <= ci:
            return jsonify({"ok": True, "available": False, "message": "Rango de fechas no válido"}), 200
    
        # Columnas disponibles
        has_cap = _col_exists("Habitacion", "Capacidad")
        has_tipo = _col_exists("Habitacion", "Tipo")
    
        # Si pidieron un tipo pero no existe en catálogo, devuélvelo claro
        if tipo and has_tipo:
            exists_type = db.session.execute(
                text("SELECT 1 FROM Habitacion WHERE Tipo = :t LIMIT 1"),
                {"t": tipo}
            ).first()
            if not exists_type:
                return jsonify({
                    "ok": True,
                    "available": False,
                    "nights": (co - ci).days,
                    "rooms_available": 0,
                    "rooms_requested": rooms_req,
                    "guests": guests,
                    "checkin": checkin,
                    "checkout": checkout,
                    "message": f"No hay habitaciones de tipo '{tipo}' configuradas."
                }), 200
    
        # Requisitos de capacidad (por habitación)
        need_per_room = ceil(guests / max(rooms_req, 1))
    
        # Habitaciones libres (sin solape con reservas Confirmadas/Pendientes)
        conditions = []
        params = {"ci": checkin, "co": checkout}
        if tipo and has_tipo:
            conditions.append("h.Tipo = :tipo")
            params["tipo"] = tipo
        if has_cap:
            conditions.append("h.Capacidad >= :cap")
            params["cap"] = need_per_room
    
        where_extra = (" AND " + " AND ".join(conditions)) if conditions else ""
    
        rows = db.session.execute(
            text(f"""
                SELECT
                  h.Codigo_Habitacion   AS id
                FROM Habitacion h
                WHERE 1=1
                  AND (h.Estado IS NULL OR h.Estado = 'Disponible')
                  {where_extra}
                  AND NOT EXISTS (
                        SELECT 1
                          FROM Reserva r
                         WHERE r.Codigo_Habitacion = h.Codigo_Habitacion
                           AND r.Estado IN ('Confirmada','Pendiente')
                           AND DATE(r.Fecha_Entrada) < DATE(:co)
                           AND DATE(r.Fecha_Salida)  > DATE(:ci)
                  )
                ORDER BY h.Codigo_Habitacion
            """),
            params
        ).mappings().all()
    
        available_rooms = len(rows)
        nights = (co - ci).days
        is_available = available_rooms >= rooms_req
    
        return jsonify({
            "ok": True,
            "available": bool(is_available),
            "nights": nights,
            "rooms_available": available_rooms,
            "rooms_requested": rooms_req,
            "guests": guests,
            "per_room_capacity_required": need_per_room if has_cap else None,
            "checkin": checkin,
            "checkout": checkout,
            "tipo": tipo or None,
            "message": ("Disponibilidad confirmada" if is_available else "Sin cupo para ese rango"),
        }), 200
        
    @app.get("/api/availability/rooms")
    def api_availability_rooms():
        """
        Devuelve TODAS las habitaciones con estado por habitación:
        available=True/False, disabled=True si está bloqueada y waitlist_url para aplicar.
        Query: ?checkin=YYYY-MM-DD&checkout=YYYY-MM-DD&guests=2&tipo=Suite
        """
        from datetime import datetime as dt
        from sqlalchemy import or_
    
        checkin = (request.args.get("checkin") or "").strip()
        checkout = (request.args.get("checkout") or "").strip()
        guests = int((request.args.get("guests") or request.args.get("adults") or 1) or 1)
        tipo = (request.args.get("tipo") or request.args.get("type") or "").strip()
    
        # Validar fechas
        try:
            ci = dt.strptime(checkin, "%Y-%m-%d").date()
            co = dt.strptime(checkout, "%Y-%m-%d").date()
            if co <= ci:
                return jsonify({"ok": False, "msg": "checkout debe ser posterior a checkin"}), 400
        except Exception:
            return jsonify({"ok": False, "msg": "Fechas inválidas (YYYY-MM-DD)"}), 400
    
        # ¿Existen estas columnas en la BD?
        has_cap  = _col_exists("Habitacion", "Capacidad")
        has_tipo = _col_exists("Habitacion", "Tipo")
    
        # 1) Traer habitaciones con ORM (evita referenciar columnas inexistentes)
        q = Habitacion.query
        if tipo and has_tipo:
            q = q.filter(Habitacion.Tipo == tipo)
        if has_cap:
            # si hay columna capacidad, aceptar null o suficiente para 'guests'
            q = q.filter(or_(Habitacion.Capacidad == None, Habitacion.Capacidad >= guests))  # noqa: E711
    
        # Orden: por número si existe, si no por código
        if hasattr(Habitacion, "Numero_Habitacion"):
            q = q.order_by(Habitacion.Numero_Habitacion.asc(), Habitacion.Codigo_Habitacion.asc())
        else:
            q = q.order_by(Habitacion.Codigo_Habitacion.asc())
    
        habs = q.all()
        ids = [int(getattr(h, "Codigo_Habitacion")) for h in habs] or []
    
        # 2) ¿Cuáles están bloqueadas por reservas en [ci, co)?
        blocked = set()
        if ids:
            try:
                # import local para no romper otros contextos
                from models_sql import Reserva
                blocked_rows = (
                    db.session.query(Reserva.Codigo_Habitacion)
                    .filter(Reserva.Codigo_Habitacion.in_(ids))
                    .filter(Reserva.Estado.in_(("Confirmada", "Pendiente")))
                    .filter(Reserva.Fecha_Entrada < co, Reserva.Fecha_Salida > ci)  # solape [ci, co)
                    .distinct()
                    .all()
                )
                blocked = {int(r[0]) for r in blocked_rows}
            except Exception as e:
                current_app.logger.exception(f"[availability/rooms] error obteniendo bloqueadas: {e}")
                # No abortamos; seguimos y marcamos todas como disponibles para no romper la UI
    
        # 3) Armar salida (con flags available/disabled y URL de waitlist para bloqueadas)
        rooms = []
        for h in habs:
            hid   = int(getattr(h, "Codigo_Habitacion"))
            num   = getattr(h, "Numero_Habitacion", None)
            tipoh = getattr(h, "Tipo", None) or ""
    
            # precio noche robusto
            price = getattr(h, "Precio_Noche", None) or getattr(h, "Precio_Base", None) or getattr(h, "Precio", None) or 0
            try:
                price = float(price or 0)
            except Exception:
                price = 0.0
    
            capacidad = getattr(h, "Capacidad", None)
            try:
                capacidad = int(capacidad or 2)
            except Exception:
                capacidad = 2
    
            img = getattr(h, "Imagen_URL", None) or ""
    
            is_blocked = hid in blocked
            rooms.append({
                "id": hid,
                "number": num,
                "tipo": tipoh,
                "capacity": capacidad,
                "price": price,
                "img": img,
                "available": (not is_blocked),
                "disabled": bool(is_blocked),
                "blockedReason": ("booked" if is_blocked else None),
                "waitlistEligible": bool(is_blocked),
                "waitlist_url": (
                    url_for(
                        "grr.waitlist_add_room",
                        room_id=hid,
                        checkin=checkin,
                        checkout=checkout,
                        guests=guests,
                        tipo=tipoh,
                    )
                    if is_blocked else None
                ),


            })
    
        return jsonify({
            "ok": True,
            "checkin": checkin,
            "checkout": checkout,
            "guests": guests,
            "tipo": (tipo or None),
            "rooms": rooms
        }), 200
    




    @app.get("/booking/search")
    def booking_search_alias():
        return redirect(url_for("api_availability", **request.args))
    
    
    # =========================
    # GRR-01-007: Reserva sin iniciar sesión (API + helpers locales)
    # =========================

    def _try_int_digits(s: str) -> int:
        """Extrae dígitos de un string y devuelve int (o 0 si no hay)."""
        if not s:
            return 0
        import re
        digs = "".join(re.findall(r"\d+", str(s)))
        try:
            return int(digs) if digs else 0
        except Exception:
            return 0

    def _upsert_cliente_con_doc(nombre, apellido, correo, telefono, doc_tipo, doc_num):
        """
        Asegura un Cliente con correo y documento.
        - Cedula (entero) se llena si hay dígitos; si es pasaporte alfa-numérico -> 0.
        - Si ya existe por correo, actualiza Nombre/Apellido/Telefono si vienen nuevos.
        """
        correo = (correo or "").strip().lower()
        row = db.session.execute(
            text("SELECT Codigo_Cliente FROM Cliente WHERE LOWER(Correo)=:e LIMIT 1"),
            {"e": correo}
        ).first()
        cedula_int = _try_int_digits(doc_num)
        if row:
            cid = int(row[0])
            db.session.execute(text("""
                UPDATE Cliente
                   SET Nombre     = COALESCE(NULLIF(:n,''), Nombre),
                       Apellido   = COALESCE(NULLIF(:a,''), Apellido),
                       Telefono   = COALESCE(NULLIF(:t,''), Telefono),
                       Cedula     = CASE WHEN :c > 0 THEN :c ELSE Cedula END
                 WHERE Codigo_Cliente = :id
            """), {"n": nombre, "a": apellido, "t": telefono, "c": cedula_int, "id": cid})
            db.session.commit()
            return cid
        # crear
        db.session.execute(text("""
            INSERT INTO Cliente (Cedula, Nombre, Apellido, Telefono, Correo, Fecha_Nacimiento)
            VALUES (:ced, :n, :a, :t, :e, '1990-01-01')
        """), {"ced": cedula_int, "n": nombre or "Cliente", "a": apellido or "", "t": telefono or "00000000", "e": correo})
        db.session.commit()
        new_id = db.session.execute(
            text("SELECT Codigo_Cliente FROM Cliente WHERE LOWER(Correo)=:e LIMIT 1"),
            {"e": correo}
        ).scalar()
        return int(new_id)

    def _create_or_link_usuario(correo: str, cliente_id: int, nombre_completo: str,
                                telefono: Optional[str] = None, doc_numero: Optional[str] = None
                               ) -> tuple[Optional[int], Optional[str]]:
        """
        Crea (o enlaza) Usuario con rol 'Cliente' y devuelve (usuario_id, temp_password).
        Incluye Telefono si la columna existe (evita error 1364) y, si existe, Cedula_Pasaporte.
        Si no hay columna de contraseña, se crea usuario sin contraseña y se forzará reset por email.
        """
        correo = (correo or "").strip().lower()
    
        # ¿Ya existe por correo?
        urow = db.session.execute(
            text("SELECT Codigo_Usuario, COALESCE(Codigo_Cliente,0) FROM Usuario WHERE LOWER(Correo)=:e LIMIT 1"),
            {"e": correo}
        ).first()
        temp_pwd = None
        if urow:
            uid, cc = int(urow[0]), int(urow[1] or 0)
            if not cc and cliente_id:
                db.session.execute(text("UPDATE Usuario SET Codigo_Cliente=:c WHERE Codigo_Usuario=:u"),
                                   {"c": cliente_id, "u": uid})
                db.session.commit()
            return uid, temp_pwd
    
        # Resolver columnas disponibles en la tabla Usuario
        tel_col  = _col_exists("Usuario", "Telefono")
        pwd_col  = _col_exists("Usuario", "Contrasena")
        doc_col  = _col_exists("Usuario", "Cedula_Pasaporte")
    
        # Telefono a usar
        tel_val = (telefono or "").strip()
        if not tel_val:
            tel_val = (db.session.execute(
                text("SELECT Telefono FROM Cliente WHERE Codigo_Cliente=:id LIMIT 1"),
                {"id": cliente_id}
            ).scalar() or "").strip()
        if not tel_val:
            tel_val = "00000000"
        tel_val = tel_val[:20]
    
        # Doc a usar (si la columna existe)
        doc_val = (doc_numero or "").strip()[:64] if doc_col else None
    
        # Preparar password temporal si existe columna de contraseña
        import secrets
        try:
            from werkzeug.security import generate_password_hash
            if pwd_col:
                temp_pwd = secrets.token_urlsafe(8)
                pwd_hash = generate_password_hash(temp_pwd)
            else:
                pwd_hash = None
        except Exception:
            if pwd_col:
                temp_pwd = secrets.token_urlsafe(8)
                pwd_hash = temp_pwd
            else:
                pwd_hash = None
    
        # Rol cliente
        rol = _get_role_by_name("Cliente")
        rol_id = getattr(rol, "Codigo_Rol", None) or getattr(rol, "id", None)
    
        # Construir INSERT dinámico según columnas existentes
        cols = ["Codigo_Cliente", "Correo", "Rol_Id", "Estado", "Nombre"]
        params = {"c": cliente_id, "e": correo, "r": rol_id, "n": nombre_completo[:80]}
        if pwd_col:
            cols.append("Contrasena"); params["p"] = pwd_hash
        if tel_col:
            cols.append("Telefono"); params["t"] = tel_val
        if doc_col and doc_val:
            cols.append("Cedula_Pasaporte"); params["d"] = doc_val
    
        placeholders = []
        for col in cols:
            if col == "Contrasena":
                placeholders.append(":p")
            elif col == "Telefono":
                placeholders.append(":t")
            elif col == "Cedula_Pasaporte":
                placeholders.append(":d")
            elif col == "Codigo_Cliente":
                placeholders.append(":c")
            elif col == "Correo":
                placeholders.append(":e")
            elif col == "Rol_Id":
                placeholders.append(":r")
            elif col == "Estado":
                placeholders.append("'Activo'")
            elif col == "Nombre":
                placeholders.append(":n")
            else:
                placeholders.append("NULL")
    
        try:
            sql = text(f"INSERT INTO Usuario ({', '.join(cols)}) VALUES ({', '.join(placeholders)})")
            res = db.session.execute(sql, params)
            db.session.commit()
            uid = res.lastrowid or db.session.execute(
                text("SELECT Codigo_Usuario FROM Usuario WHERE LOWER(Correo)=:e LIMIT 1"), {"e": correo}
            ).scalar()
            return int(uid), temp_pwd
        except Exception as e:
            current_app.logger.error(f"[USUARIO] No se pudo crear: {e}")
            db.session.rollback()
            return None, None


    def _send_new_account_and_reserva_email(to_email: str, numero: str, ci: str, co: str, temp_pwd: Optional[str]):
        """
        Envía correo de confirmación de reserva y (si aplica) contraseña temporal.
        Incluye link a iniciar sesión y a 'olvidé mi contraseña' para forzar cambio.
        """
        login_link = url_for("login_html", _external=True)
        forgot_link = url_for("forgot_password", _external=True)

        # Si existe flujo de reset por token:
        try:
            s = _get_serializer(app)
            token = s.dumps({"email": to_email})  # estandarizado
            reset_url = url_for("reset_password", token=token, _external=True)
        except Exception:
            reset_url = forgot_link

        subject = f"Reserva {numero} — Hotel Villa Grace"
        body = [
            "¡Gracias por reservar en Hotel Villa Grace!",
            f"Número de reserva: {numero}",
            f"Check-in: {ci}",
            f"Check-out: {co}",
            "",
            "Tu cuenta de huésped fue creada con este correo.",
        ]
        if temp_pwd:
            body += [f"Contraseña temporal: {temp_pwd}"]
        body += [
            "",
            f"Inicia sesión aquí: {login_link}",
            f"Para cambiar tu contraseña, usa este enlace: {reset_url}",
            "",
            "Si no solicitaste esta reserva, contáctanos.",
            "— Hotel Villa Grace"
        ]
        sender = app.config.get("MAIL_DEFAULT_SENDER") or app.config.get("MAIL_USERNAME") or "no-reply@hotel.local"

        try:
            host = app.config.get("MAIL_SERVER")
            port = int(app.config.get("MAIL_PORT", 0) or 0)
            user = app.config.get("MAIL_USERNAME")
            pwd  = app.config.get("MAIL_PASSWORD")
            use_tls = bool(app.config.get("MAIL_USE_TLS", False))
            use_ssl = bool(app.config.get("MAIL_USE_SSL", False))

            if not (host and port and user and pwd):
                current_app.logger.info(f"[MAIL MOCK] To: {to_email}\nSubj: {subject}\n\n" + "\n".join(body))
                return

            msg = EmailMessage()
            msg["Subject"] = subject
            msg["From"] = sender
            msg["To"] = to_email
            msg.set_content("\n".join(body))

            if use_ssl:
                context = ssl.create_default_context()
                with smtplib.SMTP_SSL(host, port, context=context, timeout=30) as smtp:
                    smtp.login(user, pwd); smtp.send_message(msg)
            else:
                with smtplib.SMTP(host, port, timeout=30) as smtp:
                    if use_tls:
                        smtp.starttls(context=ssl.create_default_context())
                    smtp.login(user, pwd); smtp.send_message(msg)
        except Exception as e:
            current_app.logger.warning(f"[MAIL] Fallback console: {e}")
            current_app.logger.info(f"[MAIL MOCK] To: {to_email}\nSubj: {subject}\n\n" + "\n".join(body))
            
            
    def _get_reserva_contacto(reserva_id: int) -> dict:
        """
        Devuelve los datos de contacto asociados a la reserva:
          - email (prefiere Usuario.Correo, luego Cliente.Correo)
          - phone (prefiere Usuario.Telefono, luego Cliente.Telefono)
          - nombre completo
        """
        row = db.session.execute(text("""
            SELECT 
              C.Nombre, C.Apellido, C.Correo AS c_email, C.Telefono AS c_tel,
              U.Correo AS u_email,
              CASE WHEN EXISTS(SELECT 1 FROM INFORMATION_SCHEMA.COLUMNS 
                                 WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME='Usuario' AND COLUMN_NAME='Telefono')
                   THEN U.Telefono ELSE NULL END AS u_tel
            FROM Reserva R
            JOIN Cliente C ON C.Codigo_Cliente = R.Codigo_Cliente
            LEFT JOIN Usuario U ON U.Codigo_Cliente = C.Codigo_Cliente
            WHERE R.Codigo_Reserva = :rid
            LIMIT 1
        """), {"rid": reserva_id}).mappings().first()
    
        if not row:
            return {"email": None, "phone": None, "nombre": None}
    
        nombre = f"{(row['Nombre'] or '').strip()} {(row['Apellido'] or '').strip()}".strip() or None
        email  = (row.get("u_email") or row.get("c_email") or "").strip().lower() or None
        phone  = (row.get("u_tel") or row.get("c_tel") or "").strip() or None
        return {"email": email, "phone": phone, "nombre": nombre}
    
    
    def _send_sms(to_phone: str, message: str) -> None:
        """
        Envía SMS usando Twilio si está configurado; si no, hace log consola.
        TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN, TWILIO_FROM deben estar en env/config para enviar real.
        """
        if not to_phone or not message:
            return
        sid    = current_app.config.get("TWILIO_ACCOUNT_SID") or os.getenv("TWILIO_ACCOUNT_SID")
        token  = current_app.config.get("TWILIO_AUTH_TOKEN") or os.getenv("TWILIO_AUTH_TOKEN")
        from_n = current_app.config.get("TWILIO_FROM") or os.getenv("TWILIO_FROM")
        if not (sid and token and from_n):
            current_app.logger.info(f"[SMS MOCK] To: {to_phone}\n{message}")
            return
    
        try:
            from twilio.rest import Client  # type: ignore
            cli = Client(sid, token)
            cli.messages.create(to=to_phone, from_=from_n, body=message)
            current_app.logger.info(f"[SMS SENT] {to_phone}")
        except Exception as e:
            current_app.logger.warning(f"[SMS ERROR] {e}. Haciendo LOG como fallback.")
            current_app.logger.info(f"[SMS MOCK] To: {to_phone}\n{message}")
    
    
    def _send_reserva_confirmation_email(to_email: str, reserva: dict, pdf_path: Optional[Path] = None) -> None:
        """
        Envía correo de confirmación con detalles de la reserva.
        Adjunta comprobante PDF si se pasa pdf_path.
        """
        if not to_email:
            return
    
        numero  = reserva.get("Numero") or reserva.get("numero")
        ci      = str(reserva.get("Fecha_Entrada") or reserva.get("checkin") or "")[:10]
        co      = str(reserva.get("Fecha_Salida")  or reserva.get("checkout") or "")[:10]
        tipo    = reserva.get("Tipo") or "Habitación"
        pax     = str(reserva.get("Huespedes") or reserva.get("huespedes") or "1")
        monto   = float(reserva.get("Monto_Total") or reserva.get("monto") or 0.0)
        canal   = reserva.get("Canal") or reserva.get("canal") or "Web"
        estado  = reserva.get("Estado") or reserva.get("estado") or "Confirmada"
    
        portal_link = url_for("portal_reservas_html", _external=True)
        subject = f"Confirmación de Reserva {numero} — Hotel Villa Grace"
        body = (
            "¡Gracias por tu reserva en Hotel Villa Grace!\n\n"
            f"Número de reserva: {numero}\n"
            f"Estado: {estado}\n"
            f"Habitación: {tipo}\n"
            f"Huéspedes: {pax}\n"
            f"Check-in: {ci}\n"
            f"Check-out: {co}\n"
            f"Canal: {canal}\n"
            f"Total: ₡ {monto:,.2f}\n\n".replace(",", "X").replace(".", ",").replace("X", ".")
            + f"Puedes ver tus reservas y descargar tus comprobantes aquí: {portal_link}\n\n"
            "Si no fuiste tú quien realizó esta reserva, por favor contáctanos de inmediato.\n\n"
            "— Hotel Villa Grace"
        )
    
        host = current_app.config.get("MAIL_SERVER")
        port = int(current_app.config.get("MAIL_PORT", 0) or 0)
        user = current_app.config.get("MAIL_USERNAME")
        pwd  = current_app.config.get("MAIL_PASSWORD")
        use_tls = bool(current_app.config.get("MAIL_USE_TLS", False))
        use_ssl = bool(current_app.config.get("MAIL_USE_SSL", False))
        sender = (current_app.config.get("MAIL_DEFAULT_SENDER")
                  or current_app.config.get("MAIL_USERNAME")
                  or "no-reply@hotel.local")
    
        # Fallback consola si SMTP no está configurado
        if not (host and port and user and pwd):
            current_app.logger.info(f"[MAIL MOCK] To: {to_email}\nSubj: {subject}\n\n{body}")
            return
    
        try:
            msg = EmailMessage()
            msg["Subject"] = subject
            msg["From"] = sender
            msg["To"] = to_email
            msg.set_content(body)
    
            if pdf_path and pdf_path.exists():
                with open(pdf_path, "rb") as f:
                    data = f.read()
                msg.add_attachment(
                    data,
                    maintype="application",
                    subtype="pdf",
                    filename=pdf_path.name,
                )
    
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
    
            current_app.logger.info(f"[MAIL SENT] Confirmación a {to_email} (reserva {numero})")
        except Exception as e:
            current_app.logger.warning(f"[MAIL ERROR] {e}. Fallback consola:")
            current_app.logger.info(f"[MAIL MOCK] To: {to_email}\nSubj: {subject}\n\n{body}")
            
            
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

    
    
    def _ensure_reserva_numero(reserva_id: int, fecha_entrada: Optional[str]) -> str:
        """Garantiza que la reserva tenga Numero_Comprobante; lo asigna si está vacío."""
        numero = db.session.execute(
            text("SELECT Numero_Comprobante FROM Reserva WHERE Codigo_Reserva=:r LIMIT 1"),
            {"r": reserva_id}
        ).scalar()
        if numero:
            return str(numero)
        numero = _make_unique_number(reserva_id, (fecha_entrada or "")[:10])
        try:
            db.session.execute(
                text("UPDATE Reserva SET Numero_Comprobante=:n WHERE Codigo_Reserva=:r"),
                {"n": numero, "r": reserva_id}
            )
            db.session.commit()
        except Exception:
            db.session.rollback()
        return numero
    
    
    # --- Notificación de confirmación de reserva (respeta preferencias SAC) ---
    def _notify_reserva_success(rid: int) -> None:
        """
        Envía confirmación de reserva respetando preferencias (email/sms/ambos).
        Camino principal: NotificationService.send_reserva_confirmation(rid)
        Fallback: route_and_queue() con datos mínimos del cliente.
        """
        try:
            from services.grr.notification_service import NotificationService
            ns = NotificationService()
            res = ns.send_reserva_confirmation(int(rid))
            current_app.logger.info(f"[GRR-01-009] Notificación procesada R={rid} -> {res}")
            return
        except AttributeError:
            # Si la versión no tiene send_reserva_confirmation, caemos al fallback
            pass
        except Exception as e:
            current_app.logger.warning(f"[GRR-01-009] Camino principal falló R={rid}: {e}")
    
        # ---------- Fallback seguro (sin h.Nombre) ----------
        cid = None
        email_fb = None
        tel_fb = None
        hab = None
        f_in = None
        f_out = None
    
        # Cliente (correo/teléfono)
        try:
            row = db.session.execute(
                text("""
                    SELECT 
                        r.Codigo_Cliente   AS cid,
                        c.Correo           AS email_fb,
                        c.Telefono         AS tel_fb
                    FROM Reserva r
                    LEFT JOIN Cliente c ON c.Codigo_Cliente = r.Codigo_Cliente
                    WHERE r.Codigo_Reserva = :rid
                    LIMIT 1
                """),
                {"rid": int(rid)}
            ).mappings().first()
            if row:
                cid = row.get("cid")
                email_fb = (row.get("email_fb") or None)
                tel_fb   = (row.get("tel_fb") or None)
        except Exception as e:
            current_app.logger.warning(f"[GRR-01-009] Fallback: no se pudo leer Cliente para R={rid}: {e}")
    
        # Detalles mínimos de reserva (NUNCA h.Nombre)
        try:
            rdet = db.session.execute(
                text("""
                    SELECT 
                        h.Numero_Habitacion AS habitacion,
                        r.Fecha_Entrada     AS f_in,
                        r.Fecha_Salida      AS f_out
                    FROM Reserva r
                    LEFT JOIN Habitacion h ON h.Codigo_Habitacion = r.Codigo_Habitacion
                    WHERE r.Codigo_Reserva = :rid
                    LIMIT 1
                """),
                {"rid": int(rid)}
            ).mappings().first()
            if rdet:
                hab  = rdet.get("habitacion")
                f_in = rdet.get("f_in")
                f_out= rdet.get("f_out")
        except Exception as e:
            current_app.logger.warning(f"[GRR-01-009] Fallback: no se pudo leer detalles R={rid}: {e}")
    
        # Horarios desde SAC_Config (si existen)
        checkin_ini  = "12:00"; checkin_fin = "00:00"; checkout = "12:00"
        try:
            cfgs = db.session.execute(
                text("SELECT Clave, Valor FROM SAC_Config WHERE Clave IN ('checkin_inicio','checkin_fin','checkout_limite')")
            ).mappings().all()
            kv = {r["Clave"]: (r["Valor"] or "") for r in cfgs}
            checkin_ini = kv.get("checkin_inicio", checkin_ini)
            checkin_fin = kv.get("checkin_fin", checkin_fin)
            checkout    = kv.get("checkout_limite", checkout)
        except Exception:
            pass
    
        subject = f"Confirmación de reserva #{rid} – Hotel Villa Grace"
        body = "\n".join([x for x in [
            "¡Gracias por reservar en Hotel Villa Grace!",
            f"Nº de reserva: #{rid}",
            (f"Habitación: {hab}" if hab else None),
            (f"Entrada: {f_in}  (check-in {checkin_ini}–{checkin_fin})" if f_in else None),
            (f"Salida:  {f_out} (check-out hasta {checkout})" if f_out else None),
            "Si necesitas ayuda, responde a este mensaje."
        ] if x])
    
        try:
            from services.grr.notification_service import NotificationService
            ns = NotificationService()
            res = ns.route_and_queue(
                codigo_cliente=(int(cid) if cid else None),
                asunto=subject,
                cuerpo=body,
                email_fallback=email_fb,
                tel_fallback=tel_fb,
                ref_tipo="Reserva",
                ref_id=str(rid),
            )
            current_app.logger.info(f"[GRR-01-009] Notificación (fallback) R={rid} -> {res}")
        except Exception as e2:
            current_app.logger.warning(f"[GRR-01-009] Notificación omitida R={rid}: {e2}")

    
    
    def _notify_reserva_pending(reserva_id: int):
        """
        Envía notificación de 'pendiente de confirmación' respetando preferencias (/sac/preferencias).
        """
        try:
            from services.grr.notification_service import NotificationService
            NotificationService().send_reserva_pending(int(reserva_id))
        except Exception as e:
            try:
                current_app.logger.warning(f"[NOTIFY] Pendiente fallo reserva_id={reserva_id}: {e}")
            except Exception:
                pass
    
    


    
    @app.post("/api/reservas/anon")
    def api_reservas_anon_create():
        """
        Crea una reserva pública (GRR-01-007) y asegura:
          - Cliente creado/actualizado con documento
          - Usuario con rol 'Cliente' creado o enlazado
          - Inserción en Reserva incluyendo Codigo_Funcionario si la columna existe (y mensaje claro si no hay ninguno)
          - Número de comprobante único
          - Auditoría, KPI y correo de confirmación
    
        Importante:
          - 'tipo' es OPCIONAL (coincide con templates/anon-reserva.html)
          - La verificación de disponibilidad se alinea con /grr/availability:
            solo excluye 'Mantenimiento' y solapes; no exige estado 'Disponible'.
        """
        p = request.get_json(silent=True) or {}
    
        def req(k: str) -> str:
            return (p.get(k) or "").strip()
    
        # ---- Datos del formulario ----
        checkin    = req("checkin")
        checkout   = req("checkout")
        tipo       = req("tipo")  # OPCIONAL
        nombre     = req("nombre")
        apellido   = req("apellido")
        correo     = (req("correo") or "").lower()
        telefono   = req("telefono")
        doc_tipo   = req("doc_tipo")
        doc_numero = req("doc_numero")
        acepta     = str(p.get("acepta") or "0") in ("1", "true", "True", "on", "sí", "si")
    
        try:
            huespedes = int(p.get("huespedes") or 1)
        except Exception:
            huespedes = 1
        huespedes = max(1, huespedes)
    
        # ---- Validaciones mínimas (tipo NO es obligatorio) ----
        if not (checkin and checkout and nombre and apellido and correo and telefono and doc_tipo and doc_numero and acepta):
            return jsonify({"ok": False, "message": "Faltan campos obligatorios."}), 400
    
        # ---- Parseo de fechas ----
        try:
            ci_dt = datetime.strptime(checkin, "%Y-%m-%d").date()
            co_dt = datetime.strptime(checkout, "%Y-%m-%d").date()
            if (co_dt - ci_dt).days <= 0:
                return jsonify({"ok": False, "message": "La fecha de salida debe ser posterior a la de llegada."}), 400
        except Exception:
            return jsonify({"ok": False, "message": "Fechas inválidas (YYYY-MM-DD)."}), 400
    
        # ---- Cliente (upsert por correo + documento) ----
        try:
            cliente_id = _upsert_cliente_con_doc(
                nombre, apellido, correo, telefono, doc_tipo, doc_numero
            )
        except Exception as e:
            current_app.logger.exception("[ANON] Error creando/enlazando Cliente: %s", e)
            return jsonify({"ok": False, "message": "No se pudo crear/enlazar al cliente."}), 500
    
        if not cliente_id:
            return jsonify({"ok": False, "message": "No se pudo crear/enlazar al cliente."}), 500
    
        # ---- Usuario (cuenta de portal, rol 'Cliente') ----
        try:
            uid, temp_pwd = _create_or_link_usuario(
                correo, cliente_id, f"{nombre} {apellido}", telefono=telefono, doc_numero=doc_numero
            )
        except Exception as e:
            current_app.logger.exception("[ANON] Error creando/enlazando Usuario: %s", e)
            return jsonify({"ok": False, "message": "No se pudo crear la cuenta del huésped."}), 500
    
        if not uid:
            return jsonify({"ok": False, "message": "No se pudo crear la cuenta del huésped."}), 500
    
        # ---- Comprobaciones de columnas (capacidad / tipo / funcionario) ----
        has_cap  = _col_exists("Habitacion", "Capacidad")
        has_tipo = _col_exists("Habitacion", "Tipo")
    
        # Si viene tipo pero no existe en catálogo, mensaje claro
        if tipo and has_tipo:
            _type_exists = db.session.execute(
                text("SELECT 1 FROM Habitacion WHERE Tipo = :t LIMIT 1"),
                {"t": tipo}
            ).first()
            if not _type_exists:
                return jsonify({"ok": False, "message": f"No hay habitaciones de tipo '{tipo}' configuradas."}), 400
    
        # ---- Buscar una habitación LIBRE que cumpla con tipo/capacidad y rango ----
        conds  = []
        params = {"ci": checkin, "co": checkout}
    
        if tipo and has_tipo:
            conds.append("h.Tipo = :t")  # colación UTF8MB4 normalmente es case-insensitive
            params["t"] = tipo
    
        if has_cap:
            conds.append("COALESCE(h.Capacidad, 2) >= :cap")
            params["cap"] = int(huespedes)
    
        where_extra = (" AND " + " AND ".join(conds)) if conds else ""
    
        # Alineado con /grr/availability:
        #  - NO exigimos 'Disponible' (el estado actual no bloquea reservas futuras)
        #  - SÍ excluimos 'Mantenimiento'
        #  - Excluimos reservas solapadas con Estado <> 'Cancelada'
        hab = db.session.execute(
            text(f"""
                SELECT h.Codigo_Habitacion, h.Precio_Noche
                  FROM Habitacion h
                 WHERE (h.Estado IS NULL OR h.Estado <> 'Mantenimiento')
                   {where_extra}
                   AND NOT EXISTS (
                         SELECT 1
                           FROM Reserva r
                          WHERE r.Codigo_Habitacion = h.Codigo_Habitacion
                            AND r.Estado <> 'Cancelada'
                            AND DATE(r.Fecha_Entrada) < DATE(:co)
                            AND DATE(r.Fecha_Salida)  > DATE(:ci)
                   )
                 ORDER BY h.Codigo_Habitacion
                 LIMIT 1
            """),
            params
        ).mappings().first()
    
        if not hab:
            return jsonify({
                "ok": False,
                "message": "No hay habitaciones disponibles que cumplan los criterios para ese rango."
            }), 409
    
        hab_id  = int(hab["Codigo_Habitacion"])
        price_n = float(hab["Precio_Noche"] or 0.0)
    
        # ---- Calcular noches y monto total (server-side autoritativo) ----
        nights = max((co_dt - ci_dt).days, 1)
        # IVA configurable; fallback 13%
        tax = None
        for k in ("VAT_RATE", "TAX_RATE", "IVA", "IVA_RATE"):
            if k in current_app.config:
                try:
                    tax = float(current_app.config.get(k))
                    break
                except Exception:
                    pass
        if tax is None:
            tax = 0.13
    
        subtotal    = round(price_n * nights * huespedes, 2)
        monto_total = round(subtotal * (1.0 + tax), 2)
    
        # ---- Observaciones (guardar doc y tel para check-in) ----
        obs = f"GRR-01-007 | Doc: {doc_tipo} {doc_numero} | Tel: {telefono}"
    
        # ---- Resolver Codigo_Funcionario si la columna existe ----
        has_func_col = _col_exists("Reserva", "Codigo_Funcionario")
        func_id = None
        if has_func_col:
            uid_sess = session.get("user_id")
            try:
                uid_sess = int(uid_sess) if uid_sess is not None else None
            except Exception:
                uid_sess = None
    
            if uid_sess:
                try:
                    func_id = db.session.execute(
                        text("SELECT Codigo_Funcionario FROM Funcionario WHERE Codigo_Usuario = :u LIMIT 1"),
                        {"u": uid_sess}
                    ).scalar()
                    try:
                        func_id = int(func_id) if func_id is not None else None
                    except Exception:
                        func_id = None
                except Exception:
                    func_id = None
    
            if func_id is None and not _col_nullable("Reserva", "Codigo_Funcionario"):
                try:
                    any_f = db.session.execute(
                        text("SELECT Codigo_Funcionario FROM Funcionario ORDER BY Codigo_Funcionario ASC LIMIT 1")
                    ).scalar()
                    func_id = int(any_f) if any_f is not None else None
                except Exception:
                    func_id = None
    
            if func_id is None and not _col_nullable("Reserva", "Codigo_Funcionario"):
                return jsonify({
                    "ok": False,
                    "message": "No hay funcionarios creados y la columna Reserva.Codigo_Funcionario no admite NULL."
                }), 409
    
        # ---- Insert dinámico en Reserva ----
        try:
            cols = [
                "Codigo_Cliente", "Codigo_Habitacion",
                "Fecha_Entrada", "Fecha_Salida",
                "Canal", "Estado",
                "Huespedes", "Monto_Total",
                "Observaciones", "Fecha_Registro"
            ]
            vals = [
                ":c", ":h",
                ":ci", ":co",
                "'Web'", "'Pendiente'",
                ":pax", ":m",
                ":obs", "NOW()"
            ]
            iparams = {
                "c": int(cliente_id),
                "h": int(hab_id),
                "ci": checkin,
                "co": checkout,
                "pax": int(huespedes),
                "m": float(monto_total),
                "obs": obs
            }
    
            if has_func_col and func_id is not None:
                cols.append("Codigo_Funcionario")
                vals.append(":f")
                iparams["f"] = int(func_id)
    
            sql_insert = text(f"INSERT INTO Reserva ({', '.join(cols)}) VALUES ({', '.join(vals)})")
            res = db.session.execute(sql_insert, iparams)
            db.session.commit()
        except Exception as e:
            db.session.rollback()
            current_app.logger.exception("[ANON] Error insertando Reserva: %s", e)
            return jsonify({"ok": False, "message": "No se pudo registrar la reserva."}), 500
    
        # ---- Obtener ID de la reserva recién creada ----
        try:
            reserva_id = int(
                res.lastrowid
                or db.session.execute(
                    text("""
                        SELECT Codigo_Reserva
                          FROM Reserva
                         WHERE Codigo_Cliente=:c AND Fecha_Entrada=:ci AND Fecha_Salida=:co
                         ORDER BY Codigo_Reserva DESC
                         LIMIT 1
                    """),
                    {"c": cliente_id, "ci": checkin, "co": checkout}
                ).scalar()
            )
        except Exception as e:
            current_app.logger.exception("[ANON] No se pudo recuperar Codigo_Reserva: %s", e)
            return jsonify({"ok": False, "message": "Reserva creada, pero no se pudo obtener el ID."}), 500
    
        # ---- Asignar número único de comprobante ----
        try:
            numero = _make_unique_number(reserva_id, checkin)
            db.session.execute(
                text("UPDATE Reserva SET Numero_Comprobante=:n WHERE Codigo_Reserva=:id"),
                {"n": numero, "id": reserva_id}
            )
            db.session.commit()
        except Exception as e:
            db.session.rollback()
            current_app.logger.warning("[ANON] No se pudo asignar Numero_Comprobante: %s", e)
            numero = _make_unique_number(reserva_id, checkin)  # fallback in-memory
    
        # ---- Auditoría + KPI ----
        try:
            _audit_log(correo, "reserva.creada.publica",
                       {"Codigo_Reserva": reserva_id, "Numero": numero},
                       entidad_id=str(reserva_id))
        except Exception:
            pass
        try:
            _update_kpis(float(monto_total or 0.0), checkin)
        except Exception:
            pass
    
        # ---- Enviar correo de confirmación / credenciales ----
        try:
            _send_new_account_and_reserva_email(correo, numero, checkin, checkout, temp_pwd)
        except Exception as e:
            current_app.logger.warning("[ANON] Error enviando correo de confirmación: %s", e)
    
        return jsonify({
            "ok": True,
            "reserva_id": reserva_id,
            "numero": numero,
            "redirect": url_for("anon_reserva_exito_html", numero=numero)
        }), 201
    
    
    
   

    

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
                "estado": h.Estado,  # Disponible, Ocupada, Mantenimiento
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
            adults = int((request.form.get("adults") or 2) or 2)
            children = int((request.form.get("children") or 0) or 0)
            rooms = int((request.form.get("rooms") or 1) or 1)
            guests = max(1, adults + children)
    
            return redirect(
                url_for(
                    "booking_results",
                    checkin=checkin,
                    checkout=checkout,
                    adults=adults,
                    children=children,
                    guests=guests,
                    rooms=rooms,
                )
            )
        return render_template("booking-search.html")


    @app.route("/booking-results", methods=["GET"])
    @app.route("/booking-results.html", methods=["GET"])
    def booking_results():
        args = request.args.to_dict(flat=True)
    
        # Derivar guests si no viene: adults + children
        try:
            a = int(args.get("adults") or 0)
            c = int(args.get("children") or 0)
        except Exception:
            a, c = 0, 0
        if not args.get("guests"):
            args["guests"] = str(max(1, a + c) or 1)
        if not args.get("rooms"):
            args["rooms"] = "1"
    
        with app.test_client() as c:
            resp_av = c.get(url_for("api_availability", **args))
            data_av = resp_av.get_json() if resp_av.is_json else {"ok": False}

            resp_rooms = c.get(url_for("api_availability_rooms", **args))
            data_rooms = resp_rooms.get_json() if resp_rooms.is_json else {"ok": False, "rooms": []}

        return render_template("booking-results.html",
                               availability=data_av,
                               availability_rooms=data_rooms)

    

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

    @app.route("/booking-checkout", methods=["GET"])
    @app.route("/booking-checkout.html", methods=["GET"])
    def booking_checkout_html():
        def _sac_cfg(clave: str, default=None):
            try:
                row = db.session.execute(
                    text("SELECT Valor FROM SAC_Config WHERE Clave=:c LIMIT 1"),
                    {"c": clave}
                ).first()
                val = row[0] if row else None
                return val if val not in (None, "") else default
            except Exception:
                return default
    
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
            # SINPE (Paso 1)
            "sinpe_mobile": _sac_cfg("sinpe_mobile", None),
            "sinpe_beneficiary": _sac_cfg("sinpe_beneficiary", "Hotel Villa Grace"),
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
            "reservation_code": request.args.get("code", "VG-" + datetime.now().strftime("%Y%m%d-%H%M%S")),
            "reservation_id": request.args.get("id") or request.args.get("reserva_id"),
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
        
        
    @app.route("/api/portal/reservas/export", methods=["GET"])
    @role_required("Cliente")
    def api_portal_reservas_export():
        """
        Exporta el HISTORIAL del cliente:
          - ?format=pdf  -> PDF listado
          - ?format=excel|xls|xlsx -> CSV compatible con Excel
        Respeta los mismos filtros que /api/portal/reservas: estado, fini, ffin.
        """
        if not session.get("user_id"):
            return jsonify({"ok": False, "error": "auth_required"}), 401

        email = _current_user_email()
        if not email:
            return jsonify({"ok": True, "warning": "no_email"}), 200

        estado = request.args.get("estado") or None
        fini = request.args.get("fini") or None
        ffin = request.args.get("ffin") or None
        fmt = (request.args.get("format") or "pdf").lower()

        items = _query_user_reservas(email, estado, fini, ffin) or []
        ts = datetime.utcnow().strftime("%Y%m%d-%H%M%S")

        if fmt == "pdf":
            fname = f"reservas-{ts}.pdf"
            path = _create_reservas_pdf(items, "Historial de reservas — Hotel Villa Grace", fname)
            return send_file(str(path), as_attachment=True, download_name=fname, mimetype="application/pdf")

        if fmt in ("excel", "xls", "xlsx"):
            fname = f"reservas-{ts}.csv"
            path = _create_reservas_csv(items, fname)
            return send_file(str(path), as_attachment=True, download_name=fname, mimetype="text/csv")

        return jsonify({"ok": True, "items": items})


    @app.get("/api/portal/reservas/<int:reserva_id>")
    @role_required("Cliente")
    def api_portal_reserva_get(reserva_id: int):
        email = _current_user_email()
        if not email:
            return jsonify({"ok": False, "message": "No autenticado."}), 401

        row = _get_reserva_by_id(reserva_id)
        if not row:
            return jsonify({"ok": False, "message": "Reserva no encontrada."}), 404

        if not _reserva_belongs_to_email(reserva_id, email):
            return jsonify({"ok": False, "message": "No autorizado."}), 403

        return jsonify({"ok": True, "item": _reserva_to_dict(row)}), 200

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
        """
        Exporta UNA reserva:
          - ?format=pdf  -> PDF de comprobante (por defecto)
          - ?format=excel|xls|xlsx -> CSV compatible con Excel, con campos de la reserva
        """
        if not session.get("user_id"):
            return jsonify({"ok": False, "error": "auth_required"}), 401
        email = _current_user_email()
        if not _reserva_belongs_to_email(reserva_id, email or ""):
            return jsonify({"ok": False, "error": "forbidden"}), 403

        r = _get_reserva_by_id(reserva_id)
        if not r:
            return jsonify({"ok": False, "error": "not_found"}), 404

        fmt = (request.args.get("format") or "pdf").lower()

        # PDF de comprobante (existente)
        if fmt == "pdf":
            numero = r.get("Numero") or _make_unique_number(
                reserva_id, _normalize_date_like(r.get("Fecha_Entrada")) or ""
            )
            pdf_path = COMPROBANTES_DIR / f"{numero}.pdf"
            if not pdf_path.exists():
                _create_comprobante_pdf(dict(r))
            if not pdf_path.exists():
                return jsonify({"ok": False, "error": "not_available"}), 404
            return send_file(str(pdf_path), as_attachment=True, download_name=f"{numero}.pdf")

        # Excel (CSV compatible) de UNA reserva
        if fmt in ("excel", "xls", "xlsx"):
            data = [_reserva_to_dict(r)]
            num = data[0].get("numero") or f"VG-{reserva_id}"
            fname = f"reserva-{num}.csv"
            path = _create_reservas_csv(data, fname)
            return send_file(str(path), as_attachment=True, download_name=fname, mimetype="text/csv")

        # Fallback: JSON (solo si alguien lo invoca explícitamente)
        return jsonify({"ok": True, "item": _reserva_to_dict(r)})
    
    
    
    
    @app.post("/api/portal/reservas/<int:reserva_id>/quote-changes")
    @role_required("Cliente")
    def api_portal_reserva_quote(reserva_id: int):
        email = _current_user_email()
        if not email:
            return jsonify({"ok": False, "message": "No autenticado."}), 401

        r = _get_reserva_by_id(reserva_id)
        if not r:
            return jsonify({"ok": False, "message": "Reserva no encontrada."}), 404

        if not _reserva_belongs_to_email(reserva_id, email):
            return jsonify({"ok": False, "message": "No autorizado."}), 403

        p = request.get_json(silent=True) or {}
        new_ci = (p.get("checkin") or _normalize_date_like(r.get("Fecha_Entrada")) or "")[:10]
        new_co = (p.get("checkout") or _normalize_date_like(r.get("Fecha_Salida")) or "")[:10]
        try:
            new_pax = int(p.get("huespedes") or r.get("Huespedes") or 1)
        except Exception:
            new_pax = 1

        if not new_ci or not new_co:
            return jsonify({"ok": False, "message": "Fechas inválidas."}), 400

        rid_room = int(r["Codigo_Habitacion"])
        nights = _nights(new_ci, new_co)
        if nights <= 0:
            return jsonify({"ok": True, "available": False, "message": "Rango de fechas no válido.", "nights": 0}), 200

        # Misma habitación sin solapes
        is_free, msg = _room_is_available_for(rid_room, new_ci, new_co, exclude_reserva_id=reserva_id)
        if not is_free:
            return jsonify({"ok": True, "available": False, "message": msg or "Sin disponibilidad."}), 200

        # Precio/capacidad del cuarto
        info = _get_room_info(rid_room)
        price_n = float(info.get("price") or 0.0)

        total_new = _calc_total(price_n, nights, new_pax, iva_rate=DEFAULT_IVA, include_tax=ROOM_TOTAL_INCLUDES_TAX)
        total_old = float(r.get("Monto_Total") or 0.0)
        delta = round(max(0.0, total_new - total_old), 2)

        return jsonify({
            "ok": True,
            "available": True,
            "nights": nights,
            "price_night": price_n,
            "current_total": total_old,
            "new_total": total_new,
            "delta": delta,
            "message": "Disponible. Puedes aplicar los cambios."
        }), 200
    
    
    
    
        
        
        # === Cotización de cambios de reserva (Portal) — sin ORM, robusto ===
    @app.post("/api/portal/reservas/<int:reserva_id>/quote-changes", endpoint="portal_reserva_quote_changes")
    def portal_reserva_quote_changes(reserva_id: int):
        """
        Calcula la cotización de cambios para una reserva existente y devuelve:
          - available / allowed
          - nights, price_night
          - current_total, new_total, delta (solo positivo para cobro)
          - needs_payment
          - pricing {...} (para UIs que esperan este bloque)
        """
        from decimal import Decimal
        try:
            email = _current_user_email()
            if not email:
                return jsonify(ok=False, message="No autenticado."), 401
            if not _reserva_belongs_to_email(reserva_id, email):
                return jsonify(ok=False, message="No autorizado."), 403
    
            # Carga robusta de la reserva (sin ORM)
            r = _get_reserva_by_id(reserva_id)
            if not r:
                return jsonify(ok=False, message="Reserva no encontrada."), 404
    
            # Fechas/pax propuestos (o valores actuales)
            body = request.get_json(silent=True) or {}
            ci_new = _normalize_date_like(body.get("checkin")  or _get_first_attr(r, ["Fecha_Entrada","checkin","entrada"]))
            co_new = _normalize_date_like(body.get("checkout") or _get_first_attr(r, ["Fecha_Salida","checkout","salida"]))
            if not ci_new or not co_new:
                return jsonify(ok=False, message="Fechas inválidas."), 400
            if co_new <= ci_new:
                return jsonify(ok=False, message="El checkout debe ser posterior al check-in."), 400
    
            pax_old = int(_get_first_attr(r, ["Huespedes","huespedes"]) or 1)
            pax_new = int(body.get("huespedes") or pax_old)
    
            # Identificación de habitación
            room_id = _get_first_attr(r, [
                "Codigo_Habitacion","codigo_habitacion","Habitacion","habitacion_id","habitacion"
            ])
            if not room_id:
                return jsonify(ok=False, message="La reserva no tiene habitación asociada."), 400
    
            # Info de habitación (precio, capacidad…)
            info = _habitacion_info(room_id) or {}
            # Asegura Decimal
            price = info.get("price")
            try:
                price = Decimal(str(price)) if price is not None else Decimal("0")
            except Exception:
                price = Decimal("0")
            capacity = info.get("capacity")  # puede ser None
    
            # Noches actual/nuevo
            ci_old = _normalize_date_like(_get_first_attr(r, ["Fecha_Entrada","checkin","entrada"]))
            co_old = _normalize_date_like(_get_first_attr(r, ["Fecha_Salida","checkout","salida"]))
            nights_old = _nights(ci_old, co_old) or 0
            nights_new = _nights(ci_new, co_new) or 0
    
            # Totales
            current_total = _calc_total(price, nights_old, pax_old)
            new_total     = _calc_total(price, nights_new, pax_new)
    
            # Disponibilidad de la misma habitación (excluye esta reserva)
            overlap = db.session.execute(text("""
                SELECT COUNT(1) AS c
                FROM Reserva
                WHERE Codigo_Habitacion = :room
                  AND (Estado IS NULL OR Estado <> 'Cancelada')
                  AND Codigo_Reserva <> :rid
                  AND NOT ( :co <= Fecha_Entrada OR :ci >= Fecha_Salida )
            """), {"room": room_id, "rid": reserva_id, "ci": ci_new, "co": co_new}).scalar() or 0
            room_available = (overlap == 0)
    
            # Capacidad (si la tabla no tiene columna, capacity será None => se omite la validación)
            capacity_ok = True if capacity is None else (int(pax_new) <= int(capacity))
    
            allowed = room_available and capacity_ok
    
            # Diferencial: solo positivo requiere pago
            delta = new_total - current_total
            delta_payable = delta if delta > 0 else Decimal("0")
            needs_payment = (delta_payable > 0)
    
            # Mensaje amigable
            if not capacity_ok:
                msg = "Capacidad insuficiente para la cantidad de huéspedes."
            elif not room_available:
                msg = "La habitación asignada no está disponible para el nuevo rango."
            else:
                msg = "Disponible. Puedes aplicar los cambios."
    
            return jsonify({
                "ok": True,
                "available": room_available,
                "allowed": allowed,
                "room_available": room_available,
                "capacity_ok": capacity_ok,
                "message": msg,
                "nights": nights_new,
                "price_night": float(price),
                "current_total": float(current_total),
                "new_total": float(new_total),
                "delta": float(delta_payable),
                "needs_payment": bool(needs_payment),
                # Bloque adicional para UIs que esperan estructura 'pricing'
                "pricing": {
                    "nights_old": nights_old,
                    "nights_new": nights_new,
                    "price_night": float(price),
                    "total_old": float(current_total),
                    "total_new": float(new_total),
                    "delta": float(delta_payable),
                }
            }), 200
    
        except Exception as e:
            app.logger.error(f"[QUOTE] Error cotizando reserva {reserva_id}: {e}", exc_info=True)
            # Respondemos 200 con ok=false para que la UI muestre el mensaje sin romper
            return jsonify(ok=False, message="No se pudo validar la cotización."), 200
    
    


    @app.put("/api/portal/reservas/<int:reserva_id>/apply-changes")
    @role_required("Cliente")
    def api_portal_reserva_apply(reserva_id: int):
        email = _current_user_email()
        if not email:
            return jsonify({"ok": False, "message": "No autenticado."}), 401

        r = _get_reserva_by_id(reserva_id)
        if not r:
            return jsonify({"ok": False, "message": "Reserva no encontrada."}), 404

        if not _reserva_belongs_to_email(reserva_id, email):
            return jsonify({"ok": False, "message": "No autorizado."}), 403

        p = request.get_json(silent=True) or {}
        new_ci = (p.get("checkin") or _normalize_date_like(r.get("Fecha_Entrada")) or "")[:10]
        new_co = (p.get("checkout") or _normalize_date_like(r.get("Fecha_Salida")) or "")[:10]
        obs    = (p.get("observaciones") or r.get("Observaciones") or "").strip()
        try:
            new_pax = int(p.get("huespedes") or r.get("Huespedes") or 1)
        except Exception:
            new_pax = 1

        if not new_ci or not new_co:
            return jsonify({"ok": False, "message": "Fechas inválidas."}), 400

        nights = _nights(new_ci, new_co)
        if nights <= 0:
            return jsonify({"ok": False, "message": "Rango de fechas no válido."}), 400

        room_id = int(r["Codigo_Habitacion"])
        ok_free, msg = _room_is_available_for(room_id, new_ci, new_co, exclude_reserva_id=reserva_id)
        if not ok_free:
            return jsonify({"ok": False, "message": msg or "Sin disponibilidad para ese rango."}), 409

        # Recalcular monto nuevo
        info = _get_room_info(room_id)
        price_n = float(info.get("price") or 0.0)
        total_new = _calc_total(price_n, nights, new_pax, iva_rate=DEFAULT_IVA, include_tax=ROOM_TOTAL_INCLUDES_TAX)
        total_old = float(r.get("Monto_Total") or 0.0)
        delta = round(max(0.0, total_new - total_old), 2)

        # Si hay adicional y no vino pago -> avisar
        payment = p.get("payment")
        if delta > 0 and not payment:
            return jsonify({"ok": False, "error": "payment_required", "amount_due": delta, "message": "Se requiere pago adicional."}), 402

        # Simular registro de pago (solo auditar últimos 4 / marca)
        if delta > 0 and payment:
            try:
                holder = (payment.get("holder") or "").strip()
                last4  = (payment.get("card_last4") or "").strip()
                brand  = (payment.get("card_brand") or "").strip() or None
                if not holder or not last4 or not last4.isdigit() or len(last4) != 4:
                    return jsonify({"ok": False, "message": "Datos de pago incompletos."}), 400

                # Nota de auditoría en Observaciones
                obs = (obs + f" | PAGO Δ: ₡{delta:.2f} ({brand or 'Tarjeta'}) ****{last4}").strip()

                # (Opcional) crear PDF de recibo por diferencia
                try:
                    _create_recibo_pdf(dict(r), delta, pay_method=brand or "tarjeta", reference=f"****{last4}")
                except Exception:
                    pass
            except Exception as e:
                current_app.logger.warning(f"[PAY] Error parseando pago: {e}")
                return jsonify({"ok": False, "message": "Error al validar el pago."}), 400

        # Persistir cambios en Reserva
        try:
            db.session.execute(
                text("""
                    UPDATE Reserva
                       SET Fecha_Entrada = :ci,
                           Fecha_Salida  = :co,
                           Huespedes     = :pax,
                           Monto_Total   = :monto,
                           Observaciones = :obs,
                           Fecha_Registro = Fecha_Registro  -- conservación
                     WHERE Codigo_Reserva = :rid
                """),
                {"ci": new_ci, "co": new_co, "pax": int(new_pax), "monto": float(total_new), "obs": obs, "rid": int(reserva_id)}
            )
            db.session.commit()
        except Exception as e:
            db.session.rollback()
            current_app.logger.exception("[APPLY] Error actualizando reserva: %s", e)
            return jsonify({"ok": False, "message": "No se pudo aplicar los cambios."}), 500

        # Recargar y devolver
        r2 = _get_reserva_by_id(reserva_id)
        return jsonify({"ok": True, "item": _reserva_to_dict(r2)}), 200
    
    
    
    
    @app.post("/api/portal/reservas/<int:reserva_id>/send-confirmation")
    @role_required("Cliente")
    def api_portal_reserva_send_confirmation(reserva_id: int):
        try:
            r = _get_reserva_by_id(reserva_id)
            if not r:
                return jsonify({"ok": False, "error": "not_found"}), 404
    
            # Cliente solo puede accionar sobre su propia reserva
            email = _current_user_email()
            if not email or not _reserva_belongs_to_email(reserva_id, email):
                return jsonify({"ok": False, "error": "forbidden"}), 403
    
            estado = (r.get("Estado") or "").strip()
    
            # Paso 1: si no está confirmada, NO se puede enviar confirmación
            if estado != "Confirmada":
                # opcional: re-enviar aviso de pendiente si aplica
                if estado == "Pendiente":
                    try:
                        _notify_reserva_pending(reserva_id)
                    except Exception:
                        pass
                return jsonify({"ok": False, "error": "not_confirmed", "estado": estado}), 409
    
            _notify_reserva_success(reserva_id)
            return jsonify({"ok": True})
    
        except Exception as e:
            current_app.logger.exception(f"[SEND_CONFIRMATION] error reserva={reserva_id}: {e}")
            return jsonify({"ok": False, "error": "server_error"}), 500
    



        # ---------------------- RUTA descarga comprobante ----------------------
    @app.route("/api/reservas/<int:reserva_id>/comprobante", methods=["GET"])
    @role_required("Cliente", "Administrador", "Recepcionista")
    def api_reserva_comprobante(reserva_id: int):
        # 1) Buscar reserva (primero, para no usar "r" antes de asignarlo)
        r = _get_reserva_by_id(reserva_id)
        if not r:
            return jsonify({"ok": False, "error": "not_found"}), 404

        # 2) Validar estado (solo confirmadas deben permitir comprobante)
        estado = (r.get("Estado") or "").strip()
        if estado not in ("Confirmada", "Check-in", "Check-out"):
            return jsonify({"ok": False, "error": "not_confirmed"}), 409

        # 3) Seguridad: si es Cliente, solo puede descargar sus propias reservas
        if (_user_role() or "").lower() == "cliente":
            email = _current_user_email()
            if not _reserva_belongs_to_email(reserva_id, email or ""):
                return jsonify({"ok": False, "error": "forbidden"}), 403

        # 4) Garantizar número de comprobante
        fecha_entrada_norm = _normalize_date_like(r.get("Fecha_Entrada")) or ""
        numero = r.get("Numero") or _ensure_reserva_numero(reserva_id, fecha_entrada_norm)

        # 5) Generar PDF si no existe
        pdf_path = COMPROBANTES_DIR / f"{numero}.pdf"
        if not pdf_path.exists():
            r2 = dict(r)
            r2["Numero"] = numero
            _create_comprobante_pdf(r2)

        if not pdf_path.exists():
            return jsonify({"ok": False, "error": "not_available"}), 404

        return send_file(
            str(pdf_path),
            as_attachment=True,
            download_name=f"{numero}.pdf",
            mimetype="application/pdf",
        )

    

    
    
    @app.post("/api/portal/reservas/abono")
    def api_portal_reserva_abono():
        # TODO: validar sesión/rol + ownership de la reserva
        reserva_id = request.form.get("reserva_id", type=int)
        monto = request.form.get("monto", type=float)
        metodo = request.form.get("metodo", type=str)
        referencia = request.form.get("referencia", type=str)
        created_by = session.get("user_id", 1)
    
        if not reserva_id or not monto or monto <= 0:
            return jsonify({"ok": False, "error": "Datos inválidos"}), 400
    
        # TODO: aquí conectás con tu lógica real de finanzas (fin_receipts / fin_ledger_tx)
        # Por ahora devolvemos estructura compatible con tu JS:
        return jsonify({
            "ok": True,
            "total_reserva": 0,
            "pagado_acumulado": 0,
            "saldo_pendiente": 0,
            "porcentaje_pagado": 0,
            "created_by": created_by
        })

    
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
        def _fetch(periodo, key_sql):
            row = db.session.execute(
                text(f"""
                    SELECT Total_Reservas, Total_Monto, Revenue_SinImpuesto, Total_Noches
                      FROM KPI_Stats
                     WHERE Periodo = :p AND Clave = {key_sql}
                     LIMIT 1
                """),
                {"p": periodo},
            ).first()
            if not row:
                return {"reservas": 0, "monto": 0.0, "adr": 0.0}
            reservas = int(row[0] or 0)
            monto    = float(row[1] or 0)
            rev      = float(row[2] or 0)
            noches   = int(row[3] or 0)
            adr = (rev / noches) if noches > 0 else 0.0
            return {"reservas": reservas, "monto": monto, "adr": round(adr, 2)}
    
        return jsonify({
            "ok": True,
            "day":   _fetch("day",   "DATE_FORMAT(CURDATE(), '%Y-%m-%d')"),
            "week":  _fetch("week",  "DATE_FORMAT(CURDATE(), '%x-W%v')"),
            "month": _fetch("month", "DATE_FORMAT(CURDATE(), '%Y-%m')"),
        })
    

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
            # Soporta tokens antiguos (str) y nuevos (dict)
            if isinstance(data, dict):
                email = (data.get("email") or "").strip().lower()
            else:
                email = (str(data) or "").strip().lower()
        except SignatureExpired:
            flash("El enlace expiró. Solicita uno nuevo.", "warning")
            return redirect(url_for("forgot_password"))
        except BadSignature:
            flash("Enlace inválido. Solicita uno nuevo.", "danger")
            return redirect(url_for("forgot_password"))
    
        user = Usuario.query.filter(func.lower(Usuario.Correo) == email).first()
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
            if session.get("user_id") == getattr(user, "Codigo_Usuario", None):
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

    @app.route("/register", methods=["GET", "POST"])
    def register():
        if request.method == "POST":
            # --- Entrada cruda ---
            nombre     = (request.form.get("nombre") or "").strip()
            apellidos  = (request.form.get("apellidos") or "").strip()
            full_name  = (request.form.get("full_name") or "").strip()  # viene del mapeo JS
            national_id_raw = (request.form.get("national_id") or request.form.get("cedula") or "").strip()
            email      = (request.form.get("email") or "").strip().lower()
            phone_raw  = (request.form.get("phone") or request.form.get("telefono") or "").strip()
            password   = (request.form.get("password") or "").strip()
            confirm    = (request.form.get("confirm_password") or "").strip()
            terms_ok   = (request.form.get("terms") is not None)
    
            # --- Validaciones servidor (seguridad/consistencia) ---
            # Nombres
            if not (_validate_name(nombre) and _validate_name(apellidos)):
                flash("Nombre y apellidos inválidos (solo letras y espacios, mínimo 2).", "warning")
                return render_template("register.html")
    
            # Full name (construir si viene vacío)
            if not full_name:
                full_name = f"{nombre} {apellidos}".strip()
    
            # Email
            if not _is_valid_email(email):
                flash("Correo electrónico inválido.", "warning")
                return render_template("register.html")
    
            # Teléfono CR normalizado a E.164
            phone_norm = _normalize_cr_phone(phone_raw)
            if not phone_norm:
                flash("Teléfono inválido. Debe ser de Costa Rica (8 dígitos; opcional +506).", "warning")
                return render_template("register.html")
    
            # Documento (opcional)
            national_id = None
            if national_id_raw:
                national_id = _normalize_document(national_id_raw)
                if not national_id:
                    flash("Cédula/DIMEX/Pasaporte inválido.", "warning")
                    return render_template("register.html")
    
            # Password
            if password != confirm:
                flash("Las contraseñas no coinciden.", "warning")
                return render_template("register.html")
            if not _is_strong_password(password, email):
                flash("La contraseña es débil. Usa mínimo 8 caracteres con mayúsculas, minúsculas y números; evita secuencias o repetidos.", "warning")
                return render_template("register.html")
    
            # Términos
            if not terms_ok:
                flash("Debes aceptar los Términos y la Política de Privacidad.", "warning")
                return render_template("register.html")
    
            # --- Unicidad ---
            if Usuario.query.filter(func.lower(Usuario.Correo) == email).first():
                flash("El correo ya está registrado.", "danger")
                return render_template("register.html")
    
            if national_id:
                # normalizamos comparación: Cedula_Pasaporte puede almacenar dígitos o alfanum.
                if Usuario.query.filter_by(Cedula_Pasaporte=national_id).first():
                    flash("La cédula/pasaporte ya está registrada.", "danger")
                    return render_template("register.html")
    
            # --- Rol cliente ---
            role = _get_role_by_name("Cliente")
            if not role:
                _ensure_seed_roles()
                role = _get_role_by_name("Cliente")
    
            # --- Crear usuario ---
            u = Usuario(
                Nombre=full_name[:80],
                Cedula_Pasaporte=national_id,
                Correo=email,
                Telefono=phone_norm,  # guardamos en E.164
                Rol_Id=role.Codigo_Rol if role else None,
                Estado="Activo",
            )
            u.set_password(password)
            db.session.add(u)
    
            try:
                # 1) Asegurar Cliente con documento
                cliente_id = ensure_cliente_for_email(full_name, email, phone_norm, doc_num=national_id)
                if cliente_id:
                    u.Codigo_Cliente = cliente_id
                
                # 2) Commit único (Usuario + Cliente) = consistencia total
                db.session.commit()
                
            except IntegrityError:
                db.session.rollback()
                flash("Ya existe un usuario con ese correo o cédula/pasaporte.", "danger")
                return render_template("register.html")
            except Exception as e:
                db.session.rollback()
                current_app.logger.exception(f"[REGISTER] Error creando usuario: {e}")
                flash("No se pudo completar el registro. Inténtalo de nuevo.", "danger")
                return render_template("register.html")
    
            flash("Registro exitoso. Ya puedes iniciar sesión.", "success")
            return redirect(url_for("login_html"))
    
        return render_template("register.html")
    

    @app.route("/register.html", methods=["GET", "POST"])
    def register_html():
        return register()
    
    
        # ---------------------- Perfil del usuario (API) ----------------------
    @app.get("/api/me")
    @login_required
    def api_me():
        uid = session.get("user_id")
        u = Usuario.query.filter_by(Codigo_Usuario=uid).first()
        if not u:
            return jsonify({"ok": False, "msg": "not_found"}), 404

        # Nombre, Correo, Teléfono y Rol (como espera portal-perfil.html)
        data = {
            "Nombre":   getattr(u, "Nombre", "") or "",
            "Correo":   getattr(u, "Correo", "") or "",
            "Telefono": getattr(u, "Telefono", "") if hasattr(u, "Telefono") else "",
            "Rol":      _get_role_name(u) or "Cliente",
            "Cedula":   ""  # relleno abajo
        }
        
        # Resolver documento preferentemente desde Usuario.Cedula_Pasaporte, si existe.
        try:
            doc_val = None
            if _col_exists("Usuario", "Cedula_Pasaporte") and getattr(u, "Cedula_Pasaporte", None):
                doc_val = u.Cedula_Pasaporte
            elif getattr(u, "Codigo_Cliente", None):
                drow = db.session.execute(
                    text("SELECT Cedula FROM Cliente WHERE Codigo_Cliente=:cid LIMIT 1"),
                    {"cid": u.Codigo_Cliente}
                ).first()
                if drow and drow[0] is not None:
                    doc_val = str(drow[0])
            if doc_val:
                data["Cedula"] = str(doc_val)
        except Exception:
            pass
        
        return jsonify({"ok": True, "data": data})


    @app.put("/api/me")
    @login_required
    def api_me_update():
        p = request.get_json(silent=True) or {}
        nombre   = (p.get("Nombre") or "").strip()
        correo   = (p.get("Correo") or "").strip().lower()
        telefono = (p.get("Telefono") or "").strip()
        cedula   = (p.get("Cedula") or "").strip()
    
        if not nombre or not correo:
            return jsonify({"ok": False, "msg": "Nombre y correo son obligatorios."}), 400
    
        uid = session.get("user_id")
        u = Usuario.query.filter_by(Codigo_Usuario=uid).first()
        if not u:
            return jsonify({"ok": False, "msg": "not_found"}), 404
    
        # Correo único (case-insensitive) excluyendo mi propio id
        dupe = (
            Usuario.query
            .filter(func.lower(Usuario.Correo) == correo, Usuario.Codigo_Usuario != uid)
            .first()
        )
        if dupe:
            return jsonify({"ok": False, "msg": "Ese correo ya está en uso."}), 409
    
        # === Validación de documento (opcional pero con formato sensato) ===
        # Acepta: 1-2345-6789 | 9–12 dígitos | pasaporte alfanumérico 6–20
        if cedula:
            re_doc = re.compile(r"^(?:(?:[1-9]-\d{4}-\d{4})|\d{9,12}|[A-Za-z0-9]{6,20})$")
            if not re_doc.match(cedula):
                return jsonify({"ok": False, "msg": "Documento inválido. Usa 1-2345-6789, 9–12 dígitos o pasaporte (6–20)."}), 400
    
            # Unicidad en Usuario.Cedula_Pasaporte (si existe la columna)
            try:
                if _col_exists("Usuario", "Cedula_Pasaporte"):
                    dupe_doc = (
                        Usuario.query
                        .filter(func.lower(Usuario.Cedula_Pasaporte) == cedula.lower(),
                                Usuario.Codigo_Usuario != uid)
                        .first()
                    )
                    if dupe_doc:
                        return jsonify({"ok": False, "msg": "Ese número de documento ya está en uso."}), 409
            except Exception:
                pass
    
        try:
            # === Actualizar Usuario ===
            u.Nombre = nombre
            u.Correo = correo
            try:
                if _col_exists("Usuario", "Telefono"):
                    u.Telefono = telefono or getattr(u, "Telefono", None)
            except Exception:
                pass
    
            # Guardar cédula/pasaporte si la columna existe
            try:
                if _col_exists("Usuario", "Cedula_Pasaporte"):
                    u.Cedula_Pasaporte = cedula or getattr(u, "Cedula_Pasaporte", None)
            except Exception:
                pass
    
            # === Sincronizar datos básicos en Cliente (si está vinculado) ===
            try:
                if getattr(u, "Codigo_Cliente", None):
                    # separar nombre y apellido de "Nombre completo"
                    partes = nombre.split(" ", 1)
                    nom = partes[0][:50]
                    ape = (partes[1] if len(partes) > 1 else "").strip()[:50]
    
                    # Para Cliente.Cedula solo almacenamos dígitos (0 si no hay)
                    ced_digits = _try_int_digits(cedula) if cedula else None
    
                    params = {"n": nom, "a": ape, "t": (telefono or None), "e": correo, "cid": u.Codigo_Cliente}
                    set_ced = ""
                    if ced_digits is not None:
                        set_ced = ", Cedula = :ced"
                        params["ced"] = ced_digits
    
                    db.session.execute(
                        text(f"""
                            UPDATE Cliente
                               SET Nombre   = :n,
                                   Apellido = :a,
                                   Telefono = COALESCE(:t, Telefono),
                                   Correo   = :e
                                   {set_ced}
                             WHERE Codigo_Cliente = :cid
                        """),
                        params
                    )
            except Exception:
                # no impedir guardado del perfil por esta sincronización
                pass
    
            db.session.commit()
            # refrescar nombre en sesión
            session["user_name"] = u.Nombre
            return jsonify({"ok": True})
    
        except Exception as e:
            db.session.rollback()
            current_app.logger.exception(f"[api/me] update failed: {e}")
            return jsonify({"ok": False, "msg": "No se pudo actualizar el perfil."}), 500
    

    # === Perfil: cambio de contraseña ===
    @app.route("/api/me/password", methods=["PUT", "POST"])
    @login_required
    def api_me_password():
        try:
            uid = session.get("user_id")
            if not uid:
                return jsonify({"ok": False, "msg": "No autenticado."}), 401
    
            u = Usuario.query.filter_by(Codigo_Usuario=uid).first()
            if not u:
                return jsonify({"ok": False, "msg": "Usuario no encontrado."}), 404
    
            p = request.get_json(silent=True) or {}
    
            # Acepta los nombres que manda portal-perfil.html
            current = (p.get("current") or p.get("old") or p.get("password") or "").strip()
            new_pw  = (p.get("new")     or p.get("new_password") or p.get("password_new") or "").strip()
            confirm = (p.get("confirm") or p.get("password_confirm") or "").strip()
    
            # Validaciones
            if not current or not new_pw or not confirm:
                return jsonify({"ok": False, "msg": "Completa todos los campos."}), 400
            if len(new_pw) < 8:
                return jsonify({"ok": False, "msg": "La nueva contraseña debe tener al menos 8 caracteres."}), 400
            if new_pw != confirm:
                return jsonify({"ok": False, "msg": "La confirmación no coincide."}), 400
            if new_pw == current:
                return jsonify({"ok": False, "msg": "La nueva contraseña no puede ser igual a la actual."}), 400
    
            # Verificación de la contraseña actual (compatibilidad con hash o texto plano legado)
            stored = getattr(u, "Contrasena", None)
            is_ok = False
            if stored:
                try:
                    is_ok = check_password_hash(stored, current)
                except Exception:
                    # Si lo guardado no es un hash (instalaciones viejas), compara directo
                    is_ok = (stored == current)
            else:
                # Si por diseño existían usuarios sin contraseña previa, permite setear la primera
                is_ok = True
    
            if not is_ok:
                return jsonify({"ok": False, "msg": "La contraseña actual no es válida."}), 400
    
            # Hash de la nueva contraseña y persistencia
            try:
                new_hash = generate_password_hash(new_pw)
            except Exception:
                # Fallback improbable: guarda en claro (no recomendado), pero evita NULL
                new_hash = new_pw
    
            u.Contrasena = new_hash
            if hasattr(u, "Fecha_Modificacion"):
                u.Fecha_Modificacion = datetime.utcnow()
    
            db.session.commit()
            return jsonify({"ok": True, "msg": "Contraseña actualizada."})
        except IntegrityError:
            db.session.rollback()
            return jsonify({"ok": False, "msg": "No se pudo cambiar la contraseña."}), 500
        except Exception as e:
            db.session.rollback()
            current_app.logger.exception("[api/me/password] error: %s", e)
            return jsonify({"ok": False, "msg": "No se pudo cambiar la contraseña."}), 500
    

    

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
        key_activated = db.Column(db.Boolean, nullable=False, server_default=text("0"))
        created_at = db.Column(db.DateTime, nullable=False, server_default=db.func.now())
        meta = db.Column(db.JSON)

    class KeyActivation(db.Model):
        __tablename__ = "key_activation"
        id = db.Column(db.Integer, primary_key=True)
        reserva_id = db.Column(db.Integer, nullable=False, index=True)
        habitacion_id = db.Column(db.Integer, index=True)
        cliente_id = db.Column(db.Integer, index=True)
        activated_at = db.Column(db.DateTime, nullable=False, server_default=db.func.now())
        status = db.Column(db.Enum("activated", "failed", name="key_activation_status"), server_default="activated")
        meta = db.Column(db.JSON)

    def allowed_guest_file(filename: str) -> bool:
        return "." in (filename or "") and filename.rsplit(".", 1)[1].lower() in ALLOWED_DOC_EXTS

    def _normalize_docnum(v: str) -> str:
        return (v or "").strip()

    # Opción B: si la tabla key_activation aún no existe, no intentamos escribir y no fallamos.
    def _activate_e_key(habitacion_id: Optional[int], reserva_id: int, cliente_id: Optional[int]) -> None:
        """
        Stub de activación de llave electrónica (opción B):
        - Si la tabla no existe, se omite silenciosamente.
        - Usa timestamps del servidor (server_default NOW()) para evitar warnings UTC.
        """
        try:
            if not _tabla_existe("key_activation"):
                current_app.logger.info("[KEY] Tabla key_activation no existe; omitiendo registro de activación.")
                return

            ka = KeyActivation(
                reserva_id=reserva_id,
                habitacion_id=int(habitacion_id) if habitacion_id else None,
                cliente_id=int(cliente_id) if cliente_id else None,
                status="activated",
                meta={"provider": "stub", "note": "Simulación de activación"},
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
            current_app.logger.info(f"[KEY] Activación omitida/failed: {e}")
            db.session.rollback()

    def _reservas_by_doc_when(doc_number: str, when_iso: Optional[str] = None):
        """
        Devuelve reservas cuya Cedula (Cliente) o Cedula_Pasaporte (Usuario vinculado)
        coincide con 'doc_number' y donde 'when_iso' está entre [Entrada, Salida).
        Si when_iso es None, usa HOY.
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
            WHERE DATE(:w) >= DATE(R.Fecha_Entrada)
              AND DATE(:w) <  DATE(R.Fecha_Salida)
              AND R.Estado IN ('Confirmada', 'Pendiente')
              AND ( C.Cedula = :doc OR U.Cedula_Pasaporte = :doc )
            ORDER BY R.Fecha_Entrada ASC, R.Codigo_Reserva ASC
        """), {"doc": doc, "w": when}).mappings().all()
    
        return [dict(r) for r in rows]
    

    # UI de recepción (simple)
    @app.route("/ops-checkin.html", methods=["GET"])
    @role_required("Administrador", "Recepcionista")
    def ops_checkin_html():
        return render_template("ops-checkin.html")

    # UI de Check-out (opcional, para navegación del front)
    @app.route("/ops-checkout.html", methods=["GET"])
    @role_required("Administrador", "Recepcionista")
    def ops_checkout_html():
        return render_template("ops-checkout.html")

    # ------------ ENDPOINTS COMPATIBLES CON EL FRONT (ops/*) ----------------


    @lru_cache(maxsize=1)
    def _cliente_tiene_columna(col: str) -> bool:
        try:
            row = db.session.execute(_text("""
                SELECT 1
                  FROM information_schema.COLUMNS
                 WHERE TABLE_SCHEMA = DATABASE()
                   AND TABLE_NAME   = 'Cliente'
                   AND COLUMN_NAME  = :c
                 LIMIT 1
            """), {"c": col}).first()
            return bool(row)
        except Exception:
            return False
    
    def _precio_noche_py(h) -> float:
        for c in ("Precio_Noche", "Precio_Base", "Precio", "Tarifa_Base"):
            if hasattr(h, c):
                try:
                    v = getattr(h, c)
                    if v is not None:
                        return float(v or 0)
                except Exception:
                    pass
        return 0.0

    @app.get("/api/ops/checkin/search")
    @role_required("Administrador", "Recepcionista")
    def api_ops_checkin_search():
        doc  = (request.args.get("doc") or "").strip()
        when = (request.args.get("when") or "").strip()
        if not doc:
            return jsonify({"ok": False, "items": [], "msg": "doc requerido"}), 400
    
        when_date = None
        if when:
            try:
                when_date = date.fromisoformat(when[:10])
            except Exception:
                return jsonify({"ok": False, "items": [], "msg": "when inválido (YYYY-MM-DD)"}), 400
    
        # --- CONDICIONES dinámicas según columnas reales ---
        conds = [
            "REPLACE(C.Cedula,'-','') = REPLACE(:doc,'-','')",
            "LOWER(C.Correo) = LOWER(:doc)"
        ]
        if _cliente_tiene_columna("Pasaporte"):
            conds.append("COALESCE(C.Pasaporte,'') = :doc")
    
        where_block = " OR ".join(conds)
    
        sql = f"""
            SELECT
                R.Codigo_Reserva        AS id,
                R.Fecha_Entrada         AS checkin,
                R.Fecha_Salida          AS checkout,
                R.Estado                AS estado,
                R.Codigo_Habitacion     AS Codigo_Habitacion,
                C.Nombre                AS c_nombre,
                COALESCE(C.Apellido,'') AS c_apellido,
                H.Numero_Habitacion     AS Numero_Habitacion
            FROM Reserva R
            JOIN Cliente C         ON C.Codigo_Cliente = R.Codigo_Cliente
            LEFT JOIN Habitacion H ON H.Codigo_Habitacion = R.Codigo_Habitacion
            WHERE ({where_block})
        """
    
        params = {"doc": doc}
    
        # Fecha opcional: si viene, filtra por rango conteniendo 'when'; si no, lista todas.
        if when_date:
            sql += " AND R.Fecha_Entrada <= :d AND R.Fecha_Salida > :d"
            params["d"] = when_date
    
        sql += " ORDER BY R.Fecha_Entrada DESC LIMIT 200"
    
        rows = db.session.execute(_text(sql), params).mappings().all()
    
        items = []
        for r in rows:
            room_block = None
            h = None
            try:
                if r["Codigo_Habitacion"]:
                    h = Habitacion.query.get(int(r["Codigo_Habitacion"]))
            except Exception:
                h = None
    
            if h:
                room_block = {
                    "code": int(getattr(h, "Codigo_Habitacion")),
                    "number": getattr(h, "Numero_Habitacion", None),
                    "type": getattr(h, "Tipo", None) or "",
                    "capacity": int(getattr(h, "Capacidad", None) or 0),
                    "price": _precio_noche_py(h),
                    "desc": getattr(h, "Descripcion", None) or "",
                    "img": getattr(h, "Imagen_URL", None) or "",
                }
    
            items.append({
                "id": int(r["id"]),
                "numero": f"VG-{int(r['id'])}",
                "huesped": (f"{r['c_nombre']} {r['c_apellido']}".strip() or "—"),
                "checkin": r["checkin"].isoformat() if r["checkin"] else None,
                "checkout": r["checkout"].isoformat() if r["checkout"] else None,
                "estado": r["estado"],
                "habitacion": r["Numero_Habitacion"],
                "room": room_block,  # <- detalles completos para la UI
            })
    
        return jsonify({"ok": True, "items": items, "count": len(items)}), 200

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
                   R.Codigo_Habitacion, R.Numero_Comprobante,
                   R.Fecha_Entrada, R.Fecha_Salida, R.Monto_Total, H.Tipo
              FROM Reserva R
              JOIN Cliente C ON C.Codigo_Cliente = R.Codigo_Cliente
              LEFT JOIN Habitacion H ON H.Codigo_Habitacion = R.Codigo_Habitacion
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
        sign_path = None
        docimg_path = None
        if firma_b64:
            try:
                import base64
                raw = firma_b64.split(",")[-1]
                data = base64.b64decode(raw)
                fpath = GUEST_DOCS_DIR / f"firma-R{reserva_id}-{datetime.utcnow().strftime('%Y%m%d%H%M%S')}.png"
                with open(fpath, "wb") as f:
                    f.write(data)
                sign_path = f"/storage/guest_docs/{fpath.name}"
                saved_files.append(("GuestSignature", fpath))
            except Exception as e:
                current_app.logger.warning(f"[CHECKIN] No se pudo guardar firma: {e}")

        # Guardar imagen/PDF del documento (opcional)
        if docfile:
            try:
                fn = secure_filename(docfile.filename or f"doc-R{reserva_id}.bin")
                fpath = GUEST_DOCS_DIR / f"{datetime.utcnow():%Y%m%d%H%M%S}-{fn}"
                docfile.save(str(fpath))
                docimg_path = f"/storage/guest_docs/{fpath.name}"
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

        # Marcar habitación como Ocupada al hacer check-in (si aplica)
        try:
            hab_id = r.get("Codigo_Habitacion")
            if hab_id:
                db.session.execute(
                    text("UPDATE Habitacion SET Estado='Ocupada' WHERE Codigo_Habitacion=:h"),
                    {"h": hab_id}
                )
                db.session.commit()
        except Exception:
            db.session.rollback()

        # Activación de llave (opcional, Opción B)
        try:
            _activate_e_key(r.get("Codigo_Habitacion"), reserva_id, r.get("Codigo_Cliente"))
            key_activated = True
        except Exception:
            key_activated = False

        # Registrar evento GuestCheckin (para trazabilidad)
        try:
            doc_type = "pasaporte" if not documento.isdigit() else "cedula"
            gc = GuestCheckin(
                reserva_id=reserva_id,
                cliente_id=r.get("Codigo_Cliente"),
                recep_user_id=session.get("user_id"),
                doc_type=doc_type,
                doc_number=documento,
                doc_image_path=docimg_path,
                signature_path=sign_path,
                key_activated=bool(key_activated),
                meta={"ui": "ops-checkin", "numero": r.get("Numero_Comprobante")},
            )
            db.session.add(gc)
            db.session.commit()
        except Exception as e:
            current_app.logger.info(f"[CHECKIN] GuestCheckin log omitido: {e}")
            db.session.rollback()

        # Auditoría
        _audit_log(_current_user_email(), "checkin.completed",
                   {"reserva_id": reserva_id, "documento": documento}, entidad_id=str(reserva_id))

        # (opcional) asegurar comprobante disponible
        try:
            _create_comprobante_pdf({
                "Codigo_Reserva": r["Codigo_Reserva"],
                "Numero": r["Numero_Comprobante"] or _make_unique_number(reserva_id, str(r.get("Fecha_Entrada") or "")),
                "Usuario": _current_user_email(),
                "Fecha_Entrada": r.get("Fecha_Entrada"),
                "Fecha_Salida": r.get("Fecha_Salida"),
                "Monto_Total": r.get("Monto_Total"),
                "Tipo": r.get("Tipo") or "Habitación",
            })
        except Exception:
            pass

        return jsonify({"ok": True, "reserva_id": reserva_id, "comprobante_reserva_id": reserva_id})
    
    @app.get("/api/ops/walkin/rooms")
    @role_required("Administrador", "Recepcionista")
    def api_ops_walkin_rooms():
        """
        Lista habitaciones disponibles entre ci/co con filtros opcionales:
          - tipo (exacto)
          - capacidad_min
          - precio_max
        Devuelve además 'nights' para el rango.
        """
        ci = (request.args.get("ci") or "").strip()
        co = (request.args.get("co") or "").strip()
        if not ci or not co:
            return jsonify({"ok": False, "error": "dates_required"}), 400

        tipo = (request.args.get("tipo") or "").strip()
        cap_min = request.args.get("capacidad_min", type=int)
        pmax = request.args.get("precio_max", type=float)

        has_cap = _col_exists("Habitacion", "Capacidad")
        has_precio = _col_exists("Habitacion", "Precio_Noche")
        has_tipo = _col_exists("Habitacion", "Tipo")

        conditions = ["h.Estado = 'Disponible'"]
        params = {"ci": ci, "co": co}

        if tipo and has_tipo:
            conditions.append("h.Tipo = :tipo")
            params["tipo"] = tipo
        if cap_min is not None and has_cap:
            conditions.append("h.Capacidad >= :cap")
            params["cap"] = int(cap_min)
        if pmax is not None and has_precio:
            conditions.append("h.Precio_Noche <= :pmax")
            params["pmax"] = float(pmax)

        # no solapamiento con reservas existentes
        conditions.append("""
            NOT EXISTS (
                SELECT 1 FROM Reserva r
                 WHERE r.Codigo_Habitacion = h.Codigo_Habitacion
                   AND r.Estado IN ('Confirmada','Pendiente')
                   AND DATE(r.Fecha_Entrada) < DATE(:co)
                   AND DATE(r.Fecha_Salida)  > DATE(:ci)
            )
        """)

        where = " AND ".join(conditions)
        rows = db.session.execute(
            text(f"""
                SELECT
                  h.Codigo_Habitacion AS id,
                  h.Numero_Habitacion AS numero,
                  {'h.Capacidad AS capacidad,' if has_cap else 'NULL AS capacidad,'}
                  {'h.Precio_Noche AS precio,' if has_precio else '0 AS precio,'}
                  {'h.Tipo AS tipo' if has_tipo else "NULL AS tipo"},
                  h.Estado AS estado
                FROM Habitacion h
                WHERE {where}
                ORDER BY h.Numero_Habitacion
            """),
            params
        ).mappings().all()

        # noches
        try:
            from datetime import datetime as _dt
            nights = max((_dt.strptime(co, "%Y-%m-%d") - _dt.strptime(ci, "%Y-%m-%d")).days, 1)
        except Exception:
            nights = 1

        return jsonify({"ok": True, "nights": nights, "items": [dict(r) for r in rows]})

    @app.post("/api/ops/walkin/create")
    @role_required("Administrador", "Recepcionista")
    def api_ops_walkin_create():
        """
        Crea una reserva Walk-in SOLO si se indica una habitación disponible.
        Evita insertar Codigo_Habitacion = NULL (previene IntegrityError 1048).
        Permite pago inmediato opcional.
        """
        p = request.get_json(silent=True) or {}
        doc = (p.get("documento") or "").strip()
        name = (p.get("nombre") or "").strip() or "Walk-in"
        email = (p.get("email") or "").strip().lower() or None
        ci = (p.get("checkin") or "").strip()
        co = (p.get("checkout") or "").strip()
        habitacion_id = _to_int(p.get("habitacion_id")) if hasattr(p, "get") else None
        pax = _to_int(p.get("huespedes"), 1)


        if not doc or not ci or not co or not habitacion_id:
            return jsonify({"ok": False, "error": "missing_params"}), 400

        # validar no solape para esa habitación
        overlap = db.session.execute(
            text("""
                SELECT 1
                  FROM Reserva r
                 WHERE r.Codigo_Habitacion = :h
                   AND r.Estado IN ('Confirmada','Pendiente')
                   AND DATE(r.Fecha_Entrada) < DATE(:co)
                   AND DATE(r.Fecha_Salida)  > DATE(:ci)
                 LIMIT 1
            """),
            {"h": int(habitacion_id), "ci": ci, "co": co}
        ).first()
        if overlap:
            return jsonify({"ok": False, "error": "room_unavailable",
                            "message": "La habitación ya está ocupada en ese rango."}), 409

        # precio / datos de la habitación
        row_h = db.session.execute(
            text("SELECT Precio_Noche, Tipo, Numero_Habitacion FROM Habitacion WHERE Codigo_Habitacion=:h LIMIT 1"),
            {"h": int(habitacion_id)}
        ).mappings().first()
        if not row_h:
            return jsonify({"ok": False, "error": "room_not_found"}), 404

        precio_noche = float(row_h.get("Precio_Noche") or 0.0)
        from datetime import datetime as _dt
        try:
            nights = max((_dt.strptime(co, "%Y-%m-%d") - _dt.strptime(ci, "%Y-%m-%d")).days, 1)
        except Exception:
            nights = 1
        monto_total = round(precio_noche * nights, 2)

        # asegurar Cliente por documento
        cli = db.session.execute(
            text("SELECT Codigo_Cliente, Correo, Nombre, Apellido FROM Cliente WHERE CAST(Cedula AS CHAR) = :d LIMIT 1"),
            {"d": doc}
        ).mappings().first()
        if not cli:
            nom, ape = (name.split(" ",1)+[""])[:2]
            res_cli = db.session.execute(
                text("""
                    INSERT INTO Cliente (Cedula, Nombre, Apellido, Telefono, Correo, Fecha_Nacimiento)
                    VALUES (:ced,:n,:a,'',:e,'1990-01-01')
                """),
                {"ced": doc, "n": nom[:50], "a": ape[:50], "e": email}
            )
            db.session.commit()
            cliente_id = int(res_cli.lastrowid)
        else:
            cliente_id = int(cli["Codigo_Cliente"])

        # crear reserva con la habitación indicada (NUNCA NULL)
        # funcionario que crea el walk-in (NOT NULL en la tabla)
        func_id = db.session.execute(
            text("SELECT Codigo_Funcionario FROM Funcionario ORDER BY Codigo_Funcionario LIMIT 1")
        ).scalar()
        if not func_id:
            return jsonify({"ok": False, "error": "no_funcionario_config"}), 500
        res_ins = db.session.execute(
            text("""
                INSERT INTO Reserva (Codigo_Cliente, Codigo_Habitacion, Fecha_Entrada, Fecha_Salida,
                                     Estado, Canal, Monto_Total, Huespedes, Observaciones, Fecha_Registro, Monto_Pagado,
                                     Codigo_Funcionario)
                VALUES (:c, :h, :ci, :co, 'Pendiente', 'FrontDesk', :mt, :pax, 'Walk-in (sin auto-asignación)', NOW(), 0,
                        :f)
            """),
            {"c": cliente_id, "h": int(habitacion_id), "ci": ci, "co": co, "mt": monto_total, "pax": pax, "f": func_id}
        )

        db.session.commit()
        reserva_id = int(res_ins.lastrowid)

        # asignar número único
        numero = _make_unique_number(reserva_id, ci)
        try:
            db.session.execute(
                text("UPDATE Reserva SET Numero_Comprobante=:n WHERE Codigo_Reserva=:r"),
                {"n": numero, "r": reserva_id}
            )
            db.session.commit()
        except Exception:
            db.session.rollback()

                # pago inmediato (opcional y con import diferido para evitar circularidad)
        receipt = None
        pago_monto = _to_float(p.get("pago_monto"), 0.0)
        pago_metodo = (p.get("pago_metodo") or "Efectivo")[:30]

        if pago_monto and pago_monto > 0.0:
            try:
                # Import diferido (solo si el blueprint de caja expone los helpers)
                # Si no existe, capturamos ImportError y seguimos sin bloquear el flujo.
                try:
                    from blueprints.fin_cash import FinReceipt, _make_seq, _create_receipt_pdf  # type: ignore
                    fin_cash_ok = True
                except Exception as _e:
                    current_app.logger.info(f"[WALKIN] fin_cash no disponible: {_e}")
                    fin_cash_ok = False

                if fin_cash_ok:
                    rec = FinReceipt(
                        numero="PENDING",
                        reserva_id=reserva_id,
                        invoice_id=None,
                        tx_id=None,
                        metodo=pago_metodo,
                        currency="CRC",
                        monto=pago_monto,
                        emitido_por=session.get("user_id"),
                    )
                    db.session.add(rec)
                    db.session.flush()
                    try:
                        rec.numero = _make_seq("RC", rec.id_receipt)
                    except Exception:
                        # secuencia simple de respaldo
                        rec.numero = f"RC-{rec.id_receipt:06d}"
                    db.session.commit()

                    try:
                        _create_receipt_pdf(rec)
                    except Exception as _e:
                        current_app.logger.info(f"[WALKIN] No se pudo generar PDF de recibo: {_e}")

                    receipt = {"id": getattr(rec, "id_receipt", None), "numero": getattr(rec, "numero", None)}

                # Actualizar Monto_Pagado/Estado en Reserva aunque no exista fin_cash
                try:
                    db.session.execute(
                        text("""
                            UPDATE Reserva
                               SET Monto_Pagado = COALESCE(Monto_Pagado,0) + :p,
                                   Fecha_Ultimo_Pago = NOW(),
                                   Estado = CASE WHEN (COALESCE(Monto_Pagado,0) + :p) >= Monto_Total
                                                 THEN 'Confirmada' ELSE Estado END
                             WHERE Codigo_Reserva = :r
                        """),
                        {"p": pago_monto, "r": reserva_id}
                    )
                    db.session.commit()
                except Exception as _e:
                    current_app.logger.warning(f"[WALKIN] No se pudo actualizar pago en Reserva: {_e}")
                    db.session.rollback()

            except Exception as e:
                current_app.logger.warning(f"[WALKIN] Pago inmediato falló: {e}")
                db.session.rollback()
                receipt = None


        # auditoría + KPIs
        try:
            _update_kpis(monto_total, ci)
        except Exception:
            pass
        try:
            _audit_log(_current_user_email(), "walkin.created",
                       {"reserva_id": reserva_id, "documento": doc, "habitacion_id": int(habitacion_id),
                        "monto_total": float(monto_total)}, entidad_id=str(reserva_id))
        except Exception:
            pass

        return jsonify({
            "ok": True,
            "reserva_id": reserva_id,
            "numero": numero,
            "habitacion": row_h.get("Numero_Habitacion"),
            "receipt": receipt
        })

    @app.post("/ops/run-noshow")
    @role_required("Administrador", "Recepcionista")
    def ops_run_noshow():
        with app.test_client() as c:
            r = c.post(url_for("grr.run_noshow"))
            data = r.get_json() if r.is_json else {"ok": False}
        return jsonify(data), (200 if data.get("ok") else 500)

    @app.post("/ops/test-early/<int:reserva_id>")
    @role_required("Administrador", "Recepcionista")
    def ops_test_early(reserva_id: int):
        payload = {"porcentaje": request.json.get("porcentaje", None)} if request.is_json else {}
        with app.test_client() as c:
            r = c.post(url_for("grr.reservas_early_checkin", reserva_id=reserva_id), json=payload)
            data = r.get_json() if r.is_json else {"ok": False}
        return jsonify(data), (200 if data.get("ok") else 400)

    @app.post("/ops/test-late/<int:reserva_id>")
    @role_required("Administrador", "Recepcionista")
    def ops_test_late(reserva_id: int):
        payload = {"tramo": request.json.get("tramo", "tarde")} if request.is_json else {"tramo": "tarde"}
        with app.test_client() as c:
            r = c.post(url_for("grr.reservas_late_checkout", reserva_id=reserva_id), json=payload)
            data = r.get_json() if r.is_json else {"ok": False}
        return jsonify(data), (200 if data.get("ok") else 400)


    @app.post("/api/ops/walkin")
    @role_required("Administrador", "Recepcionista")
    def api_ops_walkin():
        """
        Alias legacy para compatibilidad.
        Requiere 'habitacion_id' y redirige a /api/ops/walkin/create.
        Evita crear reservas con Codigo_Habitacion = NULL.
        """
        # Acepta JSON o form-data
        if request.content_type and request.content_type.startswith("application/x-www-form-urlencoded"):
            p = request.form.to_dict(flat=True)
            for k in ("huespedes", "habitacion_id"):
                if k in p:
                    try: p[k] = int(p[k])
                    except Exception: pass
        else:
            p = request.get_json(silent=True) or {}

        if not p.get("habitacion_id"):
            return jsonify({"ok": False, "error": "habitacion_required",
                            "message": "Debes seleccionar una habitación disponible antes de crear el walk-in."}), 400

        with current_app.test_request_context("/api/ops/walkin/create", method="POST", json=p):
            return api_ops_walkin_create()


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

    # ========= GRR-01-005 — Check-out (helpers + endpoints) =========

    def _crear_orden_limpieza(habitacion_id: int, notas: str):
        """Crea orden en LimpiezaOrden y tarea en HousekeepingTask (compatibilidad)."""
        if not habitacion_id:
            return
        # LimpiezaOrden (tabla nueva)
        try:
            db.session.execute(
                text("""
                    INSERT INTO LimpiezaOrden (Codigo_Habitacion, Estado, Notas, Fecha_Creacion)
                    VALUES (:h, 'Pendiente', :n, NOW())
                """),
                {"h": habitacion_id, "n": (notas or "")[:255]}
            )
            db.session.commit()
        except Exception as e:
            current_app.logger.warning(f"[LIMPIEZA] No se pudo crear LimpiezaOrden: {e}")
            db.session.rollback()
        # HousekeepingTask (existente)
        try:
            db.session.execute(
                text("""
                    INSERT INTO HousekeepingTask (Habitacion_Id, Estado, Observaciones, Fecha_Creacion)
                    VALUES (:h, 'Pendiente', :n, NOW())
                """),
                {"h": habitacion_id, "n": (notas or "")[:255]}
            )
            db.session.commit()
        except Exception as e:
            current_app.logger.warning(f"[HK] No se pudo crear HousekeepingTask: {e}")
            db.session.rollback()

    def _liberar_habitacion_y_lanzar_limpieza(habitacion_id: Optional[int], reserva_id: int):
        """Deja la habitación Disponible y lanza orden de limpieza."""
        if not habitacion_id:
            return
        try:
            db.session.execute(
                text("UPDATE Habitacion SET Estado='Disponible' WHERE Codigo_Habitacion=:h"),
                {"h": int(habitacion_id)}
            )
            db.session.commit()
        except Exception as e:
            current_app.logger.warning(f"[ROOM] No se pudo poner Disponible: {e}")
            db.session.rollback()
        _crear_orden_limpieza(int(habitacion_id), f"Limpieza por check-out de la reserva {reserva_id}")

    def _estancias_salen_hoy_por_documento(doc: str) -> list[dict]:
        """Estancias que finalizan HOY y pertenecen al documento indicado."""
        if not doc:
            return []
        rows = db.session.execute(text("""
            SELECT
                re.Id_Estancia        AS estancia_id,
                re.Codigo_Reserva     AS reserva_id,
                re.Habitacion_Id      AS habitacion_id,
                re.Fecha_Desde        AS desde,
                re.Fecha_Hasta        AS hasta,
                r.Numero_Comprobante  AS numero,
                r.Monto_Total         AS monto_total,
                h.Numero_Habitacion   AS hab_num,
                c.Correo              AS cliente_email
            FROM ReservaEstancia re
            JOIN Reserva r   ON r.Codigo_Reserva = re.Codigo_Reserva
            JOIN Habitacion h ON h.Codigo_Habitacion = re.Habitacion_Id
            JOIN Cliente c    ON c.Codigo_Cliente = r.Codigo_Cliente
            LEFT JOIN Usuario u ON u.Codigo_Cliente = c.Codigo_Cliente
            WHERE DATE(re.Fecha_Hasta) = CURDATE()
              AND (c.Cedula = :doc OR u.Cedula_Pasaporte = :doc)
              AND re.Estado IN ('Pendiente','Asignada')
            ORDER BY re.Fecha_Hasta ASC, re.Id_Estancia ASC
        """), {"doc": doc}).mappings().all()
        return [{
            "estancia_id": int(r["estancia_id"]),
            "reserva_id": int(r["reserva_id"]),
            "habitacion_id": int(r["habitacion_id"]),
            "habitacion": r["hab_num"],
            "desde": str(r["desde"]),
            "hasta": str(r["hasta"]),
            "numero": r["numero"],
            "monto_total_reserva": float(r["monto_total"] or 0.0),
            "cliente": r["cliente_email"],
        } for r in rows]

    def _sum_pos_consumos(reserva_id: int) -> float:
        """Suma neta de consumos desde el libro mayor POS.
           Si las tablas no existen aún, devuelve 0.0 silenciosamente."""
        try:
            row = db.session.execute(
                text("""
                    SELECT COALESCE(SUM(l.debit),0) AS d, COALESCE(SUM(l.credit),0) AS c
                      FROM fin_ledger_tx t
                      JOIN fin_ledger_line l ON l.id_tx = t.id_tx
                     WHERE t.reserva_id = :rid AND t.status = 'posted'
                """),
                {"rid": reserva_id}
            ).first()
            if not row:
                return 0.0
            d = float(row[0] or 0.0)
            c = float(row[1] or 0.0)
            return max(d - c, 0.0)
        except Exception:
            return 0.0

    def _checkout_breakdown(reserva_id: int, estancia_id: Optional[int] = None) -> Optional[dict]:
        """Retorna el desglose para check-out. Si se da estancia_id, prorratea por noches."""
        r = _get_reserva_by_id(reserva_id)
        if not r:
            return None

        # Totales de la reserva
        fecha_entrada = r.get("Fecha_Entrada")
        fecha_salida = r.get("Fecha_Salida")
        try:
            nights_res = max((fecha_salida - fecha_entrada).days, 1) if (fecha_entrada and fecha_salida) else 1
        except Exception:
            nights_res = 1
        monto_res = float(r.get("Monto_Total") or 0.0)

        room_total = monto_res
        habitacion_id = r.get("Codigo_Habitacion")

        # Prorrateo por estancia (opcional)
        if estancia_id:
            est = db.session.execute(text("""
                SELECT Habitacion_Id, Fecha_Desde, Fecha_Hasta
                  FROM ReservaEstancia
                 WHERE Id_Estancia=:e AND Codigo_Reserva=:r
                 LIMIT 1
            """), {"e": int(estancia_id), "r": int(reserva_id)}).mappings().first()
            if est:
                try:
                    nights_est = max((est["Fecha_Hasta"] - est["Fecha_Desde"]).days, 1)
                except Exception:
                    nights_est = 1
                room_total = round(monto_res * (nights_est / nights_res), 2)
                habitacion_id = est["Habitacion_Id"]

        consumos = _sum_pos_consumos(reserva_id)
        iva_rate = float(current_app.config.get("IVA_RATE", DEFAULT_IVA))

        imp_consumos = round(consumos * iva_rate, 2) if APPLY_TAX_ON_CONSUMOS else 0.0
        imp_room = 0.0 if ROOM_TOTAL_INCLUDES_TAX else round(room_total * iva_rate, 2)

        subtotal = room_total + consumos
        impuestos = imp_consumos + imp_room
        total = subtotal + impuestos

        try:
            pagado = db.session.execute(
                text("SELECT COALESCE(Monto_Pagado,0) FROM Reserva WHERE Codigo_Reserva=:r"),
                {"r": reserva_id}
            ).scalar() or 0.0
        except Exception:
            pagado = 0.0

        saldo = round(total - float(pagado), 2)

        return {
            "reserva_id": reserva_id,
            "estancia_id": estancia_id,
            "room_total": round(room_total, 2),
            "consumos": round(consumos, 2),
            "impuestos": round(impuestos, 2),
            "total": round(total, 2),
            "pagado": round(float(pagado), 2),
            "saldo": saldo,
            "checkin": _normalize_date_like(r.get("Fecha_Entrada")),
            "checkout": _normalize_date_like(r.get("Fecha_Salida")),
            "estado": r.get("Estado"),
            "habitacion_id": habitacion_id,
            "cliente_id": r.get("Codigo_Cliente"),
            "usuario": r.get("Usuario"),
        }

    # ================== GRR-01-005 — Check-out ==================

    @app.get("/api/ops/checkout/search")
    @role_required("Administrador", "Recepcionista")
    def api_ops_checkout_search():
        """Buscar estancias que salen HOY por documento (cédula/pasaporte)."""
        doc = _normalize_docnum(request.args.get("doc"))
        if not doc:
            return jsonify({"ok": False, "error": "doc_required"}), 400
        items = _estancias_salen_hoy_por_documento(doc)
        return jsonify({"ok": True, "items": items})

    @app.get("/api/ops/checkout/preview")
    @role_required("Administrador", "Recepcionista")
    def api_ops_checkout_preview():
        """Previa de cargos de una estancia/reserva antes de confirmar el check-out."""
        estancia_id = request.args.get("estancia_id", type=int)
        reserva_id = request.args.get("reserva_id", type=int)

        if not reserva_id and estancia_id:
            reserva_id = db.session.execute(
                text("SELECT Codigo_Reserva FROM ReservaEstancia WHERE Id_Estancia=:e"),
                {"e": estancia_id}
            ).scalar()

        if not reserva_id:
            return jsonify({"ok": False, "error": "reserva_or_estancia_required"}), 400

        bd = _checkout_breakdown(int(reserva_id), estancia_id=estancia_id)
        if not bd:
            return jsonify({"ok": False, "error": "not_found"}), 404
        return jsonify({"ok": True, "breakdown": bd})

    # ====== FAC-07-002: Libro Mayor / POS ======
    class FinLedgerTx(db.Model):
        __tablename__ = "fin_ledger_tx"
        id_tx = db.Column(db.Integer, primary_key=True)
        external_id = db.Column(db.String(64), unique=True, nullable=False)
        source = db.Column(db.Enum("POS", "BACKOFFICE", name="fin_ledger_source"), default="POS", nullable=False)
        reserva_id = db.Column(db.Integer)
        currency = db.Column(db.String(10), default="CRC", nullable=False)
        total = db.Column(db.Numeric(14, 2), default=0, nullable=False)
        status = db.Column(db.Enum("posted", "voided", name="fin_ledger_status"), default="posted", nullable=False)
        created_at = db.Column(db.DateTime, nullable=False, server_default=db.func.now())
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
        creado_en = db.Column(db.DateTime, nullable=False, server_default=db.func.now())
        estado = db.Column(db.Enum("Emitido", "Anulado", name="fin_receipt_status"), default="Emitido", nullable=False)

    class FinNote(db.Model):
        __tablename__ = "fin_notes"
        id_note = db.Column(db.Integer, primary_key=True)
        numero = db.Column(db.String(40), unique=True, nullable=False)
        tipo = db.Column(db.Enum("Credito", "Debito", name="fin_note_type"), nullable=False)
        ref_invoice = db.Column(db.Integer)
        ref_reserva = db.Column(db.Integer)
        currency = db.Column(db.String(10), default="CRC", nullable=False)
        monto_abs = db.Column(db.Numeric(14, 2), nullable=False)
        motivo = db.Column(db.String(255))
        emitido_por = db.Column(db.Integer, nullable=False)
        creado_en = db.Column(db.DateTime, nullable=False, server_default=db.func.now())
        estado = db.Column(db.Enum("Emitida", "Anulada", name="fin_note_status"), default="Emitida", nullable=False)

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
    
    @app.get("/ops/coupons")
    def view_ops_coupons():
        return render_template("ops-coupons.html")
    
    @app.get("/ops/no-show")
    def view_ops_noshow():
        return render_template("ops-no-show.html")


    # ================== Confirmación de Check-out ==================
    @app.post("/api/ops/checkout")
    @role_required("Administrador", "Recepcionista")
    def api_ops_checkout_confirm():
        """
        Confirma el check-out:
          - Opcionalmente registra un pago final (fin_receipts).
          - Marca la/las estancias como 'Liberada'.
          - Deja la habitación en 'Disponible' y crea LimpiezaOrden/HousekeepingTask.
        Payload JSON/form:
          - estancia_id   (int, opcional)  -> cierra solo esa estancia
          - reserva_id    (int, opcional)  -> cierra todas las estancias abiertas de la reserva
          - pago_monto    (float, opcional)
          - pago_metodo   (str, opcional; default 'Efectivo')
        """
        p = request.get_json(silent=True) or request.form or {}
        estancia_id = _to_int(p.get("estancia_id") if hasattr(p, "get") else None)
        reserva_id  = _to_int(p.get("reserva_id")  if hasattr(p, "get") else None)


        if not reserva_id and estancia_id:
            reserva_id = db.session.execute(
                text("SELECT Codigo_Reserva FROM ReservaEstancia WHERE Id_Estancia=:e"),
                {"e": estancia_id}
            ).scalar()

        if not reserva_id:
            return jsonify({"ok": False, "error": "reserva_or_estancia_required"}), 400

        bd_before = _checkout_breakdown(int(reserva_id), estancia_id=estancia_id)
        if not bd_before:
            return jsonify({"ok": False, "error": "not_found"}), 404

        # Pago final (opcional)
                # Pago final (opcional)
        pago_monto = _to_float(p.get("pago_monto") if hasattr(p, "get") else None, 0.0)
        pago_metodo = (p.get("pago_metodo") or "Efectivo")[:30] if hasattr(p, "get") else "Efectivo"


        receipt = None
        if pago_monto > 0.0:
            try:
                rec = FinReceipt(
                    numero="PENDING",
                    reserva_id=int(reserva_id),
                    invoice_id=None,
                    tx_id=None,
                    metodo=pago_metodo,
                    currency="CRC",
                    monto=pago_monto,
                    emitido_por=session["user_id"],
                )
                db.session.add(rec)
                db.session.flush()
                rec.numero = _make_seq("RC", rec.id_receipt)

                db.session.execute(
                    text("""
                        UPDATE Reserva
                           SET Monto_Pagado = COALESCE(Monto_Pagado,0) + :p,
                               Fecha_Ultimo_Pago = NOW()
                         WHERE Codigo_Reserva = :r
                    """),
                    {"p": pago_monto, "r": int(reserva_id)}
                )
                db.session.commit()
                _create_receipt_pdf(rec)
                receipt = {"id": rec.id_receipt, "numero": rec.numero}
            except Exception as e:
                current_app.logger.warning(f"[CHECKOUT] No se pudo registrar pago final: {e}")
                db.session.rollback()

        # Cerrar estancias y liberar habitación(es)
        if estancia_id:
            estancias = db.session.execute(
                text("SELECT Id_Estancia, Habitacion_Id FROM ReservaEstancia WHERE Id_Estancia=:e"),
                {"e": estancia_id}
            ).mappings().all()
        else:
            estancias = db.session.execute(
                text("""
                    SELECT Id_Estancia, Habitacion_Id
                      FROM ReservaEstancia
                     WHERE Codigo_Reserva=:r
                       AND Estado IN ('Pendiente','Asignada')
                """),
                {"r": int(reserva_id)}
            ).mappings().all()

        cerradas = []
        for e in estancias:
            try:
                db.session.execute(
                    text("UPDATE ReservaEstancia SET Estado='Liberada' WHERE Id_Estancia=:id"),
                    {"id": int(e["Id_Estancia"])}
                )
                db.session.commit()
                _liberar_habitacion_y_lanzar_limpieza(int(e["Habitacion_Id"]), int(reserva_id))
                cerradas.append(int(e["Id_Estancia"]))
            except Exception as ex:
                db.session.rollback()
                current_app.logger.warning(f"[CHECKOUT] No se pudo cerrar estancia {e['Id_Estancia']}: {ex}")

        # Auditoría
        try:
            _audit_log(
                _current_user_email(),
                "checkout.completed",
                {
                    "reserva_id": int(reserva_id),
                    "estancias_cerradas": cerradas,
                    "pago_final": float(pago_monto or 0.0),
                    "metodo": pago_metodo,
                },
                entidad_id=str(reserva_id),
            )
        except Exception:
            pass

        bd_after = _checkout_breakdown(int(reserva_id), estancia_id=estancia_id) or bd_before
        return jsonify({"ok": True, "reserva_id": int(reserva_id), "receipt": receipt, "breakdown": bd_after})
    
    
    
    
    # Configuración de carpetas de uploads ACE
    os.makedirs(app.config.get('UPLOAD_FOLDER_CLIENTES'), exist_ok=True)



    ## Parte de Brandon

    # ============================================================
    # FIN — UI y endpoints de Facturas (fin-invoices.html, CRUD)
    # ============================================================

    # Requiere:
    # - from flask import render_template, request, redirect, url_for, flash, send_file
    # - from werkzeug.utils import secure_filename
    # - from flask_login import login_required
    # - decorador role_required(...) ya definido
    # - 'db' ya inicializado
    # - BASE_DIR definido (Path de la app)
    # Si no tienes alguno de los imports arriba en tu app.py, agrégalos.

    # Carpeta de uploads para facturas (PDF/imagen del comprobante)
    INVOICE_UPLOAD_FOLDER = os.path.join(BASE_DIR, "static", "uploads", "invoices")
    os.makedirs(INVOICE_UPLOAD_FOLDER, exist_ok=True)

    # Extensiones de archivo permitidas (ajústalas si ocupás otras)
    def allowed_file(filename: str) -> bool:
        if not filename or "." not in filename:
            return False
        ext = filename.rsplit(".", 1)[1].lower()
        return ext in {"pdf", "jpg", "jpeg", "png"}

    # ------------------------------------------------------------
    # Modelo opcional (si YA tienes FinInvoice en tus modelos, no
    # dupliques: puedes borrar esta clase "fallback").
    # ------------------------------------------------------------
    try:
        FinInvoice  # type: ignore[name-defined]
    except NameError:
        from sqlalchemy import func
    
        # ========= FIN-INV-01 — Gestión de Facturas ===========
        class FinInvoice(db.Model):
            __tablename__ = "fin_invoices"
            __table_args__ = (
                db.UniqueConstraint("numero", name="UQ_fin_invoices_numero"),  # nombre explícito de la unique
            )
    
            id_factura      = db.Column(db.Integer, primary_key=True)
            numero          = db.Column(db.String(20), unique=True, nullable=False)
            cliente_nombre  = db.Column(db.String(120))
            cliente_email   = db.Column(db.String(120))
            moneda          = db.Column(db.Enum("CRC", "USD", name="fin_invoice_moneda"), server_default="CRC")
            monto_total     = db.Column(db.Numeric(12, 2), nullable=False)
            descripcion     = db.Column(db.String(255))
            archivo_path    = db.Column(db.String(255))  # nombre correcto que usas en JSON/UI
            id_reserva      = db.Column(db.Integer, index=True)
            id_usuario      = db.Column(db.Integer, index=True)
            fecha_emision   = db.Column(db.Date, server_default=func.current_date())
            estado          = db.Column(db.Enum("Emitida", "Anulada", name="fin_invoice_estado"), server_default="Emitida")
    
    
    
        # =========================
    # OPS — Aprobaciones SINPE
    # =========================

    def _ops_list_reservas_by_estado(estado: str, limit: int = 200):
        """
        Devuelve reservas para OPS con valores 100% JSON-serializables:
        - checkin/checkout: 'YYYY-MM-DD'
        - total: float
        """
        import re
        from decimal import Decimal
    
        rows = (
            db.session.execute(
                text(
                    """
                    SELECT
                        r.Codigo_Reserva        AS id,
                        r.Numero_Comprobante    AS numero,
                        r.Estado                AS estado,
                        r.Canal                 AS canal,
                        r.Fecha_Entrada         AS checkin,
                        r.Fecha_Salida          AS checkout,
                        r.Monto_Total           AS total,
                        r.Huespedes             AS huespedes,
                        r.Observaciones         AS observaciones,
                        CONCAT(c.Nombre,' ',c.Apellido) AS cliente_nombre,
                        c.Correo                AS cliente_correo,
                        c.Telefono              AS cliente_telefono,
                        h.Numero_Habitacion     AS habitacion_numero,
                        h.Tipo                  AS habitacion_tipo
                    FROM Reserva r
                    JOIN Cliente c    ON c.Codigo_Cliente    = r.Codigo_Cliente
                    JOIN Habitacion h ON h.Codigo_Habitacion = r.Codigo_Habitacion
                    WHERE r.Estado = :estado
                    ORDER BY r.Fecha_Registro DESC, r.Codigo_Reserva DESC
                    LIMIT :lim
                    """
                ),
                {"estado": estado, "lim": int(limit)},
            )
            .mappings()
            .all()
        )
    
        out = []
        for x in rows:
            d = dict(x)
    
            # 1) Normaliza fechas para que el frontend reciba YYYY-MM-DD
            for k in ("checkin", "checkout"):
                v = d.get(k)
                if v is None:
                    continue
                try:
                    # date/datetime -> isoformat
                    if hasattr(v, "isoformat"):
                        d[k] = v.isoformat()[:10]  # YYYY-MM-DD
                    else:
                        # si viniera como string raro, intentar extraer YYYY-MM-DD
                        s = str(v).strip()
                        m = re.search(r"(\d{4}-\d{2}-\d{2})", s)
                        if m:
                            d[k] = m.group(1)
                        else:
                            d[k] = s[:10] if len(s) >= 10 else s
                except Exception:
                    # Si algo falla, no rompemos la respuesta
                    try:
                        s = str(v).strip()
                        d[k] = s[:10] if len(s) >= 10 else s
                    except Exception:
                        pass
    
            # 2) Normaliza total para evitar TypeError: Decimal is not JSON serializable
            tv = d.get("total")
            if tv is None:
                d["total"] = 0.0
            elif isinstance(tv, Decimal):
                d["total"] = float(tv)
            else:
                # por si viniera como string/otro numérico
                try:
                    d["total"] = float(tv)
                except Exception:
                    # último recurso: dejarlo como string (serializable)
                    d["total"] = str(tv)
    
            out.append(d)
    
        return out
    
    

    @app.get("/api/ops/approvals/count")
    @role_required("Administrador", "Recepcionista")
    def api_ops_approvals_count():
        try:
            cnt = int(
                db.session.execute(
                    text("SELECT COUNT(*) FROM Reserva WHERE Estado='Pendiente'")
                ).scalar() or 0
            )
            return jsonify({"ok": True, "count": cnt})
        except Exception as e:
            try:
                current_app.logger.warning(f"[OPS] approvals/count error: {e}")
            except Exception:
                pass
            return jsonify({"ok": False, "count": 0, "error": "count_failed"}), 500

    @app.get("/api/ops/approvals")
    @role_required("Administrador", "Recepcionista")
    def api_ops_approvals_overview():
        limit = request.args.get("limit", "200")
        try:
            limit_i = max(1, min(500, int(limit)))
        except Exception:
            limit_i = 200

        try:
            pending = _ops_list_reservas_by_estado("Pendiente", limit_i)
            confirmed = _ops_list_reservas_by_estado("Confirmada", limit_i)
            return jsonify(
                {
                    "ok": True,
                    "pending_count": len(pending),
                    "pending": pending,
                    "confirmed": confirmed,
                }
            )
        except Exception as e:
            try:
                current_app.logger.warning(f"[OPS] approvals overview error: {e}")
            except Exception:
                pass
            return jsonify({"ok": False, "error": "overview_failed"}), 500

    @app.post("/api/ops/approvals/<int:reserva_id>/approve")
    @role_required("Administrador", "Recepcionista")
    def api_ops_approvals_approve(reserva_id: int):
        try:
            row = (
                db.session.execute(
                    text("SELECT Estado, Fecha_Entrada FROM Reserva WHERE Codigo_Reserva=:r LIMIT 1"),
                    {"r": reserva_id},
                )
                .mappings()
                .first()
            )
            if not row:
                return jsonify({"ok": False, "error": "not_found"}), 404

            estado_actual = (row.get("Estado") or "").strip()
            if estado_actual != "Pendiente":
                return jsonify({"ok": False, "error": "invalid_state", "estado": estado_actual}), 409

            db.session.execute(
                text("UPDATE Reserva SET Estado='Confirmada' WHERE Codigo_Reserva=:r"),
                {"r": reserva_id},
            )
            db.session.commit()

            # Asegurar número de comprobante (si está vacío)
            try:
                fecha_in = str(row.get("Fecha_Entrada") or "")
                numero = _ensure_reserva_numero(reserva_id, fecha_in)
            except Exception:
                numero = None

            # Generar comprobante PDF + Documento/ReservaDocumento (si aún no existe)
            try:
                exists = db.session.execute(
                    text("SELECT 1 FROM ReservaDocumento WHERE Codigo_Reserva=:r LIMIT 1"),
                    {"r": reserva_id},
                ).scalar()
                if not exists:
                    rdet = _get_reserva_by_id(reserva_id)
                    if rdet:
                        _create_comprobante_pdf(dict(rdet))
            except Exception as e2:
                try:
                    current_app.logger.warning(f"[OPS] No se pudo generar comprobante R={reserva_id}: {e2}")
                except Exception:
                    pass

            # Notificación de CONFIRMACIÓN (respeta /sac/preferencias)
            try:
                _notify_reserva_success(reserva_id)
            except Exception as e3:
                try:
                    current_app.logger.warning(f"[OPS] Notificación confirmación falló R={reserva_id}: {e3}")
                except Exception:
                    pass

            return jsonify({"ok": True, "id": reserva_id, "estado": "Confirmada", "numero": numero})
        except Exception as e:
            db.session.rollback()
            try:
                current_app.logger.warning(f"[OPS] approve error R={reserva_id}: {e}")
            except Exception:
                pass
            return jsonify({"ok": False, "error": "approve_failed"}), 500

    @app.post("/api/ops/approvals/<int:reserva_id>/reject")
    @role_required("Administrador", "Recepcionista")
    def api_ops_approvals_reject(reserva_id: int):
        data = request.get_json(silent=True) or {}
        reason = (data.get("reason") or data.get("motivo") or "").strip()
        if not reason:
            return jsonify({"ok": False, "error": "reason_required"}), 400

        try:
            row = (
                db.session.execute(
                    text("SELECT Estado, Observaciones FROM Reserva WHERE Codigo_Reserva=:r LIMIT 1"),
                    {"r": reserva_id},
                )
                .mappings()
                .first()
            )
            if not row:
                return jsonify({"ok": False, "error": "not_found"}), 404

            estado_actual = (row.get("Estado") or "").strip()
            if estado_actual != "Pendiente":
                return jsonify({"ok": False, "error": "invalid_state", "estado": estado_actual}), 409

            prev_obs = (row.get("Observaciones") or "").strip()
            stamp = datetime.now().strftime("%Y-%m-%d %H:%M")
            new_line = f"[RECHAZO SINPE {stamp}] {reason}"
            obs_final = (prev_obs + "\n" + new_line).strip() if prev_obs else new_line

            # Observaciones es VARCHAR(255) en tu script: recortamos para evitar error
            if len(obs_final) > 255:
                obs_final = obs_final[:252] + "..."

            db.session.execute(
                text("UPDATE Reserva SET Estado='Cancelada', Observaciones=:o WHERE Codigo_Reserva=:r"),
                {"o": obs_final, "r": reserva_id},
            )
            db.session.commit()
            return jsonify({"ok": True, "id": reserva_id, "estado": "Cancelada"})
        except Exception as e:
            db.session.rollback()
            try:
                current_app.logger.warning(f"[OPS] reject error R={reserva_id}: {e}")
            except Exception:
                pass
            return jsonify({"ok": False, "error": "reject_failed"}), 500

    @app.post("/api/ops/approvals/<int:reserva_id>/retract")
    @role_required("Administrador", "Recepcionista")
    def api_ops_approvals_retract(reserva_id: int):
        try:
            row = (
                db.session.execute(
                    text("SELECT Estado FROM Reserva WHERE Codigo_Reserva=:r LIMIT 1"),
                    {"r": reserva_id},
                )
                .mappings()
                .first()
            )
            if not row:
                return jsonify({"ok": False, "error": "not_found"}), 404

            estado_actual = (row.get("Estado") or "").strip()
            if estado_actual != "Confirmada":
                return jsonify({"ok": False, "error": "invalid_state", "estado": estado_actual}), 409

            db.session.execute(
                text("UPDATE Reserva SET Estado='Pendiente' WHERE Codigo_Reserva=:r"),
                {"r": reserva_id},
            )
            db.session.commit()

            # Nota: NO enviamos notificación aquí por defecto (para evitar confusión al cliente).
            return jsonify({"ok": True, "id": reserva_id, "estado": "Pendiente"})
        except Exception as e:
            db.session.rollback()
            try:
                current_app.logger.warning(f"[OPS] retract error R={reserva_id}: {e}")
            except Exception:
                pass
            return jsonify({"ok": False, "error": "retract_failed"}), 500

    

    # ------------------------------------------------------------
    # UI — Lista de facturas
    # ------------------------------------------------------------
    @app.route("/fin-invoices.html", methods=["GET"])
    @login_required
    @role_required("Administrador", "Recepcionista")
    def fin_invoices_html():
        try:
            facturas = (
                FinInvoice.query.order_by(FinInvoice.fecha_emision.desc())
                .limit(200)
                .all()
            )
        except Exception:
            # Si aún no has corrido migrations, evita romper la UI
            facturas = []
        return render_template("fin-invoices.html", facturas=facturas)

    # ------------------------------------------------------------
    # Crear factura (POST)
    # Espera campos:
    #   numero, cliente_nombre, cliente_email (opcional),
    #   moneda (CRC|USD,...), monto_total, fecha_emision (YYYY-MM-DD),
    #   comprobante (file input)
    # ------------------------------------------------------------
    
    
    
    # Listado JSON para la tabla del front
    @app.get("/api/fin/invoices")
    @role_required("Administrador", "Recepcionista")
    def api_fin_invoices_list():
        rows = db.session.query(FinInvoice).order_by(FinInvoice.id_factura.desc()).all()
        items = []
        for r in rows:
            items.append({
                "id": r.id_factura,
                "numero": r.numero,
                "cliente_nombre": r.cliente_nombre,
                "cliente_email": r.cliente_email,
                "moneda": r.moneda,
                "monto_total": float(r.monto_total or 0),
                "fecha_emision": r.fecha_emision.isoformat() if r.fecha_emision else None,
                "estado": r.estado,
                "archivo_path": r.archivo_path,  # <— nombre correcto
            })
        return jsonify({"ok": True, "items": items})

    # Alta de factura (lo que estás intentando con POST /fin/invoices/nuevo)
    

    from sqlalchemy.exc import IntegrityError
    from werkzeug.utils import secure_filename
    
    @app.post("/fin/invoices/nuevo")
    @role_required("Administrador", "Recepcionista")
    def fin_invoice_nuevo():
        """
        Crea una fila en fin_invoices con los datos del formulario:
          numero, cliente_nombre, cliente_email, moneda, monto_total, descripcion,
          archivo_path (opcional), id_reserva (opcional), id_usuario (obligatorio), estado.
        Guarda el archivo en /static/uploads/invoices o genera un PDF mínimo si no hay adjunto.
        """
        # Asegura que la tabla exista
        if not _tabla_existe("fin_invoices"):
            flash("No existe la tabla de facturas 'fin_invoices' en la BD.", "danger")
            return redirect(url_for("fin_invoices_html") if "fin_invoices_html" in current_app.view_functions else url_for("admin_dashboard_html"))
    
        # Campos del form
        f = request.form
        numero        = (f.get("numero") or "").strip()
        cliente_nombre= (f.get("cliente_nombre") or "").strip()
        cliente_email = (f.get("cliente_email") or "").strip().lower()
        moneda        = (f.get("moneda") or "CRC").strip()
        descripcion   = (f.get("descripcion") or "").strip()
        estado        = "Emitida"
    
        try:
            monto_total = float(f.get("monto_total") or 0)
        except Exception:
            monto_total = 0.0
    
        # Opcional: id_reserva
        try:
            id_reserva = int(f.get("id_reserva")) if f.get("id_reserva") else None
        except Exception:
            id_reserva = None
    
        # Obligatorio por esquema: id_usuario
        uid = session.get("user_id")
        if not uid:
            flash("Sesión requerida para emitir facturas.", "warning")
            return redirect(url_for("login_html"))
    
        # Validaciones mínimas
        if not numero or monto_total <= 0:
            flash("Número/folio y monto total son obligatorios.", "warning")
            return redirect(url_for("fin_invoices_html") if "fin_invoices_html" in current_app.view_functions else url_for("admin_dashboard_html"))
    
        # ===== Guardar/generar archivo =====
        STATIC_INVOICES_DIR = BASE_DIR / "static" / "uploads" / "invoices"
        STATIC_INVOICES_DIR.mkdir(parents=True, exist_ok=True)
    
        archivo_file = request.files.get("archivo")
        archivo_filename = None
        try:
            if archivo_file and getattr(archivo_file, "filename", ""):
                raw = secure_filename(archivo_file.filename)
                ts  = datetime.now().strftime("%Y%m%d-%H%M%S")
                archivo_filename = f"{numero}-{ts}-{raw}"
                archivo_file.save(STATIC_INVOICES_DIR / archivo_filename)
            else:
                # Generar PDF mínimo si no se adjunta nada
                archivo_filename = f"{numero}.pdf"
                _write_minimal_pdf(
                    STATIC_INVOICES_DIR / archivo_filename,
                    "Factura — Hotel Villa Grace",
                    [
                        f"Folio:       {numero}",
                        f"Fecha:       {datetime.now():%Y-%m-%d}",
                        f"Cliente:     {cliente_nombre or '-'}",
                        f"Email:       {cliente_email or '-'}",
                        f"Moneda:      {moneda}",
                        f"Total:       {('₡' if moneda=='CRC' else '$')} {monto_total:,.2f}".replace(",", "X").replace(".", ",").replace("X", "."),
                        f"Descripción: {descripcion or '-'}",
                        f"Estado:      {estado}",
                    ],
                )
        except Exception as e:
            current_app.logger.warning(f"[fin_invoices] No se pudo guardar/generar archivo: {e}")
            archivo_filename = None  # insertaremos NULL en archivo_path
    
        # ===== Insert en fin_invoices =====
        try:
            params = {
                "numero": numero,
                "cliente_nombre": cliente_nombre or None,
                "cliente_email": cliente_email or None,
                "moneda": moneda,
                "monto_total": float(monto_total),
                "descripcion": descripcion or None,
                "archivo_path": archivo_filename,  # la plantilla arma la URL con /static/uploads/invoices/<archivo_path>
                "id_reserva": id_reserva,
                "id_usuario": int(uid),
                "estado": estado,
            }
            sql = text("""
                INSERT INTO fin_invoices
                    (numero, cliente_nombre, cliente_email, moneda, monto_total, descripcion,
                     archivo_path, id_reserva, id_usuario, estado)
                VALUES
                    (:numero, :cliente_nombre, :cliente_email, :moneda, :monto_total, :descripcion,
                     :archivo_path, :id_reserva, :id_usuario, :estado)
            """)
            res = db.session.execute(sql, params)
            db.session.commit()
            new_id = int(res.lastrowid or 0)
    
            _audit_log(_current_user_email(), "fin.invoice.emitida",
                       {"id": new_id, "numero": numero, "total": monto_total}, entidad_id=str(new_id))
    
            flash("Factura emitida correctamente.", "success")
            return redirect(url_for("fin_invoices_html") if "fin_invoices_html" in current_app.view_functions else url_for("admin_dashboard_html"))
    
        except IntegrityError as ie:
            db.session.rollback()
            # Unicidad de numero
            if "UQ_fin_invoices_numero" in str(ie.orig) or "Duplicate" in str(ie.orig).lower():
                flash("El número/folio ya existe. Elige otro.", "warning")
            else:
                flash(f"No se pudo crear la factura (integridad): {ie.orig}", "danger")
            return redirect(url_for("fin_invoices_html") if "fin_invoices_html" in current_app.view_functions else url_for("admin_dashboard_html"))
    
        except Exception as e:
            db.session.rollback()
            flash(f"No se pudo crear la factura: {e}", "danger")
            return redirect(url_for("fin_invoices_html") if "fin_invoices_html" in current_app.view_functions else url_for("admin_dashboard_html"))
    
    
    

    # =========================
    # FIN-UI: Caja y Cierres Períodos
    # =========================

    @app.route("/fin-caja.html")
    @app.route("/fin-caja")
    @app.route("/fin/caja")
    @role_required("Administrador", "Recepcionista")
    def fin_caja_html():
        """
        UI de Caja (apertura, movimientos, cierre diario).
        Requiere Administrador o Recepcionista.
        Renderiza templates/fin-caja.html
        """
        return render_template("fin-caja.html")

    @app.route("/fin-periods.html")
    @app.route("/fin-periodos.html")
    @app.route("/fin/periods")
    @app.route("/fin/periodos")
    @role_required("Administrador")
    def fin_periods_html():
        """
        UI de Cierres de período (mensual) y re-aperturas.
        Solo Administrador.
        Renderiza templates/fin-periods.html
        """
        return render_template("fin-periods.html")




    # ------------------------------------------------------------
    # Marcar como Pagada
    # ------------------------------------------------------------
    @app.route("/fin/invoices/<int:id>/pagar", methods=["POST"])
    @login_required
    @role_required("Administrador", "Recepcionista")
    def fin_invoice_pagar(id: int):
        factura = FinInvoice.query.get_or_404(id)
        if factura.estado not in ("Emitida", "Anulada"):  # si usas otra lógica, ajusta
            # Permitimos pasar de 'Emitida' a 'Pagada'. Si estaba 'Anulada' no debería, pero lo controlamos arriba si querés.
            pass
        factura.estado = "Pagada"
        db.session.commit()
        flash("Factura marcada como pagada.", "success")
        return redirect(url_for("fin_invoices_html"))

    # ------------------------------------------------------------
    # Anular factura
    # ------------------------------------------------------------
    @app.route("/fin/invoices/<int:id>/anular", methods=["POST"])
    @login_required
    @role_required("Administrador")
    def fin_invoice_anular(id: int):
        factura = FinInvoice.query.get_or_404(id)
        if factura.estado == "Anulada":
            flash("La factura ya está anulada.", "info")
            return redirect(url_for("fin_invoices_html"))
        factura.estado = "Anulada"
        db.session.commit()
        flash("Factura anulada.", "info")
        return redirect(url_for("fin_invoices_html"))


    return app


def get_base_currency() -> str:
    """
    Moneda base del sistema.
    Puedes mover esto a config, por ahora usamos 'CRC' por defecto.
    """
    return current_app.config.get("FIN_BASE_CURRENCY", "CRC")


def get_fx_rate_for_date(currency: str, rate_date: date) -> float:
    """
    Devuelve el tipo de cambio (rate_to_base) para la moneda y fecha dadas.
    Si no existe para ese día exacto, busca el último <= fecha.
    """
    if not currency:
        raise ValueError("Moneda no especificada")

    base = get_base_currency()
    currency = currency.upper()
    if currency == base:
        return 1.0

    row = db.session.execute(
        text(
            """
            SELECT rate_to_base
            FROM fin_fx_rate
            WHERE currency = :curr
              AND rate_date <= :d
            ORDER BY rate_date DESC
            LIMIT 1
            """
        ),
        {"curr": currency, "d": rate_date},
    ).fetchone()

    if not row:
        raise RuntimeError(
            f"No hay tipo de cambio para {currency} (<= {rate_date.isoformat()})"
        )

    return float(row[0])


def convert_to_base(amount: float, currency: str, rate_date: date) -> Tuple[float, float]:
    """
    Convierte un monto desde 'currency' a moneda base.
    Retorna (monto_base, fx_rate).
    """
    if amount is None:
        raise ValueError("amount no puede ser None")

    currency = (currency or "").upper()
    base = get_base_currency()

    if currency == base:
        return float(amount), 1.0

    fx = get_fx_rate_for_date(currency, rate_date)
    return float(amount) * fx, fx


app = create_app()
# =========================
# EJECUCIÓN
# =========================
# al final de app.py
#if __name__ == "__main__":
#    app.run(
#        host="127.0.0.1",
#        port=int(os.getenv("PORT", 5000)),
#        debug=True,
#        use_reloader=True,   # <-- clave para quitar ese error
#    )
#