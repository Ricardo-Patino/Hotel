# models_sql.py
# Mapeos SQLAlchemy para el esquema MySQL de Hotel_VillaGrace.
# Diseñado para funcionar con la tabla Reserva extendida (Estado, Canal, Numero_Comprobante, etc.)
# y para no fallar si existen tablas opcionales (Documento, ReservaDocumento).

from __future__ import annotations
from datetime import datetime
from sqlalchemy import func
from extensions import db


# ---------------------------
# Tabla: Habitacion
# ---------------------------
class Habitacion(db.Model):
    __tablename__ = 'Habitacion'

    Codigo_Habitacion  = db.Column(db.Integer, primary_key=True, autoincrement=True)
    Numero_Habitacion  = db.Column(db.String(10), nullable=False, unique=True)
    # En MySQL es ENUM('Sencilla','Doble','Suite'), aquí usamos String para compatibilidad cruzada
    Tipo               = db.Column(db.String(20), nullable=False)
    Precio_Noche       = db.Column(db.Numeric(12, 2), nullable=False)
    # En MySQL: ENUM('Disponible','Ocupada','Mantenimiento')
    Estado             = db.Column(db.String(20), nullable=False, default='Disponible')
    Fecha_modificacion = db.Column(
        db.DateTime,
        nullable=False,
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
        db.DateTime,
        nullable=False,
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
        db.DateTime,
        nullable=False,
        server_default=func.current_timestamp(),
        server_onupdate=func.current_timestamp()
    )


# ---------------------------
# Tabla: Reserva  (versión extendida)
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

    # Campos operativos (presentes en tu error y, por tanto, en la tabla real)
    Estado             = db.Column(db.String(20), nullable=False, default='Confirmada')
    Canal              = db.Column(db.String(30), nullable=True)
    Numero_Comprobante = db.Column(db.String(32), nullable=True, unique=False)
    Fuente             = db.Column(db.String(30), nullable=True)
    Politica_Cancelacion = db.Column(db.String(200), nullable=True)
    Observaciones      = db.Column(db.String(255), nullable=True)
    Descuento_Id       = db.Column(db.String(50), nullable=True)

    # Clave del fix: default en servidor para evitar enviar NULL
    Fecha_Registro     = db.Column(
        db.DateTime,
        nullable=False,
        server_default=func.current_timestamp()
    )

    # Relaciones útiles
    Habitacion = db.relationship('Habitacion', lazy='joined')
    Cliente    = db.relationship('Cliente', lazy='joined')
    Funcionario = db.relationship('Funcionario', lazy='joined')


# ---------------------------
# Tablas opcionales para comprobantes
# (si no existen en tu BD, las operaciones que las usen deberían estar en try/except)
# ---------------------------
class Documento(db.Model):
    __tablename__ = 'Documento'

    Id            = db.Column(db.Integer, primary_key=True, autoincrement=True)
    Tipo          = db.Column(db.String(50), nullable=False)  # p.ej. 'Comprobante'
    Ruta          = db.Column(db.String(255), nullable=False)
    MimeType      = db.Column(db.String(100), nullable=False, default='application/pdf')
    TamanoBytes   = db.Column(db.Integer, nullable=True)
    Fecha_Creacion = db.Column(db.DateTime, nullable=False, server_default=func.current_timestamp())


class ReservaDocumento(db.Model):
    __tablename__ = 'ReservaDocumento'

    Id              = db.Column(db.Integer, primary_key=True, autoincrement=True)
    Codigo_Reserva  = db.Column(db.Integer, db.ForeignKey('Reserva.Codigo_Reserva'), nullable=False, index=True)
    Documento_Id    = db.Column(db.Integer, db.ForeignKey('Documento.Id'), nullable=False, index=True)
