# blueprints/grr/routes.py
from __future__ import annotations
import logging
from sqlalchemy.exc import DataError, IntegrityError

import json, random, string, secrets
from decimal import Decimal, ROUND_HALF_UP
from datetime import date, datetime, timedelta
from typing import Any, Dict, Tuple, List, Optional

from flask import Blueprint, jsonify, request, session, current_app, render_template
from sqlalchemy import func, select, text, and_, or_, text as _t

# Imports seguros para ejecución como paquete; con fallback en modo script
try:
    from ...extensions import db
    from ...models_sql import Habitacion, Reserva, Usuario, MantenimientoSolicitud
    from ...services.grr.reservation_service import ReservationService
    from ...services.grr.housekeeping_sync import mark_room_to_cleaning
except ImportError:
    # Fallback si se ejecuta sin contexto de paquete (desarrollo)
    import os, sys
    sys.path.append(os.path.dirname(os.path.dirname(os.path.dirname(__file__))))
    from extensions import db
    from models_sql import Habitacion, Reserva, Usuario, MantenimientoSolicitud
    from services.grr.reservation_service import ReservationService
    from services.grr.housekeeping_sync import mark_room_to_cleaning

TAX_RATE = 0.13  # IVA
VAT_RATE = Decimal("0.13")
NRB_DISCOUNT = Decimal("0.10")

grr_bp = Blueprint("grr", __name__)
_res_service = ReservationService()

logging.basicConfig(
    level=logging.DEBUG,
    format="%(asctime)s - %(levelname)s -%(filename)s:%(lineno)d - %(message)s",
)


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


# --- Helpers para columnas y funcionario por defecto ---
from sqlalchemy import text as _text

def _col_exists(table_name: str, column_name: str) -> bool:
    try:
        row = db.session.execute(_text("""
            SELECT 1
              FROM information_schema.COLUMNS
             WHERE TABLE_SCHEMA = DATABASE()
               AND TABLE_NAME = :t
               AND COLUMN_NAME = :c
             LIMIT 1
        """), {"t": table_name, "c": column_name}).first()
        return bool(row)
    except Exception:
        return False

def _col_nullable(table: str, column: str) -> bool:
    try:
        row = db.session.execute(_text("""
            SELECT CASE WHEN IS_NULLABLE='YES' THEN 1 ELSE 0 END
              FROM INFORMATION_SCHEMA.COLUMNS
             WHERE TABLE_SCHEMA = DATABASE()
               AND TABLE_NAME = :t
               AND COLUMN_NAME = :c
             LIMIT 1
        """), {"t": table, "c": column}).scalar()
        return bool(row)
    except Exception:
        return True

def _resolve_funcionario_id(payload: dict) -> tuple[bool, int | None, str | None]:
    """
    Devuelve (col_existe, funcionario_id, error_msg)
    - col_existe: True si la columna Codigo_Funcionario existe en la tabla Reserva
    - funcionario_id: id válido o None
    - error_msg: solo si la columna no admite NULL y no hay id que usar
    """
    col_exists = _col_exists("Reserva", "Codigo_Funcionario")
    if not col_exists:
        return False, None, None

    # 1) Si viene en JSON y existe en BD, úsalo
    fid = payload.get("Codigo_Funcionario")
    if fid is not None:
        try:
            fid = int(fid)
        except Exception:
            fid = None
    if fid:
        ok = db.session.execute(
            _text("SELECT 1 FROM Funcionario WHERE Codigo_Funcionario=:f LIMIT 1"),
            {"f": fid}
        ).first()
        if ok:
            return True, fid, None

    # 2) Intentar mapear desde el usuario en sesión
    try:
        uid = session.get("user_id")
        if uid:
            mapped = db.session.execute(
                _text("SELECT Codigo_Funcionario FROM Funcionario WHERE Codigo_Usuario=:u LIMIT 1"),
                {"u": uid}
            ).scalar()
            if mapped:
                return True, int(mapped), None
    except Exception:
        pass

    # 3) Tomar el primero disponible
    any_f = db.session.execute(
        _text("SELECT Codigo_Funcionario FROM Funcionario ORDER BY Codigo_Funcionario ASC LIMIT 1")
    ).scalar()
    if any_f:
        return True, int(any_f), None

    # 4) Si la columna permite NULL, ok; si NO, error claro
    nullable = _col_nullable("Reserva", "Codigo_Funcionario")
    if nullable:
        return True, None, None
    return True, None, "No hay funcionarios creados y la columna Reserva.Codigo_Funcionario no admite NULL."



def _compute_total_and_pax(payload: Dict[str, Any]) -> Dict[str, Any]:
    """
    Calcula noches, pax, precio_noche (aplicando NRB si corresponde) y totales:
    subtotal = precio_noche * noches * pax
    total = subtotal + 13% (TAX_RATE)
    Lee tanto claves del front en minúscula (adults, children, guests, rateCode, price)
    como las variantes antiguas en mayúscula.
    """
    # Fechas
    ini = payload.get("Fecha_Entrada") or payload.get("checkin")
    fin = payload.get("Fecha_Salida") or payload.get("checkout")
    if isinstance(ini, str) and ini:
        ini = date.fromisoformat(ini)
    if isinstance(fin, str) and fin:
        fin = date.fromisoformat(fin)
    nights = max(1, (fin - ini).days) if (ini and fin) else 1

    # Huéspedes (preferir 'guests' si viene; si no, adults+children)
    adults = int(
        payload.get("adults")
        or payload.get("Adults")
        or payload.get("Huespedes_Adultos")
        or 0
    )
    children = int(
        payload.get("children")
        or payload.get("Children")
        or payload.get("Huespedes_Ninos")
        or 0
    )
    guests_field = payload.get("guests") or payload.get("Huespedes")
    try:
        guests_field = int(guests_field) if guests_field is not None else None
    except Exception:
        guests_field = None
    total_pax = max(1, guests_field if guests_field is not None else (adults + children or 1))

    # Precio base por noche (por huésped). Aceptar también 'price' del front.
    price_night = None
    if payload.get("price") is not None:
        try:
            price_night = float(payload.get("price") or 0.0)
        except Exception:
            price_night = 0.0
    elif "Precio_Base_Noche" in payload:
        price_night = float(payload.get("Precio_Base_Noche") or 0.0)
    else:
        room_id = payload.get("Codigo_Habitacion") or payload.get("room") or payload.get("roomCode")
        if room_id:
            try:
                h = Habitacion.query.get(int(room_id))
                price_night = _precio_noche(h) if h else 0.0
            except Exception:
                price_night = 0.0
        else:
            price_night = float(payload.get("Precio_Noche") or 0.0)

    # Tarifa NRB => 10% descuento (aceptar rateCode/rate/tarifa)
    rate_code = (
        (payload.get("rateCode")
         or payload.get("rate")
         or payload.get("Tarifa")
         or payload.get("Tipo_Tarifa")
         or payload.get("RateCode")
         or "")
        .strip()
        .lower()
    )
    if rate_code in ("nrb", "no_reembolsable", "noreembolsable"):
        price_night = round(price_night * 0.90, 2)

    # Totales
    subtotal = round(price_night * nights * total_pax, 2)
    tax = round(subtotal * TAX_RATE, 2)
    total = round(subtotal + tax, 2)

    return {
        "nights": nights,
        "adults": adults,
        "children": children,
        "total_pax": total_pax,
        "price_night": float(price_night),  # por huésped/noche (ya con NRB si aplica)
        "subtotal": float(subtotal),
        "tax": float(tax),
        "total": float(total),               # <-- guardar en Monto_Total
        "rate_code": rate_code or "flex",
    }




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

 # --- Helper unificado para extraer el ID de la reserva desde cualquier forma de respuesta del Service
def _extract_reserva_id(res: Dict[str, Any]) -> int:
    """
    Devuelve el identificador de reserva desde la respuesta del service, tolerando
    distintas claves: 'id', 'Codigo_Reserva', 'reserva_id', 'numero', 'ReservaId'.
    """
    for k in ("id", "Codigo_Reserva", "reserva_id", "numero", "ReservaId", "Reserva_ID"):
        v = res.get(k)
        if v is None:
            continue
        try:
            return int(v)
        except (TypeError, ValueError):
            continue
    return 0




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


