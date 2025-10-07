# services/grr/audit_service.py
from extensions import db
from models.grr import AuditoriaLog

class AuditService:
    def log(self, usuario: str | None, entidad: str, entidad_id: str, accion: str, datos: dict | None = None):
        db.session.add(AuditoriaLog(Usuario=usuario, Entidad=entidad, Entidad_Id=str(entidad_id), Accion=accion, Datos=datos))
        db.session.commit()
