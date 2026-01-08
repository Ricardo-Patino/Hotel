from extensions import db

from datetime import date

class Ausencia(db.Model):
    __tablename__ = "ausencia"

    Id = db.Column(db.Integer, primary_key=True)
    Codigo_Usuario = db.Column(db.Integer, nullable=False)
    Fecha = db.Column(db.Date, nullable=False)
    Horas = db.Column(db.Float, default=0)
    Motivo = db.Column(db.String(255))
    Audit_Create_User = db.Column(db.Integer)
    Audit_Create_Date = db.Column(db.Date, default=date.today)