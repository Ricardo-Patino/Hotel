import logging
import os
import uuid
from typing import Literal

from extensions import db
from flask import Response, current_app, jsonify, request, url_for
from models_sql import Habitacion, HabitacionImagen
from services.grr.housekeeping_sync import create_cleaning_task_if_needed
from sqlalchemy.exc import DataError, IntegrityError
from utils.auth import role_required
from werkzeug.utils import secure_filename


from . import rooms_bp

from datetime import date, timedelta
from sqlalchemy import text
from utils.auth import role_required



rooms_logger = logging.getLogger(__name__)


# =========================================================
# Helpers
# =========================================================
def _json_error(message: str, code: int = 400) -> Response:
    return jsonify({"error": message}), code


def _coerce_room_payload(payload: dict) -> dict:
    """
    Normaliza payloads desde UI:
    - Limpia strings vacíos
    - Convierte Precio_Noche a float si aplica
    """
    if not isinstance(payload, dict):
        return {}

    cleaned = {}
    for k, v in payload.items():
        if isinstance(v, str):
            v2 = v.strip()
            if v2 == "":
                continue
            cleaned[k] = v2
        else:
            cleaned[k] = v

    # Precio_Noche
    if "Precio_Noche" in cleaned:
        try:
            cleaned["Precio_Noche"] = float(cleaned["Precio_Noche"])
        except Exception:
            pass

    return cleaned


def _serialize_room(h: Habitacion) -> dict:
    data = {col.name: str(getattr(h, col.name)) for col in h.__table__.columns}

    primary = (
        HabitacionImagen.query.filter_by(Codigo_Habitacion=h.Codigo_Habitacion, Is_Principal=True)
        .order_by(HabitacionImagen.Sort_Order.asc(), HabitacionImagen.Id.asc())
        .first()
    )

    if primary:
        data["Primary_Image_Path"] = primary.File_Path
        data["Primary_Image_Url"] = url_for("static", filename=primary.File_Path)
    else:
        data["Primary_Image_Path"] = None
        data["Primary_Image_Url"] = None

    data["Images_Count"] = (
        HabitacionImagen.query.filter_by(Codigo_Habitacion=h.Codigo_Habitacion).count()
    )

    return data


def _serialize_image(i: HabitacionImagen) -> dict:
    return {
        "Id": i.Id,
        "Codigo_Habitacion": i.Codigo_Habitacion,
        "File_Path": i.File_Path,
        "Url": url_for("static", filename=i.File_Path),
        "Mime_Type": i.Mime_Type,
        "Bytes": i.Bytes,
        "Is_Principal": bool(i.Is_Principal),
        "Sort_Order": int(i.Sort_Order or 0),
        "Alt_Text": i.Alt_Text,
        "Created_At": i.Created_At.isoformat() if i.Created_At else None,
        "Updated_At": i.Updated_At.isoformat() if i.Updated_At else None,
    }


def _allowed_image_ext(ext: str) -> bool:
    allowed = {".jpg", ".jpeg", ".png", ".webp"}
    return ext.lower() in allowed


def _room_upload_dir(room_id: int) -> str:
    subdir = os.path.join("uploads", "rooms", str(room_id))
    return os.path.join(current_app.static_folder, subdir)


def _safe_relpath(abs_path: str) -> str:
    rel = os.path.relpath(abs_path, current_app.static_folder)
    return rel.replace("\\", "/")


