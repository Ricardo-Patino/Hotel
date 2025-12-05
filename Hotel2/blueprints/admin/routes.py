from flask import render_template
from . import admin_bp
from utils.auth import role_required

@admin_bp.route("/", methods=["GET"])
@role_required("Administrador")
def dashboard():
    return render_template("admin/dashboard.html")

@admin_bp.route("/admin-hotel.html", methods=["GET"])
@role_required("Administrador")
def dashboard():
    return render_template("admin/admin-hotel.html")
