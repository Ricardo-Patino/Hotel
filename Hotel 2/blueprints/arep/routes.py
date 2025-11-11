from datetime import datetime, date
from io import BytesIO
from flask import render_template, request, jsonify, send_file
from sqlalchemy import text
from . import arep_bp
from extensions import db
from utils.auth import role_required

# ------------------------------------------
# Util: ejecutar SQL con parámetros
# ------------------------------------------
def q(sql, **params):
    return db.session.execute(text(sql), params).mappings().all()

# ------------------------------------------
# Vista principal (gráficas interactivas)
# ------------------------------------------
@arep_bp.get("/dashboard")
def dashboard():
    return render_template("arep-dashboard.html")

# ------------------------------------------
# API: métricas principales (ocupación/ingresos/servicios)
# Filtros combinados: fecha + tipo_cliente + tipo_servicio
# ------------------------------------------
@arep_bp.get("/api/metrics")
def api_metrics():
    f_desde = request.args.get("desde")
    f_hasta = request.args.get("hasta")
    tipo_cli = request.args.get("tipo_cliente", "TODOS")
    tipo_srv = request.args.get("tipo_servicio", "TODOS")

    # Normaliza fechas
    try:
        desde = datetime.strptime(f_desde, "%Y-%m-%d").date() if f_desde else date.today().replace(day=1)
    except: desde = date.today().replace(day=1)
    try:
        hasta = datetime.strptime(f_hasta, "%Y-%m-%d").date() if f_hasta else date.today()
    except: hasta = date.today()

    # ----- Ingresos por mes (desde KPI_Stats si existe) -----
    sql_ingresos = """
        SELECT Clave AS mes, COALESCE(Total_Monto,0) AS ingresos
        FROM KPI_Stats
        WHERE Periodo='month'
          AND STR_TO_DATE(CONCAT(Clave,'-01'), '%Y-%m-%d') BETWEEN :d1 AND :d2
        ORDER BY mes ASC
    """
    ingresos = q(sql_ingresos, d1=desde, d2=hasta)

    # ----- Ocupación aproximada: Total_Noches / (habitaciones * días del mes) -----
    # (usa KPI_Stats.Total_Noches como base)
    sql_ocup = """
        WITH rooms AS (SELECT COUNT(*) AS total FROM Habitacion),
             meses AS (
               SELECT Clave, Total_Noches FROM KPI_Stats WHERE Periodo='month'
                 AND STR_TO_DATE(CONCAT(Clave,'-01'), '%Y-%m-%d') BETWEEN :d1 AND :d2
             )
        SELECT m.Clave AS mes,
               ROUND( (COALESCE(m.Total_Noches,0) / NULLIF(r.total*DAY(LAST_DAY(STR_TO_DATE(CONCAT(m.Clave,'-01'),'%Y-%m-%d'))),0)) * 100, 2 ) AS ocupacion_pct
        FROM meses m CROSS JOIN rooms r
        ORDER BY m.Clave
    """
    ocupacion = q(sql_ocup, d1=desde, d2=hasta)

    # ----- "Servicios utilizados": proxy usando inventario por categoría (consumibles movidos)
    sql_servicios = """
        SELECT c.Nombre AS categoria, COUNT(m.Id) AS movimientos
        FROM InvMovimiento m
        JOIN InvInsumo i ON i.Id = m.Insumo_Id
        JOIN InvCategoria c ON c.Id = i.Categoria_Id
        WHERE m.Fecha_Creacion BETWEEN :d1 AND :d2
        GROUP BY c.Nombre
        ORDER BY movimientos DESC
        LIMIT 10
    """
    servicios = q(sql_servicios, d1=desde, d2=hasta)

    # ----- Ranking de personal: combinación simple (satisfacción y SLA)
    sql_ranking = """
        WITH sat AS (
          SELECT Codigo_Funcionario, AVG(Puntaje) AS nps
          FROM ArepSatisfaccion
          WHERE DATE(Fecha_Registro) BETWEEN :d1 AND :d2
          GROUP BY Codigo_Funcionario
        ),
        sla AS (
          SELECT Codigo_Funcionario,
                 AVG(Duracion_Seg) AS avg_seg
          FROM ArepAtencion
          WHERE DATE(Inicio_Atencion) BETWEEN :d1 AND :d2
          GROUP BY Codigo_Funcionario
        )
        SELECT f.Codigo_Funcionario, CONCAT(f.Nombre,' ',f.Apellido) AS funcionario,
               ROUND(COALESCE(s.nps,0),2) AS nps,
               ROUND(COALESCE( (CASE WHEN l.avg_seg>0 THEN 1/(l.avg_seg/60) ELSE 0 END),0),2) AS score_velocidad,
               ROUND( COALESCE(s.nps,0)*0.7 + COALESCE( (CASE WHEN l.avg_seg>0 THEN 1/(l.avg_seg/60) ELSE 0 END),0)*0.3 , 2) AS score_final
        FROM Funcionario f
        LEFT JOIN sat s ON s.Codigo_Funcionario=f.Codigo_Funcionario
        LEFT JOIN sla l ON l.Codigo_Funcionario=f.Codigo_Funcionario
        ORDER BY score_final DESC
        LIMIT 10
    """
    ranking = q(sql_ranking, d1=desde, d2=hasta)

    return jsonify({
        "params": {"desde": str(desde), "hasta": str(hasta),
                   "tipo_cliente": tipo_cli, "tipo_servicio": tipo_srv},
        "ingresos_mes": ingresos,
        "ocupacion": ocupacion,
        "servicios": servicios,
        "ranking": ranking
    })

