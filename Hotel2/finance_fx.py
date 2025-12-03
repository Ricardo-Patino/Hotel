# finance_fx.py
import requests
from flask import current_app
from datetime import date
from extensions import db
from models_sql import FinFxRate


def get_base_currency() -> str:
    # Si ya lo tienes definido más arriba, usa ese.
    return current_app.config.get("BASE_CURRENCY", "CRC").upper()


def get_today_fx_usd_crc() -> tuple[float | None, date | None]:
    """
    Devuelve (tipo_cambio_USD_CRC, fecha) para hoy.
    Si no existe en la tabla, lo trae de la API y lo guarda.
    """
    today = date.today()

    rate = (
        FinFxRate.query
        .filter(
            FinFxRate.currency == "USD",
            FinFxRate.rate_date == today,
        )
        .first()
    )
    if rate:
        return rate.rate_to_base, rate.rate_date

    # Si no existe, sincroniza con la API
    try:
        value = sync_fx_from_api(currency="USD", rate_date=today)
        return value, today
    except Exception as e:
        current_app.logger.warning("No se pudo sincronizar FX: %s", e)
        return None, None


def sync_fx_from_api(currency: str = "USD", rate_date: date | None = None) -> float:
    """
    Llama a la API open.er-api.com y guarda el tipo de cambio en fin_fx_rate.

    Guardamos siempre: cuántos CRC (moneda base) vale 1 unidad de 'currency'.
    Para ahora: cuántos CRC vale 1 USD.
    """
    if rate_date is None:
        rate_date = date.today()

    base = get_base_currency()      # Esperamos 'CRC'
    currency = currency.upper()     # 'USD', 'EUR', etc.

    if currency == base:
        return 1.0

    # URL de la API (base USD)
    url = current_app.config.get("FX_API_URL", "https://open.er-api.com/v6/latest/USD")

    try:
        resp = requests.get(url, timeout=10)
        resp.raise_for_status()
        data = resp.json()
    except Exception as e:
        current_app.logger.exception("Error al llamar API FX")
        raise RuntimeError("No se pudo obtener el tipo de cambio") from e

    # API open.er-api.com devuelve algo como:
    # { "result": "success", "base_code": "USD", "rates": { "CRC": 530.12, ... } }
    if data.get("result") != "success":
        raise RuntimeError("Respuesta inválida de la API FX")

    rates = data.get("rates", {})

    # Caso principal: queremos cuántos CRC vale 1 USD
    if base == "CRC" and currency == "USD":
        if "CRC" not in rates:
            raise RuntimeError("La API no devolvió la moneda CRC")
        rate_to_base = float(rates["CRC"])
    else:
        # Fallback genérico: calculamos cruce base ↔ currency
        base_code_api = data.get("base_code", "USD")
        if base not in rates or currency not in rates:
            raise RuntimeError("Moneda no disponible en la API")

        # Ejemplo: base_code_api = 'USD'
        # rates[X] = cuántos X por 1 USD
        # Queremos: cuántos 'base' por 1 'currency'
        # => (base_per_USD / currency_per_USD)
        base_per_usd = float(rates[base])
        currency_per_usd = float(rates[currency])
        rate_to_base = base_per_usd / currency_per_usd

    # Guarda/actualiza en fin_fx_rate
    rate = (
        FinFxRate.query
        .filter(
            FinFxRate.currency == currency,
            FinFxRate.rate_date == rate_date,
        )
        .first()
    )
    if not rate:
        rate = FinFxRate(currency=currency, rate_date=rate_date)

    rate.rate_to_base = rate_to_base
    rate.source = "open.er-api.com"
    db.session.add(rate)
    db.session.commit()

    return rate_to_base

