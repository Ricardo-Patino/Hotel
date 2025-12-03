from datetime import date
from flask import request, jsonify, render_template
from sqlalchemy import and_
from extensions import db
from models_sql import Habitacion, Reserva
from . import villas_bp

TIPOS_VALIDOS = ("Sencilla", "Doble", "Suite")


# ======================
#   VISTA HTML
# ======================
@villas_bp.route("/admin/villas.html", methods=["GET"], endpoint="admin_villas_html")
def admin_villas_html():
    return render_template("admin/villas.html")


# ======================
#   LISTAR HABITACIONES
# ======================
@villas_bp.get("/api/villas", endpoint="api_villas_list")
def api_villas_list():
    q = Habitacion.query.order_by(Habitacion.Numero_Habitacion.asc())
    data = [
        {
            "id": h.Codigo_Habitacion,
            "numero": h.Numero_Habitacion,
            "tipo": h.Tipo,
            "precio_noche": float(h.Precio_Noche or 0),
            "estado": h.Estado,
        }
        for h in q.all()
    ]
    return jsonify(data)


# ======================
#   CREAR HABITACIÓN
# ======================
@villas_bp.post("/api/villas", endpoint="api_villas_create")
def api_villas_create():
    payload = request.get_json(force=True) or {}

    numero = (payload.get("numero") or "").strip()
    tipo = (payload.get("tipo") or "").strip()
    precio = payload.get("precio_noche")
    estado = (payload.get("estado") or "Disponible").strip()

    if not numero or tipo not in TIPOS_VALIDOS:
        return jsonify({"ok": False, "msg": "Datos inválidos"}), 400

    if Habitacion.query.filter_by(Numero_Habitacion=numero).first():
        return jsonify({"ok": False, "msg": "El código ya existe"}), 409

    h = Habitacion(
        Numero_Habitacion=numero,
        Tipo=tipo,
        Precio_Noche=precio or 0,
        Estado=estado
    )
    db.session.add(h)
    db.session.commit()

    return jsonify({"ok": True, "id": h.Codigo_Habitacion})


# ======================
#   ACTUALIZAR HABITACIÓN
# ======================
@villas_bp.post("/api/villas/<int:villa_id>", endpoint="api_villas_update")
def api_villas_update(villa_id: int):
    h = Habitacion.query.get_or_404(villa_id)

    payload = request.get_json(force=True) or {}

    numero = (payload.get("numero") or h.Numero_Habitacion).strip()
    tipo = (payload.get("tipo") or h.Tipo).strip()
    precio = payload.get("precio_noche", h.Precio_Noche)
    estado = (payload.get("estado") or h.Estado).strip()

    if tipo not in TIPOS_VALIDOS:
        return jsonify({"ok": False, "msg": "Tipo inválido"}), 400

    if numero != h.Numero_Habitacion and \
       Habitacion.query.filter_by(Numero_Habitacion=numero).first():
        return jsonify({"ok": False, "msg": "El código ya existe"}), 409

    h.Numero_Habitacion = numero
    h.Tipo = tipo
    h.Precio_Noche = precio
    h.Estado = estado

    db.session.commit()
    return jsonify({"ok": True})


# ======================
#   ELIMINAR HABITACIÓN
# ======================
@villas_bp.delete("/api/villas/<int:villa_id>", endpoint="api_villas_delete")
def api_villas_delete(villa_id: int):
    h = Habitacion.query.get_or_404(villa_id)

    hoy = date.today()
    solapes = Reserva.query.filter(
        Reserva.Codigo_Habitacion == villa_id,
        and_(Reserva.Fecha_Salida > hoy, Reserva.Estado != "Cancelada")
    ).count()

    if solapes:
        return jsonify({
            "ok": False,
            "msg": "La habitación tiene reservas activas o futuras"
        }), 409

    db.session.delete(h)
    db.session.commit()
    return jsonify({"ok": True})
