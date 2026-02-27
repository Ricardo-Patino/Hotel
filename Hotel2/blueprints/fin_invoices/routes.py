# -*- coding: utf-8 -*-
import os
import uuid  # ✅ NUEVO (solo para generar número único de recibo)
from io import BytesIO
from datetime import datetime
from decimal import Decimal, InvalidOperation

from flask import (
    request, jsonify, current_app,
    render_template, redirect,
    flash, session, send_file, abort
)
from sqlalchemy import text
from werkzeug.utils import secure_filename
from werkzeug.routing import BuildError

from extensions import db
from . import fin_invoices_bp

# =========================================================
# 1) FIX url_for en templates (evita BuildError por blueprint)
#    - Tus templates pueden seguir usando: url_for('fin_invoice_pdf', id=...)
# =========================================================
from flask import url_for as flask_url_for

@fin_invoices_bp.app_context_processor
def _inject_url_for_robusto():
    def url_for(endpoint, **values):
        try:
            return flask_url_for(endpoint, **values)
        except BuildError:
            if "." not in endpoint:
                return flask_url_for(f"{fin_invoices_bp.name}.{endpoint}", **values)
            raise
    return {"url_for": url_for}

# =========================================================
# 2) PDF (ReportLab)
# =========================================================
_HAS_REPORTLAB = True
try:
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.units import cm
    from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
    from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle
    from reportlab.lib import colors
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.ttfonts import TTFont
    from reportlab.lib.fonts import addMapping
except Exception:
    _HAS_REPORTLAB = False

# ==========================
# THEME (Villa Grace)
# ==========================
VG_GREEN = "#1b7a4e"
VG_DARK  = "#0f172a"
VG_TEXT  = "#334155"
VG_MUTED = "#475569"
VG_LINE  = "#e2e8f0"
VG_BG    = "#f8fafc"

# ==========================
# Helpers DB / Sesión
# ==========================
def _db():
    return db

def _user_id_default():
    return int(session.get("user_id") or 1)

def _url(ep: str, **values):
    """
    url_for robusto: intenta endpoint plano y luego con prefijo del blueprint.
    """
    try:
        return flask_url_for(ep, **values)
    except BuildError:
        try:
            return flask_url_for(f"{fin_invoices_bp.name}.{ep}", **values)
        except BuildError:
            return "/fin-invoices.html"

# ==========================
# Helpers Files
# ==========================
def _ensure_dir(path: str):
    os.makedirs(path, exist_ok=True)

def _static_upload_dir(*parts):
    base = os.path.join(current_app.static_folder, "uploads", *parts)
    _ensure_dir(base)
    return base

def _save_upload(file_storage, subfolder: str, preferred_name: str | None = None) -> str | None:
    if not file_storage:
        return None

    filename = (file_storage.filename or "").strip()
    if not filename:
        return None

    filename = secure_filename(filename)

    if preferred_name:
        _, ext = os.path.splitext(filename)
        ext = ext if ext else ".pdf"
        filename = secure_filename(preferred_name) + ext

    ts = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    name, ext = os.path.splitext(filename)
    final_name = f"{name}_{ts}{ext}"

    folder = _static_upload_dir(subfolder)
    file_storage.save(os.path.join(folder, final_name))
    return final_name

# ==========================
# Helpers Number / Money
# ==========================
def _to_decimal(x, default=None):
    try:
        return Decimal(str(x))
    except (InvalidOperation, TypeError, ValueError):
        return default

def _fmt_num_es(n) -> str:
    try:
        v = float(n)
    except Exception:
        v = 0.0
    s = f"{v:,.2f}"              # 1,500.00
    s = s.replace(",", "X").replace(".", ",").replace("X", ".")
    return s

def _dec(x) -> Decimal:
    try:
        return Decimal(str(x or 0))
    except Exception:
        return Decimal("0")

# ==========================
# Fonts: soportar ₡ (si hay TTF) + FIX vgfamily
# ==========================
_VG_FONT = "Helvetica"
_VG_HAS_TTF = False

def _register_vg_font() -> str:
    """
    Registra fuentes TTF y hace mapping para que <b> funcione sin romper Paragraph.
    Recomendado:
      static/assets/fonts/DejaVuSans.ttf
      static/assets/fonts/DejaVuSans-Bold.ttf
    """
    global _VG_FONT, _VG_HAS_TTF
    if not _HAS_REPORTLAB:
        return _VG_FONT

    if _VG_HAS_TTF:
        return _VG_FONT

    candidates = [
        (
            os.path.join(current_app.static_folder, "assets", "fonts", "DejaVuSans.ttf"),
            os.path.join(current_app.static_folder, "assets", "fonts", "DejaVuSans-Bold.ttf"),
        ),
        ("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
         "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"),
        (r"C:\Windows\Fonts\segoeui.ttf", r"C:\Windows\Fonts\segoeuib.ttf"),
        (r"C:\Windows\Fonts\arial.ttf",   r"C:\Windows\Fonts\arialbd.ttf"),
        (r"C:\Windows\Fonts\calibri.ttf", r"C:\Windows\Fonts\calibrib.ttf"),
        (r"C:\Windows\Fonts\tahoma.ttf",  r"C:\Windows\Fonts\tahomabd.ttf"),
    ]

    for normal_path, bold_path in candidates:
        try:
            if normal_path and os.path.exists(normal_path):
                pdfmetrics.registerFont(TTFont("VGFont", normal_path))
                if bold_path and os.path.exists(bold_path):
                    pdfmetrics.registerFont(TTFont("VGFont-Bold", bold_path))
                else:
                    # si no hay bold, usa el normal
                    pdfmetrics.registerFont(TTFont("VGFont-Bold", normal_path))

                # Mapping para que el paraparser resuelva <b> sin "vgfamily"
                addMapping("VGFont", 0, 0, "VGFont")
                addMapping("VGFont", 1, 0, "VGFont-Bold")
                addMapping("VGFont", 0, 1, "VGFont")
                addMapping("VGFont", 1, 1, "VGFont-Bold")

                _VG_FONT = "VGFont"
                _VG_HAS_TTF = True
                break
        except Exception:
            pass

    return _VG_FONT

def _money_prefix(currency: str) -> str:
    cur = (currency or "CRC").upper()
    if cur == "USD":
        return "$"
    # Si NO hay TTF, no usamos ₡ para evitar el cuadrito negro ■
    return "₡" if _VG_HAS_TTF and _VG_FONT != "Helvetica" else "CRC"

def _fmt_money(n, currency: str) -> str:
    cur = (currency or "CRC").upper()
    return f"{_money_prefix(cur)} {_fmt_num_es(n)}"