# --------------------------- Disponibilidad (v1) -----------------------------
@grr_bp.get("/api/availability")   # <-- alias para compatibilidad con el front actual
@grr_bp.get("/availability")
@grr_bp.get("/disponibilidad")
def disponibilidad():
    """
    Query:
      ?checkin=YYYY-MM-DD&checkout=YYYY-MM-DD&adults=2&children=0&guests=3&tipo=Doble
    Reglas:
      - Excluye habitaciones en 'Mantenimiento' si existe Habitacion.Estado
      - Excluye reservas solapadas en [checkin, checkout)
      - Filtra por capacidad mínima y por tipo (si se envía).
    Respuesta:
      { ok, available, message, rooms:[...] , params:{...} }
    """
    try:
        checkin  = request.args.get("checkin") or ""
        checkout = request.args.get("checkout") or ""
        adults   = int(request.args.get("adults", "0") or 0)
        children = int(request.args.get("children", "0") or 0)
        guests_q = request.args.get("guests")
        tipo     = (request.args.get("tipo") or "").strip()

        # pax total: guests si viene; si no, adults+children; si nada => 1
        total_pax = None
        try:
            total_pax = int(guests_q) if guests_q is not None else None
        except Exception:
            total_pax = None
        total_pax = max(1, total_pax if total_pax is not None else (adults + children or 1))

        # Validación suave de fechas
        try:
            ini = date.fromisoformat(checkin) if checkin else None
            fin = date.fromisoformat(checkout) if checkout else None
        except ValueError:
            return jsonify({"ok": False, "message": "Fechas inválidas (YYYY-MM-DD)"}), 400

        q = Habitacion.query

        # Estado != 'Mantenimiento'
        hab_estado = _attr(Habitacion, ["Estado"])
        if hab_estado is not None:
            q = q.filter(hab_estado != "Mantenimiento")

        # Filtro por tipo (si viene y existe la columna)
        if tipo and hasattr(Habitacion, "Tipo"):
            q = q.filter(Habitacion.Tipo.ilike(tipo))

        # Capacidad >= pax (si existe)
        hab_cap = _attr(Habitacion, ["Capacidad", "Capacidad_Maxima", "Capacidad_Huespedes"])
        if hab_cap is not None:
            q = q.filter(func.coalesce(hab_cap, 2) >= int(total_pax))

        # Excluir solapadas
        if ini and fin:
            res_estado  = _attr(Reserva, ["Estado"])
            res_fini    = _attr(Reserva, ["Fecha_Entrada"])
            res_ffin    = _attr(Reserva, ["Fecha_Salida"])
            res_cod_hab = _attr(Reserva, ["Codigo_Habitacion"])

            if res_fini is not None and res_ffin is not None and res_cod_hab is not None:
                filters = [res_fini < fin, res_ffin > ini]
                if res_estado is not None:
                    filters.append(res_estado != "Cancelada")

                subq = select(res_cod_hab).where(*filters).scalar_subquery()
                q = q.filter(Habitacion.Codigo_Habitacion.notin_(subq))

        # Orden
        if hasattr(Habitacion, "Numero_Habitacion"):
            q = q.order_by(Habitacion.Numero_Habitacion.asc(), Habitacion.Codigo_Habitacion.asc())
        else:
            q = q.order_by(Habitacion.Codigo_Habitacion.asc())

        rooms = []
        for h in q.all():
            rooms.append({
                "code": int(getattr(h, "Codigo_Habitacion")),
                "name": getattr(h, "Nombre", None) or f"Habitación {getattr(h, 'Codigo_Habitacion')}",
                "Codigo_Habitacion": int(getattr(h, "Codigo_Habitacion")),
                "Numero_Habitacion": getattr(h, "Numero_Habitacion", None),
                "tipo": getattr(h, "Tipo", None) or "",
                "desc": getattr(h, "Descripcion", None) or (getattr(h, "Tipo", None) or ""),
                "capacity": int(getattr(h, "Capacidad", None) or 2),
                "price": _precio_noche(h),
                "img": getattr(h, "Imagen_URL", None) or "",
                "rateName": "Tarifa Flexible",
                "cancel": "Cancela gratis hasta 7 días antes.",
                "meal": "—",
            })

        available = len(rooms) > 0
        message = "Disponibilidad confirmada" if available else "Sin disponibilidad en el rango de fechas indicado"

        return jsonify({
            "ok": True,
            "available": available,
            "message": message,
            "rooms": rooms,
            "params": {
                "checkin": checkin,
                "checkout": checkout,
                "guests": total_pax,
                "tipo": tipo or None
            },
        }), 200

    except Exception as e:
        if current_app:
            current_app.logger.exception(f"[GRR] disponibilidad error: {e}")
        return jsonify({"ok": False, "message": f"Error en disponibilidad: {e}"}), 500



@grr_bp.get("/room-types")
def grr_room_types():
    """
    Devuelve una lista única de tipos de habitación existentes (excluyendo null/'' y en uso).
    """
    try:
        if not hasattr(Habitacion, "Tipo"):
            return jsonify({"ok": True, "types": []})

        rows = (
            db.session.query(Habitacion.Tipo)
            .filter(func.nullif(func.trim(Habitacion.Tipo), "") != None)
            .group_by(Habitacion.Tipo)
            .order_by(Habitacion.Tipo.asc())
            .all()
        )
        types = [r[0] for r in rows]
        return jsonify({"ok": True, "types": types}), 200
    except Exception as e:
        if current_app:
            current_app.logger.exception(f"[GRR] room-types error: {e}")
        return jsonify({"ok": False, "error": str(e)}), 500


@grr_bp.get("/ops/checkin/reserva-por-cedula")
def grr_checkin_por_cedula():
    """
    Query: ?cedula=########
    Devuelve la última reserva vigente/reciente del cliente y la habitación asignada.
    """
    ced = (request.args.get("cedula") or "").strip()
    if not ced:
        return jsonify({"ok": False, "msg": "Debe indicar la cédula."}), 400

    try:
        row = db.session.execute(text("""
            SELECT  R.Codigo_Reserva,
                    R.Estado,
                    R.Fecha_Entrada,
                    R.Fecha_Salida,
                    R.Codigo_Habitacion,
                    H.Numero_Habitacion
              FROM  Reserva R
              JOIN  Cliente C ON C.Codigo_Cliente = R.Codigo_Cliente
              LEFT JOIN Habitacion H ON H.Codigo_Habitacion = R.Codigo_Habitacion
             WHERE  C.Cedula = :ced
               AND  R.Estado IN ('Pendiente','Confirmada','CheckIn','EnCurso','Pagada')
             ORDER BY R.Fecha_Entrada DESC
             LIMIT 1
        """), {"ced": ced}).mappings().first()

        if not row:
            return jsonify({"ok": False, "found": False, "msg": "No hay reservas vigentes para la cédula indicada."}), 200

        data = {
            "Codigo_Reserva": int(row["Codigo_Reserva"]),
            "Estado": row["Estado"],
            "Fecha_Entrada": str(row["Fecha_Entrada"]),
            "Fecha_Salida": str(row["Fecha_Salida"]),
            "Codigo_Habitacion": int(row["Codigo_Habitacion"]) if row["Codigo_Habitacion"] else None,
            "Numero_Habitacion": row["Numero_Habitacion"],
        }
        return jsonify({"ok": True, "found": True, "reserva": data}), 200

    except Exception as e:
        if current_app:
            current_app.logger.exception(f"[GRR] checkin-cedula error: {e}")
        return jsonify({"ok": False, "msg": f"Error: {e}"}), 500




# Lista completa de habitaciones para la UI (GRR-01-014)
@grr_bp.get("/habitaciones")
def grr_habitaciones():
    try:
        q = Habitacion.query
        # Ordenar por número si existe, si no por código
        if hasattr(Habitacion, "Numero_Habitacion"):
            q = q.order_by(Habitacion.Numero_Habitacion.asc(), Habitacion.Codigo_Habitacion.asc())
        else:
            q = q.order_by(Habitacion.Codigo_Habitacion.asc())

        rooms = []
        for h in q.all():
            tipo = (getattr(h, "Tipo", None) or "Sencilla")
            # Derivar capacidad si la tabla no la tuviera
            tl = tipo.lower()
            cap = 4 if ("suite" in tl or "doble" in tl) else 2

            price = _precio_noche(h)

            item = {
                # claves "amigables"
                "code": int(getattr(h, "Codigo_Habitacion")),
                "name": getattr(h, "Nombre", None) or f"Habitación {getattr(h, 'Codigo_Habitacion')}",
                "tipo": tipo,
                "capacity": int(getattr(h, "Capacidad", None) or cap),
                "price": price,
                "img": getattr(h, "Imagen_URL", None) or "",
                # compatibilidad con vistas existentes:
                "Codigo_Habitacion": int(getattr(h, "Codigo_Habitacion")),
                "Numero_Habitacion": getattr(h, "Numero_Habitacion", None),
                "Tipo": tipo,
                "Capacidad": int(getattr(h, "Capacidad", None) or cap),
                "Precio_Noche": float(price),
                "Imagen_URL": getattr(h, "Imagen_URL", None) or "",
                "Estado": getattr(h, "Estado", None) or "Disponible",
            }
            rooms.append(item)

        return jsonify({"ok": True, "rooms": rooms}), 200

    except Exception as e:
        if current_app:
            current_app.logger.exception(f"[GRR] habitaciones error: {e}")
        return jsonify({"ok": False, "error": str(e)}), 500




# ------------------------------- Reservas ------------------------------------


@grr_bp.post("/reservas")
def crear_reserva():
    """
    Crea una reserva y garantiza:
      - Validación de solape (fin-exclusivo).
      - Cálculo backend SIEMPRE: subtotal, impuesto y total (con TAX_RATE).
      - Persistir Huespedes y Monto_Total (TOTAL con impuesto).
      - No fallar si el service requiere Codigo_Funcionario (usa 1 por defecto).
      - Devolver 'monto' en la respuesta para que Checkout/Confirmación lo muestren.
    """
    try:
        payload = request.get_json(silent=True) or {}
        _normalize_dates(payload)

        # 1) Cliente desde sesión
        email, cli_id = _current_user_email_and_cliente()
        if not cli_id:
            return jsonify({"ok": False, "msg": "No se pudo resolver el cliente del usuario (inicia sesión)."}), 401

        # 2) Enriquecer datos base
        payload.setdefault("Canal", "Web")
        payload.setdefault("Fuente", "Portal")
        payload["Codigo_Cliente"] = cli_id

        # **Parche anti-409**: si el service exige Codigo_Funcionario, dalo por defecto (1)
        # y permite que el front lo sobreescriba si lo envía.
        payload.setdefault("Codigo_Funcionario", 1)

        # 3) Validar disponibilidad si hay habitación
        room_id = payload.get("Codigo_Habitacion")
        if room_id and payload.get("Fecha_Entrada") and payload.get("Fecha_Salida"):
            ini = payload["Fecha_Entrada"]
            fin = payload["Fecha_Salida"]
            if isinstance(ini, str): ini = date.fromisoformat(ini)
            if isinstance(fin, str): fin = date.fromisoformat(fin)

            conflict = (
                db.session.query(Reserva)
                .filter(Reserva.Codigo_Habitacion == int(room_id))
                .filter(Reserva.Fecha_Entrada < fin, Reserva.Fecha_Salida > ini)  # solape [ini, fin)
                .filter(Reserva.Estado != "Cancelada")
                .first()
            )
            if conflict:
                return jsonify({"ok": False, "code": "no_availability", "msg": "Sin disponibilidad para el rango solicitado"}), 409

        # 4) Calcular totales y pax (BACKEND manda)
        calc = _compute_total_and_pax(payload)

        # Añadir al payload antes de crear (para que el Service lo use si respeta campos)
        payload["Huespedes"]   = int(calc["total_pax"])
        payload["Monto_Total"] = float(calc["total"])   # TOTAL CON IMPUESTO
        payload["Tarifa"]      = calc["rate_code"]

        # Paso 1: todas las reservas creadas desde booking-checkout (Canal Web) deben iniciar como Pendiente
        payload["Canal"] = "Web"
        payload["Estado"] = "Pendiente"
        
        # 5) Crear con el servicio
        res = _res_service.create(payload)

        # 6) Post-create: forzar que la fila tenga Monto_Total y Huespedes correctos
        rid = res.get("id") or res.get("Codigo_Reserva") or res.get("reserva_id")
        if res.get("ok") and rid:
            try:
                rid = int(rid)
            except Exception:
                rid = None

        if res.get("ok") and rid:
            try:
                # Actualiza SIEMPRE monto total (con impuesto) y número de huéspedes
                db.session.execute(
                    text("""
                        UPDATE Reserva
                           SET Huespedes   = :pax,
                               Monto_Total = :monto
                         WHERE Codigo_Reserva = :rid
                    """),
                    {"pax": int(calc["total_pax"]), "monto": float(calc["total"]), "rid": rid},
                )
                db.session.commit()
                # Asegurar dueño correcto
                _force_owner(rid, cli_id)
            except Exception as e:
                if current_app:
                    current_app.logger.warning(f"[GRR] post-create pax/monto R#{rid} fallo: {e}")

        # 7) Devolver el 'monto' correcto en la respuesta (útil para UI y correo)
        if res.get("ok"):
            res["monto"] = float(calc["total"])

        return jsonify(res), (201 if res.get("ok") else 409)

    except Exception as e:
        if current_app:
            current_app.logger.exception(f"[GRR] crear_reserva error: {e}")
        return jsonify({"ok": False, "msg": f"Error inesperado: {e}"}), 500





