# blueprints/fin_kpi/routes.py
from datetime import date
from flask import jsonify, request
from sqlalchemy import text

from extensions import db
from . import fin_kpi_bp


def _parse_fecha():
    """
    Lee ?fecha=YYYY-MM-DD desde el querystring.
    Si no viene, usa la fecha de hoy.
    """
    valor = request.args.get("fecha")
    if not valor:
        return date.today()

    try:
        año, mes, dia = map(int, valor.split("-"))
        return date(año, mes, dia)
    except Exception:
        return date.today()


@fin_kpi_bp.get("/overview")
def kpi_contabilidad_overview():
    """
    KPIs contables diarios para dashboards.

    Fuente:
      - fin_cash_movement  (caja / POS)
      - fin_ledger_tx      (transacciones de mayor)
      - fin_ledger_line    (líneas por cuenta)

    Querystring opcional:
      ?fecha=YYYY-MM-DD  (si no se envía, se usa hoy)
    """
    fecha = _parse_fecha()

    # ================================
    # 1) KPIs de caja: fin_cash_movement
    # ================================
    cash_row = db.session.execute(
        text("""
            SELECT
              COALESCE(SUM(CASE WHEN tipo = 'Ingreso' THEN monto ELSE 0 END), 0)          AS ingresos,
              COALESCE(SUM(CASE WHEN tipo = 'Egreso'  THEN monto ELSE 0 END), 0)          AS egresos,
              COALESCE(SUM(CASE
                             WHEN tipo = 'Ingreso' THEN monto
                             WHEN tipo = 'Egreso'  THEN -monto
                             ELSE 0 END), 0)                                            AS neto,
              COALESCE(SUM(CASE
                             WHEN source = 'POS' AND tipo = 'Ingreso' THEN monto
                             ELSE 0 END), 0)                                            AS ingresos_pos,
              COALESCE(SUM(CASE
                             WHEN source = 'POS' AND tipo = 'Ingreso' THEN 1
                             ELSE 0 END), 0)                                            AS tx_pos
            FROM fin_cash_movement
            WHERE DATE(created_at) = :f
              AND estado = 'Aplicado'
        """),
        {"f": fecha}
    ).mappings().fetchone()

    cash = {
        "ingresos": float(cash_row["ingresos"] or 0.0),
        "egresos": float(cash_row["egresos"] or 0.0),
        "neto": float(cash_row["neto"] or 0.0),
        "ingresos_pos": float(cash_row["ingresos_pos"] or 0.0),
        "tx_pos": int(cash_row["tx_pos"] or 0),
    }

    # Ingresos por método de pago (Efectivo, Tarjeta, etc.)
    cash_metodos_rows = db.session.execute(
        text("""
            SELECT
              metodo,
              COALESCE(SUM(CASE WHEN tipo = 'Ingreso' THEN monto ELSE 0 END), 0) AS total
            FROM fin_cash_movement
            WHERE DATE(created_at) = :f
              AND estado = 'Aplicado'
            GROUP BY metodo
            ORDER BY total DESC
        """),
        {"f": fecha}
    ).mappings().fetchall()

    cash_por_metodo = [
        {
            "metodo": r["metodo"],
            "total": float(r["total"] or 0.0),
        }
        for r in cash_metodos_rows
    ]

    # ================================
    # 2) KPIs de libro mayor: fin_ledger_tx
    # ================================
    ledger_row = db.session.execute(
        text("""
            SELECT
              COALESCE(SUM(total), 0) AS total_posted,
              COUNT(*)                AS tx_count
            FROM fin_ledger_tx
            WHERE DATE(created_at) = :f
              AND status = 'posted'
        """),
        {"f": fecha}
    ).mappings().fetchone()

    ledger = {
        "total_posted": float(ledger_row["total_posted"] or 0.0),
        "tx_count": int(ledger_row["tx_count"] or 0),
    }

    # Saldos por cuenta (Caja, Banco/Tarjeta, CxC Huesped, etc.)
    ledger_cuentas_rows = db.session.execute(
        text("""
            SELECT
              l.account,
              COALESCE(SUM(l.debit), 0)  AS debit,
              COALESCE(SUM(l.credit), 0) AS credit
            FROM fin_ledger_line l
            INNER JOIN fin_ledger_tx t ON t.id_tx = l.id_tx
            WHERE DATE(t.created_at) = :f
              AND t.status = 'posted'
            GROUP BY l.account
            ORDER BY l.account
        """),
        {"f": fecha}
    ).mappings().fetchall()

    ledger_por_cuenta = [
        {
            "account": r["account"],
            "debit": float(r["debit"] or 0.0),
            "credit": float(r["credit"] or 0.0),
            "balance": float((r["debit"] or 0.0) - (r["credit"] or 0.0)),
        }
        for r in ledger_cuentas_rows
    ]

    return jsonify({
        "ok": True,
        "fecha": fecha.isoformat(),
        "cash": cash,
        "cash_por_metodo": cash_por_metodo,
        "ledger": ledger,
        "ledger_por_cuenta": ledger_por_cuenta,
    }), 200
