# models_sql.py
# Mapeos SQLAlchemy para el esquema MySQL de Hotel_VillaGrace.
# Incluye tabla Rol y Usuario con FK Rol_Id (y FK opcional a Cliente).
# Reserva está en su versión extendida (Estado, Canal, Numero_Comprobante, etc.).

from __future__ import annotations

from datetime import datetime
from sqlalchemy import func
from extensions import db
from werkzeug.security import generate_password_hash, check_password_hash


# ---------------------------
# Tabla: Rol
# ---------------------------
class Rol(db.Model):
    __tablename__ = "Rol"

    Codigo_Rol         = db.Column(db.Integer, primary_key=True, autoincrement=True)
    Nombre             = db.Column(db.String(30), unique=True, nullable=False)
    Descripcion        = db.Column(db.String(200))
    Estado             = db.Column(db.String(10), nullable=False, default="Activo")  # ENUM en SQL
    Fecha_Creacion     = db.Column(db.DateTime, nullable=False, server_default=func.current_timestamp())
    Fecha_Modificacion = db.Column(
        db.DateTime, nullable=False,
        server_default=func.current_timestamp(),
        server_onupdate=func.current_timestamp()
    )


# ---------------------------
# Tabla: Habitacion
# ---------------------------
class Habitacion(db.Model):
    __tablename__ = 'Habitacion'

    Codigo_Habitacion  = db.Column(db.Integer, primary_key=True, autoincrement=True)
    Numero_Habitacion  = db.Column(db.String(10), nullable=False, unique=True)
    Tipo               = db.Column(db.String(20), nullable=False)  # ENUM en SQL
    Precio_Noche       = db.Column(db.Numeric(12, 2), nullable=False)
    Estado             = db.Column(db.String(20), nullable=False, default='Disponible')  # ENUM en SQL
    Fecha_modificacion = db.Column(
        db.DateTime, nullable=False,
        server_default=func.current_timestamp(),
        server_onupdate=func.current_timestamp()
    )


# ---------------------------
# Tabla: Funcionario
# ---------------------------
class Funcionario(db.Model):
    __tablename__ = 'Funcionario'

    Codigo_Funcionario  = db.Column(db.Integer, primary_key=True, autoincrement=True)
    Cedula              = db.Column(db.Integer, nullable=False)
    Nombre              = db.Column(db.String(50), nullable=False)
    Apellido            = db.Column(db.String(50), nullable=False)
    Puesto              = db.Column(db.String(50), nullable=False)
    Fecha_Nacimiento    = db.Column(db.Date, nullable=False)
    Fecha_modificacion  = db.Column(
        db.DateTime, nullable=False,
        server_default=func.current_timestamp(),
        server_onupdate=func.current_timestamp()
    )


# ---------------------------
# Tabla: Cliente
# ---------------------------
class Cliente(db.Model):
    __tablename__ = 'Cliente'

    Codigo_Cliente      = db.Column(db.Integer, primary_key=True, autoincrement=True)
    Cedula              = db.Column(db.Integer, nullable=False)
    Nombre              = db.Column(db.String(50), nullable=False)
    Apellido            = db.Column(db.String(50), nullable=False)
    Telefono            = db.Column(db.String(20), nullable=False)
    Correo              = db.Column(db.String(100))
    Fecha_Nacimiento    = db.Column(db.Date, nullable=False)
    Fecha_modificacion  = db.Column(
        db.DateTime, nullable=False,
        server_default=func.current_timestamp(),
        server_onupdate=func.current_timestamp()
    )

    usuarios = db.relationship("Usuario", back_populates="cliente", lazy="dynamic")


