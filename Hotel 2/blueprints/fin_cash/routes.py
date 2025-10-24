# blueprints/fin_cash/routes.py
from flask import request, jsonify, current_app, send_file, abort, render_template, session
from . import fin_cash_bp
from sqlalchemy import text
from datetime import datetime, date
from io import BytesIO

from reportlab.lib.pagesizes import A4
from reportlab.pdfgen import canvas
from reportlab.lib.units import cm
from reportlab.lib import colors

from extensions import db


# =========================
# Helpers de BD / utilidades
# =========================
def _db():
    return db

def _month_key(d: date) -> str:
    return f"{d:%Y-%m}"

def _is_locked(d: date) -> bool:
    """True si el mes de 'd' está cerrado en fin_period_lock."""
    row = _db().session.execute(
        text("""
            SELECT 1
              FROM fin_period_lock
             WHERE period_key = :p AND status = 'closed'
             LIMIT 1
        """),
        {"p": _month_key(d)},
    ).fetchone()
    return bool(row)

def _is_admin() -> bool:
    """Permite controlar acciones reservadas al Admin (según sesión)."""
    try:
        return (session.get("user_role") or "").strip().lower() == "administrador"
    except Exception:
        return False


# =========================
# Caja helpers
# =========================
def _get_any_open_session():
    """Devuelve True si hay alguna caja abierta o reabierta en el sistema."""
    row = _db().session.execute(
        text("""
            SELECT id_session
              FROM fin_cash_session
             WHERE status IN ('open','reopened')
             LIMIT 1
        """)
    ).fetchone()
    return bool(row)


def _create_session_unique(fecha: date, opened_by: int, opening_cash: float = 0.0):
    """Crea una nueva caja solo si no hay otra abierta en todo el sistema."""
    if _get_any_open_session():
        return None  # No permite abrir otra caja mientras exista una abierta

    _db().session.execute(
        text("""
            INSERT INTO fin_cash_session (fecha, opened_by, opening_cash, status)
            VALUES (:f, :u, :cash, 'open')
        """),
        {"f": fecha, "u": opened_by, "cash": float(opening_cash)},
    )
    _db().session.commit()

    row = _db().session.execute(
        text("""
            SELECT id_session
              FROM fin_cash_session
             WHERE fecha=:f
             ORDER BY id_session DESC
             LIMIT 1
        """),
        {"f": fecha},
    ).fetchone()
    return (row[0] if row else None)


def _get_open_session(fecha: date):
    """Devuelve la sesión de caja ABIERTA (open/reopened) para la fecha indicada."""
    row = _db().session.execute(
        text("""
            SELECT id_session
              FROM fin_cash_session
             WHERE fecha=:f AND status IN ('open','reopened')
             ORDER BY id_session DESC
             LIMIT 1
        """),
        {"f": fecha},
    ).fetchone()
    return (row[0] if row else None)


def _calc_resume(fecha: date):
    """Lee la vista de resumen y devuelve dict o None."""
    row = _db().session.execute(
        text("SELECT * FROM v_fin_caja_resumen WHERE fecha=:f"),
        {"f": fecha}
    ).mappings().fetchone()
    return dict(row) if row else None


# =========================
# Endpoints JSON
# =========================

@fin_cash_bp.post("/apertura")
def apertura():
    """
    Abre caja del día.
    Solo se permite UNA caja abierta a la vez (sin importar usuario).
    """
    payload = request.get_json(force=True, silent=True) or {}
    opened_by = int(payload.get("opened_by", 1))
    opening_cash = float(payload.get("opening_cash", 0.0))
    fecha = datetime.fromisoformat(payload.get("fecha") or date.today().isoformat()).date()

    if _is_locked(fecha):
        return jsonify({
            "ok": False,
            "error": "period_locked",
            "message": f"El periodo {_month_key(fecha)} está cerrado. No se puede abrir caja."
        }), 423

    sid = _create_session_unique(fecha, opened_by, opening_cash)
    if not sid:
        return jsonify({
            "ok": False,
            "error": "already_open",
            "message": "Ya existe una caja abierta o reabierta. Debe cerrarse antes de abrir otra."
        }), 409

    return jsonify({"ok": True, "id_session": sid})