def _save_uploaded_image(room_id: int, file_storage, max_bytes: int) -> tuple[str, int, str]:
    """
    Guarda la imagen a disco en static/uploads/rooms/<room_id>/
    Retorna: (rel_path, bytes, mime)
    """
    filename = file_storage.filename or ""
    ext = os.path.splitext(filename)[1].lower()

    if not _allowed_image_ext(ext):
        raise ValueError("Formato de imagen no permitido. Usa JPG, PNG o WEBP.")

    base = secure_filename(os.path.splitext(filename)[0])[:60] or "room"
    new_name = f"{base}-{uuid.uuid4().hex}{ext}"

    dest_dir = _room_upload_dir(room_id)
    os.makedirs(dest_dir, exist_ok=True)

    abs_path = os.path.join(dest_dir, new_name)

    size = 0
    with open(abs_path, "wb") as f:
        while True:
            chunk = file_storage.stream.read(1024 * 1024)
            if not chunk:
                break
            size += len(chunk)
            if size > max_bytes:
                try:
                    f.close()
                except Exception:
                    pass
                try:
                    os.remove(abs_path)
                except Exception:
                    pass
                raise ValueError(f"Imagen demasiado pesada. Máximo permitido: {max_bytes} bytes.")
            f.write(chunk)

    rel_path = _safe_relpath(abs_path)
    mime = getattr(file_storage, "mimetype", None) or None
    return rel_path, size, mime


def _delete_static_file(rel_path: str) -> None:
    try:
        abs_path = os.path.join(current_app.static_folder, rel_path)
        if os.path.exists(abs_path):
            os.remove(abs_path)
    except Exception:
        rooms_logger.exception("No se pudo eliminar archivo: %s", rel_path)


def _cleanup_room_folder(room_id: int) -> None:
    try:
        d = _room_upload_dir(room_id)
        if os.path.isdir(d) and not os.listdir(d):
            os.rmdir(d)
    except Exception:
        # best-effort
        pass


# =========================================================
# CRUD Habitaciones (ya existía, se extiende con imágenes)
# =========================================================
def _create(payload: dict) -> Habitacion:
    habitacion = Habitacion(**payload)
    db.session.add(habitacion)
    db.session.commit()
    db.session.refresh(habitacion)
    return habitacion


def _update(room_id: int, payload: dict) -> Habitacion | None:
    habitacion = Habitacion.query.get(room_id)
    if not habitacion:
        return None
    for key, value in payload.items():
        setattr(habitacion, key, value)
    db.session.commit()
    db.session.refresh(habitacion)
    return habitacion


def _delete(room_id: int) -> Habitacion | None:
    habitacion = Habitacion.query.get(room_id)
    if not habitacion:
        return None

    # Capturamos rutas para eliminar archivos del disco (DB hará cascade delete)
    imgs = HabitacionImagen.query.filter_by(Codigo_Habitacion=room_id).all()
    paths = [i.File_Path for i in imgs]

    # Delete directo (DB cascade elimina HabitacionImagen)
    db.session.query(Habitacion).filter(Habitacion.Codigo_Habitacion == room_id).delete(
        synchronize_session=False
    )
    db.session.commit()

    for p in paths:
        _delete_static_file(p)
    _cleanup_room_folder(room_id)
    return habitacion


@rooms_bp.route("/admin-rooms", methods=["GET"])
@role_required("Administrador", "Recepcionista")
def rooms_read():
    rooms = Habitacion.query.order_by(Habitacion.Numero_Habitacion.asc()).all()
    return jsonify([_serialize_room(r) for r in rooms])


@rooms_bp.route("/admin-rooms", methods=["POST"])
@role_required("Administrador")
def rooms_create():
    payload = request.get_json(silent=True) or {}
    payload = _coerce_room_payload(payload)

    required = ["Numero_Habitacion", "Tipo", "Precio_Noche", "Estado"]
    for f in required:
        if f not in payload:
            return _json_error(f"Falta el campo requerido: {f}", 400)

    try:
        room = _create(payload)
        return jsonify(_serialize_room(room))
    except IntegrityError:
        db.session.rollback()
        return _json_error("Número de habitación ya existe o datos inválidos.", 409)
    except DataError:
        db.session.rollback()
        return _json_error("Datos inválidos (revisa longitudes y formatos).", 400)
    except Exception:
        db.session.rollback()
        rooms_logger.exception("Error creando habitación")
        return _json_error("Error interno", 500)


