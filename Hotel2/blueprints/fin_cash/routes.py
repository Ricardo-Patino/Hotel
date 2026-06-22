# blueprints/fin_cash/routes.py
import os
from flask import request, jsonify, current_app, send_file, abort, render_template, session
from . import fin_cash_bp
from sqlalchemy import text
from datetime import datetime, date
from io import BytesIO

from extensions import db

# ReportLab (PDF)
from reportlab.lib.pagesizes import A4
from reportlab.lib.units import cm
from reportlab.lib import colors
from reportlab.lib.enums import TA_LEFT, TA_RIGHT, TA_CENTER
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.platypus import (
    SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle, Image
)

# =========================
# Helpers de BD / utilidades
# =========================
def _db():
    return db

def _month_key(d: date) -> str:
    return f"{d:%Y-%m}"

def _is_locked(d: date) -> bool:
    """(legacy) True si el mes de 'd' está cerrado explícitamente con status='closed'."""
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

def _is_locked_effective(d: date) -> bool:
    """
    Política contable (HU-06 / Periodos):
    - Si existe fila en fin_period_lock para ese period_key:
        * status = 'closed'    => bloqueado
        * status = 'open'      => NO bloqueado
        * status = 'reopened'  => NO bloqueado
    - Si NO existe fila:
        * si es el mes actual => NO bloqueado
        * si NO es el mes actual => BLOQUEADO
    """
    pk = _month_key(d)

    row = _db().session.execute(
        text("""
            SELECT status
              FROM fin_period_lock
             WHERE period_key = :p
             LIMIT 1
        """),
        {"p": pk},
    ).mappings().fetchone()

    if row:
        st = (row["status"] or "").lower()
        if st == "closed":
            return True
        return False

    current_month = date.today().strftime("%Y-%m")
    if pk == current_month:
        return False
    return True

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
    """
    Crea una nueva caja solo si no hay otra abierta en todo el sistema
    y si el periodo contable de esa fecha está permitido.
    """
    if _is_locked_effective(fecha):
        return None

    if _get_any_open_session():
        return None

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

def _get_session_info(fecha: date):
    """
    Info útil para el PDF (si existe sesión del día).
    Compatible con tablas que NO tienen opened_at/closed_at.
    """
    # 1) Descubrir columnas reales en MySQL
    cols = _db().session.execute(
        text("""
            SELECT COLUMN_NAME
            FROM INFORMATION_SCHEMA.COLUMNS
            WHERE TABLE_SCHEMA = DATABASE()
              AND TABLE_NAME = 'fin_cash_session'
        """)
    ).fetchall()

    colset = {c[0].lower() for c in cols}

    def pick(*names):
        for n in names:
            if n.lower() in colset:
                return n
        return None

    opened_at_col = pick("opened_at", "openedAt", "apertura_at", "created_at", "createdAt", "created_on")
    closed_at_col = pick("closed_at", "closedAt", "cierre_at", "updated_at", "updatedAt", "updated_on")

    # 2) Armar SELECT dinámico SIN columnas que no existan
    select_cols = [
        "id_session", "fecha", "status",
        "opened_by", "opening_cash",
        "closed_by", "closing_cash_counted"
    ]

    if opened_at_col:
        select_cols.append(f"{opened_at_col} AS opened_at")
    else:
        select_cols.append("NULL AS opened_at")

    if closed_at_col:
        select_cols.append(f"{closed_at_col} AS closed_at")
    else:
        select_cols.append("NULL AS closed_at")

    sql = f"""
        SELECT {", ".join(select_cols)}
        FROM fin_cash_session
        WHERE fecha = :f
        ORDER BY id_session DESC
        LIMIT 1
    """

    row = _db().session.execute(text(sql), {"f": fecha}).mappings().fetchone()
    return dict(row) if row else None


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
    payload = request.get_json(force=True, silent=True) or {}
    opened_by = int(payload.get("opened_by", 1))
    opening_cash = float(payload.get("opening_cash", 0.0))
    fecha = datetime.fromisoformat(payload.get("fecha") or date.today().isoformat()).date()

    if _is_locked_effective(fecha):
        return jsonify({
            "ok": False,
            "error": "period_locked",
            "message": f"El periodo {_month_key(fecha)} está bloqueado. No se puede abrir caja."
        }), 423

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
            "message": "Ya existe una caja abierta o reabierta, o el periodo está bloqueado."
        }), 409

    return jsonify({"ok": True, "id_session": sid})

