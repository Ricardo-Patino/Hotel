# services/grr/notification_service.py
class NotificationService:
    def send_confirmation(self, correo: str | None, telefono: str | None, numero: str):
        # Integra SMTP/Twilio más adelante. Aquí dejamos stub.
        return {"email": correo, "sms": telefono, "numero": numero, "status": "queued"}
