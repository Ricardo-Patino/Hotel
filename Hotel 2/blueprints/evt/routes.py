from datetime import datetime, timedelta, date
from flask import render_template, request, jsonify, send_file, flash
from . import evt_bp
from extensions import db
from models.evt import (
    EvtSalon, EvtRecurso, EvtEvento, EvtEventoRecurso, EvtPlantilla,
    EvtAsistente, EvtWaitlist, EvtPresupuesto, EvtCosto, EvtNotificacion, EvtWorkOrder
)
from models.grr import AuditoriaLog
from sqlalchemy import and_, or_
from io import BytesIO
from reportlab.pdfgen import canvas

# ---------- Util: auditoría + mini-notify ----------
def audit(usuario: str | None, entidad: str, entidad_id: str, accion: str, datos: dict | None = None):
    db.session.add(AuditoriaLog(Usuario=usuario, Entidad=entidad, Entidad_Id=str(entidad_id), Accion=accion, Datos=datos))
    db.session.commit()

def notify(subject: str, body: str, to_emails: list[str] | None = None):
    print(f"[EVT NOTIFY] {subject}\n{body}\nTo: {to_emails or ['(no-config)']}")

# ---------- Calendario (vista principal) ----------
@evt_bp.get("/calendario")
def calendario_evt():
    # Carga salones y colores para FullCalendar
    salones = EvtSalon.query.filter_by(Activo=True).all()
    return render_template("evt-calendario.html", salones=salones)

# ---------- API Eventos (FullCalendar) ----------
@evt_bp.get("/api/events")
def api_events_list():
    start = request.args.get("start")
    end   = request.args.get("end")
    q = EvtEvento.query
    if start and end:
        di = datetime.fromisoformat(start.replace("Z",""))
        df = datetime.fromisoformat(end.replace("Z",""))
        q = q.filter(or_(
            and_(EvtEvento.Inicio >= di, EvtEvento.Inicio < df),
            and_(EvtEvento.Fin    >  di, EvtEvento.Fin    <= df),
            and_(EvtEvento.Inicio <= di, EvtEvento.Fin    >= df)
        ))
    eventos = q.all()
    data = []
    for e in eventos:
        data.append({
            "id": e.Id,
            "title": f"{e.Titulo} ({e.Salon.Nombre if e.Salon else 'Sin salón'})",
            "start": e.Inicio.isoformat(),
            "end":   e.Fin.isoformat(),
            "extendedProps": {
                "estado": e.Estado,
                "salonId": e.Salon_Id,
            },
            "backgroundColor": (e.Salon.ColorHex if e.Salon else "#8a6cff"),
            "borderColor": (e.Salon.ColorHex if e.Salon else "#8a6cff"),
            "editable": True,   # drag & drop
        })
    return jsonify(data)

@evt_bp.post("/api/events")
def api_events_create():
    payload = request.get_json(silent=True) or {}
    titulo = payload.get("titulo") or "Nuevo evento"
    inicio = datetime.fromisoformat(payload["inicio"])
    fin    = datetime.fromisoformat(payload["fin"])
    salon_id = payload.get("salonId")
    evento = EvtEvento(Titulo=titulo, Inicio=inicio, Fin=fin, Salon_Id=salon_id, Estado="BORRADOR", TZ="America/Costa_Rica")
    db.session.add(evento)
    db.session.commit()
    audit(None, "EvtEvento", evento.Id, "CREATE", {"titulo": titulo})
    return jsonify({"ok": True, "id": evento.Id})

@evt_bp.patch("/api/events/<int:evento_id>")
def api_events_update(evento_id: int):
    evento = EvtEvento.query.get_or_404(evento_id)
    payload = request.get_json(silent=True) or {}

    if "titulo" in payload: evento.Titulo = payload["titulo"]
    if "inicio" in payload: evento.Inicio = datetime.fromisoformat(payload["inicio"])
    if "fin"    in payload: evento.Fin    = datetime.fromisoformat(payload["fin"])
    if "salonId" in payload: evento.Salon_Id = payload["salonId"]
    if "estado" in payload: evento.Estado = payload["estado"]

    db.session.commit()
    audit(None, "EvtEvento", evento.Id, "UPDATE", {"payload": payload})
    return jsonify({"ok": True})