# =========================================================
# 3) TABLAS FIN (ya ajustadas a tu BD)
#    fin_receipts: tiene invoice_id (o ref_invoice según tu caso)
#    fin_notes: tiene ref_invoice y ref_reserva (según screenshot)
# =========================================================
RECEIPTS_TABLE = "fin_receipts"
NOTES_TABLE    = "fin_notes"

# ---- Candidatos de columnas (para soportar variantes) ----
INV_COLS   = ["invoice_id", "ref_invoice", "id_factura", "factura_id", "id_invoice"]
RES_COLS   = ["reserva_id", "ref_reserva", "id_reserva", "reservation_id"]
DT_COLS    = ["created_at", "creado_en", "fecha", "fecha_creacion"]
USER_COLS  = ["created_by", "emitido_por", "id_usuario", "user_id", "usuario_id"]

def _cols_of(table: str) -> set[str]:
    try:
        rows = _db().session.execute(
            text("""
                SELECT LOWER(column_name) AS c
                FROM information_schema.columns
                WHERE table_schema = DATABASE()
                  AND table_name = :t
            """),
            {"t": table}
        ).mappings().fetchall()
        return {r["c"] for r in rows if r.get("c")}
    except Exception:
        return set()

def _pick_col(table: str, candidates: list[str]) -> str | None:
    cols = _cols_of(table)
    for c in candidates:
        if c.lower() in cols:
            return c
    return None

def _ensure_fin_tables() -> tuple[bool, bool]:
    """
    Retorna (receipts_ok, notes_ok)
    """
    def _exists(t: str) -> bool:
        r = _db().session.execute(
            text("""
                SELECT 1
                FROM information_schema.tables
                WHERE table_schema = DATABASE()
                  AND table_name = :t
                LIMIT 1
            """),
            {"t": t}
        ).fetchone()
        return bool(r)

    return _exists(RECEIPTS_TABLE), _exists(NOTES_TABLE)

# ==========================
# Selects / Sums (compatibles)
# ==========================
def _select_receipts_for_invoice(invoice_id: int) -> list[dict]:
    receipts_ok, _ = _ensure_fin_tables()
    if not receipts_ok:
        return []

    inv_col = _pick_col(RECEIPTS_TABLE, INV_COLS) or "invoice_id"
    res_col = _pick_col(RECEIPTS_TABLE, RES_COLS)
    dt_col  = _pick_col(RECEIPTS_TABLE, DT_COLS)
    u_col   = _pick_col(RECEIPTS_TABLE, USER_COLS)

    # columnas “normales”
    id_col = _pick_col(RECEIPTS_TABLE, ["id_receipt", "receipt_id", "id"]) or "id_receipt"
    met_col = _pick_col(RECEIPTS_TABLE, ["metodo", "method", "payment_method"])
    cur_col = _pick_col(RECEIPTS_TABLE, ["currency", "moneda", "cur"])
    amt_col = _pick_col(RECEIPTS_TABLE, ["monto", "amount", "total", "monto_total"]) or "monto"

    sel_res = f"{res_col} AS reserva_id" if res_col else "NULL AS reserva_id"
    sel_met = f"{met_col} AS metodo" if met_col else "NULL AS metodo"
    sel_cur = f"{cur_col} AS currency" if cur_col else "NULL AS currency"
    sel_dt  = f"{dt_col} AS created_at" if dt_col else "NULL AS created_at"
    sel_u   = f"{u_col} AS created_by" if u_col else "NULL AS created_by"

    q = f"""
        SELECT
          {id_col} AS id_receipt,
          {inv_col} AS invoice_id,
          {sel_res},
          {sel_met},
          {sel_cur},
          {amt_col} AS monto,
          {sel_dt},
          {sel_u}
        FROM {RECEIPTS_TABLE}
        WHERE {inv_col}=:id
        ORDER BY {id_col} DESC
    """
    rows = _db().session.execute(text(q), {"id": invoice_id}).mappings().fetchall()
    return [dict(r) for r in rows]

def _select_receipt_by_id(receipt_id: int) -> dict | None:
    receipts_ok, _ = _ensure_fin_tables()
    if not receipts_ok:
        return None

    inv_col = _pick_col(RECEIPTS_TABLE, INV_COLS) or "invoice_id"
    res_col = _pick_col(RECEIPTS_TABLE, RES_COLS)
    dt_col  = _pick_col(RECEIPTS_TABLE, DT_COLS)
    u_col   = _pick_col(RECEIPTS_TABLE, USER_COLS)

    id_col = _pick_col(RECEIPTS_TABLE, ["id_receipt", "receipt_id", "id"]) or "id_receipt"
    met_col = _pick_col(RECEIPTS_TABLE, ["metodo", "method", "payment_method"])
    cur_col = _pick_col(RECEIPTS_TABLE, ["currency", "moneda", "cur"])
    amt_col = _pick_col(RECEIPTS_TABLE, ["monto", "amount", "total", "monto_total"]) or "monto"

    sel_res = f"{res_col} AS reserva_id" if res_col else "NULL AS reserva_id"
    sel_met = f"{met_col} AS metodo" if met_col else "NULL AS metodo"
    sel_cur = f"{cur_col} AS currency" if cur_col else "NULL AS currency"
    sel_dt  = f"{dt_col} AS created_at" if dt_col else "NULL AS created_at"
    sel_u   = f"{u_col} AS created_by" if u_col else "NULL AS created_by"

    q = f"""
        SELECT
          {id_col} AS id_receipt,
          {inv_col} AS invoice_id,
          {sel_res},
          {sel_met},
          {sel_cur},
          {amt_col} AS monto,
          {sel_dt},
          {sel_u}
        FROM {RECEIPTS_TABLE}
        WHERE {id_col}=:id
        LIMIT 1
    """
    r = _db().session.execute(text(q), {"id": receipt_id}).mappings().fetchone()
    return dict(r) if r else None

def _sum_receipts(invoice_id: int) -> Decimal:
    receipts_ok, _ = _ensure_fin_tables()
    if not receipts_ok:
        return Decimal("0")

    inv_col = _pick_col(RECEIPTS_TABLE, INV_COLS) or "invoice_id"
    amt_col = _pick_col(RECEIPTS_TABLE, ["monto", "amount", "total", "monto_total"]) or "monto"

    row = _db().session.execute(
        text(f"SELECT COALESCE(SUM({amt_col}),0) AS total FROM {RECEIPTS_TABLE} WHERE {inv_col}=:id"),
        {"id": invoice_id}
    ).mappings().fetchone()

    return _dec(row["total"] if row and row.get("total") is not None else 0)

