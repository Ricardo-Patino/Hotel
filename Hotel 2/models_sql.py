# models_sql.py
# Mapeos SQLAlchemy para el esquema MySQL de Hotel_VillaGrace.
# Incluye tabla Rol y Usuario con FK Rol_Id (y FK opcional a Cliente).
# Reserva está en su versión extendida (Estado, Canal, Numero_Comprobante, etc.).
#
# IMPORTANTE:
#  - La tabla Funcionario AHORA se maneja en models/hrm.py
#    (con salario, estado, cuenta bancaria, etc.).
#  - La definición antigua de Funcionario que estaba aquí fue comentada
#    para evitar el error:
#    sqlalchemy.exc.InvalidRequestError: Table 'Funcionario' is already defined
#
#    Si necesitás usar Funcionario en otros módulos, importalo así:
#        from models import Funcionario
#
#    No lo importes desde aquí.

from __future__ import annotations

from datetime import datetime
from sqlalchemy import func
from extensions import db
from werkzeug.security import generate_password_hash, check_password_hash
from decimal import Decimal


class ReservaEstancia(db.Model):
    __tablename__ = "ReservaEstancia"
    Id = db.Column(db.Integer, primary_key=True)
    Codigo_Reserva = db.Column(db.Integer, nullable=False, index=True)
    Habitacion_Id = db.Column(db.Integer, nullable=False, index=True)
    Fecha_Desde = db.Column(db.Date, nullable=False)
    Fecha_Hasta = db.Column(db.Date, nullable=False)  # checkout (exclusivo)
    Estado = db.Column(
        db.Enum("Asignada", "Ocupada", "Cerrada", "Cancelada"),
        default="Asignada"
    )


class ReglaAsignacion(db.Model):
    __tablename__ = "ReglaAsignacion"
    Id = db.Column(db.Integer, primary_key=True)
    Nombre = db.Column(db.String(80), nullable=False, unique=True)
    Prioridad = db.Column(db.Integer, default=100, index=True)
    Activa = db.Column(db.Boolean, default=True, index=True)
    # Condición/pesos (JSON libre: puede incluir 'peso_tipo', 'peso_mant', 'peso_prefs', etc.)
    Condicion = db.Column(db.JSON)


class AsignacionDecision(db.Model):
    __tablename__ = "AsignacionDecision"
    Id = db.Column(db.Integer, primary_key=True)
    Codigo_Reserva = db.Column(db.Integer, nullable=False, index=True)
    Fecha = db.Column(
        db.DateTime,
        nullable=False,
        server_default=db.func.current_timestamp()
    )
    Tipo = db.Column(db.String(30), nullable=False)  # 'auto', 'manual', 'reassign', 'split', 'merge'
    Resultado = db.Column(
        db.Enum("ok", "fallback", "sin_disponibilidad", "error"),
        default="ok"
    )
    Detalle = db.Column(db.JSON)  # scoring, candidatos, selección final


class PreferenciaHuesped(db.Model):
    __tablename__ = "PreferenciaHuesped"
    Id = db.Column(db.Integer, primary_key=True)
    Codigo_Cliente = db.Column(db.Integer, nullable=False, index=True)
    Clave = db.Column(db.String(40), nullable=False)   # p.ej. 'vista', 'piso', 'ruido'
    Valor = db.Column(db.String(120), nullable=False)  # p.ej. 'mar', 'alto', 'bajo'
    UNIQUE_KEY = db.UniqueConstraint(
        "Codigo_Cliente",
        "Clave",
        "Valor",
        name="UQ_pref_cliente_clave_valor"
    )


# ---------------------------
# Tabla: Rol
# ---------------------------
class Rol(db.Model):
    __tablename__ = "Rol"

    Codigo_Rol         = db.Column(db.Integer, primary_key=True, autoincrement=True)
    Nombre             = db.Column(db.String(30), unique=True, nullable=False)
    Descripcion        = db.Column(db.String(200))
    Estado             = db.Column(db.String(10), nullable=False, default="Activo")  # ENUM en SQL
    Fecha_Creacion     = db.Column(
        db.DateTime,
        nullable=False,
        server_default=func.current_timestamp()
    )
    Fecha_Modificacion = db.Column(
        db.DateTime,
        nullable=False,
        server_default=func.current_timestamp(),
        server_onupdate=func.current_timestamp()
    )