@evt_bp.delete("/api/events/<int:evento_id>")
def api_events_delete(evento_id: int):
    evento = EvtEvento.query.get_or_404(evento_id)
    evento.Estado = "CANCELADO"
    db.session.commit()
    audit(None, "EvtEvento", evento.Id, "CANCEL")
    return jsonify({"ok": True})

# ---------- Conflictos de recursos / sobre-asignación ----------
def _hay_conflicto(evento: EvtEvento) -> tuple[bool, list[str]]:
    conflictos = []
    # 1) Conflicto de salón
    if evento.Salon_Id:
        solapados = EvtEvento.query.filter(
            EvtEvento.Id != evento.Id,
            EvtEvento.Salon_Id == evento.Salon_Id,
            EvtEvento.Estado != "CANCELADO",
            or_(
                and_(EvtEvento.Inicio <= evento.Inicio, EvtEvento.Fin > evento.Inicio),
                and_(EvtEvento.Inicio <  evento.Fin,    EvtEvento.Fin >= evento.Fin),
                and_(EvtEvento.Inicio >= evento.Inicio, EvtEvento.Fin <= evento.Fin)
            )
        ).count()
        if solapados:
            conflictos.append("Salón ocupado en ese horario.")

    # 2) Conflicto por recursos A/V (muy simple: solo verifica existencia de otro evento confirmado en el mismo horario que declare el mismo recurso)
    recursos_evt = [r.Recurso_Id for r in evento.Recursos]
    if recursos_evt:
        solap = db.session.query(EvtEvento).join(EvtEventoRecurso, EvtEvento.Id == EvtEventoRecurso.Evento_Id)\
            .filter(EvtEvento.Id != evento.Id,
                    EvtEventoRecurso.Recurso_Id.in_(recursos_evt),
                    EvtEvento.Estado != "CANCELADO",
                    or_(
                        and_(EvtEvento.Inicio <= evento.Inicio, EvtEvento.Fin > evento.Inicio),
                        and_(EvtEvento.Inicio <  evento.Fin,    EvtEvento.Fin >= evento.Fin),
                        and_(EvtEvento.Inicio >= evento.Inicio, EvtEvento.Fin <= evento.Fin)
                    )
                   ).first()
        if solap:
            conflictos.append("Recurso A/V asignado en otro evento.")

    return (len(conflictos) > 0, conflictos)

@evt_bp.post("/api/conflicts/<int:evento_id>")
def api_conflicts(evento_id: int):
    evento = EvtEvento.query.get_or_404(evento_id)
    conflict, causas = _hay_conflicto(evento)
    return jsonify({"conflict": conflict, "reasons": causas})

# ---------- Confirmación: bloqueo de recursos, notificaciones y órdenes ----------
@evt_bp.post("/api/confirm/<int:evento_id>")
def api_confirm(evento_id: int):
    evento = EvtEvento.query.get_or_404(evento_id)
    conflict, causas = _hay_conflicto(evento)
    if conflict:
        return jsonify({"ok": False, "msg": "Conflicto detectado", "reasons": causas}), 409

    evento.Estado = "CONFIRMADO"
    db.session.commit()

    # Notificaciones
    for tipo in ("CONFIRMACION","7D","24H","2H"):
        n = EvtNotificacion(Evento_Id=evento.Id, Tipo=tipo, Destinatario=evento.Organizador or "organizador@hotelvgrace.cr")
        db.session.add(n)
    db.session.commit()

    # Órdenes de trabajo básicas
    ordenes = [
        ("HOUSEKEEPING", f"Montaje salón {evento.Salon.Nombre if evento.Salon else ''}"),
        ("MANTENIMIENTO", "Verificar A/V y electricidad"),
        ("COCINA", "Catering según ficha del evento"),
    ]
    for area, tarea in ordenes:
        db.session.add(EvtWorkOrder(Evento_Id=evento.Id, Area=area, Tarea=tarea, Prioridad="MEDIA"))
    db.session.commit()

    audit(None, "EvtEvento", evento.Id, "CONFIRM")
    notify("Evento confirmado", f"Id {evento.Id} – {evento.Titulo}")
    return jsonify({"ok": True})

