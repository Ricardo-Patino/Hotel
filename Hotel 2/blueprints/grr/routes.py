# blueprints/grr/routes.py
# Rutas HTTP para Gestión de Reservas (GRR-01-001)

from __future__ import annotations

from datetime import date
from typing import Any, Dict, Tuple, List, Optional

from flask import Blueprint, jsonify, request, session, current_app, render_template
from sqlalchemy import func, select, text

from extensions import db
from models_sql import (
    Habitacion,
    Reserva,
    Usuario,
)  # <- usamos Usuario para resolver el cliente
from services.grr.reservation_service import ReservationService

# Se registra con: app.register_blueprint(grr_bp, url_prefix="/grr")
grr_bp = Blueprint("grr", __name__)
_res_service = ReservationService()


# -------------------------- Utilidades comunes -------------------------------


def _normalize_dates(
    payload: Dict[str, Any], keys=("Fecha_Entrada", "Fecha_Salida")
) -> None:
    for k in keys:
        if k in payload and isinstance(payload[k], str) and payload[k]:
            payload[k] = date.fromisoformat(payload[k])


def _resp(
    data: Dict[str, Any], status_ok: int = 200, status_err: int = 400
) -> Tuple[Any, int]:
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

# ======================= Cálculo autoritativo de totales ======================
from decimal import Decimal, ROUND_HALF_UP
from datetime import datetime

VAT_RATE = Decimal("0.13")      # 13%
NRB_DISCOUNT = Decimal("0.10")  # 10% descuento para tarifa No Reembolsable

def _d2(v) -> Decimal:
    if isinstance(v, Decimal):
        d = v
    else:
        try:
            d = Decimal(str(v or "0"))
        except Exception:
            d = Decimal("0")
    return d.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)

def _to_date(v):
    if isinstance(v, date):
        return v
    if not v:
        return None
    try:
        s = str(v).strip().replace("/", "-")
        return date.fromisoformat(s)
    except Exception:
        try:
            # fallback amplio "YYYY-MM-DDTHH:MM..."
            return datetime.fromisoformat(str(v)).date()
        except Exception:
            return None

def _safe_int(v, default=0):
    try:
        if v is None:
            return default
        s = str(v).strip()
        if not s:
            return default
        return int(Decimal(s))
    except Exception:
        return default

def _nights(ci: date, co: date) -> int:
    try:
        n = (co - ci).days
        return max(1, n)
    except Exception:
        return 1

def _lookup_price_per_guest_night(payload: Dict[str, Any]) -> Decimal:
    """
    Regla:
    1) Si viene Precio_Noche => usarlo como 'precio por huésped/noche'.
    2) Si viene Codigo_Habitacion => buscar Habitacion y tomar Precio_Noche (o Precio_Base).
    3) Si viene Tipo => tomar una hab. de ese tipo.
    4) Fallback 0.
    Si la tarifa es NRB => aplicar -10%.
    """
    rate_code = (str(payload.get("Tarifa_Codigo") or payload.get("rateCode") or payload.get("rate") or "flex").strip().lower())
    raw_price = payload.get("Precio_Noche")

    if raw_price:
        price = _d2(raw_price)
    else:
        # Buscar por habitación o tipo
        price = Decimal("0.00")
        try:
            room_code = payload.get("Codigo_Habitacion") or payload.get("roomCode")
            if room_code:
                h = Habitacion.query.filter(
                    (Habitacion.Codigo_Habitacion == _safe_int(room_code)) |
                    (Habitacion.Numero_Habitacion == str(room_code))
                ).first()
            else:
                tipo = payload.get("Tipo") or payload.get("tipo")
                h = Habitacion.query.filter(Habitacion.Tipo.ilike(f"%{tipo}%")).first() if tipo else None

            if h:
                # Usa _precio_noche existente (ya prueba varios campos)
                price = _d2(_precio_noche(h))
        except Exception:
            price = Decimal("0.00")

    # Descuento NRB
    if rate_code == "nrb" and price > 0:
        price = _d2(price * (Decimal("1.00") - NRB_DISCOUNT))

    return price