# ---------------------------
# Tabla: Habitacion
# ---------------------------
class Habitacion(db.Model):
    __tablename__ = "Habitacion"

    Codigo_Habitacion  = db.Column(db.Integer, primary_key=True, autoincrement=True)
    Numero_Habitacion  = db.Column(db.String(10), nullable=False, unique=True)
    Tipo               = db.Column(db.String(20), nullable=False)  # ENUM en SQL
    Precio_Noche       = db.Column(db.Numeric(12, 2), nullable=False)
    Estado             = db.Column(db.String(20), nullable=False, default="Disponible")  # ENUM en SQL
    Fecha_modificacion = db.Column(
        db.DateTime,
        nullable=False,
        server_default=func.current_timestamp(),
        server_onupdate=func.current_timestamp()
    )


# ---------------------------
# Tabla: Funcionario
# (AHORA DEFINIDA EN models/hrm.py)
# ---------------------------
# class Funcionario(db.Model):
#     __tablename__ = "Funcionario"
#
#     Codigo_Funcionario  = db.Column(db.Integer, primary_key=True, autoincrement=True)
#     Cedula              = db.Column(db.Integer, nullable=False)
#     Nombre              = db.Column(db.String(50), nullable=False)
#     Apellido            = db.Column(db.String(50), nullable=False)
#     Puesto              = db.Column(db.String(50), nullable=False)
#     Fecha_Nacimiento    = db.Column(db.Date, nullable=False)
#     Fecha_modificacion  = db.Column(
#         db.DateTime,
#         nullable=False,
#         server_default=func.current_timestamp(),
#         server_onupdate=func.current_timestamp()
#     )


# ---------------------------
# Tabla: Cliente
# ---------------------------
class Cliente(db.Model):
    __tablename__ = "Cliente"

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

    usuarios = db.relationship(
        "Usuario",
        back_populates="cliente",
        lazy="dynamic"
    )


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
    __tablename__ = "Reserva"

    Codigo_Reserva     = db.Column(db.Integer, primary_key=True, autoincrement=True)

    Codigo_Cliente     = db.Column(
        db.Integer,
        db.ForeignKey("Cliente.Codigo_Cliente"),
        nullable=False,
        index=True
    )

    Codigo_Habitacion  = db.Column(
        db.Integer,
        db.ForeignKey("Habitacion.Codigo_Habitacion"),
        nullable=False,
        index=True
    )

    Codigo_Funcionario = db.Column(
        db.Integer,
        db.ForeignKey("Funcionario.Codigo_Funcionario"),
        nullable=False,
        index=True
    )

    Fecha_Entrada      = db.Column(db.Date, nullable=False)
    Fecha_Salida       = db.Column(db.Date, nullable=False)
    Monto_Total        = db.Column(db.Numeric(12, 2), nullable=False)

    Estado               = db.Column(db.String(20), nullable=False, default="Confirmada")
    Canal                = db.Column(db.String(30), nullable=True)
    Numero_Comprobante   = db.Column(db.String(32), nullable=True)
    Fuente               = db.Column(db.String(30), nullable=True)
    Politica_Cancelacion = db.Column(db.String(200), nullable=True)
    Observaciones        = db.Column(db.String(255), nullable=True)
    Descuento_Id         = db.Column(db.String(50), nullable=True)

    Fecha_Registro     = db.Column(
        db.DateTime,
        nullable=False,
        server_default=func.current_timestamp()
    )

    Habitacion  = db.relationship("Habitacion", lazy="joined")
    Cliente     = db.relationship("Cliente", lazy="joined")

    # OJO:
    # Esta relación sigue válida porque Funcionario ahora vive en models/hrm.py,
    # pero comparte el mismo __tablename__ = "Funcionario".
    # SQLAlchemy la resuelve igual mientras exista UNA sola clase Funcionario activa.
    Funcionario = db.relationship("Funcionario", lazy="joined")


# ---------------------------
# Tablas opcionales para comprobantes
# ---------------------------
class DocumentoSQL(db.Model):
    __tablename__ = "Documento"

    Id             = db.Column(db.Integer, primary_key=True, autoincrement=True)
    Tipo           = db.Column(db.String(50), nullable=False)
    Ruta           = db.Column(db.String(255), nullable=False)
    MimeType       = db.Column(db.String(100), nullable=False, default="application/pdf")
    TamanoBytes    = db.Column(db.Integer, nullable=True)
    Fecha_Creacion = db.Column(
        db.DateTime,
        nullable=False,
        server_default=func.current_timestamp()
    )


class ReservaDocumento(db.Model):
    __tablename__ = "ReservaDocumento"

    Id             = db.Column(db.Integer, primary_key=True, autoincrement=True)
    Codigo_Reserva = db.Column(
        db.Integer,
        db.ForeignKey("Reserva.Codigo_Reserva"),
        nullable=False,
        index=True
    )
    Documento_Id   = db.Column(
        db.Integer,
        db.ForeignKey("Documento.Id"),
        nullable=False,
        index=True
    )


