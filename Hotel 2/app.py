# app.py — Aplicación principal Flask para Hotel_VillaGrace
# Ejecuta:  python "Hotel 2/app.py"

import os
import sys
from datetime import datetime
from pathlib import Path
from functools import wraps
from flask import (
    Flask, render_template, jsonify,
    request, redirect, url_for, flash, session
)
from sqlalchemy import text
from werkzeug.security import generate_password_hash, check_password_hash

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


# =========================
# MODELOS
# =========================
# Modelo mapeado a tu tabla real "Usuario"
class Usuario(db.Model):
    __tablename__ = "Usuario"  # EXACTO como en tu esquema SQL

    Codigo_Usuario     = db.Column(db.Integer, primary_key=True)
    Nombre             = db.Column(db.String(100), nullable=False)      # full_name
    Cedula_Pasaporte   = db.Column(db.String(40), unique=True)          # "username" práctico
    Correo             = db.Column(db.String(120), unique=True, index=True, nullable=False)  # email
    Telefono           = db.Column(db.String(25), nullable=False)
    Contrasena         = db.Column(db.String(255), nullable=False)      # password hash
    Rol                = db.Column(db.Enum('Administrador','Recepcionista','Limpieza','Cliente'), default='Cliente')
    Estado             = db.Column(db.Enum('Activo','Inactivo'), default='Activo')
    Fecha_Creacion     = db.Column(db.DateTime, default=datetime.utcnow)
    Fecha_Modificacion = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    # Helpers
    def set_password(self, raw: str):
        self.Contrasena = generate_password_hash(raw)

    def check_password(self, raw: str) -> bool:
        return check_password_hash(self.Contrasena, raw)


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

    # Blueprints
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
        # Para rutas del sitio web (no API)
        return render_template("404.html"), 404

    # ----------------------- Rutas de páginas estáticas ----------------------
    @app.route("/about.html")
    def about_html():
        return render_template("about.html")

    @app.route("/admin-audit.html")
    def admin_audit_html():
        return render_template("admin-audit.html")

    @app.route("/admin-calendario.html")
    def admin_calendario_html():
        return render_template("admin-calendario.html")

    @app.route("/admin-channels.html")
    def admin_channels_html():
        return render_template("admin-channels.html")

    @app.route("/admin-dashboard.html")
    def admin_dashboard_html():
        return render_template("admin-dashboard.html")

    @app.route("/admin-hotel.html")
    def admin_hotel_html():
        return render_template("admin-hotel.html")

    @app.route("/admin-rates.html")
    def admin_rates_html():
        return render_template("admin-rates.html")

    @app.route("/admin-reserva-detalle.html")
    def admin_reserva_detalle_html():
        return render_template("admin-reserva-detalle.html")

    @app.route("/admin-reservas-dashboard.html")
    def admin_reservas_dashboard_html():
        return render_template("admin-reservas-dashboard.html")

    @app.route("/admin-reservas-list.html")
    def admin_reservas_list_html():
        return render_template("admin-reservas-list.html")

    @app.route("/admin-rooms.html")
    def admin_rooms_html():
        return render_template("admin-rooms.html")

    @app.route("/admin-taxes.html")
    def admin_taxes_html():
        return render_template("admin-taxes.html")

    @app.route("/amenities.html")
    def amenities_html():
        return render_template("amenities.html")

    @app.route("/booking-checkout.html")
    def booking_checkout_html():
        return render_template("booking-checkout.html")

    @app.route("/booking-confirmation.html")
    def booking_confirmation_html():
        return render_template("booking-confirmation.html")

    @app.route("/booking-details.html")
    def booking_details_html():
        return render_template("booking-details.html")

    @app.route("/booking-results.html")
    def booking_results_html():
        return render_template("booking-results.html")

    @app.route("/booking-search.html")
    def booking_search_html():
        return render_template("booking-search.html")

    @app.route("/booking.html")
    def booking_html():
        return render_template("booking.html")

    @app.route("/contact.html")
    def contact_html():
        return render_template("contact.html")

    @app.route("/events.html")
    def events_html():
        return render_template("events.html")

    @app.route("/fin-close.html")
    def fin_close_html():
        return render_template("fin-close.html")

    @app.route("/fin-dashboard.html")
    def fin_dashboard_html():
        return render_template("fin-dashboard.html")

    @app.route("/fin-invoices.html")
    def fin_invoices_html():
        return render_template("fin-invoices.html")

    @app.route("/fin-payments.html")
    def fin_payments_html():
        return render_template("fin-payments.html")

    @app.route("/forgot-password.html")
    def forgot_password_html():
        return render_template("forgot-password.html")

    @app.route("/gallery.html")
    def gallery_html():
        return render_template("gallery.html")

    @app.route("/index.html")
    def index_html():
        return render_template("index.html")

    @app.route("/")
    def index():
        return render_template("index.html")

    @app.route("/location.html")
    def location_html():
        return render_template("location.html")

    @app.route("/offers.html")
    def offers_html():
        return render_template("offers.html")

    @app.route("/ops-arrivals-departures.html")
    def ops_arrivals_departures_html():
        return render_template("ops-arrivals-departures.html")

    @app.route("/ops-dashboard.html")
    def ops_dashboard_html():
        return render_template("ops-dashboard.html")

    @app.route("/ops-housekeeping.html")
    def ops_housekeeping_html():
        return render_template("ops-housekeeping.html")

    @app.route("/ops-incidents.html")
    def ops_incidents_html():
        return render_template("ops-incidents.html")

    @app.route("/ops-inventory.html")
    def ops_inventory_html():
        return render_template("ops-inventory.html")

    @app.route("/ops-maintenance.html")
    def ops_maintenance_html():
        return render_template("ops-maintenance.html")

    @app.route("/ops-reports.html")
    def ops_reports_html():
        return render_template("ops-reports.html")

    @app.route("/ops-rooms-status.html")
    def ops_rooms_status_html():
        return render_template("ops-rooms-status.html")

    @app.route("/ops-shift-log.html")
    def ops_shift_log_html():
        return render_template("ops-shift-log.html")

    @app.route("/portal-dashboard.html")
    def portal_dashboard_html():
        return render_template("portal-dashboard.html")

    @app.route("/portal-facturas.html")
    def portal_facturas_html():
        return render_template("portal-facturas.html")

    @app.route("/portal-pagos.html")
    def portal_pagos_html():
        return render_template("portal-pagos.html")

    @app.route("/portal-perfil.html")
    def portal_perfil_html():
        return render_template("portal-perfil.html")

    @app.route("/portal-preferencias.html")
    def portal_preferencias_html():
        return render_template("portal-preferencias.html")

    @app.route("/portal-reserva-detalle.html")
    def portal_reserva_detalle_html():
        return render_template("portal-reserva_detalle.html")

    @app.route("/portal-reservas.html")
    def portal_reservas_html():
        return render_template("portal-reservas.html")

    @app.route("/portal-soporte.html")
    def portal_soporte_html():
        return render_template("portal-soporte.html")

    @app.route("/privacy.html")
    def privacy_html():
        return render_template("privacy.html")

    @app.route("/restaurant.html")
    def restaurant_html():
        return render_template("restaurant.html")

    @app.route("/room-details.html")
    def room_details_html():
        return render_template("room-details.html")

    @app.route("/rooms.html")
    def rooms_html():
        return render_template("rooms.html")

    @app.route("/starter-page.html")
    def starter_page_html():
        return render_template("starter-page.html")

    @app.route("/terms.html")
    def terms_html():
        return render_template("terms.html")

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

            # Crear y guardar (Cliente por defecto, Activo)
            u = Usuario(
                Nombre=full_name,
                Cedula_Pasaporte=national_id or None,
                Correo=email,
                Telefono=phone,
                Rol='Cliente',
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
    def role_redirect_endpoint(rol: str) -> str:
        """
        Devuelve el endpoint de Flask al que redirigir según el rol.
        """
        mapping = {
            "Administrador": "admin_dashboard_html",
            "Recepcionista": "ops_dashboard_html",     # Panel de operaciones
            "Limpieza":      "ops_housekeeping_html",  # Housekeeping
            "Cliente":       "portal_dashboard_html",  # Portal del cliente
        }
        return mapping.get(rol, "index")

    @app.route("/login.html", methods=["GET", "POST"])
    def login_html():
        """
        Permite iniciar sesión usando:
        - Correo (Correo)  ó
        - "Nombre de usuario" práctico: Cédula/Pasaporte (Cedula_Pasaporte)
        """
        if request.method == "POST":
            # El input del formulario se llama "email" pero aceptamos email o cédula.
            identifier = (request.form.get("email") or "").strip().lower()
            password   = (request.form.get("password") or "").strip()

            # Busca por correo
            user = Usuario.query.filter_by(Correo=identifier).first()

            # Si no fue correo, intenta como "username": cédula/pasaporte (sin lower)
            if not user:
                user = Usuario.query.filter_by(Cedula_Pasaporte=(request.form.get("email") or "").strip()).first()

            if not user or user.Estado != 'Activo' or not user.check_password(password):
                flash("Credenciales inválidas o usuario inactivo.", "danger")
                return render_template("login.html")

            # Guardar sesión mínima
            session["user_id"] = user.Codigo_Usuario
            session["user_name"] = user.Nombre
            session["user_role"] = user.Rol

            flash(f"Bienvenido/a {user.Nombre}.", "success")
            return redirect(url_for(role_redirect_endpoint(user.Rol)))

        return render_template("login.html")

    # ---------------------------- Logout simple ------------------------------
    @app.route("/logout")
    def logout():
        session.clear()
        flash("Sesión cerrada correctamente.", "info")
        return redirect(url_for("index"))

    # ---------------------- Decorador opcional: login requerido --------------
    def login_required(f):
        @wraps(f)
        def wrapper(*args, **kwargs):
            if not session.get("user_id"):
                flash("Por favor inicia sesión para continuar.", "warning")
                return redirect(url_for("login_html"))
            return f(*args, **kwargs)
        return wrapper

    # Ejemplo de cómo proteger un panel (si lo deseas):
    # @app.route("/admin/panel")
    # @login_required
    # def admin_panel():
    #     if session.get("user_role") != "Administrador":
    #         flash("Acceso denegado.", "danger")
    #         return redirect(url_for("index"))
    #     return render_template("admin-dashboard.html")

    return app


# Instancia global
app = create_app()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=True)