@rooms_bp.route("/admin-rooms/<int:room_id>", methods=["PUT"])
@role_required("Administrador")
def rooms_update(room_id):
    payload = request.get_json(silent=True) or {}
    payload = _coerce_room_payload(payload)

    # Si cambia a Limpieza, se crea task housekeeping (ya existía en el proyecto)
    if payload.get("Estado") == "Limpieza":
        create_cleaning_task_if_needed(room_id)

    try:
        room = _update(room_id, payload)
        if not room:
            return _json_error("Habitación no encontrada", 404)
        return jsonify(_serialize_room(room))
    except IntegrityError:
        db.session.rollback()
        return _json_error("Conflicto: número de habitación duplicado u otro constraint.", 409)
    except DataError:
        db.session.rollback()
        return _json_error("Datos inválidos (revisa longitudes y formatos).", 400)
    except Exception:
        db.session.rollback()
        rooms_logger.exception("Error actualizando habitación")
        return _json_error("Error interno", 500)


@rooms_bp.route("/admin-rooms/<int:room_id>", methods=["DELETE"])
@role_required("Administrador")
def rooms_delete(room_id):
    try:
        room = _delete(room_id)
        if not room:
            return _json_error("Habitación no encontrada", 404)
        return jsonify({"ok": True})
    except Exception:
        db.session.rollback()
        rooms_logger.exception("Error eliminando habitación")
        return _json_error("Error interno", 500)


# =========================================================
# Imágenes de habitación (Paso 1)
# =========================================================
@rooms_bp.route("/admin-rooms/<int:room_id>/images", methods=["GET"])
@role_required("Administrador", "Recepcionista")
def room_images_list(room_id: int):
    exists = Habitacion.query.get(room_id)
    if not exists:
        return _json_error("Habitación no encontrada", 404)

    imgs = (
        HabitacionImagen.query.filter_by(Codigo_Habitacion=room_id)
        .order_by(HabitacionImagen.Is_Principal.desc(), HabitacionImagen.Sort_Order.asc(), HabitacionImagen.Id.asc())
        .all()
    )
    return jsonify([_serialize_image(i) for i in imgs])


@rooms_bp.route("/admin-rooms/<int:room_id>/images", methods=["POST"])
@role_required("Administrador")
def room_images_upload(room_id: int):
    exists = Habitacion.query.get(room_id)
    if not exists:
        return _json_error("Habitación no encontrada", 404)

    if "image" not in request.files:
        return _json_error("Falta el archivo 'image' en multipart/form-data.", 400)

    f = request.files["image"]
    if not f or not f.filename:
        return _json_error("Archivo inválido.", 400)

    max_bytes = int(current_app.config.get("ROOM_IMAGES_MAX_BYTES", 2 * 1024 * 1024))
    make_primary = str(request.form.get("is_principal", "0")).lower() in ("1", "true", "on", "yes")
    alt_text = request.form.get("alt_text")
    sort_order = request.form.get("sort_order")
    try:
        sort_order = int(sort_order) if sort_order is not None and str(sort_order).strip() != "" else 0
    except Exception:
        sort_order = 0

    try:
        rel_path, size, mime = _save_uploaded_image(room_id, f, max_bytes)

        # Si no hay ninguna imagen aún, la primera debe ser principal
        has_any = HabitacionImagen.query.filter_by(Codigo_Habitacion=room_id).count() > 0
        if not has_any:
            make_primary = True

        if make_primary:
            HabitacionImagen.query.filter_by(Codigo_Habitacion=room_id).update({"Is_Principal": False})

        img = HabitacionImagen(
            Codigo_Habitacion=room_id,
            File_Path=rel_path,
            Mime_Type=mime,
            Bytes=size,
            Is_Principal=bool(make_primary),
            Sort_Order=sort_order,
            Alt_Text=alt_text,
        )
        db.session.add(img)
        db.session.commit()
        db.session.refresh(img)
        return jsonify(_serialize_image(img))

    except ValueError as ve:
        db.session.rollback()
        return _json_error(str(ve), 400)
    except Exception:
        db.session.rollback()
        rooms_logger.exception("Error subiendo imagen de habitación")
        return _json_error("Error interno", 500)


