# blueprints/hrm/routes.py
from __future__ import annotations

from datetime import datetime, date, timedelta
from flask import request, jsonify, render_template, session, redirect, url_for
from sqlalchemy import func, and_
from extensions import db
from . import hrm_bp

# Modelos principales HRM
from models import Funcionario, FuncionarioHistorial
# Modelo de marcaciones (HU-08-002)
from models import Marcacion


# =============================================================================
# Utilidades
# =============================================================================
def _funcionario_to_dict(f: Funcionario):
    return {
        "Codigo_Funcionario": f.Codigo_Funcionario,
        "Cedula": f.Cedula,
        "Nombre": f.Nombre,
        "Apellido": f.Apellido,
        "Puesto": f.Puesto,
        "Departamento": f.Departamento,
        "Fecha_Nacimiento": f.Fecha_Nacimiento.isoformat() if f.Fecha_Nacimiento else None,
        "Fecha_Ingreso": f.Fecha_Ingreso.isoformat() if f.Fecha_Ingreso else None,
        "Salario_Base_Mensual": str(f.Salario_Base_Mensual),
        "Estado_Empleado": f.Estado_Empleado,
        "Tipo_Contrato": f.Tipo_Contrato,
        "Cuenta_Bancaria": f.Cuenta_Bancaria,
        "Banco": f.Banco,
        "Fecha_modificacion": f.Fecha_modificacion.isoformat() if f.Fecha_modificacion else None,
        "Fecha_mod_alta": f.Fecha_mod_alta.isoformat() if f.Fecha_mod_alta else None,
    }


def _registrado_por():
    # Ajusta a como guardes el usuario en sesión
    return session.get("Codigo_Usuario") or session.get("user_id")


def _current_funcionario_id() -> int | None:
    """
    Obtiene el funcionario 'logueado'.
    - Primero busca en sesión (p. ej. session["Codigo_Funcionario"]).
    - También admite ?func=ID en la URL para pruebas.
    """
    fid = session.get("Codigo_Funcionario")
    if not fid:
        fid = request.args.get("func", type=int)
    return fid


def _calc_hours(dt_in: datetime, dt_out: datetime) -> float:
    """Retorna horas decimales redondeadas a 2 decimales."""
    secs = (dt_out - dt_in).total_seconds()
    return round(secs / 3600.0, 2)


def _set_if_attr(obj, field, value):
    """Asigna si el atributo existe (seguro con distintos nombres de columnas)."""
    if hasattr(obj, field):
        setattr(obj, field, value)


# =============================================================================
# API JSON: Empleados (CRUD + Perfil)
# =============================================================================
@hrm_bp.route("/empleados", methods=["GET"])
def listar_empleados():
    estado = request.args.get("estado", default="Activo")
    query = Funcionario.query
    if estado != "todos":
        query = query.filter(Funcionario.Estado_Empleado == estado)
    empleados = query.order_by(Funcionario.Nombre, Funcionario.Apellido).all()
    data = [_funcionario_to_dict(f) for f in empleados]
    return jsonify({"ok": True, "data": data})


@hrm_bp.route("/empleados/<int:codigo_func>", methods=["GET"])
def detalle_empleado(codigo_func):
    f = Funcionario.query.get(codigo_func)
    if not f:
        return jsonify({"ok": False, "error": "Empleado no encontrado"}), 404

    hist = (
        FuncionarioHistorial.query
        .filter_by(Codigo_Funcionario=codigo_func)
        .order_by(FuncionarioHistorial.Fecha_Evento.desc())
        .all()
    )

    return jsonify({
        "ok": True,
        "empleado": _funcionario_to_dict(f),
        "historial": [
            {
                "Id_Historial": h.Id_Historial,
                "Fecha_Evento": h.Fecha_Evento.isoformat() if h.Fecha_Evento else None,
                "Tipo_Evento": h.Tipo_Evento,
                "Detalle": h.Detalle,
                "Valor_Anterior": h.Valor_Anterior,
                "Valor_Nuevo": h.Valor_Nuevo,
                "Registrado_Por": h.Registrado_Por,
            } for h in hist
        ],
    })