def _ensure_pago_table():
    try:
        db.session.execute(_t("""
            CREATE TABLE IF NOT EXISTS Pago (
              Id              INT AUTO_INCREMENT PRIMARY KEY,
              Reserva_Id      INT NOT NULL,
              Metodo          ENUM('tarjeta','datafono') NOT NULL,
              Monto           DECIMAL(12,2) NOT NULL,
              Ultimos4        VARCHAR(4) NULL,
              Autorizacion    VARCHAR(32) NULL,
              Estado          ENUM('Aprobado','Anulado') NOT NULL DEFAULT 'Aprobado',
              Fecha_Creacion  DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
              INDEX IX_Pago_Reserva (Reserva_Id)
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
        """))
        db.session.commit()
    except Exception:
        db.session.rollback()

def _gen_auth_code():
    return "AUTH-" + "".join(random.choices(string.ascii_uppercase + string.digits, k=10))

def _record_payment(reserva_id: int, amount: float, method: str, last4: str | None) -> dict:
    _ensure_pago_table()
    auth = _gen_auth_code()
    db.session.execute(_t("""
        INSERT INTO Pago (Reserva_Id, Metodo, Monto, Ultimos4, Autorizacion, Estado)
        VALUES (:r, :m, :mo, :u4, :au, 'Aprobado')
    """), {"r": reserva_id, "m": method, "mo": amount, "u4": (last4 or None), "au": auth})
    db.session.commit()
    return {"ok": True, "auth": auth}

def _ensure_reset_table():
    """Crea la tabla de tokens de reseteo si no existe."""
    try:
        db.session.execute(_t("""
            CREATE TABLE IF NOT EXISTS PasswordReset (
              Id           INT AUTO_INCREMENT PRIMARY KEY,
              Usuario_Id   INT NOT NULL,
              Token        VARCHAR(128) NOT NULL,
              Expira       DATETIME     NOT NULL,
              Creado       DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP,
              UNIQUE KEY UK_PasswordReset_Token (Token),
              INDEX IX_PasswordReset_User (Usuario_Id)
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
        """))
        db.session.commit()
    except Exception:
        db.session.rollback()

def _issue_reset_token(usuario_id: int) -> str | None:
    try:
        _ensure_reset_table()
        token = "RT-" + secrets.token_urlsafe(32)
        hours = int(current_app.config.get("RESET_TOKEN_HOURS", 24))
        exp = datetime.utcnow() + timedelta(hours=hours)
        db.session.execute(_t("""
            INSERT INTO PasswordReset (Usuario_Id, Token, Expira)
            VALUES (:u, :t, :e)
        """), {"u": usuario_id, "t": token, "e": exp})
        db.session.commit()
        base = (current_app.config.get("PUBLIC_BASE_URL") or request.url_root).rstrip("/")
        return f"{base}/reset-password?token={token}"
    except Exception as e:
        current_app.logger.warning(f"[RESET] {e}")
        return None

def _ensure_user_and_reset(email: str, nombre: str) -> tuple[str | None, str | None]:
    temp_pwd, reset_url = _maybe_create_user_with_temp_password(email, nombre)
    if (not reset_url) and email:
        try:
            # Detectar columna de email e id reales para el lookup
            cols = {
                r[0] for r in db.session.execute(text("""
                    SELECT COLUMN_NAME
                      FROM INFORMATION_SCHEMA.COLUMNS
                     WHERE TABLE_SCHEMA = DATABASE()
                       AND TABLE_NAME   = 'Usuario'
                """)).all()
            }
            email_col = next((c for c in ("Correo","Email","correo","email") if c in cols), None)
            id_col    = next((c for c in ("Codigo_Usuario","Usuario_Id","ID_Usuario","Id","id") if c in cols), "Codigo_Usuario")
            if email_col:
                uid = db.session.execute(
                    text(f"SELECT {id_col} FROM Usuario WHERE LOWER({email_col})=:e LIMIT 1"),
                    {"e": (email or '').lower()}
                ).scalar()
                if uid:
                    reset_url = _issue_reset_token(int(uid))
        except Exception as e:
            current_app.logger.warning(f"[RESET WRAP] {e}")
    return temp_pwd, reset_url



def _send_mail_simple(to_email: str, subject: str, body: str):
    try:
        host = current_app.config.get("MAIL_SERVER")
        port = int(current_app.config.get("MAIL_PORT", 0) or 0)
        user = current_app.config.get("MAIL_USERNAME"); pwd = current_app.config.get("MAIL_PASSWORD")
        sender = current_app.config.get("MAIL_DEFAULT_SENDER") or user or "no-reply@hotel.local"
        use_tls = bool(current_app.config.get("MAIL_USE_TLS", False))
        use_ssl = bool(current_app.config.get("MAIL_USE_SSL", False))
        if not (host and port and user and pwd):
            current_app.logger.info(f"[MAIL MOCK]\nTo: {to_email}\nSubj: {subject}\n\n{body}")
            return
        from email.message import EmailMessage
        import ssl, smtplib
        msg = EmailMessage()
        msg["Subject"] = subject; msg["From"] = sender; msg["To"] = to_email
        msg.set_content(body)
        if use_ssl:
            ctx = ssl.create_default_context()
            with smtplib.SMTP_SSL(host, port, context=ctx, timeout=30) as s:
                s.login(user, pwd); s.send_message(msg)
        else:
            with smtplib.SMTP(host, port, timeout=30) as s:
                if use_tls: s.starttls(context=ssl.create_default_context())
                s.login(user, pwd); s.send_message(msg)
    except Exception as e:
        current_app.logger.warning(f"[MAIL] {e}")

def _ensure_cliente_by_email(nombre: str, apellido: str, tel: str, email: str) -> int | None:
    """Resuelve o crea Cliente por correo (alineado con patrones del archivo)."""
    row = db.session.execute(text("SELECT Codigo_Cliente FROM Cliente WHERE LOWER(Correo)=:e LIMIT 1"), {"e": (email or "").lower()}).first()
    if row and row[0]:
        return int(row[0])
    # crear mínimo
    db.session.execute(text("""
        INSERT INTO Cliente (Cedula, Nombre, Apellido, Telefono, Correo, Fecha_Nacimiento)
        VALUES (0, :n, :a, :t, :e, '1990-01-01')
    """), {"n": (nombre or "Cliente").strip()[:50], "a": (apellido or "").strip()[:50], "t": (tel or "")[:20], "e": (email or "").lower()})
    db.session.commit()
    row2 = db.session.execute(text("SELECT Codigo_Cliente FROM Cliente WHERE LOWER(Correo)=:e LIMIT 1"), {"e": (email or "").lower()}).first()
    return int(row2[0]) if (row2 and row2[0]) else None

