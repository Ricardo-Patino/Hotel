# app.py — Aplicación principal Flask para Hotel_VillaGrace
# Ejecuta:  python "Hotel 2/app.py"

import os
import sys
import smtplib
import ssl
from email.message import EmailMessage
from datetime import datetime, date
from pathlib import Path
from functools import wraps

from flask import (
    Flask, render_template, jsonify,
    request, redirect, url_for, flash, session, current_app
)
from sqlalchemy import text
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
from models.room import Room  # compatibilidad

# =========================
# Helpers GPU (roles/redirects)
# =========================
DEFAULT_ROLES = ("Administrador", "Recepcionista", "Limpieza", "Cliente")

def _ensure_seed_roles() -> None:
    """Crea los roles básicos si no existen. Idempotente."""
    try:
        existing = {r.Nombre for r in Rol.query.all()}
        for name in DEFAULT_ROLES:
            if name not in existing:
                db.session.add(Rol(Nombre=name, Descripcion=f"Rol {name}", Estado='Activo'))
        db.session.commit()
    except Exception:
        db.session.rollback()

def _get_role_by_name(name: str) -> Rol | None:
    if not name:
        return None
    return Rol.query.filter_by(Nombre=name).first()

def _get_role_name(user: Usuario) -> str:
    """Devuelve el nombre del rol del usuario o 'Cliente' por defecto."""
    return user.rol.Nombre if getattr(user, "rol", None) else "Cliente"

def role_redirect_endpoint(role_name: str) -> str:
    mapping = {
        "Administrador": "admin_dashboard_html",
        "Recepcionista": "ops_dashboard_html",
        "Limpieza":      "ops_housekeeping_html",
        "Cliente":       "portal_dashboard_html",
    }
    # Fallback seguro por si el endpoint no existe
    endpoint = mapping.get(role_name, "index_html")
    if current_app and endpoint not in current_app.view_functions:
        return "index_html"
    return endpoint


# =========================
# Reset Password (GPU-01-004)
# =========================
def _get_serializer(app: Flask) -> URLSafeTimedSerializer:
    return URLSafeTimedSerializer(app.config["SECRET_KEY"], salt="pwd-reset")