# ---------------------------
# Tabla: Usuario
# ---------------------------
class Usuario(db.Model):
    __tablename__ = "Usuario"

    Codigo_Usuario     = db.Column(db.Integer, primary_key=True, autoincrement=True)
    Nombre             = db.Column(db.String(100), nullable=False)
    Cedula_Pasaporte   = db.Column(db.String(40), unique=True)
    Correo             = db.Column(db.String(120), unique=True, index=True, nullable=False)
    Telefono           = db.Column(db.String(25), nullable=False)
    Contrasena         = db.Column(db.String(255), nullable=False)  # hash

    Rol_Id             = db.Column(db.Integer, db.ForeignKey("Rol.Codigo_Rol"), nullable=True, index=True)
    Estado             = db.Column(db.String(10), default="Activo")  # ENUM en SQL
    Fecha_Creacion     = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
    Fecha_Modificacion = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False)

    Codigo_Cliente     = db.Column(db.Integer, db.ForeignKey("Cliente.Codigo_Cliente"), nullable=True, index=True)

    rol     = db.relationship("Rol", lazy="joined")
    cliente = db.relationship("Cliente", back_populates="usuarios", lazy="joined")

    # Helpers
    def set_password(self, raw: str):
        self.Contrasena = generate_password_hash(raw)

    def check_password(self, raw: str) -> bool:
        return check_password_hash(self.Contrasena, raw)

    @property
    def rol_nombre(self) -> str | None:
        return self.rol.Nombre if self.rol else None


# ---------------------------
# Tabla: Reserva (extendida)
# ---------------------------
class Reserva(db.Model):
    __tablename__ = 'Reserva'

    Codigo_Reserva     = db.Column(db.Integer, primary_key=True, autoincrement=True)
    Codigo_Cliente     = db.Column(db.Integer, db.ForeignKey('Cliente.Codigo_Cliente'), nullable=False, index=True)
    Codigo_Habitacion  = db.Column(db.Integer, db.ForeignKey('Habitacion.Codigo_Habitacion'), nullable=False, index=True)
    Codigo_Funcionario = db.Column(db.Integer, db.ForeignKey('Funcionario.Codigo_Funcionario'), nullable=False, index=True)

    Fecha_Entrada      = db.Column(db.Date, nullable=False)
    Fecha_Salida       = db.Column(db.Date, nullable=False)
    Monto_Total        = db.Column(db.Numeric(12, 2), nullable=False)

    Estado               = db.Column(db.String(20), nullable=False, default='Confirmada')
    Canal                = db.Column(db.String(30), nullable=True)
    Numero_Comprobante   = db.Column(db.String(32), nullable=True)
    Fuente               = db.Column(db.String(30), nullable=True)
    Politica_Cancelacion = db.Column(db.String(200), nullable=True)
    Observaciones        = db.Column(db.String(255), nullable=True)
    Descuento_Id         = db.Column(db.String(50), nullable=True)

    Fecha_Registro     = db.Column(db.DateTime, nullable=False, server_default=func.current_timestamp())

    Habitacion  = db.relationship('Habitacion', lazy='joined')
    Cliente     = db.relationship('Cliente', lazy='joined')
    Funcionario = db.relationship('Funcionario', lazy='joined')


# ---------------------------
# Tablas opcionales para comprobantes
# ---------------------------
class Documento(db.Model):
    __tablename__ = 'Documento'

    Id             = db.Column(db.Integer, primary_key=True, autoincrement=True)
    Tipo           = db.Column(db.String(50), nullable=False)
    Ruta           = db.Column(db.String(255), nullable=False)
    MimeType       = db.Column(db.String(100), nullable=False, default='application/pdf')
    TamanoBytes    = db.Column(db.Integer, nullable=True)
    Fecha_Creacion = db.Column(db.DateTime, nullable=False, server_default=func.current_timestamp())


class ReservaDocumento(db.Model):
    __tablename__ = 'ReservaDocumento'

    Id             = db.Column(db.Integer, primary_key=True, autoincrement=True)
    Codigo_Reserva = db.Column(db.Integer, db.ForeignKey('Reserva.Codigo_Reserva'), nullable=False, index=True)
    Documento_Id   = db.Column(db.Integer, db.ForeignKey('Documento.Id'), nullable=False, index=True)