@fin_cash_bp.post("/movimiento")
def movimiento():
    payload = request.get_json(force=True, silent=True) or {}
    required = ("tipo", "metodo", "concepto", "monto", "created_by")
    if not all(k in payload for k in required):
        return jsonify({"ok": False, "error": "missing_fields"}), 400

    fecha = datetime.fromisoformat(payload.get("fecha") or date.today().isoformat()).date()

    if _is_locked_effective(fecha):
        return jsonify({
            "ok": False,
            "error": "period_locked",
            "message": f"El periodo {_month_key(fecha)} está bloqueado. No se pueden registrar movimientos."
        }), 423

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
    payload = request.get_json(force=True, silent=True) or {}
    fecha = datetime.fromisoformat(payload.get("fecha") or date.today().isoformat()).date()
    closed_by = int(payload.get("closed_by", 1))
    contado = float(payload.get("closing_cash_counted", 0.0))

    if _is_locked_effective(fecha):
        return jsonify({
            "ok": False,
            "error": "period_locked",
            "message": f"El periodo {_month_key(fecha)} está bloqueado. No se puede cerrar caja en este mes."
        }), 423

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

    resumen = _calc_resume(fecha) or None
    return jsonify({"ok": True, "resumen": resumen})

@fin_cash_bp.post("/reabrir")
def reabrir():
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

    if _is_locked_effective(fecha):
        return jsonify({
            "ok": False,
            "error": "period_locked",
            "message": f"El periodo {_month_key(fecha)} está bloqueado. No se puede reabrir cajas."
        }), 423

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
    fecha = datetime.fromisoformat(request.args.get("fecha") or date.today().isoformat()).date()
    sid = _get_open_session(fecha)
    return jsonify({"ok": True, "fecha": fecha.isoformat(), "id_open_session": sid})

@fin_cash_bp.get("/conciliacion")
def conciliacion():
    fecha = datetime.fromisoformat(request.args.get("fecha") or date.today().isoformat()).date()
    return jsonify({"ok": True, "resumen": _calc_resume(fecha)})

# =========================
# PDF profesional
# =========================
def _money(x):
    try:
        v = float(x or 0.0)
    except Exception:
        v = 0.0
    # Formato CR (separador de miles)
    return f"CRC {v:,.2f}"



def _pretty_text(v) -> str:
    s = str(v or "")
    s = s.replace("\\r\\n", "\n")
    s = s.replace("\\n", "\n")
    s = s.replace("/n", "\n")
    s = s.replace("\r\n", "\n")
    s = s.replace("\r", "\n")
    return s.strip()


def _pdf_paragraph_text(v, style):
    from html import escape as html_escape
    safe = html_escape(_pretty_text(v))
    safe = safe.replace("\n", "<br/>")
    return Paragraph(safe or "—", style)


def _movimientos_del_dia(fecha: date) -> list[dict]:
    rows = _db().session.execute(
        text("""
            SELECT
              m.id_move,
              m.tipo,
              m.metodo,
              m.concepto,
              m.referencia,
              m.monto,
              m.created_at
            FROM fin_cash_movement m
            JOIN fin_cash_session s ON s.id_session = m.session_id
            WHERE s.fecha = :f
              AND m.estado = 'Aplicado'
            ORDER BY m.created_at ASC, m.id_move ASC
        """),
        {"f": fecha},
    ).mappings().all()

    return [dict(r) for r in rows]