def _maybe_create_user_with_temp_password(email: str, nombre: str) -> tuple[str | None, str | None]:
    """
    Garantiza un usuario por correo y devuelve (password_temporal, reset_url).
    - Detecta dinámicamente columnas de Usuario: Correo/Email, Nombre/Nombre_Completo/FullName,
      Contrasena/Password/Contrasena_Hash, etc.
    - Si ya existe, no crea password temporal y emite reset_url.
    - Si no hay columna de password, crea el usuario sin password y de todos modos emite reset_url.
    """
    try:
        email_l = (email or "").strip().lower()
        if not email_l:
            return None, None

        # Descubrir columnas reales de la tabla Usuario
        cols = {
            r[0] for r in db.session.execute(text("""
                SELECT COLUMN_NAME
                  FROM INFORMATION_SCHEMA.COLUMNS
                 WHERE TABLE_SCHEMA = DATABASE()
                   AND TABLE_NAME   = 'Usuario'
            """)).all()
        }

        # Posibles nombres de columnas
        id_candidates    = [c for c in ("Codigo_Usuario","Usuario_Id","ID_Usuario","Id","id") if c in cols]
        email_candidates = [c for c in ("Correo","Email","correo","email") if c in cols]
        name_candidates  = [c for c in ("Nombre","Nombre_Completo","FullName","NombreUsuario") if c in cols]
        pass_candidates  = [c for c in ("Contrasena","Password","Contrasena_Hash","PassHash") if c in cols]

        if not email_candidates:
            # Sin columna de email no podemos garantizar unicidad; abortar sin error duro.
            return None, None

        id_col   = id_candidates[0]    if id_candidates    else "Codigo_Usuario"
        email_col= email_candidates[0] if email_candidates else "Correo"
        name_col = name_candidates[0]  if name_candidates  else None
        pass_col = pass_candidates[0]  if pass_candidates  else None

        # ¿Ya existe?
        try:
            uid = db.session.execute(
                text(f"SELECT {id_col} FROM Usuario WHERE LOWER({email_col})=:e LIMIT 1"),
                {"e": email_l}
            ).scalar()
        except Exception:
            uid = None

        if uid:
            reset_url = _issue_reset_token(int(uid))
            return None, reset_url

        # Crear nuevo (password temporal sólo si existe columna de password)
        temp = "".join(random.choices(string.ascii_letters + string.digits, k=10))

        fields, params = [], {}
        def add(col: str, val: Any):
            fields.append(col); params[col] = val

        add(email_col, email_l)
        if name_col:
            add(name_col, (nombre or "Invitado")[:80])
        if pass_col:
            add(pass_col, temp)

        # Campos opcionales comunes, sólo si existen
        if "Activo" in cols: add("Activo", 1)
        if "Estado" in cols: add("Estado", "Activo")
        if "Rol" in cols:    add("Rol", "Huesped")
        if "Rol_Id" in cols: add("Rol_Id", 3)

        # Ejecutar INSERT parametrizado
        ph  = ", ".join(f":{f}" for f in fields)
        sql = f"INSERT INTO Usuario ({', '.join(fields)}) VALUES ({ph})"
        db.session.execute(text(sql), params)
        db.session.commit()

        # Releer ID para emitir token
        uid = db.session.execute(
            text(f"SELECT {id_col} FROM Usuario WHERE LOWER({email_col})=:e LIMIT 1"),
            {"e": email_l}
        ).scalar()

        reset_url = _issue_reset_token(int(uid)) if uid else None
        return (temp if pass_col else None), reset_url

    except Exception as e:
        if current_app:
            current_app.logger.warning(f"[TEMP USER] {e}")
        return None, None






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



@grr_bp.post("/reservas/estancia-sin-reserva")
def grr_crear_estancia_sin_reserva():
    """
    Recepción (ops): crea SIEMPRE Confirmada y registra el pago.
    Body JSON esperado: los mismos campos de anon-reserva + {
        payment:{ method:'tarjeta'|'datafono', card_last4?, card_name?, card_exp? },
        actor:'ops'
    }
    """
    p = request.get_json(silent=True) or {}

    # Normalizar fechas
    ci = p.get("Fecha_Entrada") or p.get("checkin")
    co = p.get("Fecha_Salida")  or p.get("checkout")
    if not (ci and co):
        return jsonify({"ok": False, "msg": "Fechas requeridas."}), 400

    # Resolver/crear cliente por correo
    email    = (p.get("correo") or "").strip().lower()
    nombre   = (p.get("nombre") or "").strip()
    apellido = (p.get("apellido") or "").strip()
    tel      = (p.get("telefono") or "").strip()
    if not email:
        return jsonify({"ok": False, "msg": "Correo del huésped requerido."}), 400

    cli_id = _ensure_cliente_by_email(nombre, apellido, tel, email)
    if not cli_id:
        return jsonify({"ok": False, "msg": "No fue posible registrar el cliente."}), 500

    # Mapear a payload del service
    payload = {
        "Codigo_Cliente": cli_id,
        "Codigo_Habitacion": p.get("Codigo_Habitacion"),
        "Fecha_Entrada": ci,
        "Fecha_Salida": co,
        "Huespedes": int(p.get("huespedes") or p.get("Huespedes") or 1),
        "Canal": "WalkIn",
        "Fuente": "Recepcion",
        "Tarifa": (p.get("rateCode") or p.get("Tarifa") or "flex"),
        "Estado": "Confirmada"  # Confirmada (operación en recepción)
    }

    # Requisito operativo: debe venir una habitación seleccionada
    if not payload.get("Codigo_Habitacion"):
        return jsonify({"ok": False, "msg": "Debe seleccionar una habitación disponible."}), 400

    # >>>>>>>>>>>>> NUEVO: resolver Codigo_Funcionario si la columna existe <<<<<<<<<<<<<
    col_ex, fid, ferr = _resolve_funcionario_id(p)  # respeta si el front lo envía y mapea desde la sesión
    if col_ex:
        if fid is not None:
            payload["Codigo_Funcionario"] = int(fid)
        elif ferr:  # la columna no admite NULL y no hay funcionario utilizable
            return jsonify({"ok": False, "msg": ferr}), 400
    # >>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>

    # Cálculo autoritativo de totales
    calc_in = {
        "Fecha_Entrada": ci,
        "Fecha_Salida": co,
        "Huespedes": payload["Huespedes"],
        "Codigo_Habitacion": payload.get("Codigo_Habitacion"),
        "Tarifa_Codigo": payload["Tarifa"],
        "tipo": p.get("tipo"),
        "Precio_Noche": p.get("Precio_Noche"),  # opcional desde UI
    }
    try:
        tot = _calc_totales(calc_in)
    except Exception as e:
        return jsonify({"ok": False, "msg": f"Fechas inválidas: {e}"}), 400

    payload["Monto_Total"] = float(tot["total"])

    # Crear reserva con el service
    res = _res_service.create(payload)
    if not res.get("ok"):
        code = 409 if res.get("code") == "conflict" else 400
        err = res.get("msg") or res.get("message") or res.get("error") or "No se pudo crear la reserva."
        resto = {k: v for k, v in res.items() if k not in ("ok", "msg", "message", "error")}
        return jsonify({"ok": False, "msg": err, **resto}), code

    reserva_id = _extract_reserva_id(res)
    if not reserva_id:
        return jsonify({
            "ok": False,
            "msg": "Reserva creada por el service pero sin identificador en la respuesta.",
            "raw": res
        }), 500

    # Registrar pago
    pay = p.get("payment") or {}
    method = ("datafono" if str(pay.get("method", "")).lower() == "datafono" else "tarjeta")
    last4  = (pay.get("card_last4") or None)
    try:
        _record_payment(reserva_id, float(tot["total"]), method, last4)
    except Exception as e:
        current_app.logger.warning(f"[PAGO] No se pudo registrar: {e}")

    # Asegurar estado Confirmada
    try:
        db.session.execute(text("""
            UPDATE Reserva
               SET Estado='Confirmada',
                   Monto_Total=:m
             WHERE Codigo_Reserva=:r
        """), {"m": float(tot["total"]), "r": reserva_id})
        db.session.commit()
    except Exception as e:
        current_app.logger.warning(f"[CONFIRMAR] {e}")

    # Usuario/app: crear usuario (si no existe) y emitir link de reseteo
    full_name = f"{nombre} {apellido}".strip()
    temp_pwd, reset_url = _ensure_user_and_reset(email, full_name)

    monto_str = f"₡ {float(tot['total']):,.2f}".replace(',', 'X').replace('.', ',').replace('X', '.')
    metodo_pago = "Datáfono (presencial)" if method == "datafono" else f"Tarjeta terminada en {(last4 or '****')}"

    body = (
        f"Hola {nombre},\n\n"
        f"Tu reserva #{reserva_id} ha sido CONFIRMADA.\n"
        f"Rango: {tot['checkin']} → {tot['checkout']} · Huéspedes: {tot['huespedes']} · Noches: {tot['noches']}\n"
        f"Método de pago: {metodo_pago}\n"
        f"Monto total (con impuestos): {monto_str}\n\n"
    )
    extra = []
    if temp_pwd:
        extra.append("Se creó una cuenta para ti en el Portal del Huésped.")
        extra.append(f"Contraseña temporal: {temp_pwd}")
    if reset_url:
        extra.append(f"Enlace para restablecer tu contraseña: {reset_url}")
    if extra:
        body += "\n".join(extra) + "\n\n"
    body += "Gracias por reservar en Hotel Villa Grace."

    try:
        _send_mail_simple(email, "Confirmación y comprobante de pago — Hotel Villa Grace", body)
    except Exception as e:
        current_app.logger.warning(f"[MAIL CONF] {e}")

    # Auditoría KPI
    try:
        _update_kpis(float(tot["total"]), str(tot["checkin"]), noches=int(tot["noches"]))
        _audit_log(_current_user_email(), "reserva.walkin_confirm", {
            "reserva": reserva_id,
            "monto": float(tot["total"]),
            "metodo": method
        }, str(reserva_id))
    except Exception:
        pass

    return jsonify({
        "ok": True,
        "id": reserva_id,
        "numero": reserva_id,
        "monto": float(tot["total"]),
        "estado": "Confirmada",
        "message": "Reserva creada y confirmada (Recepción)."
    }), 201






