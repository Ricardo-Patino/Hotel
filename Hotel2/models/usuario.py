from extensions import db

class Usuario(db.Model):
    __tablename__ = "usuario"

    Codigo_Usuario = db.Column(db.Integer, primary_key=True)
    Nombre = db.Column(db.String(100))
    Estado = db.Column(db.String(20))  # Activo / Inactivo