def _calc_totales(payload: Dict[str, Any]) -> Dict[str, Any]:
    """
    Calcula TODO del lado servidor (autoridad):
    - adultos, niños, huéspedes
    - noches
    - precio por huésped/noche (con NRB si aplica)
    - subtotal, impuesto, total
    """
    ci = _to_date(payload.get("Fecha_Entrada"))
    co = _to_date(payload.get("Fecha_Salida"))
    if not (ci and co):
        raise ValueError("Fechas inválidas (Fecha_Entrada / Fecha_Salida).")

    noches = _nights(ci, co)
    ad = _safe_int(payload.get("Huespedes_Adultos"), 0)
    ch = _safe_int(payload.get("Huespedes_Ninos"), 0)
    tot = _safe_int(payload.get("Huespedes"), 0)
    huespedes = tot if tot > 0 else (ad + ch if (ad + ch) > 0 else 1)

    price = _lookup_price_per_guest_night(payload)

    subtotal = _d2(price * noches * huespedes)
    impuesto = _d2(subtotal * VAT_RATE)
    total = _d2(subtotal + impuesto)

    return {
        "adultos": ad,
        "ninos": ch,
        "huespedes": huespedes,
        "noches": noches,
        "price_per_guest_night": price,
        "subtotal": subtotal,
        "impuesto": impuesto,
        "total": total,
        "checkin": ci,
        "checkout": co,
        "rate_code": str(payload.get("Tarifa_Codigo") or payload.get("rateCode") or payload.get("rate") or "flex").lower(),
        "rate_name": str(payload.get("Tarifa_Nombre") or payload.get("rateName") or ("No Reembolsable" if (str(payload.get("Tarifa_Codigo") or '').lower()=="nrb") else "Tarifa Flexible")),
        "es_nrb": (str(payload.get("Tarifa_Codigo") or payload.get("rateCode") or '').lower()=="nrb"),
    }

def _set_if(obj, field: str, value):
    """Asigna sólo si el atributo existe en el modelo, para evitar AttributeError."""
    if hasattr(obj, field):
        setattr(obj, field, value)


# ---------------------- Resolución de cliente desde la sesión -----------------


def _current_user_email_and_cliente() -> Tuple[Optional[str], Optional[int]]:
    """
    Devuelve (email, codigo_cliente) del usuario logueado.
    Si el usuario no tiene Codigo_Cliente, lo intenta resolver/crear por correo y lo vincula.
    """
    try:
        uid = session.get("user_id")
        if not uid:
            return None, None

        u: Optional[Usuario] = Usuario.query.filter_by(Codigo_Usuario=uid).first()
        if not u:
            return None, None

        email = (u.Correo or "").strip().lower() or None
        cli_id = getattr(u, "Codigo_Cliente", None)

        if cli_id:
            return email, int(cli_id)

        # Intentar encontrar Cliente por correo
        if email:
            row = db.session.execute(
                text(
                    "SELECT Codigo_Cliente FROM Cliente WHERE LOWER(Correo)=:e LIMIT 1"
                ),
                {"e": email},
            ).first()
            if row and row[0]:
                cli_id = int(row[0])
                # Vincular al usuario (mantener consistencia)
                u.Codigo_Cliente = cli_id
                db.session.commit()
                return email, cli_id

            # Si no existe cliente, crearlo con mínimos
            nombre = (u.Nombre or "Cliente Web").strip()
            partes = nombre.split(" ", 1)
            nom = partes[0][:50]
            ape = (partes[1] if len(partes) > 1 else "").strip()[:50]
            tel = (u.Telefono or "00000000")[:20]

            db.session.execute(
                text(
                    """
                INSERT INTO Cliente (Cedula, Nombre, Apellido, Telefono, Correo, Fecha_Nacimiento)
                VALUES (0, :n, :a, :t, :e, '1990-01-01')
            """
                ),
                {"n": nom, "a": ape, "t": tel, "e": email},
            )
            db.session.commit()

            row2 = db.session.execute(
                text(
                    "SELECT Codigo_Cliente FROM Cliente WHERE LOWER(Correo)=:e LIMIT 1"
                ),
                {"e": email},
            ).first()
            if row2 and row2[0]:
                cli_id = int(row2[0])
                u.Codigo_Cliente = cli_id
                db.session.commit()
                return email, cli_id

        return email, None
    except Exception as e:
        if current_app:
            current_app.logger.exception(
                f"[GRR] _current_user_email_and_cliente error: {e}"
            )
        return None, None