def _select_notes_for_invoice(invoice_id: int) -> list[dict]:
    _, notes_ok = _ensure_fin_tables()
    if not notes_ok:
        return []

    inv_col = _pick_col(NOTES_TABLE, INV_COLS) or "ref_invoice"
    res_col = _pick_col(NOTES_TABLE, RES_COLS)
    dt_col  = _pick_col(NOTES_TABLE, DT_COLS)
    u_col   = _pick_col(NOTES_TABLE, USER_COLS)

    # en tu tabla: monto_abs (no monto_abs)
    amt_col = _pick_col(NOTES_TABLE, ["monto_abs", "monto", "amount", "total"]) or "monto_abs"

    sel_res = f"{res_col} AS reserva_id" if res_col else "NULL AS reserva_id"
    sel_dt  = f"{dt_col} AS created_at" if dt_col else "NULL AS created_at"
    sel_u   = f"{u_col} AS created_by" if u_col else "NULL AS created_by"

    q = f"""
        SELECT
          id_note,
          {inv_col} AS invoice_id,
          {sel_res},
          tipo,
          currency,
          {amt_col} AS monto_abs,
          motivo,
          {sel_dt},
          {sel_u}
        FROM {NOTES_TABLE}
        WHERE {inv_col}=:id
        ORDER BY id_note DESC
    """
    rows = _db().session.execute(text(q), {"id": invoice_id}).mappings().fetchall()
    return [dict(r) for r in rows]

def _select_note_by_id(note_id: int) -> dict | None:
    _, notes_ok = _ensure_fin_tables()
    if not notes_ok:
        return None

    inv_col = _pick_col(NOTES_TABLE, INV_COLS) or "ref_invoice"
    res_col = _pick_col(NOTES_TABLE, RES_COLS)
    dt_col  = _pick_col(NOTES_TABLE, DT_COLS)
    u_col   = _pick_col(NOTES_TABLE, USER_COLS)
    amt_col = _pick_col(NOTES_TABLE, ["monto_abs", "monto", "amount", "total"]) or "monto_abs"

    sel_res = f"{res_col} AS reserva_id" if res_col else "NULL AS reserva_id"
    sel_dt  = f"{dt_col} AS created_at" if dt_col else "NULL AS created_at"
    sel_u   = f"{u_col} AS created_by" if u_col else "NULL AS created_by"

    q = f"""
        SELECT
          id_note,
          {inv_col} AS invoice_id,
          {sel_res},
          tipo,
          currency,
          {amt_col} AS monto_abs,
          motivo,
          {sel_dt},
          {sel_u}
        FROM {NOTES_TABLE}
        WHERE id_note=:id
        LIMIT 1
    """
    n = _db().session.execute(text(q), {"id": note_id}).mappings().fetchone()
    return dict(n) if n else None

def _sum_notes(invoice_id: int, tipo: str) -> Decimal:
    _, notes_ok = _ensure_fin_tables()
    if not notes_ok:
        return Decimal("0")

    inv_col = _pick_col(NOTES_TABLE, INV_COLS) or "ref_invoice"
    amt_col = _pick_col(NOTES_TABLE, ["monto_abs", "monto", "amount", "total"]) or "monto_abs"

    row = _db().session.execute(
        text(f"""
            SELECT COALESCE(SUM({amt_col}),0) AS total
            FROM {NOTES_TABLE}
            WHERE {inv_col}=:id AND tipo=:t
        """),
        {"id": invoice_id, "t": tipo}
    ).mappings().fetchone()

    return _dec(row["total"] if row and row.get("total") is not None else 0)

# ==========================
# Inserts (usan created_at/created_by)
# ==========================
def _insert_receipt(invoice_id: int, reserva_id: int | None, metodo: str, currency: str, monto: Decimal, user_id: int) -> int | None:
    receipts_ok, _ = _ensure_fin_tables()
    if not receipts_ok:
        raise RuntimeError("No existe tabla fin_receipts.")

    inv_col = _pick_col(RECEIPTS_TABLE, INV_COLS) or "invoice_id"
    res_col = _pick_col(RECEIPTS_TABLE, RES_COLS)
    met_col = _pick_col(RECEIPTS_TABLE, ["metodo", "method", "payment_method"])
    cur_col = _pick_col(RECEIPTS_TABLE, ["currency", "moneda", "cur"])
    amt_col = _pick_col(RECEIPTS_TABLE, ["monto", "amount", "total", "monto_total"]) or "monto"

    # ✅ detectar ambas variantes de fecha/usuario (legacy y nuevas)
    created_at_col = _pick_col(RECEIPTS_TABLE, ["created_at"])
    creado_en_col  = _pick_col(RECEIPTS_TABLE, ["creado_en"])
    created_by_col = _pick_col(RECEIPTS_TABLE, ["created_by"])
    emitido_por_col = _pick_col(RECEIPTS_TABLE, ["emitido_por"])

    # ✅ NUEVO: detectar y llenar "numero" si existe (tu BD la exige)
    num_col = _pick_col(RECEIPTS_TABLE, ["numero", "folio", "receipt_numero", "num"])
    receipt_numero = f"RC-{datetime.now().strftime('%Y%m%d')}-{uuid.uuid4().hex[:8].upper()}"

    cols = [inv_col, amt_col]
    vals = [":invoice_id", ":monto"]
    params = {"invoice_id": int(invoice_id), "monto": float(monto)}

    if num_col:
        cols.insert(0, num_col)
        vals.insert(0, ":numero")
        params["numero"] = receipt_numero

    if res_col:
        cols.append(res_col); vals.append(":reserva_id"); params["reserva_id"] = (int(reserva_id) if reserva_id else None)
    if met_col:
        cols.append(met_col); vals.append(":metodo"); params["metodo"] = metodo
    if cur_col:
        cols.append(cur_col); vals.append(":currency"); params["currency"] = currency

    # ✅ fechas: llenar ambas si existen
    if created_at_col:
        cols.append(created_at_col); vals.append("NOW()")
    if creado_en_col:
        cols.append(creado_en_col); vals.append("NOW()")

    # ✅ usuario: llenar ambas si existen (esto arregla el error emitido_por NOT NULL)
    if created_by_col:
        cols.append(created_by_col); vals.append(":u"); params["u"] = int(user_id)
    if emitido_por_col:
        cols.append(emitido_por_col); vals.append(":u2"); params["u2"] = int(user_id)

    q = f"INSERT INTO {RECEIPTS_TABLE} ({', '.join(cols)}) VALUES ({', '.join(vals)})"
    ins = _db().session.execute(text(q), params)

    rid = getattr(ins, "lastrowid", None)
    if not rid:
        rid_row = _db().session.execute(text("SELECT LAST_INSERT_ID() AS id")).mappings().fetchone()
        rid = int(rid_row["id"]) if rid_row and rid_row.get("id") else None
    return int(rid) if rid else None

