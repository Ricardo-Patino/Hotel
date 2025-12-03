# models/room.py
from extensions import db
from datetime import datetime

class Room(db.Model):
    __tablename__ = "habitacion"

    Codigo_Habitacion = db.Column(db.Integer, primary_key=True)
    Numero_Habitacion = db.Column(db.String(10), nullable=False)
    Tipo = db.Column(db.String(50), nullable=False)
    Precio_Noche = db.Column(db.Numeric(10, 2), nullable=False)
    Estado = db.Column(db.String(50), nullable=False, default="Disponible")
    Fecha_modificacion = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    def __repr__(self):
        return f"<Room {self.Numero_Habitacion} ({self.Tipo})>"