@rooms_bp.route("/admin-rooms/images/<int:image_id>", methods=["PUT"])
@role_required("Administrador")
def room_image_update(image_id: int):
    payload = request.get_json(silent=True) or {}

    img = HabitacionImagen.query.get(image_id)
    if not img:
        return _json_error("Imagen no encontrada", 404)

    try:
        if "Alt_Text" in payload:
            img.Alt_Text = payload.get("Alt_Text")
        if "Sort_Order" in payload:
            try:
                img.Sort_Order = int(payload.get("Sort_Order") or 0)
            except Exception:
                img.Sort_Order = 0

        if payload.get("Is_Principal") is True:
            HabitacionImagen.query.filter_by(Codigo_Habitacion=img.Codigo_Habitacion).update({"Is_Principal": False})
            img.Is_Principal = True

        db.session.commit()
        db.session.refresh(img)
        return jsonify(_serialize_image(img))
    except Exception:
        db.session.rollback()
        rooms_logger.exception("Error actualizando metadata de imagen")
        return _json_error("Error interno", 500)


@rooms_bp.route("/admin-rooms/images/<int:image_id>", methods=["DELETE"])
@role_required("Administrador")
def room_image_delete(image_id: int):
    img = HabitacionImagen.query.get(image_id)
    if not img:
        return _json_error("Imagen no encontrada", 404)

    room_id = img.Codigo_Habitacion
    rel_path = img.File_Path
    was_primary = bool(img.Is_Principal)

    try:
        db.session.delete(img)
        db.session.commit()
        _delete_static_file(rel_path)

        # Si se eliminó la principal, escoger otra
        if was_primary:
            next_img = (
                HabitacionImagen.query.filter_by(Codigo_Habitacion=room_id)
                .order_by(HabitacionImagen.Sort_Order.asc(), HabitacionImagen.Id.asc())
                .first()
            )
            if next_img:
                HabitacionImagen.query.filter_by(Codigo_Habitacion=room_id).update({"Is_Principal": False})
                next_img.Is_Principal = True
                db.session.commit()

        _cleanup_room_folder(room_id)
        return jsonify({"ok": True})
    except Exception:
        db.session.rollback()
        rooms_logger.exception("Error eliminando imagen")
        return _json_error("Error interno", 500)
    
    
    