def _insert_note(invoice_id: int, reserva_id: int | None, tipo: str, currency: str, monto_abs: Decimal, motivo: str | None, user_id: int) -> int | None:
    _, notes_ok = _ensure_fin_tables()
    if not notes_ok:
        raise RuntimeError("No existe tabla fin_notes.")

    inv_col = _pick_col(NOTES_TABLE, INV_COLS) or "ref_invoice"
    res_col = _pick_col(NOTES_TABLE, RES_COLS)
    amt_col = _pick_col(NOTES_TABLE, ["monto_abs", "monto", "amount", "total"]) or "monto_abs"

    # ✅ detectar ambas variantes de fecha/usuario (legacy y nuevas)
    created_at_col = _pick_col(NOTES_TABLE, ["created_at"])
    creado_en_col  = _pick_col(NOTES_TABLE, ["creado_en"])
    created_by_col = _pick_col(NOTES_TABLE, ["created_by"])
    emitido_por_col = _pick_col(NOTES_TABLE, ["emitido_por"])

    # ✅ NUEVO: detectar y llenar "numero" si existe (tu BD la exige)
    num_col = _pick_col(NOTES_TABLE, ["numero", "folio", "note_numero", "num"])
    note_numero = f"N{('C' if tipo=='Credito' else 'D')}-{datetime.now().strftime('%Y%m%d')}-{uuid.uuid4().hex[:8].upper()}"

    cols = [inv_col, "tipo", "currency", amt_col, "motivo"]
    vals = [":invoice_id", ":tipo", ":currency", ":monto_abs", ":motivo"]
    params = {
        "invoice_id": int(invoice_id),
        "tipo": tipo,
        "currency": currency,
        "monto_abs": float(monto_abs),
        "motivo": motivo
    }

    # ✅ numero primero si existe
    if num_col:
        cols.insert(0, num_col)
        vals.insert(0, ":numero")
        params["numero"] = note_numero

    if res_col:
        cols.append(res_col); vals.append(":reserva_id"); params["reserva_id"] = (int(reserva_id) if reserva_id else None)

    # ✅ fechas: llenar ambas si existen
    if created_at_col:
        cols.append(created_at_col); vals.append("NOW()")
    if creado_en_col:
        cols.append(creado_en_col); vals.append("NOW()")

    # ✅ usuario: llenar ambas si existen
    if created_by_col:
        cols.append(created_by_col); vals.append(":u"); params["u"] = int(user_id)
    if emitido_por_col:
        cols.append(emitido_por_col); vals.append(":u2"); params["u2"] = int(user_id)

    q = f"INSERT INTO {NOTES_TABLE} ({', '.join(cols)}) VALUES ({', '.join(vals)})"
    ins = _db().session.execute(text(q), params)

    nid = getattr(ins, "lastrowid", None)
    if not nid:
        nid_row = _db().session.execute(text("SELECT LAST_INSERT_ID() AS id")).mappings().fetchone()
        nid = int(nid_row["id"]) if nid_row and nid_row.get("id") else None
    return int(nid) if nid else None

