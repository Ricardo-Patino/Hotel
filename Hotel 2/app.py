# app.py — Aplicación principal Flask para Hotel_VillaGrace
# Ejecuta:  python "Hotel 2/app.py"

import os
import sys
import smtplib
import ssl
from functools import wraps
from email.message import EmailMessage
from datetime import datetime, date
from pathlib import Path
from sqlalchemy import text, func

from flask import (
    Flask, render_template, jsonify,
    request, redirect, url_for, flash, session, current_app
)

from sqlalchemy.exc import IntegrityError
from itsdangerous import URLSafeTimedSerializer, BadSignature, SignatureExpired

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
from config import Config
from extensions import db, migrate
from models_sql import Usuario, Rol, Habitacion
try:
    from models_sql import Reserva as ReservaModel  # si existiera
except Exception:
    ReservaModel = None

# =========================
# Helpers (roles/redirects)
# =========================
DEFAULT_ROLES = ("Administrador", "Recepcionista", "Limpieza", "Cliente")

def _ensure_seed_roles() -> None:
    try:
        existing = {r.Nombre for r in Rol.query.all()}
        for name in DEFAULT_ROLES:
            if name not in existing:
                db.session.add(Rol(Nombre=name, Descripcion=f"Rol {name}", Estado='Activo'))
        db.session.commit()
    except Exception:
        db.session.rollback()

def _get_role_by_name(name: str):
    if not name:
        return None
    return Rol.query.filter_by(Nombre=name).first()

def _get_role_name(user: Usuario) -> str:
    # Si el relationship no está configurado, intentar resolver por Rol_Id
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
        "Limpieza":      "ops_housekeeping_html",
        "Cliente":       "portal_dashboard_html",
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
    """
    Permite el acceso solo si el rol actual está en 'roles'.
    Uso: @role_required("Administrador") o @role_required("Administrador", "Recepcionista")
    """
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
    sender   = app.config.get("MAIL_DEFAULT_SENDER") or app.config.get("MAIL_USERNAME") or "no-reply@hotel.local"
    subject  = "Restablecimiento de contraseña — Hotel Villa Grace"
    body     = (
        "Hola,\n\n"
        "Recibimos una solicitud para restablecer tu contraseña en Hotel Villa Grace.\n"
        f"Para continuar, abre este enlace:\n\n{reset_url}\n\n"
        "Si no fuiste tú, ignora este mensaje. El enlace expira en 1 hora.\n\n"
        "Atentamente,\nHotel Villa Grace"
    )

    host     = app.config.get("MAIL_SERVER")
    port     = int(app.config.get("MAIL_PORT", 0) or 0)
    user     = app.config.get("MAIL_USERNAME")
    pwd      = app.config.get("MAIL_PASSWORD")
    use_tls  = bool(app.config.get("MAIL_USE_TLS", False))
    use_ssl  = bool(app.config.get("MAIL_USE_SSL", False))

    if not (host and port and user and pwd):
        app.logger.warning("[MAIL] Config SMTP incompleta; usando consola.")
        app.logger.info(f"[RESET LINK] Para {to_email}: {reset_url}")
        return

    if not sender or ("@" not in sender):
        sender = user

    try:
        msg = EmailMessage()
        msg["Subject"] = subject
        msg["From"]    = sender
        msg["To"]      = to_email
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

        app.logger.info(f"[MAIL SENT] Reset a {to_email} vía {host}:{port} (TLS={use_tls}, SSL={use_ssl})")
    except Exception as e:
        app.logger.error(f"[MAIL ERROR] {type(e).__name__}: {e}")
        app.logger.info(f"[RESET LINK] Para {to_email}: {reset_url}")

# =========================
# Utilidades para reservas
# =========================
def _current_user_email() -> str | None:
    try:
        uid = session.get("user_id")
        if not uid:
            return None
        u = Usuario.query.filter_by(Codigo_Usuario=uid).first()
        return (u.Correo or "").strip().lower() if u else None
    except Exception:
        return None

