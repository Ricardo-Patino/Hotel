from datetime import datetime, timedelta, date
from flask import render_template, request, redirect, url_for, flash, jsonify
from . import mnt_bp
from extensions import db
from models_sql import Habitacion, MantenimientoSolicitud
from models_sql import MantenimientoPreventivo  # (lo añadimos en el paso 3)
from utils.auth import role_required  # igual que usas en admin/inv

# ---------- util notificaciones (mínimas) ----------
def notify(subject: str, body: str, to_emails: list[str] | None = None):
    """
    Simula notificaciones: imprime a consola y flashea en UI.
    Integra SMTP más tarde si lo deseas.
    """
    print(f"[NOTIFY] {subject}\n{body}\nPara: {to_emails or ['(no-config)']}")
    flash(subject + " — " + body, "info")


# ========== MNT-06-001: Registro de solicitudes ==========
@mnt_bp.route("/solicitudes/nueva", methods=["GET", "POST"])
@role_required("Administrador")
def solicitud_new():
    habitaciones = Habitacion.query.order_by(Habitacion.Numero_Habitacion.asc()).all()
    if request.method == "POST":
        hab_id = request.form.get("Codigo_Habitacion")
        titulo = request.form.get("Titulo", "").strip()
        desc   = request.form.get("Descripcion", "").strip()
        prioridad = request.form.get("Prioridad", "Media")

        if not hab_id or not titulo:
            flash("Habitación y Título son obligatorios", "warning")
            return render_template("mnt/solicitud_new.html", habitaciones=habitaciones)

        s = MantenimientoSolicitud(
            Codigo_Habitacion=int(hab_id),
            Titulo=titulo,
            Descripcion=desc,
            Prioridad=prioridad,
            Estado="Abierta"
        )
        db.session.add(s)
        db.session.commit()

        notify("Solicitud registrada",
               f"#{s.Id} '{s.Titulo}' creada con prioridad {s.Prioridad}.",
               to_emails=None)

        return redirect(url_for("mnt.solicitudes_list"))

    return render_template("mnt/solicitud_new.html", habitaciones=habitaciones)


# ========== MNT-06-002: Listado + cambio de estado (notifica) ==========
@mnt_bp.route("/solicitudes", methods=["GET"])
@role_required("Administrador")
def solicitudes_list():
    q = MantenimientoSolicitud.query.order_by(MantenimientoSolicitud.Fecha_Creacion.desc())
    estado = request.args.get("estado")
    if estado:
        q = q.filter(MantenimientoSolicitud.Estado == estado)
    prioridad = request.args.get("prioridad")
    if prioridad:
        q = q.filter(MantenimientoSolicitud.Prioridad == prioridad)
    items = q.all()
    return render_template("mnt/solicitudes_list.html", items=items)


@mnt_bp.route("/solicitudes/<int:sol_id>/estado", methods=["POST"])
@role_required("Administrador")
def solicitudes_cambiar_estado(sol_id: int):
    s = MantenimientoSolicitud.query.get_or_404(sol_id)

    # Acepta JSON o form-url-encoded
    data = request.get_json(silent=True) or request.form or {}
    nuevo_estado = (data.get("estado") or "").strip()

    estados_validos = ["Abierta", "En Progreso", "En Espera", "Cerrada", "Cancelada"]
    if nuevo_estado not in estados_validos:
        if request.is_json:
            return jsonify({"ok": False, "error": "Estado inválido"}), 400
        flash("Estado inválido", "danger")
        return redirect(url_for("mnt.solicitudes_list"))

    old = s.Estado
    s.Estado = nuevo_estado
    s.Fecha_Actualiza = datetime.utcnow()
    db.session.commit()

    notify("Solicitud actualizada", f"#{s.Id} pasó de {old} → {s.Estado}.", to_emails=None)

    # Si viene de AJAX devolvemos JSON; si viene de <form>, redirigimos
    if request.is_json:
        return jsonify({"ok": True})
    return redirect(url_for("mnt.solicitudes_list"))


# ========== MNT-06-003: Preventivo (programación simple) ==========
def _sumar_periodo(base: date, frecuencia: str, cada_dias: int | None) -> date:
    if frecuencia == "Mensual":
        return base + timedelta(days=30)
    if frecuencia == "Trimestral":
        return base + timedelta(days=90)
    if frecuencia == "Semestral":
        return base + timedelta(days=180)
    if frecuencia == "Anual":
        return base + timedelta(days=365)
    if frecuencia == "Semanas" and cada_dias:
        return base + timedelta(days=cada_dias)
    return base + timedelta(days=30)

@mnt_bp.route("/preventivo", methods=["GET", "POST"])
@role_required("Administrador")
def preventivo():
    if request.method == "POST":
        equipo   = request.form.get("Equipo", "").strip()
        ubic     = request.form.get("Ubicacion", "").strip()
        frec     = request.form.get("Frecuencia", "Mensual")
        cada     = request.form.get("Cada_Dias")
        prox_str = request.form.get("Proxima_Fecha")
        resp     = request.form.get("Responsable", "").strip()
        notas    = request.form.get("Notas", "").strip()

        if not equipo or not ubic or not prox_str:
            flash("Equipo, Ubicación y Próxima Fecha son obligatorios", "warning")
        else:
            cada_int = int(cada) if (cada and cada.isdigit()) else None
            prox = datetime.strptime(prox_str, "%Y-%m-%d").date()
            p = MantenimientoPreventivo(
                Equipo=equipo,
                Ubicacion=ubic,
                Frecuencia=frec,
                Cada_Dias=cada_int,
                Proxima_Fecha=prox,
                Responsable=resp,
                Notas=notas,
                Activo=True
            )
            db.session.add(p)
            db.session.commit()
            notify("Preventivo programado",
                   f"Equipo {equipo} en {ubic}. Próximo: {prox.isoformat()}",
                   to_emails=None)
        return redirect(url_for("mnt.preventivo"))

    planes = MantenimientoPreventivo.query.order_by(MantenimientoPreventivo.Proxima_Fecha.asc()).all()
    return render_template("mnt/preventivo.html", planes=planes)


@mnt_bp.route("/preventivo/<int:pid>/realizado", methods=["POST"])
@role_required("Administrador")
def preventivo_realizado(pid: int):
    p = MantenimientoPreventivo.query.get_or_404(pid)
    hoy = date.today()
    next_date = _sumar_periodo(hoy, p.Frecuencia, p.Cada_Dias)
    p.Proxima_Fecha = next_date
    p.Fecha_Actualiza = datetime.utcnow()
    db.session.commit()
    notify("Preventivo realizado",
           f"{p.Equipo} → próxima fecha {next_date.isoformat()}",
           to_emails=None)
    return redirect(url_for("mnt.preventivo"))