# ==========================
# PDF core builder
# ==========================
def _pdf_doc_build(elements, title_for_frame: str, header_title: str, header_sub: str, badge_text: str | None = None) -> bytes:
    if not _HAS_REPORTLAB:
        raise RuntimeError("ReportLab no está instalado")

    _register_vg_font()

    buf = BytesIO()
    doc = SimpleDocTemplate(
        buf,
        pagesize=A4,
        leftMargin=1.55 * cm,
        rightMargin=1.55 * cm,
        topMargin=3.25 * cm,
        bottomMargin=1.55 * cm,
        title=title_for_frame,
    )

    logo_path = os.path.join(current_app.static_folder, "assets", "img", "favicon.png")

    def _split_sub(s: str):
        s = (s or "").strip()
        if " · " in s and len(s) > 70:
            parts = s.split(" · ")
            mid = max(1, len(parts)//2)
            return " · ".join(parts[:mid]), " · ".join(parts[mid:])
        return s, ""

    def _badge_color(txt: str):
        t = (txt or "").strip().lower()
        if t == "pagada":
            return colors.HexColor("#16a34a")
        if t == "anulada":
            return colors.HexColor("#dc2626")
        if t == "emitida":
            return colors.HexColor("#2563eb")
        return colors.HexColor(VG_DARK)

    def _on_page(canvas, _doc):
        w, h = A4
        canvas.saveState()

        canvas.setFillColor(colors.HexColor("#f6f7fb"))
        canvas.rect(0, 0, w, h, fill=1, stroke=0)

        canvas.setFillColor(colors.white)
        canvas.setStrokeColor(colors.HexColor("#0b1220"))
        canvas.setLineWidth(0.9)
        canvas.roundRect(1.2*cm, 1.2*cm, w-2.4*cm, h-2.4*cm, 10, fill=1, stroke=1)

        canvas.setFillColor(colors.HexColor(VG_GREEN))
        canvas.roundRect(1.2*cm, h-2.95*cm, w-2.4*cm, 1.35*cm, 10, fill=1, stroke=0)
        canvas.setFillColor(colors.HexColor(VG_GREEN))
        canvas.rect(1.2*cm, h-2.95*cm, w-2.4*cm, 1.0*cm, fill=1, stroke=0)

        if os.path.exists(logo_path):
            try:
                canvas.drawImage(logo_path, 1.55*cm, h-2.62*cm, width=0.85*cm, height=0.85*cm, mask='auto')
            except Exception:
                pass

        canvas.setFillColor(colors.white)
        canvas.setFont(_VG_FONT, 12)
        canvas.drawString(2.55*cm, h-2.36*cm, "Hotel Villa Grace")

        canvas.setFont(_VG_FONT, 10)
        canvas.drawRightString(w-1.55*cm, h-2.38*cm, header_title)

        if badge_text:
            btxt = str(badge_text)
            bw = max(2.2*cm, min(4.2*cm, (len(btxt)*0.18)*cm + 1.2*cm))
            bx = w - 1.55*cm - bw
            by = h - 3.55*cm
            canvas.setFillColor(_badge_color(btxt))
            canvas.roundRect(bx, by, bw, 0.62*cm, 7, fill=1, stroke=0)
            canvas.setFillColor(colors.white)
            canvas.setFont(_VG_FONT, 8.5)
            canvas.drawCentredString(bx + bw/2, by + 0.20*cm, btxt.upper())

        line1, line2 = _split_sub(header_sub)
        canvas.setFillColor(colors.HexColor(VG_TEXT))
        canvas.setFont(_VG_FONT, 9)
        canvas.drawString(1.55*cm, h-3.45*cm, line1)
        if line2:
            canvas.setFont(_VG_FONT, 8.6)
            canvas.setFillColor(colors.HexColor(VG_MUTED))
            canvas.drawString(1.55*cm, h-3.85*cm, line2)

        canvas.setFillColor(colors.HexColor(VG_MUTED))
        canvas.setFont(_VG_FONT, 8)
        canvas.drawString(1.55*cm, 0.9*cm, "Hotel Villa Grace · Documento interno")
        canvas.drawRightString(w-1.55*cm, 0.9*cm, f"Página {_doc.page}")

        canvas.restoreState()

    doc.build(elements, onFirstPage=_on_page, onLaterPages=_on_page)
    pdf = buf.getvalue()
    buf.close()
    return pdf

# ==========================
# PDF blocks
# ==========================
def _styles():
    _register_vg_font()
    ss = getSampleStyleSheet()

    sec = ParagraphStyle(
        "vg_sec",
        parent=ss["Heading3"],
        fontName=_VG_FONT,
        fontSize=11,
        leading=13,
        textColor=colors.HexColor(VG_DARK),
        spaceBefore=6,
        spaceAfter=6,
    )
    normal = ParagraphStyle(
        "vg_norm",
        parent=ss["Normal"],
        fontName=_VG_FONT,
        fontSize=10,
        leading=12,
        textColor=colors.HexColor(VG_TEXT),
    )
    small = ParagraphStyle(
        "vg_small",
        parent=ss["Normal"],
        fontName=_VG_FONT,
        fontSize=9,
        leading=11,
        textColor=colors.HexColor(VG_TEXT),
    )
    return sec, normal, small

def _section_title(txt: str):
    sec, _, _ = _styles()
    return Paragraph(txt, sec)

def _kv_table_2x2(rows_2x2):
    _, normal, _ = _styles()
    data = []
    for r in rows_2x2:
        k1, v1, k2, v2 = (r + ["", "", "", ""])[:4]
        data.append([
            Paragraph(f"<b>{k1}</b>", normal),
            Paragraph(str(v1 or "-"), normal),
            Paragraph(f"<b>{k2}</b>" if k2 else "", normal),
            Paragraph(str(v2 or "-") if k2 else "", normal),
        ])

    t = Table(data, colWidths=[3.2 * cm, 5.9 * cm, 3.0 * cm, 4.9 * cm], hAlign="LEFT")
    t.setStyle(TableStyle([
        ("BOX", (0, 0), (-1, -1), 0.8, colors.HexColor(VG_DARK)),
        ("INNERGRID", (0, 0), (-1, -1), 0.4, colors.HexColor("#cbd5e1")),
        ("BACKGROUND", (0, 0), (0, -1), colors.HexColor(VG_BG)),
        ("BACKGROUND", (2, 0), (2, -1), colors.HexColor(VG_BG)),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("LEFTPADDING", (0, 0), (-1, -1), 8),
        ("RIGHTPADDING", (0, 0), (-1, -1), 8),
        ("TOPPADDING", (0, 0), (-1, -1), 6),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
    ]))
    return t

def _simple_table(headers, rows, col_widths=None, right_align_cols=None, stripe=True):
    _, _, small = _styles()
    right_align_cols = set(right_align_cols or [])

    data = [[Paragraph(f"<b>{h}</b>", small) for h in headers]]
    for r in rows:
        data.append([Paragraph(str(x) if x is not None else "-", small) for x in r])

    t = Table(data, colWidths=col_widths, hAlign="LEFT")

    style = [
        ("BOX", (0, 0), (-1, -1), 0.8, colors.HexColor(VG_DARK)),
        ("INNERGRID", (0, 0), (-1, -1), 0.4, colors.HexColor(VG_LINE)),
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor(VG_GREEN)),
        ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("LEFTPADDING", (0, 0), (-1, -1), 6),
        ("RIGHTPADDING", (0, 0), (-1, -1), 6),
        ("TOPPADDING", (0, 0), (-1, -1), 5),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
    ]

    for c in right_align_cols:
        style.append(("ALIGN", (c, 1), (c, -1), "RIGHT"))
        style.append(("ALIGN", (c, 0), (c, 0), "RIGHT"))

    if stripe and len(rows) > 1:
        for i in range(1, len(rows) + 1):
            if i % 2 == 0:
                style.append(("BACKGROUND", (0, i), (-1, i), colors.HexColor("#fbfdff")))

    t.setStyle(TableStyle(style))
    return t

