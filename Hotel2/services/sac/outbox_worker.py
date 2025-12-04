# services/sac/outbox_worker.py
from __future__ import annotations

from datetime import datetime
from typing import Optional

from extensions import db
from models.sac import SACOutbox


def _send_email_stub(msg: SACOutbox) -> None:
    """
    Implementación mínima.
    Aquí puedes integrar tu servicio de correo real:
      - SMTP
      - API de SendGrid, SES, etc.
    Por ahora solo sirve como placeholder para marcar el mensaje como enviado.
    """
    # TODO: integrar envío real de correo si se desea
    # Ejemplo de campos disponibles:
    #   msg.Canal, msg.Para, msg.Asunto, msg.Cuerpo
    return


def process_outbox(batch: int = 50) -> int:
    """
    Procesa hasta `batch` registros pendientes en SAC_Outbox.

    Regla:
      - Consideramos pendientes aquellos con Estado NULL o 'PENDIENTE'
        y Programado_At <= ahora.
      - Si el envío "funciona", se marca como 'ENVIADO'.
      - Si falla, se marca como 'ERROR' y se almacena el texto del error.

    Retorna:
      - Cantidad de mensajes procesados (enviados + con error).
    """
    now = datetime.utcnow()

    # Filtrado defensivo: algunas columnas pueden no existir, ajusta si tu modelo difiere
    pendientes = (
        SACOutbox.query.filter(
            (SACOutbox.Estado.is_(None)) | (SACOutbox.Estado == "PENDIENTE"),
            SACOutbox.Programado_At <= now,
        )
        .order_by(SACOutbox.Programado_At.asc())
        .limit(batch)
        .all()
    )

    procesados = 0

    for msg in pendientes:
        try:
            _send_email_stub(msg)
            # Campos típicos en un outbox; ajusta a tu modelo si fuera necesario
            msg.Estado = "ENVIADO"
            setattr(msg, "Enviado_At", datetime.utcnow())
            if hasattr(msg, "Error"):
                msg.Error = None
            procesados += 1
        except Exception as e:
            msg.Estado = "ERROR"
            if hasattr(msg, "Error"):
                msg.Error = str(e)[:500]
            procesados += 1

    if procesados:
        db.session.commit()

    return procesados
