# services/grr/housekeeping_sync.py
from extensions import db
from models.grr import HousekeepingTask

class HousekeepingSync:
    def enqueue_cleaning(self, codigo_habitacion: int, prioridad: str = 'Media', origen: str = 'CheckOut'):
        task = HousekeepingTask(Codigo_Habitacion=codigo_habitacion, Tipo='Limpieza', Prioridad=prioridad, Origen=origen)
        db.session.add(task)
        db.session.commit()
        return task.Id
