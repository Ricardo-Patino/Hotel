# services/sac/notifier.py
from __future__ import annotations
from datetime import datetime, timedelta
from extensions import db
from models.sac import SACOutbox, SACNotifPref, SACConfig

def programar_recordatorio_reserva(codigo_reserva: int, codigo_cliente: int,
                                   correo_cli: str | None, tel_cli: str | None,
                                   fecha_entrada: datetime, numero_comprobante: str):
    pref = SACNotifPref.query.get(codigo_cliente)
    canal = (pref.Canal if pref else "email")
    checkin = (SACConfig.query.get("checkin_inicio").Valor
               if SACConfig.query.get("checkin_inicio") else "12:00")
    checkout = (SACConfig.query.get("checkout_limite").Valor
                if SACConfig.query.get("checkout_limite") else "12:00")

    cuerpo = (f"¡Gracias por su reserva {numero_comprobante}! "
              f"Check-in desde {checkin}. Check-out hasta {checkout}. "
              f"Si necesita ayuda, responda este mensaje.")

    def enqueue(canal: str, para: str | None, when: datetime):
        if not para:
            return
        db.session.add(SACOutbox(
            Canal=canal, Para=para, Asunto="Recordatorio de reserva",
            Cuerpo=cuerpo, Estado="PENDIENTE", Programado_At=when,
            Ref_Entidad="Reserva", Ref_Id=str(codigo_reserva)
        ))

    t48 = fecha_entrada - timedelta(hours=48)
    t24 = fecha_entrada - timedelta(hours=24)

    if canal in ("email", "ambos"):
        enqueue("email", correo_cli, t48)
        enqueue("email", correo_cli, t24)
    if canal in ("sms", "ambos"):
        enqueue("sms", tel_cli, t24)

    db.session.commit()
