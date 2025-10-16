# blueprints/fin_periods/routes.py
from __future__ import annotations

import re
from datetime import datetime, date, timedelta
from typing import Tuple, Optional

from flask import request, jsonify, current_app, render_template, session
from sqlalchemy import text

from . import fin_periods_bp
from extensions import db


# ============== Helpers ==============

def _is_admin() -> bool:
    """Retorna True si el usuario en sesión tiene rol Administrador."""
    try:
        return (session.get("user_role") or "").strip().lower() == "administrador"
    except Exception:
        return False


def _norm_period(p: Optional[str]) -> Optional[str]:
    """
    Normaliza distintos formatos a 'YYYY-MM'.
    Acepta:
      - 2025-10 / 2025/10 / 2025.10
      - October 2025 / Oct 2025 / 2025 October / 2025 Oct
      - Si es None o vacío -> mes actual
    Devuelve None si no se puede interpretar.
    """
    if not p or not str(p).strip():
        return date.today().strftime("%Y-%m")

    s = str(p).strip()

    # Caso 1: 'YYYY-MM' exacto
    if re.fullmatch(r"\d{4}-\d{2}", s):
        try:
            datetime.strptime(s, "%Y-%m")
            return s
        except ValueError:
            return None

    # Caso 2: 'YYYY/MM' o 'YYYY.MM'
    m = re.fullmatch(r"(\d{4})[\/\.](\d{1,2})", s)
    if m:
        y, mth = int(m.group(1)), int(m.group(2))
        if 1 <= mth <= 12:
            return f"{y:04d}-{mth:02d}"
        return None

    # Caso 3: Month-name + Year (en varias combinaciones)
    for fmt in ("%B %Y", "%b %Y", "%Y %B", "%Y %b"):
        try:
            dt = datetime.strptime(s, fmt)
            return dt.strftime("%Y-%m")
        except ValueError:
            pass

    return None


def _month_bounds(period_key: str) -> Tuple[date, date]:
    """Devuelve (primer_dia, ultimo_dia) para 'YYYY-MM'."""
    dt0 = datetime.strptime(period_key + "-01", "%Y-%m-%d").date()
    # próximo mes - 1 día
    if dt0.month == 12:
        next_m = date(dt0.year + 1, 1, 1)
    else:
        next_m = date(dt0.year, dt0.month + 1, 1)
    return (dt0, next_m - timedelta(days=1))


def _get_lock_row(period_key: str):
    row = db.session.execute(
        text("""
            SELECT id_lock, period_key, status, locked_by, locked_at, notes
              FROM fin_period_lock
             WHERE period_key = :p
             LIMIT 1
        """),
        {"p": period_key}
    ).mappings().fetchone()
    return row


def _has_open_cash_in_month(period_key: str) -> bool:
    """True si existe alguna caja con estado open/reopened en ese mes."""
    d1, d2 = _month_bounds(period_key)
    row = db.session.execute(
        text("""
            SELECT 1
              FROM fin_cash_session
             WHERE fecha >= :d1 AND fecha <= :d2
               AND status IN ('open','reopened')
             LIMIT 1
        """),
        {"d1": d1, "d2": d2}
    ).fetchone()
    return bool(row)


def _shift_months(d: date, months_back: int) -> date:
    """
    Devuelve la fecha 'd' movida 'months_back' meses hacia atrás, con el día forzado a 1.
    Ej: d=2025-10-01, months_back=3 => 2025-07-01
    """
    year = d.year
    month = d.month - months_back
    while month <= 0:
        month += 12
        year -= 1
    return date(year, month, 1)


# ============== Endpoints ==============

@fin_periods_bp.get("/status")
def get_status():
    """
    Si pasas ?period=YYYY-MM (o un formato aceptado) -> estado de ese periodo.
    Si no, devuelve una lista de los últimos 12 periodos (status si existe, 'open' si no).
    """
    raw = request.args.get("period")
    if raw is not None:
        period = _norm_period(raw)
        if not period:
            return jsonify({"ok": False, "error": "period_invalid"}), 400
        row = _get_lock_row(period)
        status = row["status"] if row else "open"
        return jsonify({
            "ok": True,
            "period": period,
            "status": status,
            "lock": dict(row) if row else None
        })

    # últimos 12 meses (incluye el actual)
    out = []
    start = date.today().replace(day=1)
    for i in range(12):
        dt = _shift_months(start, i)
        pk = dt.strftime("%Y-%m")
        row = _get_lock_row(pk)
        out.append({
            "period": pk,
            "status": row["status"] if row else "open",
            "lock": dict(row) if row else None
        })

    return jsonify({"ok": True, "items": out})