@grr_bp.post("/api/reservas/anon")
def api_reservas_anon():
    """
    Crea reserva confirmada para cliente sin sesión:
    - Resuelve/crea Cliente por correo
    - Calcula totales (backend autoritativo)
    - Registra pago (tarjeta, last4)
    - Crea usuario (si no existe) y emite reset
    - Envía correo con comprobante + temp password + reset link
    - Devuelve redirect a página de éxito
    """
    p = request.get_json(silent=True) or {}

    # Validaciones mínimas
    ci = (p.get("checkin") or p.get("Fecha_Entrada") or "").strip()
    co = (p.get("checkout") or p.get("Fecha_Salida") or "").strip()
    email    = (p.get("correo") or p.get("email") or "").strip().lower()
    nombre   = (p.get("nombre") or "").strip()
    apellido = (p.get("apellido") or "").strip()
    tel      = (p.get("telefono") or "").strip()
    if not (ci and co and email and nombre):
        return jsonify({"ok": False, "msg": "Datos incompletos (fechas, nombre y correo)."}), 400

    # Resolver/crear Cliente
    cli_id = _ensure_cliente_by_email(nombre, apellido, tel, email)
    if not cli_id:
        return jsonify({"ok": False, "msg": "No fue posible registrar el cliente."}), 500

    # Requisito operativo: no permitir reservas anónimas sin habitación
    if not (p.get("Codigo_Habitacion")):
        return jsonify({"ok": False, "msg": "Debe seleccionar una habitación disponible."}), 400


    # Totales autoritativos
    calc_in = {
        "Fecha_Entrada": ci,
        "Fecha_Salida":  co,
        "Huespedes": int(p.get("huespedes") or p.get("Huespedes") or 1),
        "Codigo_Habitacion": p.get("Codigo_Habitacion"),
        "Tarifa_Codigo": (p.get("rateCode") or p.get("Tarifa") or "flex"),
        "tipo": p.get("tipo"),
        "Precio_Noche": p.get("Precio_Noche"),  # opcional desde UI
    }
    try:
        tot = _calc_totales(calc_in)
    except Exception as e:
        return jsonify({"ok": False, "msg": f"Fechas inválidas: {e}"}), 400

    # Crear reserva confirmada
    payload = {
        "Codigo_Cliente":    cli_id,
        "Codigo_Habitacion": p.get("Codigo_Habitacion"),
        "Fecha_Entrada":     ci,
        "Fecha_Salida":      co,
        "Huespedes":         int(tot["huespedes"]),
        "Tarifa":            tot["rate_code"],  # 'flex' | 'nrb'
        "Canal":             "Web",
        "Fuente":            "Portal",
        "Estado":            "Confirmada",
        "Monto_Total":       float(tot["total"]),
    }

    col_ex, fid, ferr = _resolve_funcionario_id(p)
    if col_ex:
        if fid is not None:
            payload["Codigo_Funcionario"] = int(fid)
        elif ferr:
            return jsonify({"ok": False, "msg": ferr}), 400


    # Crear reserva con el service
    res = _res_service.create(payload)
    if not res.get("ok"):
        code = 409 if res.get("code") == "conflict" else 400
        err = res.get("msg") or res.get("message") or res.get("error") or "No se pudo crear la reserva."
        resto = {k: v for k, v in res.items() if k not in ("ok", "msg", "message", "error")}
        return jsonify({"ok": False, "msg": err, **resto}), code

    # Soportar 'reserva_id' (lo que realmente te está devolviendo el Service)
    reserva_id = _extract_reserva_id(res)
    if not reserva_id:
        return jsonify({
            "ok": False,
            "msg": "Reserva creada por el service pero sin identificador en la respuesta.",
            "raw": res
        }), 500


    # Registrar pago online (tarjeta)
    pay = p.get("payment") or {}
    last4 = (pay.get("card_last4") or None)
    try:
        _record_payment(reserva_id, float(tot["total"]), "tarjeta", last4)
    except Exception as e:
        current_app.logger.warning(f"[PAGO ANON] {e}")

    # Persistir coherencia
    try:
        db.session.execute(_t("""
            UPDATE Reserva
               SET Estado='Confirmada',
                   Monto_Total=:m
             WHERE Codigo_Reserva=:r
        """), {"m": float(tot["total"]), "r": reserva_id})
        db.session.commit()
    except Exception as e:
        current_app.logger.warning(f"[CONFIRMAR ANON] {e}")

    # Usuario + token reset
    full_name = f"{nombre} {apellido}".strip()
    temp_pwd, reset_url = _ensure_user_and_reset(email, full_name)

    # Correo
    monto_str = f"₡ {float(tot['total']):,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")
    metodo_pago = f"Tarjeta terminada en {(last4 or '****')}"
    body = (
        f"Hola {nombre},\n\n"
        f"Tu reserva #{reserva_id} ha sido CONFIRMADA.\n"
        f"Rango: {tot['checkin']} → {tot['checkout']} · Huéspedes: {tot['huespedes']} · Noches: {tot['noches']}\n"
        f"Método de pago: {metodo_pago}\n"
        f"Monto total (con impuestos): {monto_str}\n\n"
    )
    extra = []
    if temp_pwd:
        extra.append("Se creó una cuenta para ti en el Portal del Huésped.")
        extra.append(f"Contraseña temporal: {temp_pwd}")
    if reset_url:
        extra.append(f"Enlace para restablecer tu contraseña: {reset_url}")
    if extra:
        body += "\n".join(extra) + "\n\n"
    body += "Gracias por reservar en Hotel Villa Grace."

    try:
        _send_mail_simple(email, "Confirmación y comprobante de pago — Hotel Villa Grace", body)
    except Exception as e:
        current_app.logger.warning(f"[MAIL CONF ANON] {e}")

    # KPI + auditoría (noches del periodo)
    try:
        _update_kpis(float(tot["total"]), str(tot["checkin"]), noches=int(tot["noches"]))
        _audit_log(email, "reserva.anon_confirm", {"reserva": reserva_id, "monto": float(tot["total"]), "metodo": "tarjeta"}, str(reserva_id))
    except Exception:
        pass

    # URL de éxito (ajusta si tienes una ruta distinta)
    redirect_url = f"/reserva-sin-sesion/exito?numero={reserva_id}"
    return jsonify({"ok": True, "id": reserva_id, "numero": reserva_id, "monto": float(tot["total"]), "estado": "Confirmada", "redirect": redirect_url}), 201

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
    return jsonify({"ok": True, "items": rooms})
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
    # import local para evitar errores si el modelo no está cargado antes
    from models_sql import HousekeepingTask

    ordenes = HousekeepingTask.query.order_by(HousekeepingTask.Fecha_Creacion.desc()).all()
    items = []
    for o in ordenes:
        hab_num = None
        try:
            hab = Habitacion.query.get(getattr(o, "Habitacion_Id", None))
            hab_num = hab.Numero_Habitacion if hab else None
        except Exception:
            pass
        items.append({
            "id": o.Id,
            "habitacion": hab_num,
            "estado": o.Estado,
            "notas": getattr(o, "Observaciones", None),
            "creado": o.Fecha_Creacion.isoformat() if getattr(o, "Fecha_Creacion", None) else None,
        })
    return jsonify(items)



@grr_bp.post("/housekeeping/orden/<int:orden_id>/estado")
def hk_set_estado(orden_id):

    from models_sql import HousekeepingTask  # import local

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
    try:
        db.session.commit()
    except (DataError, IntegrityError):
        error = "Hubo un problema creando la orden de matenimiento."
        logging.exception(error)
        return jsonify({"error": error})
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



# =========================
# GRR-01-014: Lista de espera y ofertas
# =========================
from datetime import datetime, timedelta

def _send_offer_email(to_email: str, subject: str, body: str):
    """
    Envío mínimo: si no hay SMTP configurado, log a consola.
    (Puedes unificar con utilidades de app.py si prefieres).
    """
    try:
        host = current_app.config.get("MAIL_SERVER")
        port = int(current_app.config.get("MAIL_PORT", 0) or 0)
        user = current_app.config.get("MAIL_USERNAME"); pwd = current_app.config.get("MAIL_PASSWORD")
        sender = current_app.config.get("MAIL_DEFAULT_SENDER") or user or "no-reply@hotel.local"
        use_tls = bool(current_app.config.get("MAIL_USE_TLS", False))
        use_ssl = bool(current_app.config.get("MAIL_USE_SSL", False))
        if not (host and port and user and pwd):
            current_app.logger.info(f"[WAITLIST OFFER MOCK]\nTo: {to_email}\nSubj: {subject}\n\n{body}")
            return
        from email.message import EmailMessage
        import ssl, smtplib
        msg = EmailMessage()
        msg["Subject"] = subject; msg["From"] = sender; msg["To"] = to_email
        msg.set_content(body)
        if use_ssl:
            ctx = ssl.create_default_context()
            with smtplib.SMTP_SSL(host, port, context=ctx, timeout=30) as s:
                s.login(user, pwd); s.send_message(msg)
        else:
            with smtplib.SMTP(host, port, timeout=30) as s:
                if use_tls: s.starttls(context=ssl.create_default_context())
                s.login(user, pwd); s.send_message(msg)
    except Exception as e:
        current_app.logger.warning(f"[WAITLIST MAIL] {e}")

def _room_free_in_range(room_id: int, ci: date, co: date) -> bool:
    r = (
        db.session.query(Reserva)
        .filter(Reserva.Codigo_Habitacion == int(room_id))
        .filter(Reserva.Estado.in_(("Confirmada","Pendiente")))
        .filter(Reserva.Fecha_Entrada < co, Reserva.Fecha_Salida > ci)
        .first()
    )
    return r is None

def _find_room_for_waitlist(tipo: str, ci: date, co: date, pax: int, superior_ok: bool=True):
    """
    Busca habitación libre del mismo tipo; si no hay y superior_ok=True,
    busca otra con capacidad >= pax y tarifa >= del tipo solicitado (aprox “superior”).
    """
    def _q(base_cond=None):
        q = Habitacion.query
        if base_cond is not None:
            q = q.filter(base_cond)
        return q.order_by(Habitacion.Codigo_Habitacion.asc()).all()

    # 1) mismo tipo
    if hasattr(Habitacion, "Tipo"):
        same = _q(Habitacion.Tipo == tipo)
    else:
        same = _q()

    for h in same:
        cap = int(getattr(h, "Capacidad", 2) or 2)
        if cap >= pax and _room_free_in_range(h.Codigo_Habitacion, ci, co):
            return h

    if not superior_ok:
        return None

    # 2) superior: más caro y con capacidad
    all_rooms = _q()
    # precio del tipo solicitado (si existe)
    type_price = None
    for h in all_rooms:
        if getattr(h, "Tipo", None) == tipo:
            type_price = _precio_noche(h); break
    if type_price is None:
        type_price = 0.0

    for h in all_rooms:
        cap = int(getattr(h, "Capacidad", 2) or 2)
        p = _precio_noche(h)
        if cap >= pax and p >= type_price and _room_free_in_range(h.Codigo_Habitacion, ci, co):
            return h
    return None