@hrm_bp.route("/empleados", methods=["POST"])
def crear_empleado():
    payload = request.get_json(silent=True) or {}
    fecha_ingreso = payload.get("Fecha_Ingreso") or date.today().isoformat()

    nuevo = Funcionario(
        Cedula=payload.get("Cedula"),
        Nombre=payload.get("Nombre"),
        Apellido=payload.get("Apellido"),
        Puesto=payload.get("Puesto"),
        Departamento=payload.get("Departamento"),
        Fecha_Nacimiento=payload.get("Fecha_Nacimiento"),
        Fecha_Ingreso=fecha_ingreso,
        Salario_Base_Mensual=payload.get("Salario_Base_Mensual", 0.00),
        Estado_Empleado=payload.get("Estado_Empleado", "Activo"),
        Tipo_Contrato=payload.get("Tipo_Contrato", "Tiempo completo"),
        Cuenta_Bancaria=payload.get("Cuenta_Bancaria"),
        Banco=payload.get("Banco"),
        Fecha_modificacion=datetime.utcnow(),
        Fecha_mod_alta=datetime.utcnow(),
    )

    db.session.add(nuevo)
    db.session.flush()  # obtiene Codigo_Funcionario

    hist = FuncionarioHistorial(
        Codigo_Funcionario=nuevo.Codigo_Funcionario,
        Fecha_Evento=datetime.utcnow(),
        Tipo_Evento="Ingreso",
        Detalle="Ingreso al hotel",
        Valor_Anterior=None,
        Valor_Nuevo=f"Salario inicial {payload.get('Salario_Base_Mensual', 0.00)}",
        Registrado_Por=_registrado_por(),
    )
    db.session.add(hist)
    db.session.commit()

    return jsonify({"ok": True, "empleado": _funcionario_to_dict(nuevo)}), 201


@hrm_bp.route("/empleados/<int:codigo_func>", methods=["PUT"])
def actualizar_empleado(codigo_func):
    payload = request.get_json(silent=True) or {}
    f = Funcionario.query.get(codigo_func)
    if not f:
        return jsonify({"ok": False, "error": "Empleado no encontrado"}), 404

    cambios = []

    # Cambio de puesto
    nuevo_puesto = payload.get("Puesto", f.Puesto)
    if nuevo_puesto != f.Puesto:
        cambios.append(("Cambio Puesto", "Actualización de puesto", f.Puesto, nuevo_puesto))
        f.Puesto = nuevo_puesto

    # Cambio de salario
    nuevo_salario = payload.get("Salario_Base_Mensual", f.Salario_Base_Mensual)
    if str(nuevo_salario) != str(f.Salario_Base_Mensual):
        cambios.append(("Cambio Salario", "Ajuste salarial", str(f.Salario_Base_Mensual), str(nuevo_salario)))
        f.Salario_Base_Mensual = nuevo_salario

    # Campos generales
    f.Cedula           = payload.get("Cedula", f.Cedula)
    f.Nombre           = payload.get("Nombre", f.Nombre)
    f.Apellido         = payload.get("Apellido", f.Apellido)
    f.Departamento     = payload.get("Departamento", f.Departamento)
    f.Fecha_Nacimiento = payload.get("Fecha_Nacimiento", f.Fecha_Nacimiento)
    f.Fecha_Ingreso    = payload.get("Fecha_Ingreso", f.Fecha_Ingreso)
    f.Estado_Empleado  = payload.get("Estado_Empleado", f.Estado_Empleado)
    f.Tipo_Contrato    = payload.get("Tipo_Contrato", f.Tipo_Contrato)
    f.Cuenta_Bancaria  = payload.get("Cuenta_Bancaria", f.Cuenta_Bancaria)
    f.Banco            = payload.get("Banco", f.Banco)
    f.Fecha_modificacion = datetime.utcnow()
    f.Fecha_mod_alta     = datetime.utcnow()

    db.session.flush()

    for tipo, detalle, val_ant, val_nuevo in cambios:
        db.session.add(FuncionarioHistorial(
            Codigo_Funcionario=f.Codigo_Funcionario,
            Fecha_Evento=datetime.utcnow(),
            Tipo_Evento=tipo,
            Detalle=detalle,
            Valor_Anterior=val_ant,
            Valor_Nuevo=val_nuevo,
            Registrado_Por=_registrado_por(),
        ))

    db.session.commit()
    return jsonify({"ok": True, "empleado": _funcionario_to_dict(f)})