@rooms_bp.get("/admin-rooms/dashboard-summary")
@role_required("Administrador", "Recepcionista")
def rooms_dashboard_summary():
    """
    Resumen operativo de habitaciones (solo lectura).
    Usa info real de:
      - Habitacion (Estado/Tipo/Precio)
      - Reserva (llegadas/salidas/ocupación por fechas)
      - HousekeepingTask (pendientes/en proceso)
      - MaintenanceRequest (pendiente/en progreso)
    """
    hoy = date.today()

    # ---- Habitaciones (totales / por estado / por tipo) ----
    total_rooms = db.session.execute(text("SELECT COUNT(*) FROM Habitacion")).scalar() or 0

    by_status_rows = db.session.execute(text("""
        SELECT Estado, COUNT(*) AS c
        FROM Habitacion
        GROUP BY Estado
    """)).mappings().all()

    by_type_rows = db.session.execute(text("""
        SELECT Tipo, COUNT(*) AS c
        FROM Habitacion
        GROUP BY Tipo
    """)).mappings().all()

    by_status = {r["Estado"]: int(r["c"]) for r in by_status_rows}
    by_type = {r["Tipo"]: int(r["c"]) for r in by_type_rows}

    # "Ocupadas" por estado (incluye Ocupada-temprana / Ocupada-extendida)
    occupied_by_status = int(db.session.execute(text("""
        SELECT COUNT(*)
        FROM Habitacion
        WHERE Estado LIKE 'Ocupada%'
    """)).scalar() or 0)

    available_now = int(by_status.get("Disponible", 0))
    in_cleaning = int(by_status.get("Limpieza", 0))
    in_maintenance = int(by_status.get("Mantenimiento", 0))

    # ---- Reservas: llegadas/salidas y ocupación por fecha (Confirmadas) ----
    arrivals_today = int(db.session.execute(text("""
        SELECT COUNT(*)
        FROM Reserva
        WHERE Estado='Confirmada' AND Fecha_Entrada = :hoy
    """), {"hoy": hoy}).scalar() or 0)

    departures_today = int(db.session.execute(text("""
        SELECT COUNT(*)
        FROM Reserva
        WHERE Estado='Confirmada' AND Fecha_Salida = :hoy
    """), {"hoy": hoy}).scalar() or 0)

    occupied_by_reservation_today = int(db.session.execute(text("""
        SELECT COUNT(DISTINCT Codigo_Habitacion)
        FROM Reserva
        WHERE Estado='Confirmada'
          AND Fecha_Entrada <= :hoy
          AND Fecha_Salida > :hoy
    """), {"hoy": hoy}).scalar() or 0)

    occupancy_pct_today = 0
    if total_rooms > 0:
        occupancy_pct_today = round((occupied_by_reservation_today / total_rooms) * 100, 2)

    # Próximos 7 días (ocupación estimada por reservas confirmadas)
    next7 = []
    for i in range(0, 7):
        d = hoy + timedelta(days=i)
        occ = int(db.session.execute(text("""
            SELECT COUNT(DISTINCT Codigo_Habitacion)
            FROM Reserva
            WHERE Estado='Confirmada'
              AND Fecha_Entrada <= :d
              AND Fecha_Salida > :d
        """), {"d": d}).scalar() or 0)

        avail = max(int(total_rooms) - int(occ), 0)
        pct = 0
        if total_rooms > 0:
            pct = round((occ / total_rooms) * 100, 2)

        next7.append({
            "date": d.isoformat(),
            "occupied": occ,
            "available": avail,
            "occupancy_pct": pct
        })

    # ---- HousekeepingTask ----
    hk_rows = db.session.execute(text("""
        SELECT Estado, COUNT(*) AS c
        FROM HousekeepingTask
        WHERE Estado IN ('Pendiente','En proceso')
        GROUP BY Estado
    """)).mappings().all()

    hk_by_status = {r["Estado"]: int(r["c"]) for r in hk_rows}
    hk_open_total = int(sum(hk_by_status.values()))

    # ---- MaintenanceRequest ----
    mr_rows = db.session.execute(text("""
        SELECT Estado, COUNT(*) AS c
        FROM MaintenanceRequest
        WHERE Estado IN ('Pendiente','En progreso')
        GROUP BY Estado
    """)).mappings().all()

    mr_by_status = {r["Estado"]: int(r["c"]) for r in mr_rows}
    mr_open_total = int(sum(mr_by_status.values()))

    return jsonify({
        "ok": True,
        "date": hoy.isoformat(),
        "rooms": {
            "total": int(total_rooms),
            "by_status": by_status,
            "by_type": by_type,
            "available_now": available_now,
            "occupied_by_status": occupied_by_status,
            "in_cleaning": in_cleaning,
            "in_maintenance": in_maintenance
        },
        "reservations": {
            "arrivals_today": arrivals_today,
            "departures_today": departures_today,
            "occupied_today": occupied_by_reservation_today,
            "occupancy_pct_today": occupancy_pct_today,
            "next7": next7
        },
        "housekeeping": {
            "open_total": hk_open_total,
            "by_status": hk_by_status
        },
        "maintenance": {
            "open_total": mr_open_total,
            "by_status": mr_by_status
        }
    })

