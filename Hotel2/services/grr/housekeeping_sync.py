# services/grr/housekeeping_sync.py
from __future__ import annotations
from typing import Optional, TYPE_CHECKING
from extensions import db

# Solo para type hints (no ejecuta import real en runtime → evita ciclo)
if TYPE_CHECKING:
    from models.grr import HousekeepingTask
    from models_sql import Habitacion, Reserva


def create_cleaning_task_if_needed(room_id: int) -> Optional["HousekeepingTask"]:
    """Crea una tarea de limpieza si no existe una pendiente/en proceso para la habitación."""
    from models.grr import HousekeepingTask  # import local para evitar import circular

    existing = (
        HousekeepingTask.query
        .filter(
            HousekeepingTask.Habitacion_Id == room_id,
            HousekeepingTask.Estado.in_(("Pendiente", "En proceso")),
        )
        .first()
    )
    if existing:
        return None

    task = HousekeepingTask(Habitacion_Id=room_id, Estado="Pendiente")
    db.session.add(task)
    db.session.flush()  # deja listo task.Id
    return task


def mark_room_to_cleaning(room_id: int) -> dict:
    """
    Marca la habitación en estado 'Limpieza' y crea (si hace falta) la tarea
    de Housekeeping correspondiente.
    """
    from models_sql import Habitacion  # ✅ CORRECTO: viene de models_sql, no de models.grr

    hab = Habitacion.query.get(room_id)
    if not hab:
        return {"ok": False, "error": "Habitación no encontrada."}

    hab.Estado = "Limpieza"

    task = create_cleaning_task_if_needed(room_id)
    db.session.commit()
    return {"ok": True, "task_id": getattr(task, "Id", None)}


def on_checkout_transition(reserva: "Reserva") -> None:
    """
    Llamar cuando una reserva termina/libera la habitación.
    Marca la habitación a 'Limpieza' y crea la tarea si hace falta.
    """
    from models_sql import Reserva  # solo para referencia local

    room_id = reserva.Habitacion_Id
    mark_room_to_cleaning(room_id)