@fin_cash_bp.post("/movimiento")
def movimiento():
    """
    Inserta un movimiento MANUAL en la caja ABIERTA del día.
    """
    payload = request.get_json(force=True, silent=True) or {}
    required = ("tipo", "metodo", "concepto", "monto", "created_by")
    if not all(k in payload for k in required):
        return jsonify({"ok": False, "error": "missing_fields"}), 400

    fecha = datetime.fromisoformat(payload.get("fecha") or date.today().isoformat()).date()

    if _is_locked(fecha):
        return jsonify({
            "ok": False,
            "error": "period_locked",
            "message": f"El periodo {_month_key(fecha)} está cerrado. No se pueden registrar movimientos."
        }), 423

    sid = _get_open_session(fecha)
    if sid is None:
        return jsonify({
            "ok": False,
            "error": "no_open_session",
            "message": "No hay caja abierta para esa fecha. Abre la caja antes de registrar movimientos."
        }), 409

    _db().session.execute(
        text("""
            INSERT INTO fin_cash_movement
                (session_id, tipo, metodo, concepto, referencia, monto, created_by, source, estado)
            VALUES
                (:sid, :tipo, :metodo, :concepto, :ref, :monto, :by, 'Manual', 'Aplicado')
        """),
        {
            "sid": sid,
            "tipo": payload["tipo"],
            "metodo": payload["metodo"],
            "concepto": payload["concepto"],
            "ref": payload.get("referencia"),
            "monto": float(payload["monto"]),
            "by": int(payload["created_by"]),
        },
    )
    _db().session.commit()
    return jsonify({"ok": True, "session_id": sid})


@fin_cash_bp.post("/cierre")
def cierre():
    """
    Cierra la caja ABIERTA del día (si existe).
    """
    payload = request.get_json(force=True, silent=True) or {}
    fecha = datetime.fromisoformat(payload.get("fecha") or date.today().isoformat()).date()
    closed_by = int(payload.get("closed_by", 1))
    contado = float(payload.get("closing_cash_counted", 0.0))

    if _is_locked(fecha):
        return jsonify({
            "ok": False,
            "error": "period_locked",
            "message": f"El periodo {_month_key(fecha)} está cerrado. No se puede cerrar caja en este mes."
        }), 423

    sid = _get_open_session(fecha)
    if sid is None:
        return jsonify({
            "ok": False,
            "error": "no_open_session",
            "message": "No hay sesión abierta para esa fecha."
        }), 404

    _db().session.execute(
        text("""
            UPDATE fin_cash_session
               SET closing_cash_counted=:c, closed_at=NOW(), closed_by=:u, status='closed'
             WHERE id_session=:sid
        """),
        {"c": contado, "u": closed_by, "sid": sid},
    )
    _db().session.commit()

    resumen = _calc_resume(fecha) or {}
    return jsonify({"ok": True, "resumen": resumen})


@fin_cash_bp.post("/reabrir")
def reabrir():
    """
    Reabre una caja (solo Administrador).
    """
    if not _is_admin():
        return jsonify({"ok": False, "error": "forbidden", "message": "Solo Administrador puede reabrir cajas."}), 403

    payload = request.get_json(force=True, silent=True) or {}
    sid = int(payload.get("id_session") or 0)
    if not sid:
        return jsonify({"ok": False, "error": "missing_id"}), 400

    row = _db().session.execute(
        text("""
            SELECT id_session, fecha, status
              FROM fin_cash_session
             WHERE id_session=:sid
             LIMIT 1
        """),
        {"sid": sid}
    ).mappings().fetchone()

    if not row:
        return jsonify({"ok": False, "error": "not_found"}), 404

    fecha = row["fecha"]
    status = (row["status"] or "").lower()

    if _is_locked(fecha):
        return jsonify({
            "ok": False,
            "error": "period_locked",
            "message": f"El periodo {_month_key(fecha)} está cerrado. No se puede reabrir cajas."
        }), 423

    if status in ("open", "reopened"):
        return jsonify({"ok": False, "error": "already_open", "message": "La caja ya está abierta."}), 409

    if _get_any_open_session():
        return jsonify({
            "ok": False,
            "error": "another_open",
            "message": "Ya existe otra caja abierta. Ciérrala antes de reabrir otra."
        }), 409

    _db().session.execute(
        text("""
            UPDATE fin_cash_session
               SET status='reopened'
             WHERE id_session=:sid
        """),
        {"sid": sid}
    )
    _db().session.commit()

    return jsonify({"ok": True, "id_session": sid, "status": "reopened"})


