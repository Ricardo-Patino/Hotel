# services/grr/availability_service.py
from typing import Tuple
from sqlalchemy import and_
from extensions import db

class AvailabilityService:
    def __init__(self, habitacion_model, reserva_model):
        self.H = habitacion_model
        self.R = reserva_model

    def _overlaps(self, entrada, salida):
        R = self.R
        return and_(R.Fecha_Entrada < salida, R.Fecha_Salida > entrada,
                    R.Estado.in_(['Pendiente','Confirmada','CheckIn']))

    def validate(self, fecha_entrada, fecha_salida, tipo, capacidad, excluir_reserva_id=None) -> Tuple[bool, list]:
        subq = db.session.query(self.R.Codigo_Habitacion).filter(self._overlaps(fecha_entrada, fecha_salida))
        if excluir_reserva_id:
            subq = subq.filter(self.R.Codigo_Reserva != excluir_reserva_id)
        ocupadas = [hid for (hid,) in subq.distinct()]
        q = self.H.query.filter(self.H.Tipo == tipo)
        if ocupadas:
            q = q.filter(~self.H.Codigo_Habitacion.in_(ocupadas))
        # (capacidad: si la agregas como columna en Habitacion, filtra aquí)
        opciones = q.all()
        return (len(opciones) > 0, opciones)
