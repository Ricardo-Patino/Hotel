# models/sac.py
from __future__ import annotations

from datetime import datetime
from extensions import db
from sqlalchemy.orm import synonym


class SACConfig(db.Model):
    __tablename__ = "SAC_Config"

    Clave = db.Column(db.String(60), primary_key=True)
    Valor = db.Column(db.String(240), nullable=False)
    Actualizado = db.Column(
        db.DateTime,
        nullable=False,
        default=datetime.utcnow,
        onupdate=datetime.utcnow,
    )

    def __repr__(self) -> str:
        return f"<SACConfig {self.Clave}={self.Valor!r}>"


class SACNotifPref(db.Model):
    __tablename__ = "SAC_NotifPref"

    Codigo_Cliente = db.Column(db.Integer, primary_key=True)
    Canal = db.Column(db.String(10), nullable=False, default="email")  # email | sms | ambos
    Email = db.Column(db.String(120))
    Telefono = db.Column(db.String(25))
    Verif_Email = db.Column(db.Boolean, nullable=False, default=False)
    Verif_SMS = db.Column(db.Boolean, nullable=False, default=False)
    Actualizado = db.Column(
        db.DateTime,
        nullable=False,
        default=datetime.utcnow,
        onupdate=datetime.utcnow,
    )

    def __repr__(self) -> str:
        return f"<SACNotifPref cli={self.Codigo_Cliente} canal={self.Canal}>"


class SACOutbox(db.Model):
    __tablename__ = "SAC_Outbox"

    Id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    Canal = db.Column(db.String(10), nullable=False)  # email | sms
    Para = db.Column(db.String(180), nullable=False)
    Asunto = db.Column(db.String(160))
    Cuerpo = db.Column(db.Text)
    Estado = db.Column(db.String(10), nullable=False, default="PENDIENTE")
    ErrorMsg = db.Column(db.String(240))
    Programado_At = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)
    Enviado_At = db.Column(db.DateTime)
    Ref_Entidad = db.Column(db.String(40))
    Ref_Id = db.Column(db.String(40))
    Meta_JSON = db.Column(db.JSON)

    def __repr__(self) -> str:
        return f"<SACOutbox {self.Id} {self.Canal}->{self.Para} {self.Estado}>"


class SACSolicitud(db.Model):
    __tablename__ = "SAC_Solicitud"

    Id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    Codigo_Reserva = db.Column(db.Integer, nullable=True)
    Codigo_Cliente = db.Column(db.Integer, nullable=True)
    Clave = db.Column(db.String(60), nullable=False)
    Valor = db.Column(db.String(240))
    Estado = db.Column(db.String(12), nullable=False, default="NUEVA")
    Creada_At = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)

    def __repr__(self) -> str:
        return f"<SACSolicitud {self.Id} cli={self.Codigo_Cliente} est={self.Estado}>"


class SACConversation(db.Model):
    __tablename__ = "SAC_Conversation"

    Id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    Codigo_Cliente = db.Column(db.Integer, nullable=True)
    Session_Id = db.Column(db.String(64), nullable=True)

    # === NUEVOS CAMPOS DE ESTADO / META ===
    Status = db.Column(db.String(20), nullable=False, default="bot")
    Needs_Agent = db.Column(db.Boolean, nullable=False, default=False)
    Guest_Name = db.Column(db.String(160))
    Guest_Email = db.Column(db.String(160))
    Channel = db.Column(db.String(20), nullable=False, default="web")
    Last_Msg_At = db.Column(db.DateTime)
    Last_Msg_Role = db.Column(db.String(10))

    Abierta = db.Column(db.Boolean, nullable=False, default=True)
    Creada_At = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)
    Actualizada_At = db.Column(
        db.DateTime,
        nullable=False,
        default=datetime.utcnow,
        onupdate=datetime.utcnow,
    )

    mensajes = db.relationship(
        "SACConversationMsg",
        back_populates="conversation",
        lazy="dynamic",
        cascade="all, delete-orphan",
    )


class SACConversationMsg(db.Model):
    __tablename__ = "SAC_ConversationMsg"

    # Compatibilidad de esquema:
    # - En MySQL la FK se llama `Conversation_Id` (NO `Conv_Id`)
    # - El texto del mensaje se llama `Msg_Text` (NO `Texto`)
    # El backend usa Conv_Id/Texto, así que creamos aliases con synonym().

    Id = db.Column(db.BigInteger, primary_key=True, autoincrement=True)

    # Columna REAL en MySQL
    Conversation_Id = db.Column(
        "Conversation_Id",
        db.BigInteger,
        db.ForeignKey("SAC_Conversation.Id", ondelete="CASCADE"),
        nullable=False,
    )
    # Alias usado por el código
    Conv_Id = synonym("Conversation_Id")

    Rol = db.Column(db.String(10), nullable=False)  # user | bot | agent

    # Columna REAL en MySQL
    Msg_Text = db.Column("Msg_Text", db.Text, nullable=False)
    # Alias usado por el código
    Texto = synonym("Msg_Text")

    # Existe en el script base (opcional)
    Meta_JSON = db.Column(db.JSON)

    Creada_At = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)

    conversation = db.relationship("SACConversation", back_populates="mensajes")

    def __repr__(self) -> str:
        return f"<SACConversationMsg {self.Id} conv={self.Conv_Id} rol={self.Rol}>"


class SACIncident(db.Model):
    __tablename__ = "sac_incident"

    Id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    Codigo_Reserva = db.Column(db.Integer, nullable=True)
    Codigo_Cliente = db.Column(db.Integer, nullable=True)
    Reportado_Por = db.Column(db.String(80), nullable=True)
    Asignado_A = db.Column(db.String(30), nullable=True)
    Tipo = db.Column(db.String(20), nullable=False, default="INCIDENTE")
    Severidad = db.Column(db.String(10), nullable=False, default="MEDIA")
    Titulo = db.Column(db.String(160), nullable=False)
    Detalle = db.Column(db.Text)
    Estado = db.Column(db.String(15), nullable=False, default="ABIERTA")
    Creada_At = db.Column("Creado_At", db.DateTime, nullable=False, default=datetime.utcnow)

    def __repr__(self) -> str:
        return f"<SACIncident {self.Id} tipo={self.Tipo} sev={self.Severidad}>"


class SACIncidentComment(db.Model):
    __tablename__ = "sac_incident_comment"

    Id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    Incident_Id = db.Column(db.Integer, nullable=False)
    Autor = db.Column(db.String(120), nullable=True)
    Comentario = db.Column(db.Text, nullable=False)
    Creada_At = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)


class SACFeedback(db.Model):
    __tablename__ = "SAC_Feedback"

    Id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    Codigo_Reserva = db.Column(db.Integer, nullable=False)
    NPS = db.Column(db.SmallInteger)  # 0–10
    Comentario = db.Column(db.Text)
    Registrada_At = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)

    def __repr__(self) -> str:
        return f"<SACFeedback {self.Id} reserva={self.Codigo_Reserva} nps={self.NPS}>"
