# models/hrm.py
from datetime import datetime
from extensions import db

class Funcionario(db.Model):
    __tablename__ = "Funcionario"

    Codigo_Funcionario = db.Column(db.Integer, primary_key=True, autoincrement=True)
    Cedula             = db.Column(db.String(20), nullable=False)
    Nombre             = db.Column(db.String(50), nullable=False)
    Apellido           = db.Column(db.String(50), nullable=False)
    Puesto             = db.Column(db.String(50), nullable=False)

    Departamento         = db.Column(db.String(80))
    Fecha_Nacimiento     = db.Column(db.Date, nullable=False)
    Fecha_Ingreso        = db.Column(db.Date, nullable=False, default=datetime.utcnow)
    Salario_Base_Mensual = db.Column(db.Numeric(12, 2), nullable=False, default=0.00)
    Estado_Empleado      = db.Column(
        db.Enum('Activo', 'Inactivo', 'Suspendido', name='estado_empleado_enum'),
        nullable=False,
        default='Activo'
    )
    Tipo_Contrato        = db.Column(
        db.Enum('Tiempo completo', 'Medio tiempo', 'Servicios profesionales', 'Temporal', name='tipo_contrato_enum'),
        nullable=False,
        default='Tiempo completo'
    )
    Cuenta_Bancaria      = db.Column(db.String(60))
    Banco                = db.Column(db.String(60))
    Fecha_modificacion   = db.Column(db.DateTime, nullable=False, default=datetime.utcnow, onupdate=datetime.utcnow)
    Fecha_mod_alta       = db.Column(db.DateTime, nullable=False, default=datetime.utcnow, onupdate=datetime.utcnow)

    historial = db.relationship(
        "FuncionarioHistorial",
        backref="funcionario",
        lazy="dynamic",
        cascade="all, delete-orphan"
    )


class FuncionarioHistorial(db.Model):
    __tablename__ = "FuncionarioHistorial"

    Id_Historial = db.Column(db.Integer, primary_key=True, autoincrement=True)
    Codigo_Funcionario = db.Column(
        db.Integer,
        db.ForeignKey("Funcionario.Codigo_Funcionario", onupdate="CASCADE", ondelete="CASCADE"),
        nullable=False
    )
    Fecha_Evento = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)
    Tipo_Evento  = db.Column(
        db.Enum(
            'Ingreso',
            'Cambio Puesto',
            'Cambio Salario',
            'Amonestacion',
            'Reconocimiento',
            'Vacaciones',
            'Incapacidad',
            'Salida',
            name='tipo_evento_funcionario_enum'
        ),
        nullable=False
    )
    Detalle        = db.Column(db.String(500))
    Valor_Anterior = db.Column(db.String(200))
    Valor_Nuevo    = db.Column(db.String(200))
    Registrado_Por = db.Column(db.Integer)

# --- Marcación de horas (entrada/salida) ---
from datetime import datetime, date
from sqlalchemy import func
from extensions import db

class Marcacion(db.Model):
    __tablename__ = "Marcacion"

    Id                 = db.Column(db.Integer, primary_key=True, autoincrement=True)
    Codigo_Funcionario = db.Column(db.Integer, db.ForeignKey("Funcionario.Codigo_Funcionario"), nullable=False, index=True)

    Fecha              = db.Column(db.Date, nullable=False, index=True)        # día de la marcación (zona local)
    Hora_Entrada       = db.Column(db.DateTime, nullable=True)
    Hora_Salida        = db.Column(db.DateTime, nullable=True)

    Horas_Regulares    = db.Column(db.Numeric(6, 2), nullable=True)            # horas calculadas (decimal, 2)
    Estado             = db.Column(db.Enum("Abierta", "Cerrada", "Pendiente"), nullable=False, default="Abierta", index=True)
    Observaciones      = db.Column(db.String(255))

    Fecha_Creacion     = db.Column(db.DateTime, nullable=False, server_default=func.current_timestamp())
    Fecha_Actualiza    = db.Column(db.DateTime, nullable=False, server_default=func.current_timestamp(), server_onupdate=func.current_timestamp())

    # rel opcional
    Funcionario        = db.relationship("Funcionario", lazy="joined")