def current_cliente_id() -> int | None:
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
            if n in obj:
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
    tipo   = _get_first_attr(r, ["Tipo", "Habitacion", "RoomType"])
    plan   = _get_first_attr(r, ["Plan", "Tarifa", "RatePlan"]) or "Tarifa Flexible"
    canal  = _get_first_attr(r, ["Canal", "Source", "Origen"]) or "Web"

    huesp  = _get_first_attr(r, ["Huespedes", "Pax", "Huespedes_Count"])
    obs    = _get_first_attr(r, ["Observaciones", "Notas", "Comentarios"]) or ""
    usuario= _get_first_attr(r, ["Usuario", "Email", "Correo", "Cliente_Email", "email"])

    ci     = _normalize_date_like(_get_first_attr(r, ["Fecha_Entrada", "Checkin", "CheckIn", "Inicio", "Desde"]))
    co     = _normalize_date_like(_get_first_attr(r, ["Fecha_Salida", "Checkout", "CheckOut", "Fin", "Hasta"]))

    monto  = _get_first_attr(r, ["Monto_Total", "Total", "Importe", "Total_Monto"]) or 0
    price  = _get_first_attr(r, ["Precio_Noche", "Tarifa_Noche", "PriceNight"]) or 0

    created= _get_first_attr(r, ["Fecha_Creacion", "Created_At", "Creado", "Fecha_Registro"])
    updated= _get_first_attr(r, ["Fecha_Modificacion", "Updated_At", "Modificado", "Fecha_Registro"])

    return {
        "id":            id_val,
        "numero":        numero or (f"VG-{id_val}" if id_val else None),
        "estado":        estado,
        "tipo":          tipo,
        "plan":          plan,
        "canal":         canal,
        "huespedes":     str(huesp or ""),
        "observaciones": obs,
        "usuario":       usuario or "",
        "checkin":       ci,
        "checkout":      co,
        "monto":         float(monto or 0),
        "precio_noche":  float(price or 0),
        "created_at":    created.isoformat() if hasattr(created, "isoformat") else (str(created) if created else None),
        "updated_at":    updated.isoformat() if hasattr(updated, "isoformat") else (str(updated) if updated else None),
    }

def _get_reserva_by_id(reserva_id: int):
    row = db.session.execute(text("""
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
        WHERE R.Codigo_Reserva = :id
        LIMIT 1
    """), {"id": reserva_id}).mappings().first()
    return row

def _reserva_belongs_to_email(reserva_id: int, email: str) -> bool:
    if not email:
        return False
    row = db.session.execute(text("""
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
    """), {"id": reserva_id, "email": (email or "").strip().lower()}).first()
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

def ensure_cliente_for_email(nombre: str, correo: str, telefono: str | None = None) -> int | None:
    if not correo:
        return None
    correo = correo.strip().lower()
    row = db.session.execute(
        text("SELECT Codigo_Cliente FROM Cliente WHERE LOWER(Correo)=:e LIMIT 1"),
        {"e": correo}
    ).first()
    if row:
        return row[0]
    nombre = (nombre or "").strip() or "Cliente Web"
    partes = nombre.split(" ", 1)
    nom = partes[0][:50]
    ape = (partes[1] if len(partes) > 1 else "").strip()[:50]
    tel = (telefono or "").strip()[:20] or "00000000"

    db.session.execute(text("""
        INSERT INTO Cliente (Cedula, Nombre, Apellido, Telefono, Correo, Fecha_Nacimiento)
        VALUES (0, :n, :a, :t, :e, '1990-01-01')
    """), {"n": nom, "a": ape, "t": tel, "e": correo})
    db.session.commit()

    row2 = db.session.execute(
        text("SELECT Codigo_Cliente FROM Cliente WHERE LOWER(Correo)=:e LIMIT 1"),
        {"e": correo}
    ).first()
    return row2[0] if row2 else None