def _force_owner(reserva_id: int, cli_id: int) -> None:
    """Asegura que la reserva pertenezca al cliente indicado."""
    try:
        db.session.execute(
            text(
                """
                UPDATE Reserva
                   SET Codigo_Cliente = :cli,
                       Fecha_Registro = COALESCE(Fecha_Registro, NOW())
                 WHERE Codigo_Reserva = :rid AND Codigo_Cliente <> :cli
            """
            ),
            {"cli": cli_id, "rid": reserva_id},
        )
        db.session.commit()
    except Exception as e:
        if current_app:
            current_app.logger.warning(
                f"[GRR] No se pudo forzar propietario Reserva {reserva_id} -> Cliente {cli_id}: {e}"
            )


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
        hab_cap = _attr(
            Habitacion, ["Capacidad", "Capacidad_Maxima", "Capacidad_Huespedes"]
        )
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
            rooms.append(
                {
                    "code": int(getattr(h, "Codigo_Habitacion")),
                    "name": getattr(h, "Nombre", None)
                    or f"Habitación {getattr(h, 'Codigo_Habitacion')}",
                    "tipo": getattr(h, "Tipo", None) or "",
                    "desc": getattr(h, "Descripcion", None)
                    or (getattr(h, "Tipo", None) or ""),
                    "capacity": int(getattr(h, "Capacidad", None) or 2),
                    "price": _precio_noche(h),
                    "img": getattr(h, "Imagen_URL", None) or "",
                    "rateName": "Tarifa Flexible",
                    "cancel": "Cancela gratis hasta 7 días antes.",
                    "meal": "—",
                }
            )

        return jsonify(
            {
                "ok": True,
                "rooms": rooms,
                "params": {
                    "checkin": checkin,
                    "checkout": checkout,
                    "adults": adults,
                    "children": children,
                },
            }
        )

    except Exception as e:
        # En vez de 500, devolvemos un error legible para poder depurar rápido
        return jsonify({"ok": False, "msg": f"Error en disponibilidad: {e}"}), 200


# ------------------------------- Reservas ------------------------------------