# blueprints/grr/routes.py


@grr_bp.post("/waitlist")
def waitlist_add():
    """
    Alta de lista de espera compatible con el esquema real.
    - Obliga a mandar un 'Tipo_Solicitado' (derivado por room_id si no viene).
    - Tolera distintos nombres de campos desde el front (nombre/name, correo/email, etc).
    - Inserta solo las columnas que existan realmente en la tabla.
    """
    p = request.get_json(silent=True) or request.form or {}

    # --- Leer payload desde el front (con sinónimos) ---
    name     = (p.get("nombre") or p.get("name") or "").strip()
    email    = (p.get("correo") or p.get("email") or "").strip().lower()
    room_id  = (p.get("room_id") or p.get("habitacion_id") or request.args.get("room_id"))
    tipo     = (p.get("tipo") or request.args.get("tipo"))
    checkin  = (p.get("checkin") or request.args.get("checkin") or "").strip()
    checkout = (p.get("checkout") or request.args.get("checkout") or "").strip()
    guests   = (p.get("huespedes") or p.get("guests") or
                request.args.get("huespedes") or request.args.get("guests") or 1)
    preferir_superior = p.get("preferir_superior", 1)  # 1 = sí (por defecto)

    try:
        guests = int(guests)
    except Exception:
        guests = 1

    # Validación mínima de datos de entrada
    if not (name and email and checkin and checkout):
        return jsonify({"ok": False, "msg": "Datos incompletos (nombre, correo, fechas)."}), 400

    # Si no vino 'tipo', intentar derivarlo por room_id desde Habitacion
    if (not tipo) and room_id:
        try:
            row = db.session.execute(
                text("SELECT Tipo FROM Habitacion WHERE Codigo_Habitacion=:id LIMIT 1"),
                {"id": int(room_id)}
            ).first()
            if row and row[0]:
                tipo = str(row[0])
        except Exception:
            pass

    # Fallback de tipo si sigue sin definirse
    if not tipo:
        tipo = "Sencilla"  # Ajusta si quieres otro default

    # --- Garantizar que la tabla existe (no altera si ya existe) ---
    # Alineado con tu script de esquema: Tipo_Solicitado es NOT NULL y sin default.
    try:
        db.session.execute(text("""
            CREATE TABLE IF NOT EXISTS Waitlist (
              Id                 INT AUTO_INCREMENT PRIMARY KEY,
              Correo             VARCHAR(120) NOT NULL,
              Nombre             VARCHAR(120) NOT NULL,
              Tipo_Solicitado    VARCHAR(80)  NOT NULL,
              Fecha_Entrada      DATE         NOT NULL,
              Fecha_Salida       DATE         NOT NULL,
              Huespedes          INT          NOT NULL DEFAULT 1,
              Preferir_Superior  TINYINT(1)   NOT NULL DEFAULT 1,
              Estado             ENUM('Pendiente','Ofertado','Confirmado','Expirado','Cancelado') NOT NULL DEFAULT 'Pendiente',
              Expira_Oferta      DATETIME NULL,
              Oferta_Reserva_Id  INT NULL,
              Observaciones      VARCHAR(255),
              Fecha_Creacion     DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
              KEY IX_WL_Estado (Estado),
              KEY IX_WL_Fechas (Fecha_Entrada, Fecha_Salida),
              KEY IX_WL_Correo (Correo)
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
        """))
        db.session.commit()
    except Exception:
        db.session.rollback()  # si falla, continuaremos asumiendo que ya existe

    # --- Descubrir columnas reales para construir un INSERT seguro ---
    try:
        columns = {
            r[0]
            for r in db.session.execute(text("""
                SELECT COLUMN_NAME
                  FROM INFORMATION_SCHEMA.COLUMNS
                 WHERE TABLE_SCHEMA = DATABASE()
                   AND TABLE_NAME   = 'Waitlist'
            """)).all()
        }
    except Exception as e:
        current_app.logger.warning(f"[WAITLIST] No fue posible leer INFORMATION_SCHEMA: {e}")
        # Si no podemos leer columnas, usamos el esquema "oficial"
        columns = {
            "Correo","Nombre","Tipo_Solicitado","Fecha_Entrada","Fecha_Salida",
            "Huespedes","Preferir_Superior","Estado","Expira_Oferta","Oferta_Reserva_Id",
            "Observaciones","Fecha_Creacion"
        }

    def has(col: str) -> bool:
        return col in columns

    # --- Construir los pares columna/valor a insertar ---
    fields, params = [], {}

    def add(col: str, value):
        if has(col):
            fields.append(col)
            params[col] = value

    # Obligatorios (según esquema real)
    add("Correo", email)
    add("Nombre", name)
    add("Tipo_Solicitado", tipo)
    add("Fecha_Entrada", checkin)   # YYYY-MM-DD esperado; el front ya lo genera así
    add("Fecha_Salida", checkout)

    # Opcionales
    add("Huespedes", int(guests))
    add("Preferir_Superior", 1 if str(preferir_superior) in ("1", "true", "True", "on", "sí", "si") else 0)
    add("Estado", "Pendiente")

    # Algunos entornos tenían variantes con estas columnas; si existen, se agregan:
    # (no están en tu esquema oficial, pero no hace daño soportarlas)
    if room_id:
        # Alias que podrían existir en implementaciones previas:
        for alt in ("Codigo_Habitacion", "Habitacion_Id"):
            if has(alt):
                try:
                    add(alt, int(room_id))
                except Exception:
                    add(alt, None)
                break

    # Seguridad: asegurar que incluimos Tipo_Solicitado si existe en la tabla
    if has("Tipo_Solicitado") and "Tipo_Solicitado" not in params:
        params["Tipo_Solicitado"] = tipo
        fields.append("Tipo_Solicitado")

    # Validación final: no intentes hacer INSERT vacío
    if not fields:
        return jsonify({"ok": False, "msg": "No hay columnas válidas para insertar en Waitlist."}), 500

    # --- Ejecutar INSERT parametrizado ---
    ph = ", ".join(f":{f}" for f in fields)
    sql = f"INSERT INTO Waitlist ({', '.join(fields)}) VALUES ({ph})"

    try:
        db.session.execute(text(sql), params)
        db.session.commit()
        return jsonify({"ok": True, "msg": "Te hemos agregado a la lista de espera."}), 201
    except Exception as e:
        db.session.rollback()
        current_app.logger.exception(f"[WAITLIST] insert error: {e}")
        return jsonify({"ok": False, "msg": f"Error al registrar en la lista de espera: {e}"}), 500



@grr_bp.post("/waitlist/room/<int:room_id>")
def waitlist_add_room(room_id: int):
    # Unificar payload
    p = request.get_json(silent=True) or request.form or {}

    # Aceptar distintos nombres
    name  = (p.get("name") or p.get("nombre") or "").strip()
    email = (p.get("email") or p.get("correo") or "").strip().lower()

    # Fallback a querystring si no vienen en body
    checkin  = (p.get("checkin")  or request.args.get("checkin")  or None)
    checkout = (p.get("checkout") or request.args.get("checkout") or None)
    tipo     = (p.get("tipo")     or request.args.get("tipo")     or None)
    guests   = p.get("guests") or request.args.get("guests") or 1
    try:
        guests = int(guests)
    except Exception:
        guests = 1

    # Validar solo lo imprescindible
    if not name or not email:
        return jsonify({"ok": False, "message": "Nombre y correo son obligatorios."}), 400

    # Guardar (ajusta el nombre de tu tabla/columnas si difieren)
    db.session.execute(text("""
        INSERT INTO Waitlist
            (Habitacion_Id, Tipo, Checkin, Checkout, Guests, Nombre, Email, Estado, Fecha_Solicitud)
        VALUES
            (:hid, :tipo, :ci, :co, :g, :n, :e, 'Pendiente', NOW())
    """), {
        "hid": room_id, "tipo": tipo, "ci": checkin, "co": checkout,
        "g": guests, "n": name, "e": email
    })
    db.session.commit()

    return jsonify({"ok": True, "message": "Te hemos agregado a la lista de espera."}), 201



@grr_bp.get("/waitlist")
def waitlist_list():
    estado = request.args.get("estado")
    sql = "SELECT * FROM Waitlist"
    if estado:
        sql += " WHERE Estado=:e"
        rows = db.session.execute(text(sql), {"e": estado}).mappings().all()
    else:
        rows = db.session.execute(text(sql)).mappings().all()
    return jsonify({"ok": True, "items": [dict(r) for r in rows]})

@grr_bp.post("/waitlist/process")
def waitlist_process():
    """
    Recorre pendings y genera 'oferta' si hay cupo.
    La oferta expira en X horas (config por env WAITLIST_OFFER_HOURS, default 12).
    """
    hours = int(current_app.config.get("WAITLIST_OFFER_HOURS", 12))
    pend = db.session.execute(text("""
        SELECT * FROM Waitlist
         WHERE Estado='Pendiente'
         ORDER BY Fecha_Creacion ASC
    """)).mappings().all()

    processed = 0
    for w in pend:
        try:
            ci = date.fromisoformat(str(w["Fecha_Entrada"]))
            co = date.fromisoformat(str(w["Fecha_Salida"]))
            pax = int(w["Huespedes"] or 1)
            sup_ok = bool(w["Preferir_Superior"])
            tipo = w["Tipo_Solicitado"]
            hab = _find_room_for_waitlist(tipo, ci, co, pax, superior_ok=sup_ok)
            if not hab:
                continue
            # Generar oferta (no bloquea inventario aquí)
            exp = datetime.utcnow() + timedelta(hours=hours)
            db.session.execute(text("""
                UPDATE Waitlist
                   SET Estado='Ofertado', Expira_Oferta=:exp
                 WHERE Id=:id
            """), {"id": w["Id"], "exp": exp})
            db.session.commit()

            # Email con instrucciones (link genérico)
            body = (
                f"Hola {w['Nombre']},\n\n"
                f"Se liberó una habitación para tu solicitud ({tipo}) del {ci} al {co}.\n"
                f"Puedes completar tu reserva desde el portal. Oferta válida hasta {exp} UTC.\n\n"
                f"— Hotel Villa Grace"
            )
            _send_offer_email(w["Correo"], "Oferta de disponibilidad — Hotel Villa Grace", body)
            _audit_log(w["Correo"], "waitlist.offer", {"id": int(w["Id"]), "hab": int(hab.Codigo_Habitacion), "expira": exp.isoformat()})
            processed += 1
        except Exception as e:
            current_app.logger.warning(f"[WAITLIST] {e}")

    return jsonify({"ok": True, "processed": processed})