@fin_periods_bp.get("/list")
def list_periods():
    """Lista de locks registrados (paginación simple)."""
    limit = int(request.args.get("limit") or 100)
    rows = db.session.execute(
        text("""
            SELECT id_lock, period_key, status, locked_by, locked_at, notes
              FROM fin_period_lock
             ORDER BY period_key DESC
             LIMIT :lim
        """),
        {"lim": limit}
    ).mappings().all()
    return jsonify({"ok": True, "items": [dict(r) for r in rows]})


@fin_periods_bp.post("/lock")
def lock_period():
    """
    Bloquea un periodo (YYYY-MM). Solo Administrador.
    Reglas:
      - Si hay cajas abiertas dentro del mes -> 409.
      - Si ya está 'closed' -> idempotente (devuelve ok, status=closed).
    """
    if not _is_admin():
        return jsonify({"ok": False, "error": "forbidden",
                        "message": "Solo Administrador puede bloquear periodos."}), 403

    payload = request.get_json(force=True, silent=True) or {}
    period = _norm_period(payload.get("period"))
    if not period:
        return jsonify({"ok": False, "error": "period_invalid"}), 400

    notes = (payload.get("notes") or "").strip()[:255]
    user_id = int(session.get("user_id") or 0)

    # Validar cajas abiertas
    if _has_open_cash_in_month(period):
        return jsonify({
            "ok": False,
            "error": "open_cash_sessions",
            "message": "Existen cajas ABIERTAS dentro de ese mes. Ciérralas antes de bloquear."
        }), 409

    # Upsert: si existe -> update a 'closed', si no -> insert 'closed'
    row = _get_lock_row(period)
    if row and (row["status"] or "").lower() == "closed":
        return jsonify({"ok": True, "period": period, "status": "closed", "lock": dict(row)})

    if row:
        db.session.execute(
            text("""
                UPDATE fin_period_lock
                   SET status='closed', locked_by=:u, locked_at=NOW(), notes=:n
                 WHERE id_lock=:id
            """),
            {"u": user_id or None, "n": notes or None, "id": row["id_lock"]}
        )
    else:
        db.session.execute(
            text("""
                INSERT INTO fin_period_lock (period_key, status, locked_by, locked_at, notes)
                VALUES (:p, 'closed', :u, NOW(), :n)
            """),
            {"p": period, "u": user_id or None, "n": notes or None}
        )
    db.session.commit()

    row2 = _get_lock_row(period)
    return jsonify({"ok": True, "period": period, "status": "closed",
                    "lock": dict(row2) if row2 else None})


@fin_periods_bp.post("/unlock")
def unlock_period():
    """
    Desbloquea un periodo (YYYY-MM). Solo Administrador.
    Reglas:
      - Si ya está 'open' (o no existe registro) -> idempotente (status='open').
    """
    if not _is_admin():
        return jsonify({"ok": False, "error": "forbidden",
                        "message": "Solo Administrador puede desbloquear periodos."}), 403

    payload = request.get_json(force=True, silent=True) or {}
    period = _norm_period(payload.get("period"))
    if not period:
        return jsonify({"ok": False, "error": "period_invalid"}), 400

    notes = (payload.get("notes") or "").strip()[:255]
    user_id = int(session.get("user_id") or 0)

    row = _get_lock_row(period)
    if row:
        if (row["status"] or "").lower() == "open":
            return jsonify({"ok": True, "period": period, "status": "open", "lock": dict(row)})
        db.session.execute(
            text("""
                UPDATE fin_period_lock
                   SET status='open', locked_by=:u, locked_at=NOW(), notes=:n
                 WHERE id_lock=:id
            """),
            {"u": user_id or None, "n": notes or None, "id": row["id_lock"]}
        )
        db.session.commit()
        row2 = _get_lock_row(period)
        return jsonify({"ok": True, "period": period, "status": "open", "lock": dict(row2)})

    # si no existía, lo creamos como 'open' para dejar constancia
    db.session.execute(
        text("""
            INSERT INTO fin_period_lock (period_key, status, locked_by, locked_at, notes)
            VALUES (:p, 'open', :u, NOW(), :n)
        """),
        {"p": period, "u": user_id or None, "n": notes or None}
    )
    db.session.commit()
    row3 = _get_lock_row(period)
    return jsonify({"ok": True, "period": period, "status": "open", "lock": dict(row3)})


@fin_periods_bp.get("/ui")
def ui_periods():
    """Página mínima (si deseas un panel visual)."""
    return render_template("fin-periods.html")