# ------------------------------------------
# Export: XLSX y PDF (aplica filtros actuales)
# ------------------------------------------
@arep_bp.get("/export")
def export():
    fmt = (request.args.get("fmt") or "xlsx").lower()
    desde = request.args.get("desde")
    hasta = request.args.get("hasta")
    if fmt not in ("xlsx","pdf"):
        fmt = "xlsx"

    # Reusar los datos del endpoint anterior:
    with arep_bp.test_request_context(f"/api/metrics?desde={desde}&hasta={hasta}"):
        pass
    data = api_metrics().json

    # Log de export
    db.session.execute(text("""
        INSERT INTO ArepExportLog (Tipo, Nombre_Reporte, Codigo_Usuario, Filtros_JSON)
        VALUES (:t, 'Dashboard General', NULL, JSON_OBJECT('desde',:d1,'hasta',:d2))
    """), {"t": fmt.upper(), "d1": data["params"]["desde"], "d2": data["params"]["hasta"]})
    db.session.commit()

    if fmt == "xlsx":
        # XLSX con openpyxl (instalado con Flask estándar suele estar disponible)
        try:
            import xlsxwriter
        except Exception:
            # Fallback a CSV en un .xlsx si no está xlsxwriter (sigue siendo abrible por Excel)
            from io import StringIO
            buf = StringIO()
            buf.write("Hoja;Columna;Valor\n")
            for item in data["ingresos_mes"]:
                buf.write(f"ingresos;{item['mes']};{item['ingresos']}\n")
            f = BytesIO(buf.getvalue().encode("utf-8"))
            return send_file(f, as_attachment=True, download_name="reporte.xlsx",
                             mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")

        output = BytesIO()
        wb = xlsxwriter.Workbook(output, {'in_memory': True})
        def write_sheet(name, rows):
            ws = wb.add_worksheet(name[:31])
            if not rows:
                return
            headers = list(rows[0].keys())
            for c,h in enumerate(headers): ws.write(0,c,h)
            for r,row in enumerate(rows, start=1):
                for c,h in enumerate(headers): ws.write(r,c,row[h])
        write_sheet("Ingresos", data["ingresos_mes"])
        write_sheet("Ocupacion", data["ocupacion"])
        write_sheet("Servicios", data["servicios"])
        write_sheet("Ranking", data["ranking"])
        wb.close()
        output.seek(0)
        return send_file(output, as_attachment=True, download_name="reporte.xlsx",
                         mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")

    else:
        # PDF con reportlab
        from reportlab.pdfgen import canvas
        from reportlab.lib.pagesizes import A4
        from reportlab.lib.units import cm

        f = BytesIO()
        c = canvas.Canvas(f, pagesize=A4)
        W, H = A4
        y = H-2*cm
        c.setFont("Helvetica-Bold", 14)
        c.drawString(2*cm, y, "Hotel Villa Grace — Dashboard (AREP)")
        y -= 0.8*cm
        c.setFont("Helvetica", 10)
        c.drawString(2*cm, y, f"Periodo: {data['params']['desde']} a {data['params']['hasta']}")
        y -= 1.0*cm

        def block(title, rows, cols):
            nonlocal y
            c.setFont("Helvetica-Bold", 11); c.drawString(2*cm, y, title); y -= 0.5*cm
            c.setFont("Helvetica", 9)
            if not rows: c.drawString(2*cm, y, "Sin datos"); y -= 0.6*cm; return
            head = " | ".join(cols)
            c.drawString(2*cm, y, head); y -= 0.4*cm
            for r in rows[:20]:
                line = " | ".join(str(r.get(k,"")) for k in cols)
                c.drawString(2*cm, y, line[:110]); y -= 0.35*cm
                if y < 3*cm: c.showPage(); y = H-2*cm
            y -= 0.2*cm

        block("Ingresos por mes", data["ingresos_mes"], ["mes","ingresos"])
        block("Ocupación (%)", data["ocupacion"], ["mes","ocupacion_pct"])
        block("Top servicios (movimientos)", data["servicios"], ["categoria","movimientos"])
        block("Ranking personal", data["ranking"], ["funcionario","nps","score_velocidad","score_final"])
        c.showPage(); c.save(); f.seek(0)
        return send_file(f, as_attachment=True, download_name="reporte.pdf", mimetype="application/pdf")

# ------------------------------------------
# Guardar configuración visual (encabezado/columnas/filtros)
# ------------------------------------------
@arep_bp.post("/config/save")
def config_save():
    payload = request.get_json(silent=True) or {}
    nombre = payload.get("nombre") or "Dashboard General"
    # Aquí podrías identificar al usuario logueado; dejamos NULL para admin
    db.session.execute(text("""
      INSERT INTO ArepViewConfig (Codigo_Usuario, Nombre_Reporte, Encabezado_JSON, Columnas_JSON, Filtros_JSON)
      VALUES (NULL, :nom, :enc, :col, :fil)
      ON DUPLICATE KEY UPDATE
        Encabezado_JSON=:enc, Columnas_JSON=:col, Filtros_JSON=:fil
    """), {"nom": nombre,
           "enc": payload.get("encabezado"),
           "col": payload.get("columnas"),
           "fil": payload.get("filtros")})
    db.session.commit()
    return jsonify({"ok": True})

@arep_bp.route("/admin-audit.html")
@role_required("Administrador")
def admin_audit_html():
    """
    Vista de bitácora y auditoría del sistema
    """
    return render_template("admin-audit.html")