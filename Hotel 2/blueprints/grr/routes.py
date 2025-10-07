# blueprints/grr/routes.py
# Rutas HTTP para Gestión de Reservas (GRR-01-001)

from __future__ import annotations

from datetime import date
from typing import Any, Dict, Tuple, List, Optional

from flask import Blueprint, jsonify, request
from sqlalchemy import func, select

from extensions import db
from models_sql import Habitacion, Reserva
from services.grr.reservation_service import ReservationService

# Se registra con: app.register_blueprint(grr_bp, url_prefix="/grr")
grr_bp = Blueprint("grr", __name__)
_res_service = ReservationService()


# -------------------------- Utilidades comunes -------------------------------

def _normalize_dates(payload: Dict[str, Any], keys=("Fecha_Entrada", "Fecha_Salida")) -> None:
    for k in keys:
        if k in payload and isinstance(payload[k], str) and payload[k]:
            payload[k] = date.fromisoformat(payload[k])


def _resp(data: Dict[str, Any], status_ok: int = 200, status_err: int = 400) -> Tuple[Any, int]:
    return jsonify(data), (status_ok if data.get("ok") else status_err)


def _attr(model, candidates: List[str]) -> Optional[Any]:
    """Devuelve el primer atributo existente del modelo según una lista de nombres."""
    for c in candidates:
        if hasattr(model, c):
            return getattr(model, c)
    return None


def _precio_noche(h: Habitacion) -> float:
    # Intenta varios nombres posibles
    for c in ("Precio_Noche", "Precio_Base", "Precio", "Tarifa_Base"):
        try:
            v = getattr(h, c)
            if v is not None:
                return float(v or 0)
        except Exception:
            continue
    return 0.0


# ------------------------------- Health --------------------------------------

@grr_bp.get("/ping")
def ping():
    return jsonify({"ok": True, "scope": "grr"}), 200


# --------------------------- Disponibilidad (v1) -----------------------------

@grr_bp.get("/availability")
@grr_bp.get("/disponibilidad")
def disponibilidad():
    """
    Query:
      ?checkin=YYYY-MM-DD&checkout=YYYY-MM-DD&adults=2&children=0
    Reglas:
      - Excluye habitaciones en 'Mantenimiento' si existe la columna Estado en Habitacion
      - Excluye habitaciones con reservas solapadas en el rango [checkin, checkout),
        ignorando reservas en 'Cancelada' si Reserva.Estado existe.
      - Filtra por capacidad mínima si hay columna de capacidad.
    """
    try:
        checkin = request.args.get("checkin")
        checkout = request.args.get("checkout")
        adults = int(request.args.get("adults", "2") or 2)
        children = int(request.args.get("children", "0") or 0)
        total_pax = max(1, adults + children)

        # Validación suave de fechas (si vienen)
        try:
            if checkin:
                date.fromisoformat(checkin)
            if checkout:
                date.fromisoformat(checkout)
        except ValueError:
            return jsonify({"ok": False, "msg": "Fechas inválidas (YYYY-MM-DD)"}), 400

        q = Habitacion.query

        # Estado != 'Mantenimiento' si la columna existe
        hab_estado = _attr(Habitacion, ["Estado"])
        if hab_estado is not None:
            q = q.filter(hab_estado != "Mantenimiento")

        # Capacidad >= pax si hay columna de capacidad
        hab_cap = _attr(Habitacion, ["Capacidad", "Capacidad_Maxima", "Capacidad_Huespedes"])
        if hab_cap is not None:
            q = q.filter(func.coalesce(hab_cap, 2) >= int(total_pax))

        # Excluir habitaciones con reservas solapadas usando SELECT (compat 1.4/2.x)
        if checkin and checkout:
            ini = date.fromisoformat(checkin)
            fin = date.fromisoformat(checkout)

            res_estado = _attr(Reserva, ["Estado"])
            res_cod_hab = _attr(Reserva, ["Codigo_Habitacion"])
            res_fini = _attr(Reserva, ["Fecha_Entrada"])
            res_ffin = _attr(Reserva, ["Fecha_Salida"])

            filters = []
            if res_fini is not None and res_ffin is not None:
                filters += [res_fini < fin, res_ffin > ini]
            if res_estado is not None:
                filters.append(res_estado != "Cancelada")

            if res_cod_hab is not None and filters:
                subq = select(res_cod_hab).filter(*filters)
                q = q.filter(~Habitacion.Codigo_Habitacion.in_(subq))

        # Orden estable
        q = q.order_by(Habitacion.Codigo_Habitacion.asc())

        rooms: List[Dict[str, Any]] = []
        for h in q.all():
            rooms.append({
                "code": int(getattr(h, "Codigo_Habitacion")),
                "name": getattr(h, "Nombre", None) or f"Habitación {getattr(h, 'Codigo_Habitacion')}",
                "tipo": getattr(h, "Tipo", None) or "",
                "desc": getattr(h, "Descripcion", None) or (getattr(h, "Tipo", None) or ""),
                "capacity": int(getattr(h, "Capacidad", None) or 2),
                "price": _precio_noche(h),
                "img": getattr(h, "Imagen_URL", None) or "",
                "rateName": "Tarifa Flexible",
                "cancel": "Cancela gratis hasta 7 días antes.",
                "meal": "—",
            })

        return jsonify({
            "ok": True,
            "rooms": rooms,
            "params": {"checkin": checkin, "checkout": checkout, "adults": adults, "children": children}
        })

    except Exception as e:
        # En vez de 500, devolvemos un error legible para poder depurar rápido
        return jsonify({"ok": False, "msg": f"Error en disponibilidad: {e}"}), 200