# ==========================
# PDF generators
# ==========================
def _pdf_invoice_planilla(inv: dict, receipts: list[dict], notes: list[dict]) -> bytes:
    numero = inv.get("numero") or "-"
    fecha = inv.get("fecha_emision")
    fecha_txt = fecha.strftime("%Y-%m-%d %H:%M") if hasattr(fecha, "strftime") else (str(fecha) if fecha else "-")
    estado = inv.get("estado") or "-"
    moneda = (inv.get("moneda") or "CRC").upper()

    total = _dec(inv.get("monto_total"))
    paid = sum(_dec(x.get("monto")) for x in receipts)
    nc = sum(_dec(x.get("monto_abs")) for x in notes if (x.get("tipo") == "Credito"))
    nd = sum(_dec(x.get("monto_abs")) for x in notes if (x.get("tipo") == "Debito"))

    total_aj = total + nd - nc
    saldo = total_aj - paid

    elements = []

    elements.append(_section_title("Datos de la factura"))
    elements.append(_kv_table_2x2([
        ["Folio", numero, "Fecha emisión", fecha_txt],
        ["Cliente", inv.get("cliente_nombre") or "-", "Email", inv.get("cliente_email") or "-"],
        ["Moneda", moneda, "Reserva", str(inv.get("id_reserva") or "-")],
        ["Usuario", str(inv.get("id_usuario") or "-"), "ID factura", str(inv.get("id_factura") or "-")],
    ]))
    elements.append(Spacer(1, 10))

    desc = (inv.get("descripcion") or "").strip()
    if desc:
        elements.append(_section_title("Descripción"))
        elements.append(_simple_table(
            headers=["Detalle"],
            rows=[[desc]],
            col_widths=[17.0 * cm],
            stripe=False
        ))
        elements.append(Spacer(1, 10))

    elements.append(_section_title("Resumen"))
    resumen_rows = [
        ["Total original", _fmt_money(total, moneda)],
        ["Notas débito (ND)", _fmt_money(nd, moneda)],
        ["Notas crédito (NC)", _fmt_money(nc, moneda)],
        ["Total ajustado", _fmt_money(total_aj, moneda)],
        ["Pagado", _fmt_money(paid, moneda)],
        ["Saldo", _fmt_money(saldo, moneda)],
    ]
    resumen_tbl = _simple_table(
        headers=["Concepto", "Monto"],
        rows=resumen_rows,
        col_widths=[11.0 * cm, 6.0 * cm],
        right_align_cols=[1],
        stripe=True
    )
    resumen_tbl.setStyle(TableStyle([
        ("BACKGROUND", (0, 6), (-1, 6), colors.HexColor(VG_DARK)),
        ("TEXTCOLOR", (0, 6), (-1, 6), colors.white),
    ]))
    elements.append(resumen_tbl)
    elements.append(Spacer(1, 12))

    elements.append(_section_title("Recibos de pago"))
    if receipts:
        rrows = []
        for r in receipts:
            dt = r.get("created_at")
            dt_txt = dt.strftime("%Y-%m-%d %H:%M") if hasattr(dt, "strftime") else (str(dt) if dt else "-")
            cur = (r.get("currency") or moneda)
            rrows.append([
                dt_txt,
                r.get("metodo") or "-",
                cur,
                _fmt_money(r.get("monto") or 0, cur),
            ])
        elements.append(_simple_table(
            headers=["Fecha", "Método", "Moneda", "Monto"],
            rows=rrows,
            col_widths=[4.3 * cm, 5.0 * cm, 2.2 * cm, 5.5 * cm],
            right_align_cols=[3]
        ))
    else:
        elements.append(_simple_table(
            headers=["Detalle"],
            rows=[["Sin recibos registrados."]],
            col_widths=[17.0 * cm],
            stripe=False
        ))
    elements.append(Spacer(1, 10))

    elements.append(_section_title("Notas (NC / ND)"))
    if notes:
        nrows = []
        for n in notes:
            dt = n.get("created_at")
            dt_txt = dt.strftime("%Y-%m-%d %H:%M") if hasattr(dt, "strftime") else (str(dt) if dt else "-")
            tipo = n.get("tipo") or "-"
            cur = (n.get("currency") or moneda)
            nrows.append([
                dt_txt,
                "NC" if tipo == "Credito" else ("ND" if tipo == "Debito" else tipo),
                cur,
                _fmt_money(n.get("monto_abs") or 0, cur),
                (n.get("motivo") or "-"),
            ])
        elements.append(_simple_table(
            headers=["Fecha", "Tipo", "Moneda", "Monto", "Motivo"],
            rows=nrows,
            col_widths=[3.6 * cm, 1.6 * cm, 1.8 * cm, 3.0 * cm, 7.0 * cm],
            right_align_cols=[3]
        ))
    else:
        elements.append(_simple_table(
            headers=["Detalle"],
            rows=[["Sin notas registradas."]],
            col_widths=[17.0 * cm],
            stripe=False
        ))

    header_title = "FACTURA"
    header_sub = f"Folio: {numero} · Fecha: {fecha_txt} · Estado: {estado}"
    return _pdf_doc_build(elements, title_for_frame=f"Factura {numero}", header_title=header_title, header_sub=header_sub, badge_text=estado)

def _pdf_receipt_planilla(inv: dict, receipt: dict) -> bytes:
    numero = inv.get("numero") or "-"
    moneda = (receipt.get("currency") or inv.get("moneda") or "CRC").upper()
    dt = receipt.get("created_at")
    dt_txt = dt.strftime("%Y-%m-%d %H:%M") if hasattr(dt, "strftime") else (str(dt) if dt else "-")

    elements = []
    elements.append(_section_title("Datos del recibo"))
    elements.append(_kv_table_2x2([
        ["Factura", numero, "ID recibo", str(receipt.get("id_receipt") or "-")],
        ["Cliente", inv.get("cliente_nombre") or "-", "Moneda", moneda],
        ["Método", receipt.get("metodo") or "-", "Monto", _fmt_money(receipt.get("monto") or 0, moneda)],
        ["Reserva", str(receipt.get("reserva_id") or inv.get("id_reserva") or "-"), "Usuario", str(receipt.get("created_by") or inv.get("id_usuario") or "-")],
    ]))
    elements.append(Spacer(1, 12))

    total = _dec(inv.get("monto_total"))
    paid = _dec(receipt.get("paid_total"))
    nc = _dec(receipt.get("nc_total"))
    nd = _dec(receipt.get("nd_total"))
    total_aj = total + nd - nc
    saldo = total_aj - paid

    elements.append(_section_title("Resumen"))
    elements.append(_simple_table(
        headers=["Concepto", "Monto"],
        rows=[
            ["Total ajustado", _fmt_money(total_aj, (inv.get("moneda") or moneda))],
            ["Pagado acumulado", _fmt_money(paid, (inv.get("moneda") or moneda))],
            ["Saldo", _fmt_money(saldo, (inv.get("moneda") or moneda))],
        ],
        col_widths=[11.0 * cm, 6.0 * cm],
        right_align_cols=[1],
        stripe=True
    ))

    header_title = "RECIBO DE PAGO"
    header_sub = f"Factura: {numero} · Fecha: {dt_txt}"
    return _pdf_doc_build(elements, title_for_frame=f"Recibo {receipt.get('id_receipt') or ''} - {numero}", header_title=header_title, header_sub=header_sub)

def _pdf_note_planilla(inv: dict, note: dict) -> bytes:
    numero = inv.get("numero") or "-"
    tipo = (note.get("tipo") or "-")
    tipo_txt = "NOTA DE CRÉDITO" if tipo == "Credito" else ("NOTA DE DÉBITO" if tipo == "Debito" else f"NOTA ({tipo})")
    moneda = (note.get("currency") or inv.get("moneda") or "CRC").upper()
    dt = note.get("created_at")
    dt_txt = dt.strftime("%Y-%m-%d %H:%M") if hasattr(dt, "strftime") else (str(dt) if dt else "-")
    motivo = (note.get("motivo") or "-")

    elements = []
    elements.append(_section_title("Datos de la nota"))
    elements.append(_kv_table_2x2([
        ["Factura", numero, "ID nota", str(note.get("id_note") or "-")],
        ["Cliente", inv.get("cliente_nombre") or "-", "Moneda", moneda],
        ["Tipo", "NC" if tipo == "Credito" else ("ND" if tipo == "Debito" else tipo), "Monto", _fmt_money(note.get("monto_abs") or 0, moneda)],
        ["Reserva", str(note.get("reserva_id") or inv.get("id_reserva") or "-"), "Usuario", str(note.get("created_by") or inv.get("id_usuario") or "-")],
    ]))
    elements.append(Spacer(1, 10))

    elements.append(_section_title("Motivo"))
    elements.append(_simple_table(headers=["Detalle"], rows=[[motivo]], col_widths=[17.0 * cm], stripe=False))
    elements.append(Spacer(1, 10))

    total = _dec(inv.get("monto_total"))
    paid = _dec(note.get("paid_total"))
    nc = _dec(note.get("nc_total"))
    nd = _dec(note.get("nd_total"))
    total_aj = total + nd - nc
    saldo = total_aj - paid

    elements.append(_section_title("Resumen"))
    elements.append(_simple_table(
        headers=["Concepto", "Monto"],
        rows=[
            ["Total original", _fmt_money(inv.get("monto_total") or 0, (inv.get("moneda") or moneda))],
            ["Total ajustado", _fmt_money(total_aj, (inv.get("moneda") or moneda))],
            ["Pagado acumulado", _fmt_money(paid, (inv.get("moneda") or moneda))],
            ["Saldo", _fmt_money(saldo, (inv.get("moneda") or moneda))],
        ],
        col_widths=[11.0 * cm, 6.0 * cm],
        right_align_cols=[1],
        stripe=True
    ))

    header_title = tipo_txt
    header_sub = f"Factura: {numero} · Fecha: {dt_txt}"
    return _pdf_doc_build(elements, title_for_frame=f"Nota {note.get('id_note') or ''} - {numero}", header_title=header_title, header_sub=header_sub)