@grr_bp.post("/reservas")
def crear_reserva():
    """
    Crea una reserva:
      - Fuerza cliente desde sesión
      - Calcula del lado servidor: huéspedes, noches y totales (con NRB/Flex)
      - Inyecta al service
      - Tras crear, refuerza los valores en BD (por si el service los ignora)
    """
    try:
        payload = request.get_json(silent=True) or {}
        _normalize_dates(payload)  # deja date objects si venían strings

        # 1) Resolver email y cliente desde la sesión
        email, cli_id = _current_user_email_and_cliente()
        if not cli_id:
            return (
                jsonify(
                    {
                        "ok": False,
                        "msg": "No se pudo resolver el cliente del usuario (inicia sesión).",
                    }
                ),
                401,
            )

        # 2) Canal/Fuente + dueño
        payload.setdefault("Canal", "Web")
        payload.setdefault("Fuente", "Portal")
        payload["Codigo_Cliente"] = cli_id

        # 3) Cálculo autoritativo
        calc = _calc_totales(payload)

        # 3.1) Inyectar campos económicos y de tarifa en el payload al service
        payload.update(
            {
                "Huespedes": calc["huespedes"],
                "Huespedes_Adultos": calc["adultos"] or None,
                "Huespedes_Ninos": calc["ninos"] or None,
                "Noches": calc["noches"],

                # Precio por HUÉSPED / NOCHE (clave para tu caso):
                "Precio_Noche": float(calc["price_per_guest_night"]),  # Decimal -> float seguro

                "Monto_Subtotal": float(calc["subtotal"]),
                "Impuesto": float(calc["impuesto"]),
                "Monto_Total": float(calc["total"]),

                # Tarifa
                "Tarifa_Codigo": calc["rate_code"],
                "Tarifa_Nombre": calc["rate_name"],
                "Es_No_Reembolsable": bool(calc["es_nrb"]),

                # Asegura fechas (si front las mandó como string)
                "Fecha_Entrada": calc["checkin"],
                "Fecha_Salida": calc["checkout"],
            }
        )

        # 4) Crear con el servicio
        res = _res_service.create(payload)

        # 5) Si OK, asegurar pertenencia y **reforzar valores** en la fila real
        if res.get("ok") and res.get("id"):
            rid = int(res["id"])
            try:
                _force_owner(rid, cli_id)

                r: Optional[Reserva] = Reserva.query.get(rid)
                if r:
                    # Reforzar valores críticos por si el service no los aplicó
                    _set_if(r, "Huespedes", calc["huespedes"])
                    _set_if(r, "Fecha_Entrada", calc["checkin"])
                    _set_if(r, "Fecha_Salida", calc["checkout"])

                    _set_if(r, "Noches", calc["noches"])
                    _set_if(r, "Precio_Noche", calc["price_per_guest_night"])
                    _set_if(r, "Monto_Subtotal", calc["subtotal"])
                    _set_if(r, "Impuesto", calc["impuesto"])
                    _set_if(r, "Monto_Total", calc["total"])

                    _set_if(r, "Tarifa_Codigo", calc["rate_code"])
                    _set_if(r, "Tarifa_Nombre", calc["rate_name"])
                    _set_if(r, "Es_No_Reembolsable", calc["es_nrb"])

                    # por claridad de modelo
                    _set_if(r, "Huespedes_Adultos", calc["adultos"] or None)
                    _set_if(r, "Huespedes_Ninos", calc["ninos"] or None)

                    db.session.commit()

                # Ajustar respuesta con los números reales
                res["monto"] = float(calc["total"])
                res["huespedes"] = calc["huespedes"]
                res["noches"] = calc["noches"]
                res["price_per_guest_night"] = float(calc["price_per_guest_night"])

            except Exception as e:
                if current_app:
                    current_app.logger.warning(f"[GRR] refuerzo post-create R={rid} falló: {e}")

        return jsonify(res), (201 if res.get("ok") else 409)

    except Exception as e:
        if current_app:
            current_app.logger.exception(f"[GRR] crear_reserva error: {e}")
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
        if current_app:
            current_app.logger.exception(f"[GRR] listar_reservas error: {e}")
        return jsonify({"ok": False, "msg": f"Error inesperado: {e}"}), 500


@grr_bp.get("/reservas/<int:reserva_id>")
def obtener_reserva(reserva_id: int):
    try:
        res = _res_service.get(reserva_id)
        return _resp(res, status_ok=200, status_err=404)
    except Exception as e:
        if current_app:
            current_app.logger.exception(f"[GRR] obtener_reserva error: {e}")
        return jsonify({"ok": False, "msg": f"Error inesperado: {e}"}), 500


@grr_bp.put("/reservas/<int:reserva_id>")
def actualizar_reserva(reserva_id: int):
    try:
        payload = request.get_json(silent=True) or {}
        _normalize_dates(payload)

        # Asegurar que no cambien el dueño desde el front
        if "Codigo_Cliente" in payload:
            payload.pop("Codigo_Cliente", None)

        res = _res_service.update(reserva_id, payload)
        code = 200 if res.get("ok") else (409 if res.get("code") == "conflict" else 404)
        return jsonify(res), code
    except Exception as e:
        if current_app:
            current_app.logger.exception(f"[GRR] actualizar_reserva error: {e}")
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
        if current_app:
            current_app.logger.exception(f"[GRR] cancelar_reserva error: {e}")
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
        if current_app:
            current_app.logger.exception(f"[GRR] eliminar_reserva error: {e}")
        return jsonify({"ok": False, "msg": f"Error inesperado: {e}"}), 500