# ------------------------------- Reservas ------------------------------------

@grr_bp.post("/reservas")
def crear_reserva():
    try:
        payload = request.get_json(silent=True) or {}
        _normalize_dates(payload)
        res = _res_service.create(payload)
        return jsonify(res), (201 if res.get("ok") else 409)
    except Exception as e:
        return jsonify({"ok": False, "msg": f"Error inesperado: {e}"}), 500


@grr_bp.get("/reservas")
def listar_reservas():
    try:
        filtros = {
            "estado": request.args.get("estado") or None,
            "canal": request.args.get("canal") or None,
            "fuente": request.args.get("fuente") or None,
            "desde": request.args.get("desde") or None,
            "hasta": request.args.get("hasta") or None,
        }
        page = int(request.args.get("page", 1))
        page_size = int(request.args.get("page_size", 20))
        res = _res_service.list(filters=filtros, page=page, page_size=page_size)
        return _resp(res, status_ok=200, status_err=400)
    except Exception as e:
        return jsonify({"ok": False, "msg": f"Error inesperado: {e}"}), 500


@grr_bp.get("/reservas/<int:reserva_id>")
def obtener_reserva(reserva_id: int):
    try:
        res = _res_service.get(reserva_id)
        return _resp(res, status_ok=200, status_err=404)
    except Exception as e:
        return jsonify({"ok": False, "msg": f"Error inesperado: {e}"}), 500


@grr_bp.put("/reservas/<int:reserva_id>")
def actualizar_reserva(reserva_id: int):
    try:
        payload = request.get_json(silent=True) or {}
        _normalize_dates(payload)
        res = _res_service.update(reserva_id, payload)
        code = 200 if res.get("ok") else (409 if res.get("code") == "conflict" else 404)
        return jsonify(res), code
    except Exception as e:
        return jsonify({"ok": False, "msg": f"Error inesperado: {e}"}), 500


@grr_bp.post("/reservas/<int:reserva_id>/cancel")
def cancelar_reserva(reserva_id: int):
    try:
        data = request.get_json(silent=True) or {}
        motivo = data.get("motivo", "Cancelación manual")
        usuario = data.get("Usuario")
        res = _res_service.cancel(reserva_id, motivo, usuario)
        return _resp(res, status_ok=200, status_err=404)
    except Exception as e:
        return jsonify({"ok": False, "msg": f"Error inesperado: {e}"}), 500


@grr_bp.delete("/reservas/<int:reserva_id>")
def eliminar_reserva(reserva_id: int):
    try:
        data = request.get_json(silent=True) or {}
        motivo = data.get("motivo", "Cancelación manual")
        usuario = data.get("Usuario")
        res = _res_service.cancel(reserva_id, motivo, usuario)
        return _resp(res, status_ok=200, status_err=404)
    except Exception as e:
        return jsonify({"ok": False, "msg": f"Error inesperado: {e}"}), 500


# ---------------------- Errores locales del blueprint ------------------------

@grr_bp.app_errorhandler(404)
def grr_not_found(e):
    return jsonify({"ok": False, "error": "Not Found", "path": request.path}), 404


@grr_bp.app_errorhandler(405)
def grr_method_not_allowed(e):
    return jsonify({"ok": False, "error": "Method Not Allowed", "path": request.path}), 405
