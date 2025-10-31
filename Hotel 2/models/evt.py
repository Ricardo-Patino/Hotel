# models/evt.py
from datetime import datetime
from extensions import db

# === Recursos de evento (salones y equipos A/V) ===
class EvtSalon(db.Model):
    __tablename__ = "EvtSalon"
    Id              = db.Column(db.Integer, primary_key=True, autoincrement=True)
    Nombre          = db.Column(db.String(120), nullable=False, unique=True, index=True)
    Capacidad       = db.Column(db.Integer, nullable=False, default=10)
    Ubicacion       = db.Column(db.String(120))
    Activo          = db.Column(db.Boolean, nullable=False, default=True, index=True)
    ColorHex        = db.Column(db.String(7), nullable=False, default="#8a6cff")  # mantiene paleta del tema

    def __repr__(self):
        return f"<EvtSalon {self.Id} {self.Nombre}>"

class EvtRecurso(db.Model):
    __tablename__ = "EvtRecurso"
    Id              = db.Column(db.Integer, primary_key=True, autoincrement=True)
    Nombre          = db.Column(db.String(120), nullable=False, unique=True, index=True)
    Tipo            = db.Column(db.Enum("AV", "MOBILIARIO", "CONSUMIBLE", "OTRO"), nullable=False, default="AV")
    InvInsumo_Id    = db.Column(db.Integer, db.ForeignKey("InvInsumo.Id"), nullable=True, index=True)  # link a inventario
    Activo          = db.Column(db.Boolean, nullable=False, default=True, index=True)

    def __repr__(self):
        return f"<EvtRecurso {self.Id} {self.Nombre} {self.Tipo}>"

# === Evento principal ===
class EvtEvento(db.Model):
    __tablename__ = "EvtEvento"
    Id              = db.Column(db.Integer, primary_key=True, autoincrement=True)
    Titulo          = db.Column(db.String(200), nullable=False)
    Estado          = db.Column(db.Enum("BORRADOR","CONFIRMADO","CANCELADO"), nullable=False, default="BORRADOR", index=True)
    Inicio          = db.Column(db.DateTime, nullable=False, index=True)
    Fin             = db.Column(db.DateTime, nullable=False, index=True)
    TZ              = db.Column(db.String(64), nullable=False, default="America/Costa_Rica")
    Organizador     = db.Column(db.String(120), nullable=True)  # user/email o username
    Notas           = db.Column(db.Text)
    Salon_Id        = db.Column(db.Integer, db.ForeignKey("EvtSalon.Id"), nullable=True, index=True)
    ReservaEstancia_Id = db.Column(db.Integer, nullable=True, index=True)  # FK lógica a ReservaEstancia.Id (models_sql)
    Plantilla_Id    = db.Column(db.Integer, db.ForeignKey("EvtPlantilla.Id"), nullable=True, index=True)

    Salon           = db.relationship("EvtSalon", lazy="joined")
    Recursos        = db.relationship("EvtEventoRecurso", backref="Evento", cascade="all, delete-orphan", lazy="dynamic")
    Presupuestos    = db.relationship("EvtPresupuesto", backref="Evento", cascade="all, delete-orphan", lazy="dynamic")
    Costos          = db.relationship("EvtCosto", backref="Evento", cascade="all, delete-orphan", lazy="dynamic")

    Fecha_Creacion      = db.Column(db.DateTime, nullable=False, server_default=db.func.current_timestamp())
    Fecha_Actualizacion = db.Column(db.DateTime, nullable=False, server_default=db.func.current_timestamp(),
                                    server_onupdate=db.func.current_timestamp())

    def __repr__(self):
        return f"<EvtEvento {self.Id} {self.Titulo} {self.Estado} {self.Inicio}->{self.Fin}>"

# Detalle de recursos por evento (con cantidades)
class EvtEventoRecurso(db.Model):
    __tablename__ = "EvtEventoRecurso"
    Id          = db.Column(db.Integer, primary_key=True, autoincrement=True)
    Evento_Id   = db.Column(db.Integer, db.ForeignKey("EvtEvento.Id", ondelete="CASCADE"), nullable=False)
    Recurso_Id  = db.Column(db.Integer, db.ForeignKey("EvtRecurso.Id"), nullable=False)
    Cantidad    = db.Column(db.Integer, nullable=False, default=1)

    Recurso     = db.relationship("EvtRecurso", lazy="joined")

