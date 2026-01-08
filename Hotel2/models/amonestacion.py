from extensions import db

from datetime import date

class Amonestacion(db.Model):
    __tablename__ = "amonestacion"

    Id = db.Column(db.Integer, primary_key=True)
    Codigo_Usuario = db.Column(db.Integer, nullable=False)
    Severidad = db.Column(db.String(10))  # LEVE / MEDIA / GRAVE
    Motivo = db.Column(db.String(255))
    Audit_Create_User = db.Column(db.Integer)
    Audit_Create_Date = db.Column(db.Date, default=date.today)