@hrm_bp.route("/empleados/<int:codigo_func>", methods=["DELETE"])
def desactivar_empleado(codigo_func):
    f = Funcionario.query.get(codigo_func)
    if not f:
        return jsonify({"ok": False, "error": "Empleado no encontrado"}), 404

    if f.Estado_Empleado != "Inactivo":
        f.Estado_Empleado = "Inactivo"
        f.Fecha_modificacion = datetime.utcnow()
        f.Fecha_mod_alta = datetime.utcnow()
        db.session.flush()

        hist = FuncionarioHistorial(
            Codigo_Funcionario=f.Codigo_Funcionario,
            Fecha_Evento=datetime.utcnow(),
            Tipo_Evento="Salida",
            Detalle="Colaborador desactivado",
            Valor_Anterior=None,
            Valor_Nuevo="Estado Inactivo",
            Registrado_Por=_registrado_por(),
        )
        db.session.add(hist)

    db.session.commit()
    return jsonify({"ok": True, "empleado": _funcionario_to_dict(f)})


@hrm_bp.route("/empleados/<int:codigo_func>/perfil", methods=["PATCH"])
def actualizar_perfil(codigo_func):
    payload = request.get_json(silent=True) or {}
    f = Funcionario.query.get(codigo_func)
    if not f:
        return jsonify({"ok": False, "error": "Empleado no encontrado"}), 404

    cambios = []
    # Puesto
    if "Puesto" in payload and payload["Puesto"] != f.Puesto:
        cambios.append(("Cambio Puesto", "Actualización de puesto", f.Puesto, payload["Puesto"]))
        f.Puesto = payload["Puesto"]

    # Departamento
    if "Departamento" in payload and payload["Departamento"] != f.Departamento:
        cambios.append(("Cambio Departamento", "Actualización de departamento",
                        f.Departamento, payload["Departamento"]))
        f.Departamento = payload["Departamento"]

    # Salario
    if "Salario_Base_Mensual" in payload and str(payload["Salario_Base_Mensual"]) != str(f.Salario_Base_Mensual):
        cambios.append(("Cambio Salario", "Ajuste salarial", str(f.Salario_Base_Mensual),
                        str(payload["Salario_Base_Mensual"])))
        f.Salario_Base_Mensual = payload["Salario_Base_Mensual"]

    if not cambios:
        return jsonify({"ok": True, "empleado": _funcionario_to_dict(f), "msg": "Sin cambios"})

    f.Fecha_modificacion = datetime.utcnow()
    f.Fecha_mod_alta = datetime.utcnow()
    db.session.flush()

    reg_por = _registrado_por()
    for tipo, detalle, val_ant, val_nuevo in cambios:
        db.session.add(FuncionarioHistorial(
            Codigo_Funcionario=f.Codigo_Funcionario,
            Fecha_Evento=datetime.utcnow(),
            Tipo_Evento=tipo,
            Detalle=detalle,
            Valor_Anterior=val_ant,
            Valor_Nuevo=val_nuevo,
            Registrado_Por=reg_por
        ))

    db.session.commit()
    return jsonify({"ok": True, "empleado": _funcionario_to_dict(f)})


# =============================================================================
# Interfaz HTML: Listado y Detalle
# =============================================================================
@hrm_bp.route("/empleados-ui", methods=["GET"])
def empleados_ui():
    estado = request.args.get("estado", default="Activo")

    # Conteos para chips
    conteos = dict(
        db.session.query(Funcionario.Estado_Empleado, func.count(Funcionario.Codigo_Funcionario))
        .group_by(Funcionario.Estado_Empleado)
        .all()
    )
    total = db.session.query(func.count(Funcionario.Codigo_Funcionario)).scalar() or 0

    query = Funcionario.query
    if estado != "todos":
        query = query.filter(Funcionario.Estado_Empleado == estado)

    empleados = query.order_by(Funcionario.Nombre, Funcionario.Apellido).all()

    return render_template(
        "empleados.html",
        empleados=empleados,
        estado=estado,
        conteos=conteos,
        total=total,
    )


