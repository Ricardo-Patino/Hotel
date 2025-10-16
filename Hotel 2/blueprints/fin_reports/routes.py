# blueprints/fin_reports/routes.py
from __future__ import annotations

from flask import request, jsonify, send_file, render_template
from . import fin_reports_bp
from sqlalchemy import text
from datetime import datetime, date
from pathlib import Path
from io import BytesIO
import csv
import re

from reportlab.lib.pagesizes import LETTER
from reportlab.pdfgen import canvas
from reportlab.lib.units import cm
from reportlab.lib import colors

from extensions import db

# ---------- Paths base ----------
BASE_DIR = Path(__file__).resolve().parents[2]
STORAGE_DIR = BASE_DIR / "storage"
REPORTS_BASE = STORAGE_DIR / "fin_reports"
REPORTS_BASE.mkdir(parents=True, exist_ok=True)

def _yyyymm_dirs(d: date):
    y = f"{d:%Y}"
    m = f"{d:%m}"
    out = REPORTS_BASE / y / m
    out.mkdir(parents=True, exist_ok=True)
    return out

# ---------- Rangos de fecha ----------
def _parse_date(s: str | None, default: date) -> date:
    try:
        return datetime.strptime((s or "").strip(), "%Y-%m-%d").date()
    except Exception:
        return default

def _range_from_args():
    today = date.today()
    d_from = _parse_date(request.args.get("from"), today.replace(day=1))
    d_to   = _parse_date(request.args.get("to"),   today)
    if d_to < d_from:
        d_from, d_to = d_to, d_from
    return d_from, d_to

# ---------- Clasificación simple de cuentas ----------
ACCOUNT_TYPES = {
    "VENTA": "income",
    "ING":   "income",
    "IVA":   "liability",
    "TPV":   "asset",
    "CAJA":  "asset",
    "BANK":  "asset",
    "GASTO": "expense",
    "COSTO": "expense",
}

def _guess_type(account: str) -> str:
    up = (account or "").upper()
    for pref, t in ACCOUNT_TYPES.items():
        if up.startswith(pref):
            return t
    return "asset"

# ---------- SQL ----------
SQL_JOURNAL = text("""
    SELECT
      t.created_at AS fecha,
      t.external_id,
      t.currency,
      l.line_no,
      l.account,
      l.debit,
      l.credit,
      l.description
    FROM fin_ledger_tx t
    JOIN fin_ledger_line l ON l.id_tx = t.id_tx
    WHERE t.created_at >= :dfrom AND t.created_at < DATE_ADD(:dto, INTERVAL 1 DAY)
      AND t.status = 'posted'
    ORDER BY t.created_at ASC, t.id_tx ASC, l.line_no ASC
""")

SQL_LEDGER = text("""
    SELECT
      l.account,
      SUM(l.debit)  AS total_debit,
      SUM(l.credit) AS total_credit
    FROM fin_ledger_tx t
    JOIN fin_ledger_line l ON l.id_tx = t.id_tx
    WHERE t.created_at >= :dfrom AND t.created_at < DATE_ADD(:dto, INTERVAL 1 DAY)
      AND t.status = 'posted'
    GROUP BY l.account
    ORDER BY l.account
""")

# ---------- CSV ----------
def _write_csv(rows, headers, out_path: Path):
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(headers)
        for r in rows:
            w.writerow(r)

# ==========================================================
# =============== PDF BONITO (estilizado) ==================
# ==========================================================
_BRAND_NAME = "Hotel Villa Grace"

def _asset_logo_path() -> str | None:
    for rel in [
        "static/assets/img/favicon.png",
        "static/assets/img/apple-touch-icon.png",
        "static/assets/img/logo.png",
    ]:
        p = BASE_DIR / rel
        if p.exists():
            return str(p)
    return None

