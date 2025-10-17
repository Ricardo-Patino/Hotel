# models/inv.py
from datetime import datetime
from extensions import db

class InvCategoria(db.Model):
    __tablename__ = "Inv_Categoria"

    Id                  = db.Column(db.Integer, primary_key=True, autoincrement=True)
    Nombre              = db.Column(db.String(80), nullable=False, unique=True, index=True)
    Descripcion         = db.Column(db.String(255), nullable=True)
    Activa              = db.Column(db.Boolean, nullable=False, default=True, index=True)
    Fecha_Creacion      = db.Column(db.DateTime, nullable=False, server_default=db.func.current_timestamp())
    Fecha_Actualizacion = db.Column(db.DateTime, nullable=False, server_default=db.func.current_timestamp(),
                                    server_onupdate=db.func.current_timestamp())

    def __repr__(self) -> str:
        return f"<InvCategoria {self.Id} {self.Nombre} activa={self.Activa}>"