# =========================
# GRR-01-015: Tarifas dinámicas / cupones / override
# =========================
def _apply_coupon_to_amount(codigo: str, monto_total: float) -> tuple[float, str]:
    row = db.session.execute(
        text("""SELECT Codigo, Tipo, Valor, Valido_Desde, Valido_Hasta, Max_Usos, Usos, Activo
                FROM Coupon WHERE Codigo=:c LIMIT 1"""),
        {"c": (codigo or "").strip()}
    ).mappings().first()
    if not row or not row["Activo"]:
        return monto_total, "Código inválido o inactivo"

    today = date.today()
    vd, vh = row["Valido_Desde"], row["Valido_Hasta"]
    if (vd and vd > today) or (vh and vh < today):
        return monto_total, "Código fuera de vigencia"

    if row["Max_Usos"] and row["Usos"] >= row["Max_Usos"]:
        return monto_total, "Código agotado"

    tipo = row["Tipo"]
    val  = float(row["Valor"] or 0.0)
    if tipo == "porcentaje":
        nuevo = max(0.0, monto_total * (1.0 - val / 100.0))
    elif tipo == "monto":
        nuevo = max(0.0, monto_total - val)
    else:  # corporativo -> tratamos como porcentaje por simplicidad
        nuevo = max(0.0, monto_total * (1.0 - val / 100.0))

    # reservar el incremento de uso (no confirmamos aquí; lo hará la ruta)
    return round(nuevo, 2), ""

@grr_bp.post("/reservas/<int:reserva_id>/apply-coupon")
def reservas_apply_coupon(reserva_id: int):
    """
    Aplica cupón a una reserva, recalcula Monto_Total y deja trazabilidad.
    Body JSON: { codigo:"ACME10" }
    """
    codigo = (request.json or {}).get("codigo", "").strip()
    if not codigo:
        return jsonify({"ok": False, "msg": "Falta código."}), 400

    row = db.session.execute(text(
        "SELECT Monto_Total, Fecha_Entrada, Fecha_Salida FROM Reserva WHERE Codigo_Reserva=:r LIMIT 1"
    ), {"r": reserva_id}).mappings().first()
    if not row:
        return jsonify({"ok": False, "msg": "Reserva no encontrada."}), 404

    monto_antes = float(row["Monto_Total"] or 0.0)
    nuevo, err = _apply_coupon_to_amount(codigo, monto_antes)
    if err:
        return jsonify({"ok": False, "msg": err}), 409

    # guardar ajuste
    db.session.execute(text(
        "UPDATE Reserva SET Monto_Total=:m WHERE Codigo_Reserva=:r"
    ), {"m": nuevo, "r": reserva_id})
    db.session.execute(text(
        """INSERT INTO ReservaAjuste (Codigo_Reserva, Tipo, Codigo, Monto_Antes, Monto_Despues, Usuario, Motivo)
           VALUES (:r,'coupon',:c,:a,:d,:u,'Cupón aplicado')"""
    ), {"r": reserva_id, "c": codigo, "a": monto_antes, "d": nuevo, "u": _current_user_email() or ""})
    # incrementar uso del cupón
    db.session.execute(text("UPDATE Coupon SET Usos=Usos+1 WHERE Codigo=:c"), {"c": codigo})
    db.session.commit()

    _audit_log(_current_user_email(), "reserva.coupon", {"reserva": reserva_id, "codigo": codigo, "antes": monto_antes, "despues": nuevo}, str(reserva_id))
    return jsonify({"ok": True, "monto": nuevo})

@grr_bp.post("/reservas/<int:reserva_id>/manual-rate")
def reservas_manual_rate(reserva_id: int):
    """
    Permite override manual (recepción) y calcula ADR para la trazabilidad.
    Body JSON: { monto_total: 123456.78, motivo:"Tarifa corporativa XYZ" }
    """
    p = request.get_json(silent=True) or {}
    nuevo  = float(p.get("monto_total") or 0.0)
    motivo = (p.get("motivo") or "Ajuste manual").strip()
    if nuevo <= 0:
        return jsonify({"ok": False, "msg": "Monto inválido."}), 400

    row = db.session.execute(text(
        "SELECT Monto_Total, Fecha_Entrada, Fecha_Salida FROM Reserva WHERE Codigo_Reserva=:r LIMIT 1"
    ), {"r": reserva_id}).mappings().first()
    if not row:
        return jsonify({"ok": False, "msg": "Reserva no encontrada."}), 404

    monto_antes = float(row["Monto_Total"] or 0.0)
    # ADR (sin impuestos): revenue / noches
    try:
        ci = row["Fecha_Entrada"]; co = row["Fecha_Salida"]
        noches = max(1, (co - ci).days)
    except Exception:
        noches = 1
    adr_antes = round((monto_antes / (1.0 + TAX_RATE)) / noches, 2) if noches else 0.0
    adr_desp  = round((nuevo        / (1.0 + TAX_RATE)) / noches, 2) if noches else 0.0

    db.session.execute(text("UPDATE Reserva SET Monto_Total=:m WHERE Codigo_Reserva=:r"), {"m": nuevo, "r": reserva_id})
    db.session.execute(text(
        """INSERT INTO ReservaAjuste (Codigo_Reserva, Tipo, Monto_Antes, Monto_Despues, ADR_Antes, ADR_Despues, Usuario, Motivo)
           VALUES (:r,'manual',:a,:d,:aa,:ad,:u,:mot)"""
    ), {"r": reserva_id, "a": monto_antes, "d": nuevo, "aa": adr_antes, "ad": adr_desp, "u": _current_user_email() or "", "mot": motivo})
    db.session.commit()

    _audit_log(_current_user_email(), "reserva.rate_override", {"reserva": reserva_id, "antes": monto_antes, "despues": nuevo, "motivo": motivo}, str(reserva_id))
    return jsonify({"ok": True, "monto": nuevo, "adr": adr_desp})

# routes.py (o donde declares tus rutas)
from flask import Blueprint, render_template

ops_bp = Blueprint("ops", __name__, url_prefix="/grr/ops")

@ops_bp.get("/coupons")
def ops_coupons_page():
    # Renderiza la vista de administración de cupones (GRR-01-015)
    return render_template("ops-coupons.html")


# --- PREVIEW DE CUPONES (estimación) ---
from datetime import date as _date, datetime as _dt
from sqlalchemy import text as _text

def _d10(v):
    """Normaliza fechas a 'YYYY-MM-DD' o None."""
    if not v:
        return None
    try:
        if hasattr(v, "strftime"):
            return v.strftime("%Y-%m-%d")
        s = str(v)
        return s[:10]
    except Exception:
        return None

@grr_bp.get("/coupons/preview")
def coupons_preview():
    code = (request.args.get("code") or "").strip().upper()
    try:
        subtotal = float(request.args.get("subtotal") or 0.0)
    except Exception:
        subtotal = 0.0

    if not code:
        return jsonify({"ok": False, "msg": "Código de cupón requerido."}), 200

    row = db.session.execute(_text("""
        SELECT Codigo, Tipo, Valor, Valido_Desde, Valido_Hasta, Max_Usos, Usos, Activo
          FROM Coupon
         WHERE Codigo = :c
         LIMIT 1
    """), {"c": code}).mappings().first()

    if not row or not row["Activo"]:
        return jsonify({"ok": False, "msg": "Cupón inválido o inactivo."}), 200

    # Vigencia: usa _d10 para no cortar objetos date/datetime
    today = _date.today()
    vd_s = _d10(row.get("Valido_Desde"))
    vh_s = _d10(row.get("Valido_Hasta"))
    if vd_s and vd_s > str(today):
        return jsonify({"ok": False, "msg": "Fuera de vigencia (aún no inicia)."}), 200
    if vh_s and vh_s < str(today):
        return jsonify({"ok": False, "msg": "Fuera de vigencia (ya expiró)."}), 200

    # Límite de usos
    max_usos = row.get("Max_Usos")
    usos     = row.get("Usos") or 0
    if max_usos and usos >= max_usos:
        return jsonify({"ok": False, "msg": "Cupón agotado."}), 200

    # Cálculo de descuento estimado
    tipo = (row.get("Tipo") or "").lower()
    valor = float(row.get("Valor") or 0.0)
    amount = 0.0
    if tipo == "porcentaje":
        amount = round(subtotal * (valor / 100.0), 2)
    elif tipo in ("monto", "monto_fijo"):
        amount = valor

    return jsonify({
        "ok": True,
        "type": tipo or "monto",
        "amount": float(amount),
        "msg": f"Descuento estimado: ₡ {amount:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")
    })


@grr_bp.get("/coupons/validate")
def coupons_validate():
    """
    Alias compatible con el front:
    /grr/coupons/validate?code=ACME10&total=12345
    """
    code = (request.args.get("code") or "").strip().upper()
    if not code:
        return jsonify({"valid": False, "msg": "code requerido"}), 400

    # aceptar 'total' o 'subtotal' del querystring
    raw_total = request.args.get("total") or request.args.get("subtotal") or "0"
    try:
        base = float(raw_total)
    except Exception:
        base = 0.0

    nuevo, err = _apply_coupon_to_amount(code, base)
    return jsonify({"valid": (err == ""), "type": ("-" if err else "ok"),
                    "value": None, "total": nuevo, "msg": (err or "Cupón válido.")})
