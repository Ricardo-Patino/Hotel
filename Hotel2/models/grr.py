# models/grr.py
from datetime import datetime
from extensions import db

#class Documento(db.Model):
#    __tablename__ = "Documento"
#    Id = db.Column(db.BigInteger, primary_key=True)
#    Tipo = db.Column(db.Enum('Comprobante','IDFrontal','IDReverso','Firma','Otro'), nullable=False)
#    Ruta = db.Column(db.String(255), nullable=False)
#    MimeType = db.Column(db.String(80), nullable=False)
#    TamanoBytes = db.Column(db.BigInteger, nullable=False, default=0)
#    Fecha_Subida = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)

#class ReservaDocumento(db.Model):
#    __tablename__ = "ReservaDocumento"
#    Id = db.Column(db.BigInteger, primary_key=True)
#    Codigo_Reserva = db.Column(db.Integer, db.ForeignKey('Reserva.Codigo_Reserva'), nullable=False)
#    Documento_Id = db.Column(db.BigInteger, db.ForeignKey('Documento.Id'), nullable=False)

class AuditoriaLog(db.Model):
    __tablename__ = "Auditoria_Log"
    Id = db.Column(db.BigInteger, primary_key=True)
    Usuario = db.Column(db.String(120))
    Entidad = db.Column(db.String(80), nullable=False)
    Entidad_Id = db.Column(db.String(80), nullable=False)
    Accion = db.Column(db.String(40), nullable=False)
    Datos = db.Column(db.JSON)
    Fecha = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)

class ListaEspera(db.Model):
    __tablename__ = "Lista_Espera"
    Id = db.Column(db.BigInteger, primary_key=True)
    Codigo_Cliente = db.Column(db.Integer, db.ForeignKey('Cliente.Codigo_Cliente'), nullable=False)
    Tipo = db.Column(db.Enum('Sencilla','Doble','Suite'), nullable=False)
    Fecha_Entrada = db.Column(db.Date, nullable=False)
    Fecha_Salida = db.Column(db.Date, nullable=False)
    Huespedes = db.Column(db.Integer, nullable=False, default=1)
    Estado = db.Column(db.Enum('Activa','OfertaEnviada','Atendida','Cancelada'), nullable=False, default='Activa')
    Fecha_Creacion = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)

class CodigoDescuento(db.Model):
    __tablename__ = "Codigo_Descuento"
    Id = db.Column(db.BigInteger, primary_key=True)
    Codigo = db.Column(db.String(40), unique=True, nullable=False)
    Descripcion = db.Column(db.String(160))
    Porcentaje = db.Column(db.Numeric(5,2), nullable=False, default=0.00)
    MontoFijo = db.Column(db.Numeric(12,2))
    Valido_Desde = db.Column(db.Date)
    Valido_Hasta = db.Column(db.Date)
    Segmento = db.Column(db.String(60))
    Activo = db.Column(db.Boolean, nullable=False, default=True)

class HousekeepingTask(db.Model):
    __tablename__ = "HousekeepingTask"

    Id = db.Column(db.BigInteger, primary_key=True)
    Habitacion_Id = db.Column(db.Integer, db.ForeignKey('Habitacion.Codigo_Habitacion'), nullable=False)  # ✅ CAMBIO AQUÍ
    Estado = db.Column(
        db.Enum('Pendiente', 'En proceso', 'Terminado', name='estado_hk_enum'),
        nullable=False,
        default='Pendiente'
    )
    Observaciones = db.Column(db.String(255))
    Fecha_Creacion = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)
    Fecha_Cierre = db.Column(db.DateTime)

    Habitacion = db.relationship("Habitacion", backref="tareas_limpieza")