@fin_cash_bp.get("/estado")
def estado():
    """Devuelve el id de caja abierta (si existe) para la fecha dada (o hoy por defecto)."""
    fecha = datetime.fromisoformat(request.args.get("fecha") or date.today().isoformat()).date()
    sid = _get_open_session(fecha)
    return jsonify({"ok": True, "fecha": fecha.isoformat(), "id_open_session": sid})


@fin_cash_bp.get("/conciliacion")
def conciliacion():
    """Devuelve el resumen de conciliación de la vista para la fecha dada."""
    fecha = datetime.fromisoformat(request.args.get("fecha") or date.today().isoformat()).date()
    return jsonify({"ok": True, "resumen": _calc_resume(fecha)})


@fin_cash_bp.get("/reporte.pdf")
def reporte_pdf():
    """Genera el PDF de conciliación para la fecha indicada."""
    fecha = datetime.fromisoformat(request.args.get("fecha") or date.today().isoformat()).date()
    resumen = _calc_resume(fecha)
    if not resumen:
        abort(404, description="No hay datos para esa fecha")

    buf = BytesIO()
    c = canvas.Canvas(buf, pagesize=A4)
    W, H = A4

    c.setFont("Helvetica-Bold", 14)
    c.drawString(2*cm, H-2*cm, "Conciliación diaria de caja")
    c.setFont("Helvetica", 10)
    c.drawString(2*cm, H-2.6*cm, f"Fecha: {fecha.isoformat()}")
    c.drawString(12*cm, H-2.6*cm, "Hotel Villa Grace")

    def row(y, label, value):
        c.setFont("Helvetica", 10)
        c.drawString(2*cm, y, label)
        try:
            val = float(value or 0)
        except Exception:
            val = 0.0
        c.drawRightString(W-2*cm, y, f"₡ {val:,.2f}")

    y = H - 4*cm
    row(y, "Efectivo inicial", resumen.get("opening_cash", 0)); y -= 0.6*cm
    row(y, "Ingresos (efectivo)", resumen.get("ingresos_efectivo", 0)); y -= 0.6*cm
    row(y, "Egresos (efectivo)", resumen.get("egresos_efectivo", 0)); y -= 0.6*cm
    row(y, "Ajustes (+)", resumen.get("ajustes_mas", 0)); y -= 0.6*cm
    row(y, "Ajustes (-)", resumen.get("ajustes_menos", 0)); y -= 0.6*cm

    c.setLineWidth(0.5)
    c.line(2*cm, y-0.2*cm, W-2*cm, y-0.2*cm)
    y -= 0.8*cm

    esperado = resumen.get("efectivo_esperado", 0)
    contado = resumen.get("efectivo_contado", 0)
    desc = resumen.get("descuadre", 0)

    c.setFont("Helvetica-Bold", 11)
    row(y, "Efectivo ESPERADO", esperado); y -= 0.6*cm
    row(y, "Efectivo CONTADO", contado); y -= 0.6*cm

    c.setStrokeColor(colors.black)
    box_y = y - 0.2*cm
    c.rect(2*cm, box_y-0.4*cm, W-4*cm, 1.2*cm, stroke=1, fill=0)
    c.setFont("Helvetica-Bold", 12)
    try:
        desc_val = float(desc or 0)
    except Exception:
        desc_val = 0.0
    c.drawString(2.3*cm, box_y+0.1*cm, f"DESCUADRE: ₡ {desc_val:,.2f}")

    c.showPage()
    c.save()
    buf.seek(0)
    return send_file(
        buf,
        mimetype="application/pdf",
        as_attachment=True,
        download_name=f"Conciliacion_Caja_{fecha.isoformat()}.pdf"
    )


# =========================
# UI mínima
# =========================
@fin_cash_bp.get("/ui")
def ui():
    return render_template("fin-caja.html")



