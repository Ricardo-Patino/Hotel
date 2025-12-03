# utils/pdf_utils.py
import os
from datetime import datetime

def generate_reservation_pdf(reserva, habitacion):
    """
    Stub: escribe un HTML con extensión .pdf en carpeta 'booking'.
    Sustituir por WeasyPrint/xhtml2pdf más adelante.
    """
    numero = reserva.Numero_Comprobante or f"R-{reserva.Codigo_Reserva:08d}"
    html = f"""
    <html><body>
      <h2>Hotel Villa Grace - Comprobante de Reserva</h2>
      <p>Número: {numero}</p>
      <p>Habitación: {habitacion.Numero_Habitacion}</p>
      <p>Entrada: {reserva.Fecha_Entrada} - Salida: {reserva.Fecha_Salida}</p>
      <p>Monto: {reserva.Monto_Total}</p>
      <small>Generado: {datetime.utcnow().isoformat()}</small>
    </body></html>
    """.strip()
    out_dir = "booking"
    os.makedirs(out_dir, exist_ok=True)
    path = os.path.join(out_dir, f"{numero}.pdf")
    with open(path, 'wb') as f:
        f.write(html.encode('utf-8'))
    return html.encode('utf-8'), path, 'application/pdf'