class LimpiezaOrden(db.Model):
    __tablename__ = "LimpiezaOrden"
    Id                = db.Column(db.Integer, primary_key=True, autoincrement=True)
    Codigo_Habitacion = db.Column(
        db.Integer,
        db.ForeignKey("Habitacion.Codigo_Habitacion"),
        nullable=False,
        index=True
    )
    Estado            = db.Column(db.String(20), nullable=False, default="Pendiente")  # Enum en MySQL
    Notas             = db.Column(db.String(255))
    Fecha_Creacion    = db.Column(
        db.DateTime,
        nullable=False,
        server_default=func.current_timestamp()
    )
    Fecha_Actualiza   = db.Column(
        db.DateTime,
        nullable=False,
        server_default=func.current_timestamp(),
        server_onupdate=func.current_timestamp()
    )

    Habitacion = db.relationship("Habitacion", lazy="joined")


class LimpiezaInsumo(db.Model):
    __tablename__ = "LimpiezaInsumo"
    Id       = db.Column(db.Integer, primary_key=True, autoincrement=True)
    Orden_Id = db.Column(
        db.Integer,
        db.ForeignKey("LimpiezaOrden.Id"),
        nullable=False,
        index=True
    )
    Insumo   = db.Column(db.String(120), nullable=False)
    Cantidad = db.Column(db.Numeric(10, 2), nullable=False, default=1)

    Orden = db.relationship(
        "LimpiezaOrden",
        backref="Insumos",
        lazy="joined"
    )


class Temporada(db.Model):
    __tablename__ = "Temporada"
    Id           = db.Column(db.Integer, primary_key=True, autoincrement=True)
    Nombre       = db.Column(db.String(60), nullable=False)
    Fecha_Inicio = db.Column(db.Date, nullable=False)
    Fecha_Fin    = db.Column(db.Date, nullable=False)


class TarifaTemporada(db.Model):
    __tablename__ = "TarifaTemporada"
    Id                = db.Column(db.Integer, primary_key=True, autoincrement=True)
    Codigo_Habitacion = db.Column(
        db.Integer,
        db.ForeignKey("Habitacion.Codigo_Habitacion"),
        nullable=False,
        index=True
    )
    Temporada_Id      = db.Column(
        db.Integer,
        db.ForeignKey("Temporada.Id"),
        nullable=False,
        index=True
    )
    Precio_Noche      = db.Column(db.Numeric(12, 2), nullable=False)

    Habitacion        = db.relationship("Habitacion", lazy="joined")
    Temporada         = db.relationship("Temporada",  lazy="joined")


class MantenimientoSolicitud(db.Model):
    __tablename__ = "MantenimientoSolicitud"
    Id                = db.Column(db.Integer, primary_key=True, autoincrement=True)
    Codigo_Habitacion = db.Column(
        db.Integer,
        db.ForeignKey("Habitacion.Codigo_Habitacion"),
        nullable=False,
        index=True
    )
    Titulo            = db.Column(db.String(120), nullable=False)
    Descripcion       = db.Column(db.String(500))
    Prioridad         = db.Column(db.String(10), nullable=False, default="Media")
    Estado            = db.Column(db.String(12), nullable=False, default="Abierta")
    Fecha_Creacion    = db.Column(
        db.DateTime,
        nullable=False,
        server_default=func.current_timestamp()
    )
    Fecha_Actualiza   = db.Column(
        db.DateTime,
        nullable=False,
        server_default=func.current_timestamp(),
        server_onupdate=func.current_timestamp()
    )

    Habitacion        = db.relationship("Habitacion", lazy="joined")


# ---------------------------
# Tabla: MantenimientoPreventivo
# ---------------------------
class MantenimientoPreventivo(db.Model):
    __tablename__ = "MantenimientoPreventivo"

    Id              = db.Column(db.Integer, primary_key=True, autoincrement=True)
    Equipo          = db.Column(db.String(120), nullable=False)
    Ubicacion       = db.Column(db.String(120), nullable=False)  # texto libre: "Cuarto bombas", "Hab 203", etc.
    Frecuencia      = db.Column(
        db.Enum("Mensual", "Trimestral", "Semestral", "Anual", "Semanas"),
        nullable=False,
        default="Mensual"
    )
    Cada_Dias       = db.Column(db.Integer)   # usado si Frecuencia='Semanas' (ej. 14 días)
    Proxima_Fecha   = db.Column(db.Date, nullable=False)
    Responsable     = db.Column(db.String(120))
    Notas           = db.Column(db.String(500))
    Activo          = db.Column(db.Boolean, default=True, index=True)
    Fecha_Creacion  = db.Column(
        db.DateTime,
        nullable=False,
        server_default=func.current_timestamp()
    )
    Fecha_Actualiza = db.Column(
        db.DateTime,
        nullable=False,
        server_default=func.current_timestamp(),
        server_onupdate=func.current_timestamp()
    )

    def __repr__(self) -> str:
        return (
            f"<MantenimientoPreventivo {self.Id} {self.Equipo} "
            f"{self.Ubicacion} prox={self.Proxima_Fecha}>"
        )