def _draw_header(c: canvas.Canvas, title: str, subtitle: str = ""):
    W, H = LETTER
    logo = _asset_logo_path()

    # Franja
    c.setFillColorRGB(0.09, 0.33, 0.25)  # verde oscuro
    c.rect(0, H-60, W, 60, stroke=0, fill=1)

    # Logo + Marca
    x = 36
    if logo:
        try:
            c.drawImage(logo, x, H-54, width=24, height=24, preserveAspectRatio=True, mask='auto')
            x += 30
        except Exception:
            pass
    c.setFillColor(colors.white)
    c.setFont("Helvetica-Bold", 14)
    c.drawString(x, H-40, _BRAND_NAME)

    # Título
    c.setFont("Helvetica-Bold", 12)
    c.drawRightString(W-36, H-40, title)

    # Subtítulo (rango fechas u otro)
    if subtitle:
        c.setFont("Helvetica", 9)
        c.drawRightString(W-36, H-54, subtitle)

def _draw_footer(c: canvas.Canvas, pageno: int):
    W, _ = LETTER
    c.setFont("Helvetica-Oblique", 9)
    c.setFillColorRGB(0.35,0.35,0.35)
    c.drawString(36, 36, "Documento generado digitalmente")
    c.drawRightString(W-36, 36, f"Página {pageno}")

_num_re = re.compile(r"^-?\d+(?:[.,]\d+)?$")

def _is_number_like(s: str) -> bool:
    return bool(_num_re.match(str(s).strip()))

def _format_money_like(s: str) -> str:
    # Si ya viene con dos decimales, respétalo; si es número, aplica miles
    try:
        v = float(str(s).replace(",", "").strip())
        return f"{v:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")
    except Exception:
        return str(s)

def _pdf_table_beauty(title: str, subtitle: str, headers, rows, out_path: Path):
    """
    Tabla estilo: header sombreado, zebra, números a la derecha, cabecera/pie.
    """
    buf = BytesIO()
    c = canvas.Canvas(buf, pagesize=LETTER)
    W, H = LETTER
    left = 36
    right = W - 36
    top = H - 90   # debajo de la franja
    bottom = 60

    # Detección de columnas numéricas
    numeric_cols = set()
    if rows:
        for idx in range(len(headers)):
            # Si >70% de filas parecen número -> numérica
            vals = [str(r[idx]) for r in rows if len(r) > idx]
            if not vals:
                continue
            num_like = sum(1 for v in vals if _is_number_like(v))
            if num_like >= 0.7 * len(vals):
                numeric_cols.add(idx)

    # Preparar cabecera y anchuras
    colw = (right - left) / max(1, len(headers))
    line_h = 16

    page = 1
    def new_page():
        nonlocal page
        c.showPage()
        page += 1

    # Primera página
    _draw_header(c, title, subtitle)
    y = top

    # Encabezado de tabla
    c.setFillColorRGB(0.96,0.98,0.97)
    c.roundRect(left, y-18, (right-left), 20, 6, stroke=0, fill=1)
    c.setFillColorRGB(0.09,0.33,0.25)
    c.setFont("Helvetica-Bold", 9)
    for i, h in enumerate(headers):
        x = left + i*colw + 4
        if i in numeric_cols:
            c.drawRightString(left + (i+1)*colw - 4, y-5, str(h))
        else:
            c.drawString(x, y-5, str(h))
    y -= 22

    # Filas
    c.setFont("Helvetica", 9)
    zebra = False
    for row in rows:
        if y < bottom + line_h:
            _draw_footer(c, page)
            new_page()
            _draw_header(c, title, subtitle)
            y = top
            # Redibujar header de tabla
            c.setFillColorRGB(0.96,0.98,0.97)
            c.roundRect(left, y-18, (right-left), 20, 6, stroke=0, fill=1)
            c.setFillColorRGB(0.09,0.33,0.25)
            c.setFont("Helvetica-Bold", 9)
            for i, h in enumerate(headers):
                x = left + i*colw + 4
                if i in numeric_cols:
                    c.drawRightString(left + (i+1)*colw - 4, y-5, str(h))
                else:
                    c.drawString(x, y-5, str(h))
            y -= 22
            c.setFont("Helvetica", 9)

        # Zebra
        if zebra:
            c.setFillColorRGB(0.985,0.985,0.985)
            c.rect(left, y-13, (right-left), 14, stroke=0, fill=1)
        zebra = not zebra

        # Texto
        c.setFillColor(colors.black)
        for i, v in enumerate(row):
            text = str(v)
            x = left + i*colw + 4
            if i in numeric_cols:
                # derecha y con miles/decimales si aplica
                text = _format_money_like(text)
                c.drawRightString(left + (i+1)*colw - 4, y-3, text)
            else:
                c.drawString(x, y-3, text)
        y -= line_h

    _draw_footer(c, page)
    c.showPage()
    c.save()

    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_bytes(buf.getvalue())