# =========================
# GRR-01-016: Gestión de No-Show
# =========================
def _no_show_penalty_for_reserva(reserva_id: int) -> float:
    """
    Penalización por No-Show:
      - 1 noche (con impuestos) según tarifa actual de la habitación.
    """
    row = db.session.execute(text("""
        SELECT R.Codigo_Habitacion, H.Precio_Noche
          FROM Reserva R JOIN Habitacion H ON H.Codigo_Habitacion=R.Codigo_Habitacion
         WHERE R.Codigo_Reserva=:r LIMIT 1
    """), {"r": reserva_id}).mappings().first()
    if not row:
        return 0.0
    price = float(row["Precio_Noche"] or 0.0)
    total = round(price * (1.0 + TAX_RATE), 2)
    return total

@grr_bp.post("/jobs/run-noshow")
def run_noshow():
    """
    Marca como No-Show a reservas Confirmadas/Pendientes que no hicieron check-in
    antes de la hora de corte (NOSHOW_CUTOFF_HOUR, default 18).
    - Libera la habitación (no genera estancia)
    - Aplica penalización y la suma al Monto_Total
    - Actualiza KPI (ingresos y noches=0 para ADR)
    """
    cutoff = int(current_app.config.get("NOSHOW_CUTOFF_HOUR", 18))
    today = date.today()
    now_h = datetime.utcnow().hour  # usa UTC; si deseas TZ local, ajusta aquí

    # Candidatas: fecha_entrada < hoy, o (== hoy y hora >= cutoff)
    cand = db.session.execute(text("""
        SELECT Codigo_Reserva, Codigo_Habitacion, Fecha_Entrada, Fecha_Salida, Monto_Total
          FROM Reserva
         WHERE Estado IN ('Confirmada','Pendiente')
           AND (
                DATE(Fecha_Entrada) < CURDATE()
                OR (DATE(Fecha_Entrada) = CURDATE() AND :h >= :cut)
           )
    """), {"h": now_h, "cut": cutoff}).mappings().all()

    changed = 0
    for r in cand:
        try:
            # penalización
            fee = _no_show_penalty_for_reserva(int(r["Codigo_Reserva"]))
            nuevo_total = float(r["Monto_Total"] or 0.0) + fee
            db.session.execute(text("""
                UPDATE Reserva
                   SET Estado='NoShow',
                       Monto_Total=:m,
                       Observaciones = CONCAT(COALESCE(Observaciones,''),' | No-Show aplicado')
                 WHERE Codigo_Reserva=:id
            """), {"m": nuevo_total, "id": r["Codigo_Reserva"]})
            # devolver la habitación a Disponible
            db.session.execute(text("""
                UPDATE Habitacion SET Estado='Disponible'
                 WHERE Codigo_Habitacion=:h
            """), {"h": r["Codigo_Habitacion"]})
            db.session.commit()
            _audit_log(_current_user_email(), "reserva.noshow", {"reserva": int(r["Codigo_Reserva"]), "fee": fee}, str(r["Codigo_Reserva"]))
            # KPI: ingresos (sin noches) para ADR correcto
            _update_kpis(nuevo_total, str(r["Fecha_Entrada"]), noches=0)
            changed += 1
        except Exception as e:
            db.session.rollback()
            current_app.logger.warning(f"[NOSHOW] {e}")

    return jsonify({"ok": True, "processed": changed})


# =========================
# GRR-01-017: Early Check-in / Late Check-out
# =========================
def _night_price_for_reserva(reserva_id: int) -> float:
    row = db.session.execute(text("""
        SELECT H.Precio_Noche
          FROM Reserva R JOIN Habitacion H ON H.Codigo_Habitacion = R.Codigo_Habitacion
         WHERE R.Codigo_Reserva=:r LIMIT 1
    """), {"r": reserva_id}).mappings().first()
    return float(row["Precio_Noche"] or 0.0) if row else 0.0

@grr_bp.post("/reservas/<int:reserva_id>/early-checkin")
def reservas_early_checkin(reserva_id: int):
    """
    Adelanta la habitación (estado 'Ocupada-temprana') y aplica cargo.
    Body: { porcentaje: 30 }  # default EARLY_CHECKIN_PERCENT (30%)
    """
    percent = float((request.json or {}).get("porcentaje") or current_app.config.get("EARLY_CHECKIN_PERCENT", 30))
    base = _night_price_for_reserva(reserva_id)
    cargo = round(base * (percent / 100.0) * (1.0 + TAX_RATE), 2)
    db.session.execute(text("""
        UPDATE Reserva
           SET Monto_Total = Monto_Total + :c,
               Observaciones = CONCAT(COALESCE(Observaciones,''), ' | Early check-in ', :p, '%')
         WHERE Codigo_Reserva=:r
    """), {"c": cargo, "p": int(percent), "r": reserva_id})
    # marcar habitación
    db.session.execute(text("""
        UPDATE Habitacion
           SET Estado='Ocupada-temprana'
         WHERE Codigo_Habitacion = (SELECT Codigo_Habitacion FROM Reserva WHERE Codigo_Reserva=:r LIMIT 1)
    """), {"r": reserva_id})
    db.session.commit()
    _audit_log(_current_user_email(), "reserva.early_checkin", {"reserva": reserva_id, "cargo": cargo, "percent": percent}, str(reserva_id))
    return jsonify({"ok": True, "cargo": cargo})

@grr_bp.post("/reservas/<int:reserva_id>/late-checkout")
def reservas_late_checkout(reserva_id: int):
    """
    Extiende salida (estado 'Ocupada-extendida') y aplica cargo.
    Body: { tramo: "tarde" }  # "medio" (30%) | "tarde" (60%) | "noche" (100%)
    Reglas por env:
      LATE_CO_30=30  LATE_CO_60=60  LATE_CO_100=100
    """
    tramo = (request.json or {}).get("tramo") or "tarde"
    p30 = float(current_app.config.get("LATE_CO_30", 30))
    p60 = float(current_app.config.get("LATE_CO_60", 60))
    p100 = float(current_app.config.get("LATE_CO_100", 100))
    table = {"medio": p30, "tarde": p60, "noche": p100}
    percent = float(table.get(tramo, p60))
    base = _night_price_for_reserva(reserva_id)
    cargo = round(base * (percent/100.0) * (1.0 + TAX_RATE), 2)

    db.session.execute(text("""
        UPDATE Reserva
           SET Monto_Total = Monto_Total + :c,
               Observaciones = CONCAT(COALESCE(Observaciones,''), ' | Late check-out ', :p, '%')
         WHERE Codigo_Reserva=:r
    """), {"c": cargo, "p": int(percent), "r": reserva_id})
    db.session.execute(text("""
        UPDATE Habitacion
           SET Estado='Ocupada-extendida'
         WHERE Codigo_Habitacion = (SELECT Codigo_Habitacion FROM Reserva WHERE Codigo_Reserva=:r LIMIT 1)
    """), {"r": reserva_id})
    db.session.commit()
    _audit_log(_current_user_email(), "reserva.late_checkout", {"reserva": reserva_id, "cargo": cargo, "percent": percent, "tramo": tramo}, str(reserva_id))
    return jsonify({"ok": True, "cargo": cargo})



# --- helpers que faltaban ---
import json
from typing import Optional
from datetime import date

def _current_user_email() -> Optional[str]:
    email, _cli = _current_user_email_and_cliente()
    return email

def _audit_log(usuario: Optional[str], accion: str, datos: dict | None = None,
               entidad_id: Optional[str] = None, entidad: str = "GRR") -> None:
    """Inserta una línea en Auditoria_Log y, por trigger, se replica en Audit_Log."""
    try:
        db.session.execute(
            text("""
                INSERT INTO Auditoria_Log (Usuario, Entidad, Entidad_Id, Accion, Datos)
                VALUES (:u, :ent, :eid, :acc, :dat)
            """),
            {
                "u": usuario or "",
                "ent": entidad,
                "eid": entidad_id,
                "acc": accion,
                "dat": json.dumps(datos or {}, ensure_ascii=False),
            },
        )
        db.session.commit()
    except Exception as e:
        if current_app:
            current_app.logger.warning(f"[AUDIT] {e}")

def _update_kpis(total_con_impuesto: float, fecha_ref: str | date, noches: int) -> None:
    """
    Suma a KPI_Stats:
      - Total_Reservas += 1
      - Total_Monto += total_con_impuesto
      - Revenue_SinImpuesto += total_con_impuesto / (1+TAX_RATE)
      - Total_Noches += noches
    """
    try:
        if isinstance(fecha_ref, str):
            f = date.fromisoformat(fecha_ref[:10])
        else:
            f = fecha_ref
        rev_sin_iva = round(float(total_con_impuesto) / (1.0 + TAX_RATE), 2)

        def up(periodo: str, clave: str):
            db.session.execute(
                text("""
                    INSERT INTO KPI_Stats (Periodo, Clave, Total_Reservas, Total_Monto, Revenue_SinImpuesto, Total_Noches)
                    VALUES (:p,:c,1,:m,:r,:n)
                    ON DUPLICATE KEY UPDATE
                      Total_Reservas = Total_Reservas + 1,
                      Total_Monto = Total_Monto + :m,
                      Revenue_SinImpuesto = Revenue_SinImpuesto + :r,
                      Total_Noches = Total_Noches + :n,
                      Fecha_Ultima = NOW()
                """),
                {"p": periodo, "c": clave, "m": total_con_impuesto, "r": rev_sin_iva, "n": int(noches)},
            )

        up("day",   f.strftime("%Y-%m-%d"))
        up("week",  f.strftime("%G-W%V"))  # ISO week
        up("month", f.strftime("%Y-%m"))
        db.session.commit()
    except Exception as e:
        if current_app:
            current_app.logger.warning(f"[KPI] {e}")
