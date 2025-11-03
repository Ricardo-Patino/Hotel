# models/hrm.py
from __future__ import annotations
from datetime import datetime, date
from sqlalchemy import func
from extensions import db

# =========================
# Funcionario + Historial
# =========================
class Funcionario(db.Model):
    __tablename__ = "Funcionario"

    Codigo_Funcionario   = db.Column(db.Integer, primary_key=True, autoincrement=True)
    Cedula               = db.Column(db.String(20), nullable=False)
    Nombre               = db.Column(db.String(50), nullable=False)
    Apellido             = db.Column(db.String(50), nullable=False)
    Puesto               = db.Column(db.String(50), nullable=False)

    Departamento         = db.Column(db.String(80))
    Fecha_Nacimiento     = db.Column(db.Date, nullable=True)
    Fecha_Ingreso        = db.Column(db.Date, nullable=False, default=date.today)
    Salario_Base_Mensual = db.Column(db.Numeric(12, 2), nullable=False, default=0.00)

    Estado_Empleado      = db.Column(
        db.Enum('Activo', 'Inactivo', 'Suspendido', name='estado_empleado_enum'),
        nullable=False, default='Activo'
    )
    Tipo_Contrato        = db.Column(
        db.Enum('Tiempo completo', 'Medio tiempo', 'Servicios profesionales', 'Temporal',
                name='tipo_contrato_enum'),
        nullable=False, default='Tiempo completo'
    )

    Cuenta_Bancaria      = db.Column(db.String(60))
    Banco                = db.Column(db.String(60))

    Fecha_modificacion   = db.Column(db.DateTime, nullable=False,
                                     default=datetime.utcnow, onupdate=datetime.utcnow)
    Fecha_mod_alta       = db.Column(db.DateTime, nullable=False,
                                     default=datetime.utcnow, onupdate=datetime.utcnow)

    historial = db.relationship(
        "FuncionarioHistorial",
        backref="funcionario",
        lazy="dynamic",
        cascade="all, delete-orphan"
    )


class FuncionarioHistorial(db.Model):
    __tablename__ = "FuncionarioHistorial"

    Id_Historial       = db.Column(db.Integer, primary_key=True, autoincrement=True)
    Codigo_Funcionario = db.Column(
        db.Integer,
        db.ForeignKey("Funcionario.Codigo_Funcionario", onupdate="CASCADE", ondelete="CASCADE"),
        nullable=False, index=True
    )
    Fecha_Evento  = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)
    Tipo_Evento   = db.Column(
        db.Enum(
            'Ingreso', 'Cambio Puesto', 'Cambio Salario', 'Amonestacion',
            'Reconocimiento', 'Vacaciones', 'Incapacidad', 'Salida',
            name='tipo_evento_funcionario_enum'
        ),
        nullable=False
    )
    Detalle        = db.Column(db.String(500))
    Valor_Anterior = db.Column(db.String(200))
    Valor_Nuevo    = db.Column(db.String(200))
    Registrado_Por = db.Column(db.Integer)


# =========================
# Marcación
# =========================
class Marcacion(db.Model):
    __tablename__ = "Marcacion"

    Id                 = db.Column(db.Integer, primary_key=True, autoincrement=True)
    Codigo_Funcionario = db.Column(
        db.Integer,
        db.ForeignKey("Funcionario.Codigo_Funcionario"),
        nullable=False, index=True
    )

    Fecha           = db.Column(db.Date, nullable=False, index=True, default=date.today)
    Hora_Entrada    = db.Column(db.DateTime)
    Hora_Salida     = db.Column(db.DateTime)

    # Horas (decimales)
    Horas           = db.Column(db.Numeric(10, 2))
    Total_Horas     = db.Column(db.Numeric(10, 2))
    Horas_Regulares = db.Column(db.Numeric(6, 2))

    # Estado como texto para no chocar con ENUM al leer 'Aprobado/Rechazado'
    Estado          = db.Column(db.String(20), nullable=False, server_default="Pendiente", index=True)

    # Solo tienes 'Observaciones' en la tabla
    Observaciones   = db.Column(db.String(255))

    Fecha_Creacion  = db.Column(db.DateTime, nullable=False, server_default=func.current_timestamp())
    Fecha_Actualiza = db.Column(db.DateTime, nullable=False, server_default=func.current_timestamp(),
                                server_onupdate=func.current_timestamp())

    Funcionario     = db.relationship("Funcionario", lazy="joined")

class HoraExtra(db.Model):
    __tablename__ = "HoraExtra"

    Id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    Marcacion_Id = db.Column(db.Integer, db.ForeignKey("Marcacion.Id"), nullable=False)
    Codigo_Funcionario = db.Column(db.Integer, db.ForeignKey("Funcionario.Codigo_Funcionario"), nullable=False)
    Fecha = db.Column(db.Date, nullable=False, default=date.today)
    Horas_Extras = db.Column(db.Numeric(6, 2), nullable=False)
    Motivo = db.Column(db.String(255))
    Estado = db.Column(db.Enum("Pendiente", "Aprobado", "Rechazado"), default="Pendiente", nullable=False)
    Validado_Por = db.Column(db.String(100))

    Fecha_Registro = db.Column(db.DateTime, nullable=False, server_default=func.current_timestamp())
    Fecha_Actualiza = db.Column(db.DateTime, nullable=False,
                                server_default=func.current_timestamp(),
                                server_onupdate=func.current_timestamp())

    Funcionario = db.relationship("Funcionario", lazy="joined")
    Marcacion = db.relationship("Marcacion", lazy="joined")

