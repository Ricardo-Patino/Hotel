# app.py — Aplicación principal Flask para Hotel_VillaGrace
# Ejecuta:  python "Hotel 2/app.py"

import os
import sys
import smtplib
import ssl
from email.message import EmailMessage
from datetime import datetime
from pathlib import Path
from functools import wraps

from flask import (
    Flask, render_template, jsonify,
    request, redirect, url_for, flash, session
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
from models_sql import Usuario, Rol  # 👈 Modelos solo aquí (no se redeclaran)


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
    """Devuelve el nombre del rol del usuario o 'Cliente' por defecto si no hay relación."""
    return user.rol.Nombre if getattr(user, "rol", None) else "Cliente"

def role_redirect_endpoint(role_name: str) -> str:
    mapping = {
        "Administrador": "admin_dashboard_html",
        "Recepcionista": "ops_dashboard_html",
        "Limpieza":      "ops_housekeeping_html",
        "Cliente":       "portal_dashboard_html",
    }
    return mapping.get(role_name, "index")


# =========================
# Reset Password (GPU-01-004)
# =========================
def _get_serializer(app: Flask) -> URLSafeTimedSerializer:
    # Se basa en SECRET_KEY y un sal adicional para separar usos
    return URLSafeTimedSerializer(app.config["SECRET_KEY"], salt="pwd-reset")

def _build_reset_link(token: str) -> str:
    # Este helper es env-independent, lo sustituimos dentro del app_context
    return token

def _send_reset_email(app: Flask, to_email: str, reset_url: str) -> None:
    """Envía email con enlace o hace fallback a consola."""
    
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

    # Validar config mínima
    if not (host and port and user and pwd):
        app.logger.warning("[MAIL] Config SMTP incompleta; usando consola.")
        app.logger.info(f"[RESET LINK] Para {to_email}: {reset_url}")
        return

    # Forzar From coherente (muchos proveedores lo exigen)
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
        # Si falla, registra el motivo y muestra también el enlace en consola
        app.logger.error(f"[MAIL ERROR] {type(e).__name__}: {e}")
        app.logger.info(f"[RESET LINK] Para {to_email}: {reset_url}")



# =========================
# FACTORY
# =========================
def create_app() -> Flask:
    app = Flask(
        __name__,
        template_folder=str(BASE_DIR / "templates"),
        static_folder=str(BASE_DIR / "static"),
    )
    app.config.from_object(Config)

    # Extensiones
    db.init_app(app)
    migrate.init_app(app, db)

    # Seed de roles en arranque de app (dentro de contexto)
    with app.app_context():
        try:
            _ensure_seed_roles()
        except Exception:
            pass

    # Blueprints (GRR)
    from blueprints.grr.routes import grr_bp
    app.register_blueprint(grr_bp, url_prefix="/grr")

    # ---------------------------- Endpoints de salud -------------------------
    @app.get("/health")
    def health():
        return {"ok": True, "app": "Hotel_VillaGrace"}

    @app.get("/db-ping")
    def db_ping():
        try:
            result = db.session.execute(text("SELECT 1")).scalar()
            ok = (result == 1)
            return jsonify({"database": "ok" if ok else "unknown"}), 200 if ok else 500
        except Exception as e:
            return jsonify({"database": "error", "detail": str(e)}), 500

    # --------------------------- Manejo de errores web -----------------------
    @app.errorhandler(404)
    def not_found(e):
        return render_template("404.html"), 404

    # ----------------------- Rutas de páginas estáticas ----------------------
    @app.route("/about.html")
    def about_html(): return render_template("about.html")

    @app.route("/admin-audit.html")
    def admin_audit_html(): return render_template("admin-audit.html")

    @app.route("/admin-calendario.html")
    def admin_calendario_html(): return render_template("admin-calendario.html")

    @app.route("/admin-channels.html")
    def admin_channels_html(): return render_template("admin-channels.html")

    @app.route("/admin-dashboard.html")
    def admin_dashboard_html(): return render_template("admin-dashboard.html")

    @app.route("/admin-hotel.html")
    def admin_hotel_html(): return render_template("admin-hotel.html")

    @app.route("/admin-rates.html")
    def admin_rates_html(): return render_template("admin-rates.html")

    @app.route("/admin-reserva-detalle.html")
    def admin_reserva_detalle_html(): return render_template("admin-reserva-detalle.html")

    @app.route("/admin-reservas-dashboard.html")
    def admin_reservas_dashboard_html(): return render_template("admin-reservas-dashboard.html")

    @app.route("/admin-reservas-list.html")
    def admin_reservas_list_html(): return render_template("admin-reservas-list.html")

    @app.route("/admin-rooms.html")
    def admin_rooms_html(): return render_template("admin-rooms.html")

    @app.route("/admin-taxes.html")
    def admin_taxes_html(): return render_template("admin-taxes.html")

    @app.route("/amenities.html")
    def amenities_html(): return render_template("amenities.html")

    @app.route("/booking-checkout.html")
    def booking_checkout_html(): return render_template("booking-checkout.html")

    @app.route("/booking-confirmation.html")
    def booking_confirmation_html(): return render_template("booking-confirmation.html")

    @app.route("/booking-details.html")
    def booking_details_html(): return render_template("booking-details.html")

    @app.route("/booking-results.html")
    def booking_results_html(): return render_template("booking-results.html")

    @app.route("/booking-search.html")
    def booking_search_html(): return render_template("booking-search.html")

    @app.route("/booking.html")
    def booking_html(): return render_template("booking.html")

    @app.route("/contact.html")
    def contact_html(): return render_template("contact.html")

    @app.route("/events.html")
    def events_html(): return render_template("events.html")

    @app.route("/fin-close.html")
    def fin_close_html(): return render_template("fin-close.html")

    @app.route("/fin-dashboard.html")
    def fin_dashboard_html(): return render_template("fin-dashboard.html")

    @app.route("/fin-invoices.html")
    def fin_invoices_html(): return render_template("fin-invoices.html")

    @app.route("/fin-payments.html")
    def fin_payments_html(): return render_template("fin-payments.html")

    # --------- GPU-01-004: Recuperar contraseña (GET/POST mismo template) ----
    @app.route("/forgot-password", methods=["GET", "POST"])
    def forgot_password():
        if request.method == "POST":
            email = (request.form.get("email") or "").strip().lower()
            # Mensaje genérico por seguridad (no revelar si existe o no)
            generic_msg = "Si el correo existe, te hemos enviado un enlace para restablecer tu contraseña."

            if not email or "@" not in email:
                flash(generic_msg, "info")
                return render_template("forgot-password.html")

            user = Usuario.query.filter_by(Correo=email).first()
            # Generar token aunque no exista (mismo tiempo de respuesta)
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

        # GET
        return render_template("forgot-password.html")

    # Compatibilidad con /forgot-password.html (render simple)
    @app.route("/forgot-password.html", methods=["GET"])
    def forgot_password_html():
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
            # No revelar nada específico
            flash("El enlace no es válido o expiró. Solicita uno nuevo.", "danger")
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

            # Actualizar y guardar
            user.set_password(pwd1)
            db.session.commit()

            # Opcional: cerrar sesión actual por seguridad
            if session.get("user_id") == user.Codigo_Usuario:
                session.clear()

            flash("Tu contraseña fue actualizada. Ya puedes iniciar sesión.", "success")
            return redirect(url_for("login_html"))

        # GET: mostrar formulario de nueva contraseña
        return render_template("reset-password.html", token=token, email=email)

    @app.route("/gallery.html")
    def gallery_html(): return render_template("gallery.html")

    @app.route("/index.html")
    def index_html(): return render_template("index.html")

    @app.route("/")
    def index(): return render_template("index.html")

    @app.route("/location.html")
    def location_html(): return render_template("location.html")

    @app.route("/offers.html")
    def offers_html(): return render_template("offers.html")

    @app.route("/ops-arrivals-departures.html")
    def ops_arrivals_departures_html(): return render_template("ops-arrivals-departures.html")

    @app.route("/ops-dashboard.html")
    def ops_dashboard_html(): return render_template("ops-dashboard.html")

    @app.route("/ops-housekeeping.html")
    def ops_housekeeping_html(): return render_template("ops-housekeeping.html")

    @app.route("/ops-incidents.html")
    def ops_incidents_html(): return render_template("ops-incidents.html")

    @app.route("/ops-inventory.html")
    def ops_inventory_html(): return render_template("ops-inventory.html")

    @app.route("/ops-maintenance.html")
    def ops_maintenance_html(): return render_template("ops-maintenance.html")

    @app.route("/ops-reports.html")
    def ops_reports_html(): return render_template("ops-reports.html")

    @app.route("/ops-rooms-status.html")
    def ops_rooms_status_html(): return render_template("ops-rooms-status.html")

    @app.route("/ops-shift-log.html")
    def ops_shift_log_html(): return render_template("ops-shift-log.html")

    @app.route("/portal-dashboard.html")
    def portal_dashboard_html(): return render_template("portal-dashboard.html")

    @app.route("/portal-facturas.html")
    def portal_facturas_html(): return render_template("portal-facturas.html")

    @app.route("/portal-pagos.html")
    def portal_pagos_html(): return render_template("portal-pagos.html")

    @app.route("/portal-perfil.html")
    def portal_perfil_html(): return render_template("portal-perfil.html")

    @app.route("/portal-preferencias.html")
    def portal_preferencias_html(): return render_template("portal-preferencias.html")

    @app.route("/portal-reserva-detalle.html")
    def portal_reserva_detalle_html(): return render_template("portal-reserva_detalle.html")

    @app.route("/portal-reservas.html")
    def portal_reservas_html(): return render_template("portal-reservas.html")

    @app.route("/portal-soporte.html")
    def portal_soporte_html(): return render_template("portal-soporte.html")

    @app.route("/privacy.html")
    def privacy_html(): return render_template("privacy.html")

    @app.route("/restaurant.html")
    def restaurant_html(): return render_template("restaurant.html")

    @app.route("/room-details.html")
    def room_details_html(): return render_template("room-details.html")

    @app.route("/rooms.html")
    def rooms_html(): return render_template("rooms.html")

    @app.route("/starter-page.html")
    def starter_page_html(): return render_template("starter-page.html")

    @app.route("/terms.html")
    def terms_html(): return render_template("terms.html")

    # ---------------------- REGISTRO DE USUARIOS (HU GPU-01-001) -------------
    @app.route("/register", methods=["GET", "POST"])
    def register():
        if request.method == "POST":
            full_name   = (request.form.get("full_name") or "").strip()
            national_id = (request.form.get("national_id") or "").strip()
            email       = (request.form.get("email") or "").strip().lower()
            phone       = (request.form.get("phone") or "").strip()
            password    = (request.form.get("password") or "").strip()
            confirm     = (request.form.get("confirm_password") or "").strip()

            # Validaciones mínimas
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

            # Unicidades según tu esquema
            if Usuario.query.filter_by(Correo=email).first():
                flash("El correo ya está registrado.", "danger")
                return render_template("register.html")
            if national_id and Usuario.query.filter_by(Cedula_Pasaporte=national_id).first():
                flash("La cédula/pasaporte ya está registrada.", "danger")
                return render_template("register.html")

            # Rol por defecto: Cliente (FK)
            role = _get_role_by_name("Cliente")
            if not role:
                _ensure_seed_roles()
                role = _get_role_by_name("Cliente")

            # Crear y guardar
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

    # Compatibilidad con /register.html (GET)
    @app.route("/register.html", methods=["GET"])
    def register_html():
        return render_template("register.html")

    # ------------------ GPU-01-003: LOGIN por correo o "username" ------------
    @app.route("/login.html", methods=["GET", "POST"])
    def login_html():
        """
        Permite iniciar sesión usando:
        - Correo (Correo)  ó
        - "Nombre de usuario" práctico: Cédula/Pasaporte (Cedula_Pasaporte)
        """
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

            session["user_id"] = user.Codigo_Usuario
            session["user_name"] = user.Nombre
            session["user_role"] = role_name

            flash(f"Bienvenido/a {user.Nombre}.", "success")
            return redirect(url_for(role_redirect_endpoint(role_name)))

        return render_template("login.html")

    # ---------------------------- Logout simple ------------------------------
    @app.route("/logout")
    def logout():
        session.clear()
        flash("Sesión cerrada correctamente.", "info")
        return redirect(url_for("index"))

    # ---------------------- Decoradores de protección ------------------------
    def login_required(f):
        @wraps(f)
        def wrapper(*args, **kwargs):
            if not session.get("user_id"):
                flash("Por favor inicia sesión para continuar.", "warning")
                return redirect(url_for("login_html"))
            return f(*args, **kwargs)
        return wrapper

    def roles_required(*allowed_roles):
        def decorator(f):
            @wraps(f)
            def wrapper(*args, **kwargs):
                if not session.get("user_id"):
                    flash("Por favor inicia sesión para continuar.", "warning")
                    return redirect(url_for("login_html"))
                role = session.get("user_role")
                if role not in allowed_roles:
                    flash("No tienes permisos para ver esta página.", "danger")
                    return redirect(url_for(role_redirect_endpoint(role) if role else "index"))
                return f(*args, **kwargs)
            return wrapper
        return decorator

    return app


# Instancia global
app = create_app()

if __name__ == "__main__":
    # Evitar doble proceso del reloader si te molesta con SQLAlchemy
    app.run(host="0.0.0.0", port=5000, debug=True)