@hrm_bp.route("/empleados/detalle/<int:codigo_func>", methods=["GET"])
def ver_empleado_detalle(codigo_func):
    f = Funcionario.query.get(codigo_func)
    if not f:
        return render_template("404.html", mensaje="Colaborador no encontrado"), 404

    historial = (
        FuncionarioHistorial.query
        .filter_by(Codigo_Funcionario=codigo_func)
        .order_by(FuncionarioHistorial.Fecha_Evento.desc())
        .all()
    )

    return render_template(
        "empleado_detalle.html",
        empleado=f,
        historial=historial
    )


# Alias HTML para evitar romper enlaces antiguos (si tu listado usa /hrm/empleados/<id>)
@hrm_bp.route("/empleados/<int:codigo_func>/ui", methods=["GET"])
def ver_empleado_detalle_alias(codigo_func):
    return redirect(url_for("hrm.ver_empleado_detalle", codigo_func=codigo_func))


# =============================================================================
# HU-08-002 — Registro de horas por el colaborador
# =============================================================================
@hrm_bp.route("/mis-horas", methods=["GET"])
def mis_horas_ui():
    """
    Panel del colaborador para ver/registrar sus horas.
    Parámetros opcionales:
      - y (año), m (mes)
    """
    fid = _current_funcionario_id()
    if not fid:
        # sin sesión/funcionario: redirige a colaboradores (o podrías ir a login)
        return redirect(url_for("hrm.empleados_ui"))

    year = request.args.get("y", type=int) or datetime.today().year
    month = request.args.get("m", type=int) or datetime.today().month

    # Rango del mes
    start = date(year, month, 1)
    end = (start.replace(day=28) + timedelta(days=4)).replace(day=1)  # primer día del siguiente mes

    marcas = (
        Marcacion.query
        .filter(
            Marcacion.Codigo_Funcionario == fid,
            Marcacion.Fecha >= start,
            Marcacion.Fecha < end
        )
        .order_by(Marcacion.Fecha.desc(), Marcacion.Id.desc())
        .all()
    )

    # Totales del mes
    total_horas = 0.0
    for m in marcas:
        horas_val = None
        if hasattr(m, "Horas") and m.Horas is not None:
            horas_val = float(m.Horas)
        elif hasattr(m, "Total_Horas") and m.Total_Horas is not None:
            horas_val = float(m.Total_Horas)
        if horas_val:
            total_horas += horas_val

    return render_template(
        "mis_horas.html",
        marcas=marcas,
        total_horas=round(total_horas, 2),
        year=year,
        month=month,
        hoy=date.today(),
        funcionario=Funcionario.query.get(fid)
    )


@hrm_bp.route("/marcar/entrada", methods=["POST"])
def marcar_entrada():
    """Crea la marcación de entrada del día si no existe una abierta."""
    fid = _current_funcionario_id()
    if not fid:
        return jsonify({"ok": False, "error": "No hay colaborador en sesión"}), 401

    ahora = datetime.utcnow()
    hoy = ahora.date()

    # ¿Ya hay una marcación abierta (sin salida) hoy?
    abierto = (
        Marcacion.query
        .filter(
            Marcacion.Codigo_Funcionario == fid,
            Marcacion.Fecha == hoy,
            # campo de salida nulo (soportar Hora_Salida o Salida)
            or_(
                getattr(Marcacion, "Hora_Salida", None) == None,  # noqa: E711
                getattr(Marcacion, "Salida", None) == None
            )
        )
        .order_by(Marcacion.Id.desc())
        .first()
    )
    if abierto:
        return jsonify({"ok": False, "error": "Ya existe una marcación abierta para hoy."}), 400

    m = Marcacion()
    _set_if_attr(m, "Codigo_Funcionario", fid)
    _set_if_attr(m, "Fecha", hoy)
    _set_if_attr(m, "Hora_Entrada", ahora)
    _set_if_attr(m, "Entrada", ahora)
    _set_if_attr(m, "Estado", "Pendiente")
    _set_if_attr(m, "Observacion", "Marcación de entrada")
    _set_if_attr(m, "Registrado_Por", _registrado_por())

    db.session.add(m)
    db.session.commit()

    return jsonify({"ok": True, "id": getattr(m, "Id", None)})