# ---------- Estados financieros (a partir del mayor) ----------
def _build_pl(rows_ledger):
    income = expense = 0.0
    det = []
    for acc, deb, cred in rows_ledger:
        typ = _guess_type(acc)
        bal_ing = float(cred or 0) - float(deb or 0)    # ingresos positivos
        bal_gas = float(deb or 0) - float(cred or 0)    # gastos positivos
        if typ == "income":
            income += bal_ing
            det.append(("Ingreso", acc, f"{bal_ing:.2f}"))
        elif typ == "expense":
            expense += bal_gas
            det.append(("Gasto", acc, f"{bal_gas:.2f}"))
    util = income - expense
    return income, expense, util, det

def _build_balance(rows_ledger):
    assets = liabilities = equity = 0.0
    det = []
    for acc, deb, cred in rows_ledger:
        typ = _guess_type(acc)
        bal = float(deb or 0) - float(cred or 0)  # activos positivos
        if typ == "asset":
            assets += bal
            det.append(("Activo", acc, f"{bal:.2f}"))
        elif typ == "liability":
            liabilities += -bal
            det.append(("Pasivo", acc, f"{(-bal):.2f}"))
        elif typ == "equity":
            equity += -bal
            det.append(("Patrimonio", acc, f"{(-bal):.2f}"))
    return assets, liabilities, equity, det

# ---------- Registrar en Documento ----------
def _register_document(path: Path, mime="application/pdf"):
    try:
        res = db.session.execute(
            text("""INSERT INTO Documento (Tipo, Ruta, MimeType, TamanoBytes)
                    VALUES ('Reporte', :r, :m, :sz)"""),
            {"r": "/" + str(path.relative_to(BASE_DIR)).replace("\\", "/"),
             "m": mime, "sz": path.stat().st_size}
        )
        db.session.commit()
        return res.lastrowid
    except Exception:
        db.session.rollback()
        return None