# ---------------------------
# Inventario: Categorías e Insumos
# ---------------------------
class InvCategoria(db.Model):
    __tablename__ = "InvCategoria"

    Id              = db.Column(db.Integer, primary_key=True, autoincrement=True)
    Nombre          = db.Column(db.String(80), nullable=False, unique=True)
    Descripcion     = db.Column(db.String(255))
    Activa          = db.Column(db.Boolean, default=True, index=True)
    Fecha_Creacion  = db.Column(
        db.DateTime,
        nullable=False,
        server_default=func.current_timestamp()
    )
    Fecha_Actualiza = db.Column(
        db.DateTime,
        nullable=False,
        server_default=func.current_timestamp(),
        server_onupdate=func.current_timestamp()
    )

    # Relación inversa (ruta calificada para evitar colisiones en el registry)
    insumos = db.relationship(
        "models_sql.InvInsumo",
        back_populates="categoria"
    )

    def __repr__(self) -> str:
        return f"<InvCategoria {self.Id} {self.Nombre}>"



class InvInsumo(db.Model):
    __tablename__ = "InvInsumo"

    Id              = db.Column(db.Integer, primary_key=True, autoincrement=True)
    Categoria_Id    = db.Column(
        db.Integer,
        db.ForeignKey("InvCategoria.Id"),
        nullable=False,
        index=True
    )
    Nombre          = db.Column(db.String(120), nullable=False)
    Unidad          = db.Column(db.String(20), nullable=False)
    Stock_Actual    = db.Column(db.Numeric(12, 3), nullable=False, default=0)
    Stock_Minimo    = db.Column(db.Numeric(12, 3), nullable=False, default=0)
    Activo          = db.Column(db.Boolean, default=True, index=True)
    Fecha_Creacion  = db.Column(
        db.DateTime,
        nullable=False,
        server_default=func.current_timestamp()
    )
    Fecha_Actualiza = db.Column(
        db.DateTime,
        nullable=False,
        server_default=func.current_timestamp(),
        server_onupdate=func.current_timestamp()
    )

    # Relación correcta (sin db.back_populates, y con ruta calificada)
    categoria = db.relationship(
        "models_sql.InvCategoria",
        back_populates="insumos",
        foreign_keys=[Categoria_Id],
        lazy="joined"
    )

    @property
    def bajo_minimo(self) -> bool:
        actual = self.Stock_Actual or Decimal("0")
        minimo = self.Stock_Minimo or Decimal("0")
        return actual < minimo

    def __repr__(self) -> str:
        return (
            f"<InvInsumo {self.Id} {self.Nombre} - "
            f"Stock: {self.Stock_Actual} {self.Unidad}>"
        )



# ---------------------------
# Inventario: Movimientos (historial)
# ---------------------------
class InvMovimiento(db.Model):
    __tablename__ = "InvMovimiento"

    Id             = db.Column(db.Integer, primary_key=True, autoincrement=True)
    Insumo_Id      = db.Column(
        db.Integer,
        db.ForeignKey("InvInsumo.Id"),
        nullable=False,
        index=True
    )
    Tipo           = db.Column(db.Enum(
        "AJUSTE",
        "EDICION",
        "INACTIVACION",
        "REACTIVACION",
        "ENTRADA_COMPRA",
        "ENTRADA_DEVOLUCION"
    ), nullable=False)
    Campo          = db.Column(db.String(60))
    Valor_Antes    = db.Column(db.String(120))
    Valor_Despues  = db.Column(db.String(120))
    Delta          = db.Column(db.Numeric(14, 3))
    Motivo         = db.Column(db.String(255), nullable=False)
    Doc_Tipo       = db.Column(db.String(30))
    Doc_Numero     = db.Column(db.String(60))
    Proveedor      = db.Column(db.String(120))
    Fecha_Mov      = db.Column(
        db.DateTime,
        nullable=False,
        server_default=func.current_timestamp()
    )

    Usuario_Id     = db.Column(db.Integer)
    Usuario_Nombre = db.Column(db.String(120))
    Usuario_Email  = db.Column(db.String(120))

    Insumo = db.relationship("InvInsumo", lazy="joined")

    def __repr__(self) -> str:
        return (
            f"<InvMovimiento {self.Id} "
            f"Insumo={self.Insumo_Id} {self.Tipo} Δ={self.Delta}>"
        )