# ---------------------- Errores locales del blueprint ------------------------


@grr_bp.app_errorhandler(404)
def grr_not_found(e):
    return jsonify({"ok": False, "error": "Not Found", "path": request.path}), 404


@grr_bp.app_errorhandler(405)
def grr_method_not_allowed(e):
    return (
        jsonify({"ok": False, "error": "Method Not Allowed", "path": request.path}),
        405,
    )


# =========================
# GHL-02-001: Board + Calendario
# =========================
from datetime import date
from flask import request
from models_sql import Habitacion, Reserva
from sqlalchemy import and_, or_
from extensions import db


def _habs_ocupadas_hoy_ids():
    hoy = date.today()
    # rango [Entrada, Salida) => ocupada si hoy pertenece a ese rango
    sub = (
        db.session.query(Reserva.Codigo_Habitacion)
        .filter(
            and_(Reserva.Fecha_Entrada <= hoy, Reserva.Fecha_Salida > hoy),
            Reserva.Estado.in_(("Confirmada", "CheckIn", "EnCurso", "Pagada")),
        )
        .distinct()
    )
    return {row[0] for row in sub.all()}


# -----Comentado por Leidy porque hace que no se muestren las habitaciones desde la DB
# _bp.get("/api/rooms/status")
# def api_rooms_status():
#    ocupadas_ids = _habs_ocupadas_hoy_ids()
#    data = {"Disponible": [], "Ocupada": [], "Limpieza": [], "Mantenimiento": []}
#    for h in Habitacion.query.order_by(Habitacion.Numero_Habitacion.asc()).all():
#        if h.Codigo_Habitacion in ocupadas_ids:
#            estado = "Ocupada"
#        else:
#            # Estado del registro manda si no está ocupada por reserva
#            estado = h.Estado if h.Estado in data else "Disponible"
#        data[estado].append({
#            "id": h.Codigo_Habitacion,
#            "numero": h.Numero_Habitacion,
#            "tipo": h.Tipo,
#            "precio": float(h.Precio_Noche),
#        })
#   return jsonify(data)


# Cambiando por este.
@grr_bp.get("/api/rooms/status")
def api_rooms_status():
    ocupadas_ids = _habs_ocupadas_hoy_ids()
    rooms = []
    for h in Habitacion.query.order_by(Habitacion.Numero_Habitacion.asc()).all():
        if h.Codigo_Habitacion in ocupadas_ids:
            estado = "Ocupada"
        else:
            estado = h.Estado if h.Estado else "Disponible"
        rooms.append(
            {
                "id": h.Codigo_Habitacion,
                "numero": h.Numero_Habitacion,
                "tipo": h.Tipo,
                "precio": float(h.Precio_Noche),
                "estado": estado,
            }
        )
    return jsonify(rooms)
#Aqui quedaría

# 👇 usa la función consolidada del service
from services.grr.housekeeping_sync import mark_room_to_cleaning

@grr_bp.post("/api/rooms/<int:room_id>/send_to_cleaning")
def api_send_to_cleaning(room_id: int):
    """Permite crear manualmente una orden de limpieza desde el panel de habitaciones."""
    try:
        hab = Habitacion.query.get(room_id)
        if not hab:
            return jsonify({"ok": False, "error": "Habitación no encontrada."}), 404

        # delega todo al service (marca 'Limpieza' y crea HK si hace falta)
        out = mark_room_to_cleaning(room_id)
        if out.get("ok"):
            return jsonify({
                "ok": True,
                "message": f"Habitación {hab.Numero_Habitacion} enviada a limpieza.",
                "task_id": out.get("task_id")
            }), 200

        return jsonify({"ok": False, "error": out.get("error", "No se pudo crear la orden")}), 400

    except Exception as e:
        db.session.rollback()
        return jsonify({"ok": False, "error": str(e)}), 500