@fin_cash_bp.get("/reporte.pdf")
def reporte_pdf():
    """
    Genera el PDF de conciliación para la fecha indicada.
    ✅ IMPORTANTE: ya NO devuelve 404 cuando no hay datos.
    En su lugar genera un PDF "SIN MOVIMIENTOS" (proforma) con totales en 0.
    """
    fecha = datetime.fromisoformat(request.args.get("fecha") or date.today().isoformat()).date()

    resumen = _calc_resume(fecha)
    has_data = bool(resumen)

    # Si no hay resumen, armamos estructura en 0 (PDF vacío / proforma)
    if not resumen:
        resumen = {}

    # Asegurar llaves esperadas (por si tu vista no trae algo)
    resumen.setdefault("opening_cash", 0)
    resumen.setdefault("ingresos_efectivo", 0)
    resumen.setdefault("egresos_efectivo", 0)
    resumen.setdefault("ajustes_mas", 0)
    resumen.setdefault("ajustes_menos", 0)
    resumen.setdefault("efectivo_esperado", 0)
    resumen.setdefault("efectivo_contado", resumen.get("closing_cash_counted", 0) or 0)
    resumen.setdefault("descuadre", 0)

    # Info de sesión (si existe)
    sess = _get_session_info(fecha) or {}

    hotel_name = "Hotel Villa Grace"
    generado_por = (session.get("user_name") or "—").strip()
    rol = (session.get("user_role") or "—").strip()

    # Logo opcional
    logo_path = os.path.join(current_app.root_path, "static", "assets", "img", "favicon.png")
    logo_exists = os.path.exists(logo_path)

    buf = BytesIO()
    doc = SimpleDocTemplate(
        buf,
        pagesize=A4,
        leftMargin=2*cm,
        rightMargin=2*cm,
        topMargin=2.0*cm,
        bottomMargin=2.0*cm
    )

    styles = getSampleStyleSheet()
    s_title = ParagraphStyle(
        "title",
        parent=styles["Heading1"],
        fontName="Helvetica-Bold",
        fontSize=16,
        leading=18,
        alignment=TA_LEFT,
        textColor=colors.HexColor("#1b7a4e")
    )
    s_sub = ParagraphStyle(
        "sub",
        parent=styles["Normal"],
        fontName="Helvetica",
        fontSize=10,
        leading=12,
        textColor=colors.HexColor("#3b3b3b")
    )
    s_small = ParagraphStyle(
        "small",
        parent=styles["Normal"],
        fontName="Helvetica",
        fontSize=8.5,
        leading=10,
        textColor=colors.HexColor("#6b7280")
    )
    s_right = ParagraphStyle(
        "right",
        parent=styles["Normal"],
        fontName="Helvetica",
        fontSize=10,
        leading=12,
        alignment=TA_RIGHT
    )
    s_center = ParagraphStyle(
        "center",
        parent=styles["Normal"],
        fontName="Helvetica",
        fontSize=10,
        leading=12,
        alignment=TA_CENTER
    )

    def _on_page(canv, _doc):
        # Barra superior fina
        canv.saveState()
        canv.setFillColor(colors.HexColor("#1b7a4e"))
        canv.rect(0, A4[1]-1.0*cm, A4[0], 0.18*cm, stroke=0, fill=1)
        canv.restoreState()

        # Pie de página
        canv.saveState()
        canv.setStrokeColor(colors.HexColor("#e5e7eb"))
        canv.setLineWidth(0.8)
        canv.line(2*cm, 1.55*cm, A4[0]-2*cm, 1.55*cm)

        canv.setFont("Helvetica", 8.5)
        canv.setFillColor(colors.HexColor("#6b7280"))
        now = datetime.now().strftime("%Y-%m-%d %H:%M")
        canv.drawString(2*cm, 1.1*cm, f"Generado: {now}")
        canv.drawRightString(A4[0]-2*cm, 1.1*cm, f"Página {canv.getPageNumber()}")
        canv.restoreState()

    elements = []

    # Header (logo + títulos)
    left_block = []
    if logo_exists:
        try:
            left_block.append(Image(logo_path, width=1.5*cm, height=1.5*cm))
        except Exception:
            pass

    title = Paragraph("Conciliación diaria de caja", s_title)
    subtitle = Paragraph(f"Fecha: <b>{fecha.isoformat()}</b>", s_sub)
    meta = Paragraph(f"{hotel_name}<br/>Usuario: <b>{generado_por}</b> · Rol: <b>{rol}</b>", s_small)

    # Armado header en tabla
    header_data = []
    if left_block:
        header_data = [[left_block[0], title]]
        header_tbl = Table(header_data, colWidths=[1.8*cm, (A4[0]-4*cm)-1.8*cm])
        header_tbl.setStyle(TableStyle([
            ("VALIGN", (0,0), (-1,-1), "MIDDLE"),
            ("LEFTPADDING", (0,0), (-1,-1), 0),
            ("RIGHTPADDING", (0,0), (-1,-1), 0),
            ("BOTTOMPADDING", (0,0), (-1,-1), 6),
        ]))
        elements.append(header_tbl)
    else:
        elements.append(title)

    elements.append(subtitle)
    elements.append(Spacer(1, 6))
    elements.append(meta)
    elements.append(Spacer(1, 12))

    # Si NO hay datos, sello / aviso
    if not has_data:
        warn = Paragraph(
            "<b>Estado:</b> SIN MOVIMIENTOS / SIN INFORMACIÓN REGISTRADA PARA ESTA FECHA.",
            ParagraphStyle(
                "warn",
                parent=styles["Normal"],
                fontName="Helvetica-Bold",
                fontSize=10.5,
                leading=13,
                textColor=colors.HexColor("#b45309")
            )
        )
        elements.append(warn)
        elements.append(Spacer(1, 10))

    # Info sesión (si existe)
    sid = sess.get("id_session") or "—"
    st = (sess.get("status") or "—")
    opened_at = sess.get("opened_at")
    closed_at = sess.get("closed_at")

    info_txt = f"""
    <b>Sesión:</b> {sid} &nbsp;&nbsp; <b>Estado:</b> {st}<br/>
    <b>Apertura:</b> {opened_at or "—"} &nbsp;&nbsp; <b>Cierre:</b> {closed_at or "—"}
    """
    elements.append(Paragraph(info_txt, s_small))
    elements.append(Spacer(1, 10))

    # Tabla principal (resumen)
    rows = [
        ["Concepto", "Monto"],
        ["Efectivo inicial", _money(resumen.get("opening_cash"))],
        ["Ingresos (efectivo)", _money(resumen.get("ingresos_efectivo"))],
        ["Egresos (efectivo)", _money(resumen.get("egresos_efectivo"))],
        ["Ajustes (+)", _money(resumen.get("ajustes_mas"))],
        ["Ajustes (-)", _money(resumen.get("ajustes_menos"))],
    ]
    tbl = Table(rows, colWidths=[(A4[0]-4*cm)*0.68, (A4[0]-4*cm)*0.32])
    tbl.setStyle(TableStyle([
        ("BACKGROUND", (0,0), (-1,0), colors.HexColor("#1b7a4e")),
        ("TEXTCOLOR", (0,0), (-1,0), colors.white),
        ("FONTNAME", (0,0), (-1,0), "Helvetica-Bold"),
        ("FONTSIZE", (0,0), (-1,0), 10),
        ("ALIGN", (1,1), (1,-1), "RIGHT"),
        ("VALIGN", (0,0), (-1,-1), "MIDDLE"),
        ("GRID", (0,0), (-1,-1), 0.5, colors.HexColor("#e5e7eb")),
        ("ROWBACKGROUNDS", (0,1), (-1,-1), [colors.white, colors.HexColor("#f9fafb")]),
        ("LEFTPADDING", (0,0), (-1,-1), 8),
        ("RIGHTPADDING", (0,0), (-1,-1), 8),
        ("TOPPADDING", (0,0), (-1,-1), 6),
        ("BOTTOMPADDING", (0,0), (-1,-1), 6),
    ]))
    elements.append(tbl)
    elements.append(Spacer(1, 12))

    # Totales destacados
    esperado = resumen.get("efectivo_esperado", 0)
    contado = resumen.get("efectivo_contado", 0)
    desc = resumen.get("descuadre", 0)
    try:
        desc_val = float(desc or 0)
    except Exception:
        desc_val = 0.0

    desc_bg = colors.HexColor("#fee2e2") if abs(desc_val) > 0.01 else colors.HexColor("#dcfce7")
    desc_fg = colors.HexColor("#991b1b") if abs(desc_val) > 0.01 else colors.HexColor("#166534")

    totals = [
        ["Efectivo ESPERADO", _money(esperado)],
        ["Efectivo CONTADO", _money(contado)],
        ["DESCUADRE", _money(desc)],
    ]
    tot_tbl = Table([["Totales", ""], *totals], colWidths=[(A4[0]-4*cm)*0.68, (A4[0]-4*cm)*0.32])
    tot_tbl.setStyle(TableStyle([
        ("BACKGROUND", (0,0), (-1,0), colors.HexColor("#0f172a")),
        ("TEXTCOLOR", (0,0), (-1,0), colors.white),
        ("FONTNAME", (0,0), (-1,0), "Helvetica-Bold"),
        ("FONTSIZE", (0,0), (-1,0), 10),

        ("GRID", (0,0), (-1,-1), 0.5, colors.HexColor("#e5e7eb")),
        ("ALIGN", (1,1), (1,-1), "RIGHT"),
        ("VALIGN", (0,0), (-1,-1), "MIDDLE"),

        ("FONTNAME", (0,1), (-1,-1), "Helvetica-Bold"),
        ("FONTSIZE", (0,1), (-1,-1), 10),

        ("BACKGROUND", (0,3), (-1,3), desc_bg),
        ("TEXTCOLOR", (0,3), (-1,3), desc_fg),

        ("LEFTPADDING", (0,0), (-1,-1), 8),
        ("RIGHTPADDING", (0,0), (-1,-1), 8),
        ("TOPPADDING", (0,0), (-1,-1), 7),
        ("BOTTOMPADDING", (0,0), (-1,-1), 7),
    ]))
    elements.append(tot_tbl)
    elements.append(Spacer(1, 14))
    
    
    # Detalle de movimientos manuales del día.
    # Esto permite ver ingresos aunque no sean efectivo, sin alterar el cálculo de efectivo esperado.
    movimientos = _movimientos_del_dia(fecha)

    if movimientos:
        elements.append(Paragraph("<b>Movimientos manuales del día</b>", ParagraphStyle(
            "h_movs",
            parent=styles["Normal"],
            fontName="Helvetica-Bold",
            fontSize=11,
            textColor=colors.HexColor("#111827")
        )))
        elements.append(Spacer(1, 8))

        mov_rows = [["Hora", "Tipo", "Método", "Concepto", "Monto"]]

        for m in movimientos:
            hora = "—"
            try:
                hora = m.get("created_at").strftime("%H:%M") if m.get("created_at") else "—"
            except Exception:
                hora = "—"

            mov_rows.append([
                hora,
                str(m.get("tipo") or "—"),
                str(m.get("metodo") or "—"),
                _pdf_paragraph_text(m.get("concepto"), s_small),
                _money(m.get("monto")),
            ])

        mov_tbl = Table(
            mov_rows,
            colWidths=[
                (A4[0]-4*cm)*0.12,
                (A4[0]-4*cm)*0.16,
                (A4[0]-4*cm)*0.18,
                (A4[0]-4*cm)*0.34,
                (A4[0]-4*cm)*0.20,
            ]
        )

        mov_tbl.setStyle(TableStyle([
            ("BACKGROUND", (0,0), (-1,0), colors.HexColor("#1b7a4e")),
            ("TEXTCOLOR", (0,0), (-1,0), colors.white),
            ("FONTNAME", (0,0), (-1,0), "Helvetica-Bold"),
            ("FONTSIZE", (0,0), (-1,0), 9),
            ("GRID", (0,0), (-1,-1), 0.5, colors.HexColor("#e5e7eb")),
            ("VALIGN", (0,0), (-1,-1), "TOP"),
            ("ALIGN", (4,1), (4,-1), "RIGHT"),
            ("ROWBACKGROUNDS", (0,1), (-1,-1), [colors.white, colors.HexColor("#f9fafb")]),
            ("LEFTPADDING", (0,0), (-1,-1), 6),
            ("RIGHTPADDING", (0,0), (-1,-1), 6),
            ("TOPPADDING", (0,0), (-1,-1), 5),
            ("BOTTOMPADDING", (0,0), (-1,-1), 5),
        ]))

        elements.append(mov_tbl)
        elements.append(Spacer(1, 14))
    
    

    # Firmas / observaciones
    elements.append(Paragraph("<b>Firmas</b>", ParagraphStyle(
        "h2",
        parent=styles["Normal"],
        fontName="Helvetica-Bold",
        fontSize=11,
        textColor=colors.HexColor("#111827")
    )))
    elements.append(Spacer(1, 8))

    sig_tbl = Table(
        [
            ["Entregado por:", "Recibido por:"],
            ["______________________________", "______________________________"],
            ["Nombre y firma", "Nombre y firma"],
            ["", ""],
            ["Observaciones:", ""],
            ["______________________________________________", ""],
        ],
        colWidths=[(A4[0]-4*cm)*0.5, (A4[0]-4*cm)*0.5]
    )
    sig_tbl.setStyle(TableStyle([
        ("FONTNAME", (0,0), (-1,0), "Helvetica-Bold"),
        ("FONTSIZE", (0,0), (-1,0), 9.5),
        ("TEXTCOLOR", (0,0), (-1,0), colors.HexColor("#374151")),

        ("FONTSIZE", (0,1), (-1,-1), 9),
        ("TEXTCOLOR", (0,1), (-1,-1), colors.HexColor("#6b7280")),

        ("TOPPADDING", (0,0), (-1,-1), 4),
        ("BOTTOMPADDING", (0,0), (-1,-1), 4),
    ]))
    elements.append(sig_tbl)

    # Construir PDF
    doc.build(elements, onFirstPage=_on_page, onLaterPages=_on_page)
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




