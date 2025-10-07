# app.py — Aplicación principal Flask para Hotel_VillaGrace
# Ejecuta:  python "Hotel 2/app.py"

import os
import sys
from pathlib import Path
from flask import Flask, render_template, jsonify
from sqlalchemy import text

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
    # ✅ Prefijo SOLO aquí
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

    @app.route("/admin-users.html")
    def admin_users_html():
        return render_template("admin-users.html")

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

    @app.route("/login.html")
    def login_html():
        return render_template("login.html")

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
        return render_template("portal-reserva-detalle.html")

    @app.route("/portal-reservas.html")
    def portal_reservas_html():
        return render_template("portal-reservas.html")

    @app.route("/portal-soporte.html")
    def portal_soporte_html():
        return render_template("portal-soporte.html")

    @app.route("/privacy.html")
    def privacy_html():
        return render_template("privacy.html")

    @app.route("/register.html")
    def register_html():
        return render_template("register.html")

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

    return app


# Instancia global
app = create_app()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=True)