@grr_bp.get("/api/rooms/calendar")
def api_rooms_calendar():
    """Eventos de reservas para un calendario. Parámetros:
    - room_id (opcional) => sólo esa habitación
    """
    room_id = request.args.get("room_id", type=int)
    q = Reserva.query
    if room_id:
        q = q.filter(Reserva.Codigo_Habitacion == room_id)
    eventos = []
    for r in q.all():
        eventos.append(
            {
                "id": r.Codigo_Reserva,
                "title": f"H#{r.Codigo_Habitacion} / {r.Estado}",
                "start": r.Fecha_Entrada.isoformat(),
                "end": r.Fecha_Salida.isoformat(),  # FullCalendar usa end-excl
            }
        )
    return jsonify(eventos)


# 👇 usa el modelo real

@grr_bp.get("/housekeeping/api/ordenes")
def hk_list():
    from models.grr import HousekeepingTask
    
    ordenes = HousekeepingTask.query.order_by(HousekeepingTask.Fecha_Creacion.desc()).all()
    return jsonify([
        {
            "id": o.Id,
            "habitacion": o.Habitacion.Numero_Habitacion if getattr(o, "Habitacion", None) else None,
            "estado": o.Estado,                  # 'Pendiente' | 'En proceso' | 'Terminado'
            "notas": getattr(o, "Observaciones", None),
            "creado": o.Fecha_Creacion.isoformat(),
        }
        for o in ordenes
    ])

@grr_bp.post("/housekeeping/orden/<int:orden_id>/estado")
def hk_set_estado(orden_id):
    
    from models.grr import HousekeepingTask  # import local
    
    nuevo = (request.form.get("estado") or "").strip()
    # valores EXACTOS del ENUM:
    if nuevo not in ("Pendiente", "En proceso", "Terminado"):
        return jsonify({"ok": False, "error": "Estado inválido"}), 400

    o = HousekeepingTask.query.get_or_404(orden_id)
    estado_anterior = o.Estado
    o.Estado = nuevo

    # si termina, podemos devolver la habitación a Disponible
    if nuevo == "Terminado" and getattr(o, "Habitacion", None):
        o.Habitacion.Estado = "Disponible"

    db.session.commit()
    return jsonify({"ok": True, "antes": estado_anterior, "despues": nuevo})



from models_sql import MantenimientoSolicitud


@grr_bp.post("/mant/solicitudes")
def mant_crear():
    hab_id = request.form.get("habitacion_id", type=int)
    titulo = (request.form.get("titulo") or "").strip()
    desc = (request.form.get("descripcion") or "").strip()
    prioridad = (request.form.get("prioridad") or "Media").strip()
    if not hab_id or not titulo:
        return jsonify({"ok": False, "error": "Datos incompletos"}), 400
    s = MantenimientoSolicitud(
        Codigo_Habitacion=hab_id,
        Titulo=titulo,
        Descripcion=desc,
        Prioridad=prioridad,
        Estado="Abierta",
    )
    db.session.add(s)
    db.session.commit()
    return jsonify({"ok": True, "id": s.Id})


@grr_bp.post("/mant/solicitudes/<int:sid>/estado")
def mant_set_estado(sid):
    nuevo = (request.form.get("estado") or "").strip()
    if nuevo not in ("Abierta", "EnProceso", "Cerrada"):
        return jsonify({"ok": False, "error": "Estado inválido"}), 400
    s = MantenimientoSolicitud.query.get_or_404(sid)
    s.Estado = nuevo
    db.session.commit()
    return jsonify({"ok": True})


@grr_bp.get("/mant/api/solicitudes")
def mant_list():
    q = MantenimientoSolicitud.query.order_by(
        MantenimientoSolicitud.Fecha_Creacion.desc()
    )
    data = [
        {
            "id": m.Id,
            "hab": m.Habitacion.Numero_Habitacion if m.Habitacion else None,
            "titulo": m.Titulo,
            "prioridad": m.Prioridad,
            "estado": m.Estado,
            "creado": m.Fecha_Creacion.isoformat(),
        }
        for m in q.all()
    ]
    return jsonify(data)

@grr_bp.get("/ops/housekeeping")
def ops_housekeeping():
    """Vista del módulo de limpieza (Housekeeping)."""
    return render_template("ops-housekeeping.html")