# ---------- PDF Agenda diaria ----------
@evt_bp.get("/agenda.pdf")
def agenda_pdf():
    # fecha de query param ?fecha=YYYY-MM-DD
    ds = request.args.get("fecha")
    if ds:
        d = datetime.strptime(ds, "%Y-%m-%d").date()
    else:
        d = date.today()
    inicio = datetime.combine(d, datetime.min.time())
    fin = datetime.combine(d, datetime.max.time())

    eventos = EvtEvento.query.filter(
        EvtEvento.Inicio <= fin,
        EvtEvento.Fin >= inicio,
        EvtEvento.Estado != "CANCELADO"
    ).order_by(EvtEvento.Inicio.asc()).all()

    # PDF sencillo con reportlab (mismo paquete que usas en otros módulos)
    buffer = BytesIO()
    c = canvas.Canvas(buffer)
    c.setTitle(f"Agenda-{d.isoformat()}")

    c.setFont("Helvetica-Bold", 14)
    c.drawString(30, 800, f"Agenda diaria — {d.isoformat()}")

    y = 770
    for e in eventos:
        linea = f"{e.Inicio.strftime('%H:%M')} - {e.Fin.strftime('%H:%M')} | {e.Titulo} | {e.Salon.Nombre if e.Salon else 'Sin salón'}"
        c.setFont("Helvetica", 11)
        c.drawString(30, y, linea[:110])
        y -= 18
        if y < 60:
            c.showPage()
            y = 800

    c.showPage()
    c.save()
    buffer.seek(0)
    return send_file(buffer, as_attachment=True, download_name=f"Agenda_{d.isoformat()}.pdf", mimetype="application/pdf")

# ---------- Catálogo externo (stub) ----------
@evt_bp.get("/api/externos")
def api_externos():
    """
    Stub que simula un catálogo externo (tours/museos). Filtra por fecha (yyyy-mm-dd) y distancia (km).
    Aquí conectarías tu API real (Viator/Ticketmaster/partner DMC).
    """
    fecha = request.args.get("fecha")  # opcional
    dist  = request.args.get("dist")   # opcional
    # respuesta simulada
    data = [
        {"id":"EXT-101","titulo":"Tour Isla Tortuga","fecha":"2025-11-02","dist_km":12,"precio":85.0},
        {"id":"EXT-205","titulo":"Museo del Jade","fecha":"2025-11-04","dist_km":5,"precio":10.0},
        {"id":"EXT-333","titulo":"Concierto Jazz","fecha":"2025-11-03","dist_km":3,"precio":25.0},
    ]
    if fecha:
        data = [x for x in data if x["fecha"] == fecha]
    if dist:
        try:
            dmax = float(dist)
            data = [x for x in data if x["dist_km"] <= dmax]
        except:
            pass
    return jsonify(data)

# ---------- Registro de asistentes, check-in y waitlist ----------
@evt_bp.post("/api/asistentes/<int:evento_id>")
def api_asistentes_add(evento_id: int):
    p = request.get_json(silent=True) or {}
    from models.evt import EvtAsistente
    a = EvtAsistente(Evento_Id=evento_id, Nombre=p.get("nombre","Invitado"), Email=p.get("email"))
    db.session.add(a); db.session.commit()
    audit(None, "EvtAsistente", a.Id, "CREATE", {"evento": evento_id})
    return jsonify({"ok": True, "id": a.Id})

@evt_bp.post("/api/checkin/<int:asistente_id>")
def api_checkin(asistente_id: int):
    a = EvtAsistente.query.get_or_404(asistente_id)
    a.Estado = "CHECKIN"
    a.CheckIn_At = datetime.utcnow()
    db.session.commit()
    audit(None, "EvtAsistente", a.Id, "CHECKIN")
    return jsonify({"ok": True})
