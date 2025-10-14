# services/grr/housekeeping_sync.py
from extensions import db
from models.grr import HousekeepingTask
from datetime import date
from models_sql import LimpiezaOrden, Habitacion, Reserva

class HousekeepingSync:
    def enqueue_cleaning(self, codigo_habitacion: int, prioridad: str = 'Media', origen: str = 'CheckOut'):
        task = HousekeepingTask(Codigo_Habitacion=codigo_habitacion, Tipo='Limpieza', Prioridad=prioridad, Origen=origen)
        db.session.add(task)
        db.session.commit()
        return task.Id


def create_cleaning_order_if_needed(hab_id: int) -> LimpiezaOrden | None:
    # Evita duplicar si hay orden pendiente/en curso
    existing = (LimpiezaOrden.query
                .filter(LimpiezaOrden.Codigo_Habitacion == hab_id,
                        LimpiezaOrden.Estado.in_(('Pendiente','EnProceso')))
                .first())
    if existing:
        return None
    orden = LimpiezaOrden(Codigo_Habitacion=hab_id, Estado='Pendiente')
    db.session.add(orden)
    db.session.flush()
    return orden

def on_checkout_transition(reserva: Reserva):
    """Invoca cuando una reserva pasa a 'Finalizada' o el cuarto queda libre."""
    hab = Habitacion.query.get(reserva.Codigo_Habitacion)
    if not hab:
        return
    # marcamos la habitación para limpieza
    hab.Estado = 'Limpieza'
    create_cleaning_order_if_needed(hab.Codigo_Habitacion)