# ========================= Endpoints =========================
@fin_reports_bp.get("")
def reports_index():
    """Exporta un reporte:
       ?type=diario|mayor|er|bg  &fmt=csv|pdf  [&from=YYYY-MM-DD&to=YYYY-MM-DD]
    """
    rtype = (request.args.get("type") or "diario").lower()
    fmt   = (request.args.get("fmt") or "csv").lower()

    dfrom, dto = _range_from_args()
    ymdir = _yyyymm_dirs(dto)
    subtitle = f"Período: {dfrom:%Y-%m-%d} a {dto:%Y-%m-%d}"

    if rtype not in ("diario", "mayor", "er", "bg"):
        return jsonify({"ok": False, "error": "type_invalid"}), 400
    if fmt not in ("csv", "pdf"):
        return jsonify({"ok": False, "error": "fmt_invalid"}), 400

    # ---- Libro Diario ----
    if rtype == "diario":
        rows = db.session.execute(SQL_JOURNAL, {"dfrom": dfrom, "dto": dto}).all()
        headers = ["Fecha", "Transacción", "Moneda", "Línea", "Cuenta", "Debe", "Haber", "Descripción"]
        data = [(r[0].strftime("%Y-%m-%d %H:%M"), r[1], r[2], r[3], r[4],
                 f"{float(r[5] or 0):.2f}", f"{float(r[6] or 0):.2f}", r[7] or "")
                for r in rows]
        fname = f"libro_diario_{dfrom:%Y%m%d}_{dto:%Y%m%d}.{fmt}"
        out = ymdir / fname
        if fmt == "csv":
            _write_csv(data, headers, out); mime = "text/csv"
        else:
            _pdf_table_beauty("Libro Diario", subtitle, headers, data, out); mime = "application/pdf"
        _register_document(out, mime)
        return send_file(out, as_attachment=True, download_name=fname, mimetype=mime)

    # ---- Libro Mayor ----
    if rtype == "mayor":
        rows = db.session.execute(SQL_LEDGER, {"dfrom": dfrom, "dto": dto}).all()
        headers = ["Cuenta", "Debe", "Haber", "Saldo (Debe - Haber)"]
        data = [(r[0], f"{float(r[1] or 0):.2f}", f"{float(r[2] or 0):.2f}",
                 f"{(float(r[1] or 0)-float(r[2] or 0)):.2f}") for r in rows]
        fname = f"libro_mayor_{dfrom:%Y%m%d}_{dto:%Y%m%d}.{fmt}"
        out = ymdir / fname
        if fmt == "csv":
            _write_csv(data, headers, out); mime = "text/csv"
        else:
            _pdf_table_beauty("Libro Mayor", subtitle, headers, data, out); mime = "application/pdf"
        _register_document(out, mime)
        return send_file(out, as_attachment=True, download_name=fname, mimetype=mime)

    # ---- Estado de Resultados ----
    if rtype == "er":
        raw = db.session.execute(SQL_LEDGER, {"dfrom": dfrom, "dto": dto}).all()
        rows_ledger = [(r[0], float(r[1] or 0), float(r[2] or 0)) for r in raw]
        income, expense, util, det = _build_pl(rows_ledger)
        headers = ["Tipo", "Cuenta", "Monto"]
        data = det + [
            ("", "", ""),
            ("Total Ingresos", "", f"{income:.2f}"),
            ("Total Gastos", "", f"{expense:.2f}"),
            ("Utilidad (Pérdida)", "", f"{util:.2f}"),
        ]
        fname = f"estado_resultados_{dfrom:%Y%m%d}_{dto:%Y%m%d}.{fmt}"
        out = ymdir / fname
        if fmt == "csv":
            _write_csv(data, headers, out); mime = "text/csv"
        else:
            _pdf_table_beauty("Estado de Resultados", subtitle, headers, data, out); mime = "application/pdf"
        _register_document(out, mime)
        return send_file(out, as_attachment=True, download_name=fname, mimetype=mime)

    # ---- Balance General ----
    if rtype == "bg":
        raw = db.session.execute(SQL_LEDGER, {"dfrom": dfrom, "dto": dto}).all()
        rows_ledger = [(r[0], float(r[1] or 0), float(r[2] or 0)) for r in raw]
        assets, liabilities, equity, det = _build_balance(rows_ledger)
        headers = ["Grupo", "Cuenta", "Monto"]
        data = det + [
            ("", "", ""),
            ("Total Activos", "", f"{assets:.2f}"),
            ("Total Pasivos", "", f"{liabilities:.2f}"),
            ("Patrimonio", "", f"{equity:.2f}"),
            ("Pasivo + Patrimonio", "", f"{(liabilities+equity):.2f}"),
        ]
        fname = f"balance_general_{dfrom:%Y%m%d}_{dto:%Y%m%d}.{fmt}"
        out = ymdir / fname
        if fmt == "csv":
            _write_csv(data, headers, out); mime = "text/csv"
        else:
            _pdf_table_beauty("Balance General", subtitle, headers, data, out); mime = "application/pdf"
        _register_document(out, mime)
        return send_file(out, as_attachment=True, download_name=fname, mimetype=mime)

@fin_reports_bp.get("/ui")
def ui_reports():
    return render_template("fin-reports.html")




