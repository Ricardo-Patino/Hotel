from flask import render_template
from . import admin_bp
from utils.auth import role_required

@admin_bp.route("/", methods=["GET"])
@role_required("Administrador")
def dashboard():
    return render_template("admin/dashboard.html")

# --- Villas (única definición) ---
#@admin_bp.route("/villas.html", methods=["GET"], endpoint="villas_html")
#@role_required("Administrador")
#def admin_villas_html():
#    return render_template("admin/villas.html")

@admin_bp.route("/villas.html", methods=["GET"], endpoint="villas_html")
def admin_villas_html():
    return render_template("admin/villas.html")