# =========================================================
# VISTA: FACTURAS (GET)
# =========================================================
@fin_invoices_bp.get("/fin-invoices.html", endpoint="fin_invoices_html")
def fin_invoices_html():
    facturas = _db().session.execute(
        text("""
            SELECT
              id_factura, numero, cliente_nombre, cliente_email,
              moneda, monto_total, descripcion, archivo_path,
              id_reserva, id_usuario, fecha_emision, estado
            FROM fin_invoices
            ORDER BY id_factura DESC
            LIMIT 300
        """)
    ).mappings().fetchall()

    return render_template("fin-invoices.html", facturas=facturas, url_any=_url)

# =========================================================
# PDF: FACTURA (GET)
# =========================================================
@fin_invoices_bp.get("/fin/invoices/<int:id>/pdf", endpoint="fin_invoice_pdf")
def fin_invoice_pdf(id: int):
    if not _HAS_REPORTLAB:
        flash("No se puede generar PDF: ReportLab no está instalado.", "danger")
        return redirect(_url("fin_invoices_html"))

    inv = _db().session.execute(
        text("""
            SELECT
              id_factura, numero, cliente_nombre, cliente_email,
              moneda, monto_total, descripcion, archivo_path,
              id_reserva, id_usuario, fecha_emision, estado
            FROM fin_invoices
            WHERE id_factura=:id
            LIMIT 1
        """),
        {"id": id}
    ).mappings().fetchone()

    if not inv:
        abort(404)

    receipts = _select_receipts_for_invoice(id)
    notes = _select_notes_for_invoice(id)

    pdf_bytes = _pdf_invoice_planilla(dict(inv), receipts, notes)
    filename = f"Factura_{(inv.get('numero') or id)}.pdf"

    return send_file(BytesIO(pdf_bytes), mimetype="application/pdf", as_attachment=False, download_name=filename)

# =========================================================
# PDF: RECIBO (GET)
# =========================================================
@fin_invoices_bp.get("/fin/receipts/<int:id>/pdf", endpoint="fin_receipt_pdf")
def fin_receipt_pdf(id: int):
    if not _HAS_REPORTLAB:
        abort(500)

    rec = _select_receipt_by_id(id)
    if not rec:
        abort(404)

    inv = _db().session.execute(
        text("""
            SELECT
              id_factura, numero, cliente_nombre, cliente_email,
              moneda, monto_total, descripcion,
              id_reserva, id_usuario, fecha_emision, estado
            FROM fin_invoices
            WHERE id_factura=:id
            LIMIT 1
        """),
        {"id": rec["invoice_id"]}
    ).mappings().fetchone()

    if not inv:
        abort(404)

    invoice_id = int(rec["invoice_id"])
    rec["paid_total"] = _sum_receipts(invoice_id)
    rec["nc_total"] = _sum_notes(invoice_id, "Credito")
    rec["nd_total"] = _sum_notes(invoice_id, "Debito")

    pdf_bytes = _pdf_receipt_planilla(dict(inv), rec)
    filename = f"Recibo_{id}_Factura_{(inv.get('numero') or invoice_id)}.pdf"
    return send_file(BytesIO(pdf_bytes), mimetype="application/pdf", as_attachment=False, download_name=filename)

# =========================================================
# PDF: NOTA (NC/ND) (GET)
# =========================================================
@fin_invoices_bp.get("/fin/notes/<int:id>/pdf", endpoint="fin_note_pdf")
def fin_note_pdf(id: int):
    if not _HAS_REPORTLAB:
        abort(500)

    _, notes_ok = _ensure_fin_tables()
    if not notes_ok:
        abort(404)

    note = _select_note_by_id(id)
    if not note:
        abort(404)

    inv = _db().session.execute(
        text("""
            SELECT
              id_factura, numero, cliente_nombre, cliente_email,
              moneda, monto_total, descripcion,
              id_reserva, id_usuario, fecha_emision, estado
            FROM fin_invoices
            WHERE id_factura=:id
            LIMIT 1
        """),
        {"id": note["invoice_id"]}
    ).mappings().fetchone()

    if not inv:
        abort(404)

    invoice_id = int(note["invoice_id"])
    note["paid_total"] = _sum_receipts(invoice_id)
    note["nc_total"] = _sum_notes(invoice_id, "Credito")
    note["nd_total"] = _sum_notes(invoice_id, "Debito")

    pdf_bytes = _pdf_note_planilla(dict(inv), note)
    tipo_slug = "NC" if note.get("tipo") == "Credito" else ("ND" if note.get("tipo") == "Debito" else "NOTA")
    filename = f"{tipo_slug}_{id}_Factura_{(inv.get('numero') or invoice_id)}.pdf"
    return send_file(BytesIO(pdf_bytes), mimetype="application/pdf", as_attachment=False, download_name=filename)