# Plantillas (bodas, congresos, coffee break)
class EvtPlantilla(db.Model):
    __tablename__ = "EvtPlantilla"
    Id          = db.Column(db.Integer, primary_key=True, autoincrement=True)
    Nombre      = db.Column(db.String(120), nullable=False, unique=True, index=True)
    Descripcion = db.Column(db.Text)
    Duracion_Min= db.Column(db.Integer, nullable=False, default=60)

# Asistentes y lista de espera
class EvtAsistente(db.Model):
    __tablename__ = "EvtAsistente"
    Id          = db.Column(db.Integer, primary_key=True, autoincrement=True)
    Evento_Id   = db.Column(db.Integer, db.ForeignKey("EvtEvento.Id", ondelete="CASCADE"), nullable=False, index=True)
    Nombre      = db.Column(db.String(120), nullable=False)
    Email       = db.Column(db.String(180))
    Estado      = db.Column(db.Enum("REGISTRADO","CHECKIN","CANCELADO"), nullable=False, default="REGISTRADO", index=True)
    CheckIn_At  = db.Column(db.DateTime)

class EvtWaitlist(db.Model):
    __tablename__ = "EvtWaitlist"
    Id          = db.Column(db.Integer, primary_key=True, autoincrement=True)
    Evento_Id   = db.Column(db.Integer, db.ForeignKey("EvtEvento.Id", ondelete="CASCADE"), nullable=False, index=True)
    Nombre      = db.Column(db.String(120), nullable=False)
    Email       = db.Column(db.String(180))
    Estado      = db.Column(db.Enum("EN_ESPERA","INVITADO","DESCARTADO"), nullable=False, default="EN_ESPERA", index=True)

# Presupuestos y costos reales
class EvtPresupuesto(db.Model):
    __tablename__ = "EvtPresupuesto"
    Id          = db.Column(db.Integer, primary_key=True, autoincrement=True)
    Evento_Id   = db.Column(db.Integer, db.ForeignKey("EvtEvento.Id", ondelete="CASCADE"), nullable=False, index=True)
    Concepto    = db.Column(db.String(200), nullable=False)
    Monto       = db.Column(db.Numeric(12,2), nullable=False, default=0)

class EvtCosto(db.Model):
    __tablename__ = "EvtCosto"
    Id          = db.Column(db.Integer, primary_key=True, autoincrement=True)
    Evento_Id   = db.Column(db.Integer, db.ForeignKey("EvtEvento.Id", ondelete="CASCADE"), nullable=False, index=True)
    Concepto    = db.Column(db.String(200), nullable=False)
    Monto       = db.Column(db.Numeric(12,2), nullable=False, default=0)

# Encuestas post-evento
class EvtEncuesta(db.Model):
    __tablename__ = "EvtEncuesta"
    Id          = db.Column(db.Integer, primary_key=True, autoincrement=True)
    Evento_Id   = db.Column(db.Integer, db.ForeignKey("EvtEvento.Id", ondelete="CASCADE"), nullable=False, index=True)
    NPS         = db.Column(db.Integer)  # 0-10
    Comentario  = db.Column(db.Text)
    Created_At  = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)

# Notificaciones programadas (7d, 24h, 2h)
class EvtNotificacion(db.Model):
    __tablename__ = "EvtNotificacion"
    Id          = db.Column(db.Integer, primary_key=True, autoincrement=True)
    Evento_Id   = db.Column(db.Integer, db.ForeignKey("EvtEvento.Id", ondelete="CASCADE"), nullable=False, index=True)
    Tipo        = db.Column(db.Enum("7D","24H","2H","CONFIRMACION","UPGRADE"), nullable=False)
    Enviado     = db.Column(db.Boolean, nullable=False, default=False, index=True)
    Destinatario= db.Column(db.String(180))
    Payload     = db.Column(db.JSON)
    Programado_At = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)

# Órdenes de trabajo generadas al confirmar (HK, MNT, COCINA)
class EvtWorkOrder(db.Model):
    __tablename__ = "EvtWorkOrder"
    Id          = db.Column(db.Integer, primary_key=True, autoincrement=True)
    Evento_Id   = db.Column(db.Integer, db.ForeignKey("EvtEvento.Id", ondelete="CASCADE"), nullable=False, index=True)
    Area        = db.Column(db.Enum("HOUSEKEEPING","MANTENIMIENTO","COCINA"), nullable=False)
    Tarea       = db.Column(db.String(240), nullable=False)
    Prioridad   = db.Column(db.Enum("BAJA","MEDIA","ALTA","URGENTE"), nullable=False, default="MEDIA")
    Fecha       = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)
    Estado      = db.Column(db.Enum("PENDIENTE","EN_PROCESO","COMPLETADA","CANCELADA"), nullable=False, default="PENDIENTE")
