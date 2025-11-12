# models/sac.py
from __future__ import annotations
from datetime import datetime
from extensions import db

class SACConfig(db.Model):
    __tablename__ = "SAC_Config"
    Clave  = db.Column(db.String(60), primary_key=True)
    Valor  = db.Column(db.String(240), nullable=False)
    Actualizado = db.Column(db.DateTime, nullable=False, default=datetime.utcnow, onupdate=datetime.utcnow)

class SACNotifPref(db.Model):
    __tablename__ = "SAC_NotifPref"
    Codigo_Cliente = db.Column(db.Integer, primary_key=True)  # FK lógico a Cliente
    Canal          = db.Column(db.Enum("email", "sms", "ambos"), nullable=False, default="email")
    Email          = db.Column(db.String(120))
    Telefono       = db.Column(db.String(25))
    Verif_Email    = db.Column(db.Boolean, nullable=False, default=False)
    Verif_SMS      = db.Column(db.Boolean, nullable=False, default=False)
    Actualizado    = db.Column(db.DateTime, nullable=False, default=datetime.utcnow, onupdate=datetime.utcnow)

class SACOutbox(db.Model):
    __tablename__ = "SAC_Outbox"
    Id            = db.Column(db.Integer, primary_key=True, autoincrement=True)
    Canal         = db.Column(db.Enum("email", "sms"), nullable=False)
    Para          = db.Column(db.String(180), nullable=False)
    Asunto        = db.Column(db.String(160))
    Cuerpo        = db.Column(db.Text)
    Estado        = db.Column(db.Enum("PENDIENTE", "ENVIADO", "ERROR"), nullable=False, default="PENDIENTE")
    ErrorMsg      = db.Column(db.String(240))
    Programado_At = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)
    Enviado_At    = db.Column(db.DateTime)
    Ref_Entidad   = db.Column(db.String(40))
    Ref_Id        = db.Column(db.String(40))
    Meta_JSON     = db.Column(db.JSON)

class SACSolicitud(db.Model):
    __tablename__ = "SAC_Solicitud"
    Id               = db.Column(db.Integer, primary_key=True, autoincrement=True)
    Codigo_Reserva   = db.Column(db.Integer, nullable=True)   # <- ahora opcional
    Codigo_Cliente   = db.Column(db.Integer, nullable=True)
    Clave            = db.Column(db.String(60), nullable=False)
    Valor            = db.Column(db.String(240), nullable=True)
    Estado           = db.Column(db.Enum("NUEVA", "VISTA", "ATENDIDA", "RECHAZADA"), nullable=False, default="NUEVA")
    Creada_At        = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)

class SACConversation(db.Model):
    __tablename__ = "SAC_Conversation"
    Id            = db.Column(db.Integer, primary_key=True, autoincrement=True)
    Codigo_Cliente= db.Column(db.Integer)
    Session_Id    = db.Column(db.String(64))
    Abierta       = db.Column(db.Boolean, nullable=False, default=True)
    Creada_At     = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)
    Actualizada_At= db.Column(db.DateTime, nullable=False, default=datetime.utcnow, onupdate=datetime.utcnow)

class SACConversationMsg(db.Model):
    __tablename__ = "SAC_ConversationMsg"
    Id         = db.Column(db.Integer, primary_key=True, autoincrement=True)
    Conv_Id    = db.Column(db.Integer, nullable=False)
    Rol        = db.Column(db.Enum("user", "bot", "agente"), nullable=False)
    Texto      = db.Column(db.Text, nullable=False)
    Creada_At  = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)

class SACIncident(db.Model):
    __tablename__ = "SAC_Incident"
    Id               = db.Column(db.Integer, primary_key=True, autoincrement=True)
    Codigo_Reserva   = db.Column(db.Integer, nullable=True)
    Codigo_Cliente   = db.Column(db.Integer, nullable=True)
    Tipo             = db.Column(db.Enum("INCIDENTE", "COMENTARIO"), nullable=False, default="INCIDENTE")
    Severidad        = db.Column(db.Enum("BAJA", "MEDIA", "ALTA", "CRITICA"), nullable=False, default="MEDIA")
    Titulo           = db.Column(db.String(160), nullable=False)
    Detalle          = db.Column(db.Text)
    Estado           = db.Column(db.Enum("ABIERTA", "EN_PROCESO", "CERRADA"), nullable=False, default="ABIERTA")
    Creada_At        = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)

class SACFeedback(db.Model):
    __tablename__ = "SAC_Feedback"
    Id             = db.Column(db.Integer, primary_key=True, autoincrement=True)
    Codigo_Reserva = db.Column(db.Integer, nullable=False)
    NPS            = db.Column(db.Integer)  # 0..10
    Comentario     = db.Column(db.Text)
    Registrada_At  = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)
