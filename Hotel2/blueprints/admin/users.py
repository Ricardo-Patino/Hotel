from flask import request, jsonify
from . import admin_bp
from models.security import Usuario, Rol
from extensions import db
from utils.auth import role_required
from flask import request, jsonify, session


@admin_bp.patch("/usuarios/<int:user_id>/rol")
def cambiar_rol_usuario(user_id):
    # 🔐 VALIDACIÓN MÍNIMA
    if session.get("user_role") != "Administrador":
        return jsonify({"ok": False, "error": "No autorizado"}), 403

    payload = request.get_json(silent=True) or {}
    rol_nombre = payload.get("rol")

    if not rol_nombre:
        return jsonify({"ok": False, "error": "Rol requerido"}), 400

    user = Usuario.query.get(user_id)
    if not user:
        return jsonify({"ok": False, "error": "Usuario no encontrado"}), 404

    rol = Rol.query.filter_by(Nombre=rol_nombre).first()
    if not rol:
        return jsonify({"ok": False, "error": "Rol inválido"}), 400

    user.Rol_Id = rol.Codigo_Rol
    db.session.commit()

    return jsonify({"ok": True})