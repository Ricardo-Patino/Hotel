# utils/fx.py
import requests
from flask import current_app

def get_usd_crc_rate():
    url = "https://api.exchangerate.host/latest?base=USD&symbols=CRC"
    try:
        resp = requests.get(url, timeout=5)
        resp.raise_for_status()
        data = resp.json()
        rate = data["rates"]["CRC"]
        fx_date = data.get("date")
        return rate, fx_date
    except Exception as e:
        current_app.logger.warning(f"Error obteniendo tipo de cambio: {e}")
        return None, None