# =========================================================
# CREAR FACTURA (POST formulario)
# =========================================================
@fin_invoices_bp.post("/fin/invoices/nuevo", endpoint="fin_invoice_nuevo")
def fin_invoice_nuevo():
    numero = (request.form.get("numero") or "").strip()
    cliente_nombre = (request.form.get("cliente_nombre") or "").strip() or None
    cliente_email = (request.form.get("cliente_email") or "").strip() or None
    moneda = (request.form.get("moneda") or "CRC").strip() or "CRC"
    descripcion = (request.form.get("descripcion") or "").strip() or None

    id_reserva_raw = (request.form.get("id_reserva") or "").strip()
    id_reserva = int(id_reserva_raw) if id_reserva_raw.isdigit() else None

    monto_total = _to_decimal(request.form.get("monto_total"), default=None)

    if not numero:
        flash("El número/folio es obligatorio.", "warning")
        return redirect(_url("fin_invoices_html"))

    if monto_total is None or monto_total <= 0:
        flash("El monto total debe ser mayor a 0.", "warning")
        return redirect(_url("fin_invoices_html"))

    archivo = request.files.get("archivo")
    archivo_path = _save_upload(archivo, "invoices", preferred_name=numero)

    try:
        _db().session.execute(
            text("""
                INSERT INTO fin_invoices
                  (numero, cliente_nombre, cliente_email, moneda,
                   monto_total, descripcion, archivo_path,
                   id_reserva, id_usuario, fecha_emision, estado)
                VALUES
                  (:numero, :cliente_nombre, :cliente_email, :moneda,
                   :monto_total, :descripcion, :archivo_path,
                   :id_reserva, :id_usuario, NOW(), 'Emitida')
            """),
            {
                "numero": numero,
                "cliente_nombre": cliente_nombre,
                "cliente_email": cliente_email,
                "moneda": moneda,
                "monto_total": float(monto_total),
                "descripcion": descripcion,
                "archivo_path": archivo_path,
                "id_reserva": id_reserva,
                "id_usuario": _user_id_default(),
            }
        )
        _db().session.commit()
        flash("Factura emitida correctamente.", "success")
    except Exception as e:
        _db().session.rollback()
        flash(f"No se pudo emitir la factura: {e}", "danger")

    return redirect(_url("fin_invoices_html"))

# =========================================================
# PAGAR / ANULAR (POST botones)
# =========================================================
@fin_invoices_bp.post("/fin/invoices/<int:id>/pagar", endpoint="fin_invoice_pagar")
def fin_invoice_pagar(id: int):
    try:
        _db().session.execute(
            text("UPDATE fin_invoices SET estado='Pagada' WHERE id_factura=:id"),
            {"id": id}
        )
        _db().session.commit()
        flash("Factura marcada como pagada.", "success")
    except Exception as e:
        _db().session.rollback()
        flash(f"No se pudo marcar como pagada: {e}", "danger")
    return redirect(_url("fin_invoices_html"))

@fin_invoices_bp.post("/fin/invoices/<int:id>/anular", endpoint="fin_invoice_anular")
def fin_invoice_anular(id: int):
    try:
        _db().session.execute(
            text("UPDATE fin_invoices SET estado='Anulada' WHERE id_factura=:id"),
            {"id": id}
        )
        _db().session.commit()
        flash("Factura anulada.", "warning")
    except Exception as e:
        _db().session.rollback()
        flash(f"No se pudo anular: {e}", "danger")
    return redirect(_url("fin_invoices_html"))

# =========================================================
# API: RECIBOS (JS fetch('/api/fin/receipts'))
# =========================================================
@fin_invoices_bp.post("/api/fin/receipts")
def api_fin_receipts():
    payload = request.get_json(silent=True) or {}
    invoice_id = payload.get("invoice_id")
    reserva_id = payload.get("reserva_id")
    metodo = (payload.get("metodo") or "").strip() or "Tarjeta"
    currency = (payload.get("currency") or "CRC").strip() or "CRC"
    monto = _to_decimal(payload.get("monto"), default=Decimal("0"))

    if not invoice_id:
        return jsonify(ok=False, error="invoice_id requerido"), 400
    if monto <= 0:
        return jsonify(ok=False, error="monto debe ser > 0"), 400

    inv = _db().session.execute(
        text("""
            SELECT id_factura, numero, moneda, monto_total, estado, id_reserva
            FROM fin_invoices
            WHERE id_factura=:id
            LIMIT 1
        """),
        {"id": invoice_id}
    ).mappings().fetchone()

    if not inv:
        return jsonify(ok=False, error="Factura no encontrada"), 404

    if reserva_id is None:
        reserva_id = inv.get("id_reserva")

    try:
        receipt_id = _insert_receipt(
            invoice_id=int(invoice_id),
            reserva_id=(int(reserva_id) if reserva_id else None),
            metodo=metodo,
            currency=currency,
            monto=monto,
            user_id=_user_id_default()
        )

        paid = _sum_receipts(int(invoice_id))
        nc = _sum_notes(int(invoice_id), "Credito")
        nd = _sum_notes(int(invoice_id), "Debito")

        total = _dec(inv["monto_total"])
        total_aj = total + nd - nc

        if paid >= total_aj and (inv.get("estado") or "") != "Anulada":
            _db().session.execute(
                text("UPDATE fin_invoices SET estado='Pagada' WHERE id_factura=:id"),
                {"id": invoice_id}
            )

        _db().session.commit()

        pdf_url = f"/fin/receipts/{receipt_id}/pdf" if receipt_id else None
        return jsonify(ok=True, pdf=pdf_url)

    except Exception as e:
        _db().session.rollback()
        return jsonify(ok=False, error=str(e)), 500

# =========================================================
# API: NOTAS (JS fetch('/api/fin/notes'))
# =========================================================
@fin_invoices_bp.post("/api/fin/notes")
def api_fin_notes():
    payload = request.get_json(silent=True) or {}
    tipo = (payload.get("tipo") or "").strip()  # Credito | Debito
    invoice_id = payload.get("invoice_id")
    reserva_id = payload.get("reserva_id")
    currency = (payload.get("currency") or "CRC").strip() or "CRC"
    monto_abs = _to_decimal(payload.get("monto_abs"), default=Decimal("0"))
    motivo = (payload.get("motivo") or "").strip() or None

    if tipo not in ("Credito", "Debito"):
        return jsonify(ok=False, error="tipo debe ser Credito o Debito"), 400
    if not invoice_id:
        return jsonify(ok=False, error="invoice_id requerido"), 400
    if monto_abs <= 0:
        return jsonify(ok=False, error="monto_abs debe ser > 0"), 400

    inv = _db().session.execute(
        text("""
            SELECT id_factura, numero, moneda, monto_total, estado, id_reserva
            FROM fin_invoices
            WHERE id_factura=:id
            LIMIT 1
        """),
        {"id": invoice_id}
    ).mappings().fetchone()

    if not inv:
        return jsonify(ok=False, error="Factura no encontrada"), 404

    if reserva_id is None:
        reserva_id = inv.get("id_reserva")

    try:
        note_id = _insert_note(
            invoice_id=int(invoice_id),
            reserva_id=(int(reserva_id) if reserva_id else None),
            tipo=tipo,
            currency=currency,
            monto_abs=monto_abs,
            motivo=motivo,
            user_id=_user_id_default()
        )

        _db().session.commit()

        pdf_url = f"/fin/notes/{note_id}/pdf" if note_id else None
        return jsonify(ok=True, pdf=pdf_url)

    except Exception as e:
        _db().session.rollback()
        return jsonify(ok=False, error=str(e)), 500




