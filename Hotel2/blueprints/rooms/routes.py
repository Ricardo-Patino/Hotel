import logging
from typing import Literal

from extensions import db
from flask import Response, jsonify, request
from models_sql import Habitacion
from sqlalchemy.exc import DataError, IntegrityError

from . import rooms_bp

logging.basicConfig(
    level=logging.DEBUG,
    format="%(asctime)s - %(levelname)s -%(filename)s:%(lineno)d - %(message)s",
)


def serialize(habitacion: Habitacion) -> dict:
    """Serialize an SQLAlchemy instance into a plain dict."""
    return {column.name: str(getattr(habitacion, column.name)) for column in habitacion.__table__.columns}


def _create(payload: dict) -> Habitacion:
    habitacion = Habitacion(**payload)
    db.session.add(habitacion)
    db.session.commit()
    db.session.flush()
    return habitacion


def _read() -> list[Habitacion]:
    """Read a list of habitaciones from DB."""
    return Habitacion.query.all()


def _update(room_id: str, payload: dict) -> Habitacion | None:
    habitacion = Habitacion.query.get(room_id)
    if not habitacion:
        return None

    [setattr(habitacion, key, value) for key, value in payload.items()]
    db.session.add(habitacion)
    db.session.commit()
    db.session.flush()
    return habitacion


def _delete(room_id: str) -> Habitacion | None:
    habitacion = Habitacion.query.get(room_id)
    if not habitacion:
        return None

    habitacion.query.filter_by(Codigo_Habitacion=room_id).delete()

    db.session.commit()
    db.session.flush()
    return habitacion


@rooms_bp.route("/admin-rooms", methods=["POST"])
def rooms_create() -> Response:
    """Create a room."""
    payload = request.get_json()
    try:
        habitacion = _create(payload)
    except (DataError, IntegrityError):
        error = "Hubo un problema creando la habitación."
        logging.exception(error)
        return jsonify({"error": error})
    return jsonify(serialize(habitacion))


@rooms_bp.route("/admin-rooms", methods=["GET"])
def rooms_read() -> Response:
    """Read all the habitaciones from DB."""
    habitaciones = _read()
    return jsonify([serialize(h) for h in habitaciones])


@rooms_bp.route("/admin-rooms/<int:room_id>", methods=["PUT"])
def rooms_update(room_id: str) -> tuple[Response, Literal[404]] | Response:
    """Update a habitacion given an incoming payload."""
    payload = request.get_json()
    try:
        habitacion = _update(room_id, payload)
    except (DataError, IntegrityError):
        error = "Hubo un problema actualizando la habitación."
        logging.exception(error)
        return jsonify({"error": error})
    if not habitacion:
        return jsonify({"ok": False, "error": "Habitacion no encontrada"}), 404

    return jsonify(serialize(habitacion))


@rooms_bp.route("/admin-rooms/<int:room_id>", methods=["DELETE"])
def rooms_delete(room_id: str) -> tuple[Response, Literal[404]] | Response:
    """Delete habitacion from DB."""
    try:
        habitacion = _delete(room_id)
    except (DataError, IntegrityError):
        error = "Hubo un problema borrando la habitación."
        logging.exception(error)
        return jsonify({"error": error})
    if not habitacion:
        return jsonify({"ok": False, "error": "Habitacion no encontrada"}), 404

    return jsonify(serialize(habitacion))