@hrm_bp.route("/marcar/salida", methods=["POST"])
def marcar_salida():
    """Completa la marcación de hoy (pone Hora_Salida y calcula horas)."""
    fid = _current_funcionario_id()
    if not fid:
        return jsonify({"ok": False, "error": "No hay colaborador en sesión"}), 401

    ahora = datetime.utcnow()
    hoy = ahora.date()

    # Buscar la última entrada abierta de hoy
    m = (
        Marcacion.query
        .filter(
            Marcacion.Codigo_Funcionario == fid,
            Marcacion.Fecha == hoy,
            or_(
                getattr(Marcacion, "Hora_Salida", None) == None,  # noqa: E711
                getattr(Marcacion, "Salida", None) == None
            )
        )
        .order_by(Marcacion.Id.desc())
        .first()
    )
    if not m:
        return jsonify({"ok": False, "error": "No hay marcación de entrada abierta hoy."}), 400

    # Obtener la hora de entrada (según el nombre que tenga tu modelo)
    dt_in = None
    if hasattr(m, "Hora_Entrada") and m.Hora_Entrada:
        dt_in = m.Hora_Entrada
    elif hasattr(m, "Entrada") and m.Entrada:
        dt_in = m.Entrada

    if not dt_in:
        return jsonify({"ok": False, "error": "La marcación no tiene hora de entrada válida."}), 400

    horas = _calc_hours(dt_in, ahora)

    _set_if_attr(m, "Hora_Salida", ahora)
    _set_if_attr(m, "Salida", ahora)
    _set_if_attr(m, "Horas", horas)
    _set_if_attr(m, "Total_Horas", horas)
    _set_if_attr(m, "Estado", "Pendiente")  # quedará para validación en HU-08-003
    _set_if_attr(m, "Observacion", "Marcación de salida")

    db.session.commit()
    return jsonify({"ok": True, "horas": horas})


@hrm_bp.route("/mis-horas/listado", methods=["GET"])
def mis_horas_listado_json():
    """Listado JSON de marcaciones del mes actual para el colaborador."""
    fid = _current_funcionario_id()
    if not fid:
        return jsonify({"ok": False, "error": "No hay colaborador en sesión"}), 401

    year = request.args.get("y", type=int) or datetime.today().year
    month = request.args.get("m", type=int) or datetime.today().month

    start = date(year, month, 1)
    end = (start.replace(day=28) + timedelta(days=4)).replace(day=1)

    marcas = (
        Marcacion.query
        .filter(
            Marcacion.Codigo_Funcionario == fid,
            Marcacion.Fecha >= start,
            Marcacion.Fecha < end
        )
        .order_by(Marcacion.Fecha.desc(), Marcacion.Id.desc())
        .all()
    )

    def _m_to_dict(m):
        entrada = getattr(m, "Hora_Entrada", None) or getattr(m, "Entrada", None)
        salida = getattr(m, "Hora_Salida", None) or getattr(m, "Salida", None)
        horas  = getattr(m, "Horas", None) or getattr(m, "Total_Horas", None)

        return {
            "Id": getattr(m, "Id", None),
            "Fecha": getattr(m, "Fecha", None).isoformat() if getattr(m, "Fecha", None) else None,
            "Entrada": entrada.isoformat() if entrada else None,
            "Salida":  salida.isoformat() if salida  else None,
            "Horas": float(horas) if horas is not None else None,
            "Estado": getattr(m, "Estado", None),
            "Observacion": getattr(m, "Observacion", None),
        }

    return jsonify({"ok": True, "data": [_m_to_dict(m) for m in marcas]})