# =========================
# FACTORY PRINCIPAL
# =========================
from flask import url_for  # necesario en _validate_no_overbooking

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

    # Blueprint de GRR (creación de reservas)
    from blueprints.grr.routes import grr_bp
    app.register_blueprint(grr_bp, url_prefix="/grr")

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
    # Portal (solo Cliente)
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

    # Operaciones
    @app.route("/ops-dashboard.html")
    @role_required("Administrador", "Recepcionista")
    def ops_dashboard_html():
        return render_template("ops-dashboard.html")

    @app.route("/ops-housekeeping.html")
    @role_required("Administrador", "Limpieza")
    def ops_housekeeping_html():
        return render_template("ops-housekeeping.html")

    # Administración
    @app.route("/admin-dashboard.html")
    @role_required("Administrador")
    def admin_dashboard_html():
        return render_template("admin-dashboard.html")

    # ---------------------- API Disponibilidad --------------------------
    @app.get("/api/availability")
    def api_availability():
        from datetime import datetime as dt
        checkin  = (request.args.get("checkin") or "").strip()
        checkout = (request.args.get("checkout") or "").strip()
        guests   = int((request.args.get("guests") or "1") or 1)
        rooms_req= int((request.args.get("rooms")  or "1") or 1)

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
            "message": "Disponibilidad confirmada" if is_available else "Sin cupo para ese rango"
        }), 200

    @app.get("/booking/search")
    def booking_search_alias():
        return redirect(url_for("api_availability", **request.args))

    # ---------------------- Búsqueda / resultados reserva ----------------------
    @app.route("/booking-search", methods=["GET", "POST"])
    @app.route("/booking-search.html", methods=["GET", "POST"])
    def booking_search():
        if request.method == "POST":
            checkin  = request.form.get("checkin")
            checkout = request.form.get("checkout")
            guests   = request.form.get("guests", "1")
            rooms    = request.form.get("rooms", "1")
            return redirect(url_for("booking_results",
                                    checkin=checkin, checkout=checkout,
                                    guests=guests, rooms=rooms))
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
            "checkin":  request.args.get("checkin"),
            "checkout": request.args.get("checkout"),
            "adults":   request.args.get("adults", "2"),
            "children": request.args.get("children", "0"),
            "room":     request.args.get("room", "1"),
            "price":    request.args.get("price"),
        }
        return render_template("booking-details.html", **params)

    @app.route("/booking-checkout", methods=["GET", "POST"])
    @app.route("/booking-checkout.html", methods=["GET", "POST"])
    def booking_checkout_html():
        ctx = {
            "checkin":  request.values.get("checkin"),
            "checkout": request.values.get("checkout"),
            "adults":   request.values.get("adults"),
            "children": request.values.get("children"),
            "room":     request.values.get("room"),
            "price":    request.values.get("price"),
            "full_name": request.values.get("full_name"),
            "email":     request.values.get("email"),
            "phone":     request.values.get("phone"),
        }
        return render_template("booking-checkout.html", **ctx)

    @app.route("/booking-confirmation", methods=["GET"])
    @app.route("/booking-confirmation.html", methods=["GET"])
    def booking_confirmation_html():
        data = {
            "checkin":  request.args.get("checkin"),
            "checkout": request.args.get("checkout"),
            "adults":   request.args.get("adults"),
            "children": request.args.get("children"),
            "room":     request.args.get("room"),
            "price":    request.args.get("price"),
            "reservation_code": request.args.get("code", "VG-" + datetime.now().strftime("%Y%m%d-%H%M%S")),
        }
        return render_template("booking-confirmation.html", **data)

    # ---------------------- API Portal Reservas (solo Cliente) ----------------------
    @app.route("/api/portal/reservas", methods=["GET"])
    @role_required("Cliente")
    def api_portal_reservas_list():
        if not session.get("user_id"):
            return jsonify({"ok": False, "error": "auth_required"}), 401
        email = _current_user_email()
        estado = request.args.get("estado") or None
        fini = request.args.get("fini") or None  # YYYY-MM-DD
        ffin = request.args.get("ffin") or None
        reservas = _query_user_reservas(email or "", estado, fini, ffin)
        app.logger.info(f"[PORTAL] user={email}, cli_id={current_cliente_id()} -> {len(reservas)} reservas")
        return jsonify({"ok": True, "items": reservas})

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

        new_ci = _norm(payload.get("checkin"))  or _normalize_date_like(r.get("Fecha_Entrada"))
        new_co = _norm(payload.get("checkout")) or _normalize_date_like(r.get("Fecha_Salida"))
        new_obs = payload.get("observaciones", None)
        new_h   = payload.get("huespedes", None)

        ok, msg = _validate_no_overbooking(str(new_ci), str(new_co))
        if not ok:
            return jsonify({"ok": False, "error": "availability", "message": msg}), 409

        sql = text("""
            UPDATE Reserva
               SET Fecha_Entrada = :ci,
                   Fecha_Salida  = :co,
                   Observaciones = COALESCE(:obs, Observaciones),
                   Huespedes     = COALESCE(:h, Huespedes)
             WHERE Codigo_Reserva = :id
        """)
        db.session.execute(sql, {
            "ci": new_ci,
            "co": new_co,
            "obs": new_obs,
            "h":  str(new_h) if new_h is not None else None,
            "id": reserva_id
        })
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
            {"id": reserva_id}
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
        return jsonify({"ok": True, "comprobante": {
            "numero": data.get("numero") or f"VG-{data.get('id')}",
            "fecha": datetime.utcnow().isoformat() + "Z",
            "cliente": email,
            "reserva": data,
            "emisor": {"hotel": "Hotel Villa Grace", "canal": data.get("canal", "Web")},
        }})

    # ---------------------- RUTA DE HABITACIONES DINÁMICAS (opcional) ----------------------
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
                    "Estado": h.Estado
                }
                for h in habitaciones
            ]
            return jsonify({"ok": True, "habitaciones": data})
        except Exception as e:
            return jsonify({"ok": False, "error": str(e)})

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
            password   = (request.form.get("password") or "").strip()

            user = None

            # 1) Buscar por correo, case-insensitive y sin espacios basura
            if identifier:
                user = Usuario.query.filter(func.lower(Usuario.Correo) == identifier).first()

            # 2) Si no está por correo, intentar por cédula/pasaporte EXACTA
            if not user:
                ced = (request.form.get("email") or "").strip()
                if ced:
                    user = Usuario.query.filter_by(Cedula_Pasaporte=ced).first()

            if not user:
                flash("Credenciales inválidas o usuario inactivo.", "danger")
                return render_template("login.html")

            if user.Estado != 'Activo':
                flash("Usuario inactivo.", "danger")
                return render_template("login.html")

            # 3) Verificación de password con hash
            ok = False
            try:
                ok = bool(user.check_password(password))
            except Exception:
                ok = False

            # 4) Fallback “legacy”: si la BD trae una contraseña en claro
            if not ok:
                legacy_plain = None
                for col in ("Contrasena", "Password", "Password_Plain", "Pwd"):
                    if hasattr(user, col):
                        legacy_plain = getattr(user, col)
                        break
                if legacy_plain and str(legacy_plain) == password:
                    # Rehash inmediato para dejar limpia la cuenta
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

            # Vincula Usuario <-> Cliente por correo si no está vinculado
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

            session["user_id"]   = user.Codigo_Usuario
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
            full_name   = (request.form.get("full_name") or "").strip()
            national_id = (request.form.get("national_id") or "").strip()
            email       = (request.form.get("email") or "").strip().lower()
            phone       = (request.form.get("phone") or "").strip()
            password    = (request.form.get("password") or "").strip()
            confirm     = (request.form.get("confirm_password") or "").strip()

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
                Estado='Activo',
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

    return app

# =========================
# EJECUCIÓN
# =========================
if __name__ == "__main__":
    app = create_app()
    try:
        print(f"DB -> {os.getenv('DB_USER','root')} @ {os.getenv('DB_HOST','127.0.0.1')} : {os.getenv('DB_PORT','3306')} / {os.getenv('DB_NAME','Hotel_VillaGrace')}")
    except Exception:
        pass
    app.run(host="0.0.0.0", port=5000, debug=True)