def _send_reset_email(app: Flask, to_email: str, reset_url: str) -> None:
    """Envía email con enlace de restablecimiento o cae a consola si no hay SMTP válido."""
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

    # Blueprint (reservas front GRR si lo usas)
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

    # ---------------------- RUTA DE HABITACIONES DINÁMICAS ----------------------
    @app.route("/rooms.html")
    def rooms_html():
        try:
            habitaciones = Habitacion.query.order_by(Habitacion.Numero_Habitacion.asc()).all()
            print(f"[DEBUG] Se cargaron {len(habitaciones)} habitaciones desde la BD.")
        except Exception as e:
            print(f"[ERROR] Al cargar habitaciones: {e}")
            habitaciones = []
        return render_template("rooms.html", habitaciones=habitaciones)

    # ---------------------- TEST DE CONEXIÓN ----------------------
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

    # /booking.html no existe como template: normalizamos al buscador
    @app.route("/booking.html")
    def booking_html():
        return redirect(url_for("booking_search"))

    # ---------------------- ENDPOINTS DE PORTAL / OPS / ADMIN ----------------
    @app.route("/portal-dashboard.html")
    def portal_dashboard_html():
        return render_template("portal-dashboard.html")

    @app.route("/portal-perfil.html")
    def portal_perfil_html():
        return render_template("portal-perfil.html")

    @app.route("/portal-preferencias.html")
    def portal_preferencias_html():
        return render_template("portal-preferencias.html")

    @app.route("/portal-reservas.html")
    def portal_reservas_html():
        return render_template("portal-reservas.html")

    @app.route("/ops-dashboard.html")
    def ops_dashboard_html():
        return render_template("ops-dashboard.html")

    @app.route("/ops-housekeeping.html")
    def ops_housekeeping_html():
        return render_template("ops-housekeeping.html")

    @app.route("/admin-dashboard.html")
    def admin_dashboard_html():
        return render_template("admin-dashboard.html")

    # ---------------------- API Disponibilidad (arreglo “sin fechas”) ----------
    @app.get("/api/availability")
    def api_availability():
        """
        Responde disponibilidad básica para el rango solicitado.
        Parámetros: checkin=YYYY-MM-DD, checkout=YYYY-MM-DD, guests, rooms
        """
        from datetime import datetime as dt

        checkin  = (request.args.get("checkin") or "").strip()
        checkout = (request.args.get("checkout") or "").strip()
        guests   = int((request.args.get("guests") or "1") or 1)
        rooms_req= int((request.args.get("rooms")  or "1") or 1)

        # Validación de fechas
        try:
            ci = dt.strptime(checkin, "%Y-%m-%d").date()
            co = dt.strptime(checkout, "%Y-%m-%d").date()
        except Exception:
            return jsonify({"ok": False, "available": False, "message": "Fechas inválidas"}), 400

        today = date.today()
        if ci < today or co <= ci:
            return jsonify({"ok": True, "available": False, "message": "Rango de fechas no válido"}), 200

        # Capacidad: intenta leer de BD; si falla, usa un default
        try:
            total_rooms = db.session.query(Habitacion).count()
            if not total_rooms:
                total_rooms = 10  # fallback si tabla vacía
        except Exception:
            total_rooms = 10

        available_rooms = total_rooms  # sin engine de inventario aún
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
        # Consulta la API interna para obtener disponibilidad
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

    # ---------------------- Recuperar contraseña ----------------------
    @app.route("/forgot-password", methods=["GET", "POST"])
    def forgot_password():
        if request.method == "POST":
            email = (request.form.get("email") or "").strip().lower()
            generic_msg = "Si el correo existe, te hemos enviado un enlace para restablecer tu contraseña."
            if not email or "@" not in email:
                flash(generic_msg, "info")
                return render_template("forgot-password.html")

            user = Usuario.query.filter_by(Correo=email).first()
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

        user = Usuario.query.filter_by(Correo=(email or "").lower()).first()
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

            user = Usuario.query.filter_by(Correo=identifier).first()
            if not user:
                user = Usuario.query.filter_by(Cedula_Pasaporte=(request.form.get("email") or "").strip()).first()

            if not user or user.Estado != 'Activo' or not user.check_password(password):
                flash("Credenciales inválidas o usuario inactivo.", "danger")
                return render_template("login.html")

            role_name = _get_role_name(user)
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
            if "@" not in email:
                flash("Correo electrónico inválido.", "warning")
                return render_template("register.html")
            if len(password) < 8:
                flash("La contraseña debe tener al menos 8 caracteres.", "warning")
                return render_template("register.html")

            if Usuario.query.filter_by(Correo=email).first():
                flash("El correo ya está registrado.", "danger")
                return render_template("register.html")

            role = _get_role_by_name("Cliente")
            if not role:
                _ensure_seed_roles()
                role = _get_role_by_name("Cliente")

            u = Usuario(
                Nombre=full_name,
                Cedula_Pasaporte=national_id or None,
                Correo=email,
                Telefono=phone,
                Rol_Id=role.Codigo_Rol if role else None,
                Estado='Activo',
            )
            u.set_password(password)
            db.session.add(u)
            db.session.commit()

            flash("Registro exitoso. Ya puedes iniciar sesión.", "success")
            return redirect(url_for("login_html"))
        return render_template("register.html")

    # ---------------------- Decoradores reutilizables ----------------------
    def login_required(f):
        @wraps(f)
        def wrapper(*args, **kwargs):
            if not session.get("user_id"):
                flash("Por favor inicia sesión para continuar.", "warning")
                return redirect(url_for("login_html", next=request.path))
            return f(*args, **kwargs)
        return wrapper

    def roles_required(*allowed_roles):
        def decorator(f):
            @wraps(f)
            def wrapper(*args, **kwargs):
                if not session.get("user_id"):
                    flash("Por favor inicia sesión para continuar.", "warning")
                    return redirect(url_for("login_html", next=request.path))
                role = session.get("user_role")
                if role not in allowed_roles:
                    flash("No tienes permisos para ver esta página.", "danger")
                    return redirect(url_for(role_redirect_endpoint(role) if role else "index_html"))
                return f(*args, **kwargs)
            return wrapper
        return decorator

    return app


# =========================
# EJECUCIÓN
# =========================
if __name__ == "__main__":
    app = create_app()
    app.run(host="0.0.0.0", port=5000, debug=True)
