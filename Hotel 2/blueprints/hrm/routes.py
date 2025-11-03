# blueprints/hrm/routes.py
from __future__ import annotations

from datetime import datetime, date, timedelta
from flask import request, jsonify, render_template, session, redirect, url_for
from sqlalchemy import func, or_
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


def _horas_val(m) -> float | None:
    """
    Devuelve horas decimales para una marcación:
    1) Horas
    2) Total_Horas
    3) Horas_Regulares
    4) Calcula con Entrada/Salida si todo es None
    """
    h = (
        getattr(m, "Horas", None)
        or getattr(m, "Total_Horas", None)
        or getattr(m, "Horas_Regulares", None)
    )
    if h is not None:
        try:
            return float(h)
        except Exception:
            return None

    ent = getattr(m, "Hora_Entrada", None) or getattr(m, "Entrada", None)
    sal = getattr(m, "Hora_Salida", None)  or getattr(m, "Salida", None)
    if ent and sal:
        try:
            return round((sal - ent).total_seconds() / 3600.0, 2)
        except Exception:
            return None
    return None


def _estado_render(m) -> str:
    """
    Estado para la UI:
      - 'Abierta' si hay Entrada y NO hay Salida.
      - 'Cerrada' si hay Entrada y hay Salida.
      - 'Pendiente' si no hay nada.
    """
    ent = getattr(m, "Hora_Entrada", None) or getattr(m, "Entrada", None)
    sal = getattr(m, "Hora_Salida", None)  or getattr(m, "Salida", None)
    if ent and not sal:
        return "Abierta"
    if ent and sal:
        return "Cerrada"
    return "Pendiente"


def _get_periodo():
    """
    Devuelve (start:date, end:date_exclusive, label:str) según query params:
      r = 'mes' | '30d' | 'custom'
      start=YYYY-MM-DD  end=YYYY-MM-DD  (solo si r='custom')
    Por defecto: '30d'
    """
    hoy = date.today()
    r = request.args.get("r", default="30d")
    if r == "mes":
        start = hoy.replace(day=1)
        end = (start.replace(day=28) + timedelta(days=4)).replace(day=1)  # 1° del mes siguiente (exclusivo)
        label = "Mes actual"
    elif r == "custom":
        try:
            s = request.args.get("start")
            e = request.args.get("end")
            start = datetime.fromisoformat(s).date() if s else hoy - timedelta(days=30)
            # end exclusivo → sumamos 1 día a la fecha final inclusiva
            end_inclusive = datetime.fromisoformat(e).date() if e else hoy
            end = end_inclusive + timedelta(days=1)
        except Exception:
            start = hoy - timedelta(days=30)
            end = hoy + timedelta(days=1)
        label = "Personalizado"
    else:  # "30d"
        start = hoy - timedelta(days=30)
        end = hoy + timedelta(days=1)
        label = "Últimos 30 días"
    return start, end, label


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
        Fecha_modificacion=datetime.now(),
        Fecha_mod_alta=datetime.now(),
    )

    db.session.add(nuevo)
    db.session.flush()  # obtiene Codigo_Funcionario

    hist = FuncionarioHistorial(
        Codigo_Funcionario=nuevo.Codigo_Funcionario,
        Fecha_Evento=datetime.now(),
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
    f.Fecha_modificacion = datetime.now()
    f.Fecha_mod_alta     = datetime.now()

    db.session.flush()

    for tipo, detalle, val_ant, val_nuevo in cambios:
        db.session.add(FuncionarioHistorial(
            Codigo_Funcionario=f.Codigo_Funcionario,
            Fecha_Evento=datetime.now(),
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
        f.Fecha_modificacion = datetime.now()
        f.Fecha_mod_alta = datetime.now()
        db.session.flush()

        hist = FuncionarioHistorial(
            Codigo_Funcionario=f.Codigo_Funcionario,
            Fecha_Evento=datetime.now(),
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
                        str(payload["Salario_Base_Mensual"])) )
        f.Salario_Base_Mensual = payload["Salario_Base_Mensual"]

    if not cambios:
        return jsonify({"ok": True, "empleado": _funcionario_to_dict(f), "msg": "Sin cambios"})

    f.Fecha_modificacion = datetime.now()
    f.Fecha_mod_alta = datetime.now()
    db.session.flush()

    reg_por = _registrado_por()
    for tipo, detalle, val_ant, val_nuevo in cambios:
        db.session.add(FuncionarioHistorial(
            Codigo_Funcionario=f.Codigo_Funcionario,
            Fecha_Evento=datetime.now(),
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
      - func: forzar funcionario (testing)
      - r: '30d' | 'mes' | 'custom'
      - start=YYYY-MM-DD, end=YYYY-MM-DD  (si r='custom')
    """
    fid = _current_funcionario_id()
    if not fid:
        return redirect(url_for("hrm.empleados_ui"))

    start, end, _label = _get_periodo()
    hoy_local = date.today()

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

    actual = next((m for m in marcas if getattr(m, "Fecha", None) == hoy_local), None)

    total_horas = 0.0
    for m in marcas:
        hv = _horas_val(m)
        if hv is not None:
            total_horas += hv

    return render_template(
        "mis_horas.html",
        empleado=Funcionario.query.get(fid),
        actual=actual,
        items=marcas,
        total_horas=round(total_horas, 2),
        horas_val=_horas_val,
        estado_render=_estado_render,
        # Para el selector:
        rango_sel=request.args.get("r", "30d"),
        start_sel=start,  # usados para precargar inputs
        end_sel=(end - timedelta(days=1))  # fin inclusivo para UI
    )


@hrm_bp.route("/marcar/entrada", methods=["POST"])
def marcar_entrada():
    """Crea la marcación de entrada del día si no existe una abierta."""
    fid = _current_funcionario_id()
    if not fid:
        return jsonify({"ok": False, "error": "No hay colaborador en sesión"}), 401

    ahora = datetime.now()     # Hora local
    hoy = ahora.date()

    # ¿Ya hay una marcación abierta (sin salida) hoy?
    abierto = (
    Marcacion.query
    .filter(
        Marcacion.Codigo_Funcionario == fid,
        Marcacion.Fecha == hoy,
        Marcacion.Hora_Salida.is_(None)  # jornada aún sin salida
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
    _set_if_attr(m, "Observaciones", "Marcación de entrada")  # compat.
    _set_if_attr(m, "Registrado_Por", _registrado_por())

    db.session.add(m)
    db.session.commit()

    return jsonify({"ok": True, "id": getattr(m, "Id", None)})


@hrm_bp.route("/marcar/salida", methods=["POST"])
def marcar_salida():
    """Completa la marcación de hoy (pone Hora_Salida, calcula horas y registra horas extra)."""
    fid = _current_funcionario_id()
    if not fid:
        return jsonify({"ok": False, "error": "No hay colaborador en sesión"}), 401

    ahora = datetime.now()
    hoy = ahora.date()

    # Buscar la última entrada abierta de hoy
    m = (
        Marcacion.query
        .filter(
            Marcacion.Codigo_Funcionario == fid,
            Marcacion.Fecha == hoy,
            Marcacion.Hora_Salida.is_(None)  # jornada aún abierta
        )
        .order_by(Marcacion.Id.desc())
        .first()
    )

    if not m:
        return jsonify({"ok": False, "error": "No hay marcación de entrada abierta hoy."}), 400

    # Validar hora de entrada
    dt_in = getattr(m, "Hora_Entrada", None)
    if not dt_in:
        return jsonify({"ok": False, "error": "La marcación no tiene hora de entrada válida."}), 400

    # Calcular horas totales (en decimales)
    delta = (ahora - dt_in).total_seconds() / 3600
    horas_trab = round(delta, 2)

    # Actualizar marcación
    m.Hora_Salida = ahora
    m.Horas = horas_trab
    m.Total_Horas = horas_trab
    m.Horas_Regulares = horas_trab
    m.Estado = "Pendiente"
    m.Observaciones = "Marcación de salida"

    db.session.commit()

    # === HRM-08-004: Registrar horas extra si superan 8 ===
    extras = 0
    if horas_trab > 8:
        extras = round(horas_trab - 8, 2)
        nueva_extra = HoraExtra(
            Marcacion_Id=m.Id,
            Codigo_Funcionario=m.Codigo_Funcionario,
            Fecha=m.Fecha,
            Horas_Extras=extras,
            Motivo="Jornada superior a 8 horas",
            Estado="Pendiente",
            Validado_Por=None
        )
        db.session.add(nueva_extra)
        db.session.commit()

    return jsonify({
        "ok": True,
        "horas_regulares": horas_trab,
        "horas_extra": extras
    })


@hrm_bp.route("/mis-horas/listado", methods=["GET"])
def mis_horas_listado_json():
    """Listado JSON de marcaciones del periodo actual para el colaborador."""
    fid = _current_funcionario_id()
    if not fid:
        return jsonify({"ok": False, "error": "No hay colaborador en sesión"}), 401

    start, end, _ = _get_periodo()

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
        salida  = getattr(m, "Hora_Salida", None)  or getattr(m, "Salida", None)
        obs     = getattr(m, "Observacion", None) or getattr(m, "Observaciones", None)
        return {
            "Id": getattr(m, "Id", None),
            "Fecha": getattr(m, "Fecha", None).isoformat() if getattr(m, "Fecha", None) else None,
            "Entrada": entrada.isoformat() if entrada else None,
            "Salida":  salida.isoformat() if salida  else None,
            "Horas": _horas_val(m),
            "Estado": _estado_render(m),
            "Observacion": obs,
        }

    return jsonify({"ok": True, "data": [_m_to_dict(m) for m in marcas]})


# =============================================================================
# ALIAS para compatibilidad con front antiguo (/mis-horas/entrada|salida)
# =============================================================================
@hrm_bp.route("/mis-horas/entrada", methods=["POST"])
def _alias_mis_horas_entrada():
    return marcar_entrada()

@hrm_bp.route("/mis-horas/salida", methods=["POST"])
def _alias_mis_horas_salida():
    return marcar_salida()


# =============================================================================
# HU-08-003 — Validación / Corrección de horas por el jefe inmediato
# =============================================================================
def _get_dt(obj, name1, name2=None):
    """Devuelve datetime de m.name1 o (si no existe/viene nulo) m.name2."""
    v = getattr(obj, name1, None)
    if (v is None) and name2:
        v = getattr(obj, name2, None)
    return v

def _calc_and_set_hours(m):
    """Recalcula las horas y las setea en los campos disponibles."""
    dt_in = _get_dt(m, "Hora_Entrada", "Entrada")
    dt_out = _get_dt(m, "Hora_Salida", "Salida")
    if not dt_in or not dt_out:
        return None
    horas = _calc_hours(dt_in, dt_out)
    for name in ("Horas", "Total_Horas", "Horas_Regulares"):
        _set_if_attr(m, name, horas)
    return horas

def _es_jefe_y_departamento():
    """Lee sesión y devuelve (es_jefe:bool, depto:str|None). Ajusta a tu manejo real de roles."""
    rol = session.get("Rol") or session.get("rol")
    dpt = session.get("Departamento") or session.get("departamento")
    return (str(rol).lower() == "jefe", dpt)

def _filtro_equipo_query():
    """
    Devuelve un query base de marcaciones 'del equipo' del jefe.
    Reglas:
      - Si el usuario es 'jefe' y tiene Departamento en sesión: filtra por ese departamento.
      - Si viene ?dep=... usa ese dep.
      - Si viene ?func=ID, filtra por ese funcionario.
      - Si no, muestra TODO (útil para admin).
    """
    dep_qs = request.args.get("dep")
    func_id = request.args.get("func", type=int)

    es_jefe, dep_sesion = _es_jefe_y_departamento()
    dep_final = dep_qs or (dep_sesion if es_jefe else None)

    q = db.session.query(Marcacion, Funcionario).join(
        Funcionario, Funcionario.Codigo_Funcionario == Marcacion.Codigo_Funcionario
    )

    if func_id:
        q = q.filter(Marcacion.Codigo_Funcionario == func_id)
    elif dep_final:
        q = q.filter(Funcionario.Departamento == dep_final)

    return q

@hrm_bp.route("/val-horas", methods=["GET"])
def validar_horas_ui():
    """
    UI del jefe para validar/corregir horas.
    Parámetros:
      - y (año), m (mes)
      - dep (departamento), func (id), estado (Pendiente/Aprobado/Rechazado/todos)
    """
    year  = request.args.get("y", type=int) or datetime.today().year
    month = request.args.get("m", type=int) or datetime.today().month
    estado = request.args.get("estado", default="Pendiente")

    start = date(year, month, 1)
    end   = (start.replace(day=28) + timedelta(days=4)).replace(day=1)

    q = _filtro_equipo_query().filter(
        Marcacion.Fecha >= start,
        Marcacion.Fecha < end
    )

    if estado.lower() != "todos":
        q = q.filter((Marcacion.Estado == estado))

    q = q.order_by(Marcacion.Fecha.desc(), Marcacion.Id.desc())
    filas = q.all()

    # Totales
    total_pend = 0
    total_horas = 0.0
    for m, _f in filas:
        if getattr(m, "Estado", None) == "Pendiente":
            total_pend += 1
        hv = _horas_val(m)
        if hv is not None:
            total_horas += hv

    return render_template(
        "validar_horas.html",
        filas=filas,
        horas_val=_horas_val,
        year=year,
        month=month,
        estado=estado,
        total_pend=total_pend,
        total_horas=round(total_horas, 2)
    )

def _set_dt_field(obj, name, value_str):
    """
    Setea un campo datetime desde string ISO o 'YYYY-MM-DD HH:MM' en el atributo si existe.
    Si value_str vacío/None, no toca nada.
    """
    if not value_str:
        return
    try:
        value_str = value_str.replace("T", " ")
        dt = datetime.fromisoformat(value_str)
    except Exception:
        return
    _set_if_attr(obj, name, dt)

@hrm_bp.route("/marcaciones/<int:mid>/ajustar", methods=["PATCH"])
def ajustar_marcacion(mid):
    """
    Ajusta entrada/salida/horas y deja estado en 'Pendiente' (para asegurar validación).
    JSON esperado (todos opcionales):
      {
        "Entrada": "YYYY-MM-DD HH:MM",
        "Salida":  "YYYY-MM-DD HH:MM",
        "Horas": 7.5,
        "Observacion": "texto opcional"
      }
    Si se envían Entrada/Salida, se recalcula Horas. Si además se manda Horas, esta prevalece.
    """
    payload = request.get_json(silent=True) or {}
    m = Marcacion.query.get(mid)
    if not m:
        return jsonify({"ok": False, "error": "Marcación no encontrada"}), 404

    # Entrada/Salida (compatibles con nombres dobles)
    ent = payload.get("Entrada")
    sal = payload.get("Salida")

    if ent:
        _set_dt_field(m, "Hora_Entrada", ent)
        _set_dt_field(m, "Entrada", ent)
    if sal:
        _set_dt_field(m, "Hora_Salida", sal)
        _set_dt_field(m, "Salida", sal)

    horas_payload = payload.get("Horas")
    if (ent or sal) and (horas_payload is None):
        horas_calc = _calc_and_set_hours(m)
    else:
        horas_calc = None

    if horas_payload is not None:
        try:
            horas_f = float(horas_payload)
            for name in ("Horas", "Total_Horas", "Horas_Regulares"):
                _set_if_attr(m, name, horas_f)
            horas_calc = horas_f
        except Exception:
            pass

    # Observación y estado
    obs = payload.get("Observacion")
    if obs is not None:
        _set_if_attr(m, "Observacion", obs)
        _set_if_attr(m, "Observaciones", obs)

    _set_if_attr(m, "Estado", "Pendiente")
    _set_if_attr(m, "Validado_Por", None)

    db.session.commit()
    return jsonify({"ok": True, "horas": horas_calc})

@hrm_bp.route("/marcaciones/<int:mid>/ajustar-aprobar", methods=["PATCH"])
def ajustar_y_aprobar(mid):
    """
    Ajusta entrada/salida/horas y APRUEBA en un solo paso.
    JSON esperado (opcionales):
      {
        "Entrada": "YYYY-MM-DD HH:MM",
        "Salida":  "YYYY-MM-DD HH:MM",
        "Horas": 7.5,
        "Observacion": "nota visible al colaborador"
      }
    """
    payload = request.get_json(silent=True) or {}
    m = Marcacion.query.get(mid)
    if not m:
        return jsonify({"ok": False, "error": "Marcación no encontrada"}), 404

    ent = payload.get("Entrada")
    sal = payload.get("Salida")
    if ent:
        _set_dt_field(m, "Hora_Entrada", ent)
        _set_dt_field(m, "Entrada", ent)
    if sal:
        _set_dt_field(m, "Hora_Salida", sal)
        _set_dt_field(m, "Salida", sal)

    horas_payload = payload.get("Horas")
    if (ent or sal) and (horas_payload is None):
        _calc_and_set_hours(m)

    if horas_payload is not None:
        try:
            horas_f = float(horas_payload)
            for name in ("Horas", "Total_Horas", "Horas_Regulares"):
                _set_if_attr(m, name, horas_f)
        except Exception:
            pass

    obs = payload.get("Observacion")
    if obs is not None:
        _set_if_attr(m, "Observacion", obs)
        _set_if_attr(m, "Observaciones", obs)

    _set_if_attr(m, "Estado", "Aprobado")
    _set_if_attr(m, "Validado_Por", _registrado_por())

    try:
        db.session.add(FuncionarioHistorial(
            Codigo_Funcionario = m.Codigo_Funcionario,
            Fecha_Evento       = datetime.now(),
            Tipo_Evento        = "Ajuste horas",
            Detalle            = f"Ajuste/aprobación de marcación #{getattr(m,'Id',None)}",
            Valor_Anterior     = None,
            Valor_Nuevo        = f"Horas={(getattr(m,'Horas',None) or getattr(m,'Total_Horas',None) or getattr(m,'Horas_Regulares',None))}; Obs={obs or ''}",
            Registrado_Por     = _registrado_por()
        ))
    except Exception:
        pass

    db.session.commit()
    return jsonify({"ok": True})

@hrm_bp.route("/marcaciones/<int:mid>/aprobar", methods=["PATCH"])
def aprobar_marcacion(mid):
    payload = request.get_json(silent=True) or {}
    m = Marcacion.query.get(mid)
    if not m:
        return jsonify({"ok": False, "error": "Marcación no encontrada"}), 404

    _set_if_attr(m, "Estado", "Aprobado")
    obs = payload.get("Observacion")
    if obs is not None:
        _set_if_attr(m, "Observacion", obs)
        _set_if_attr(m, "Observaciones", obs)
    _set_if_attr(m, "Validado_Por", _registrado_por())
    db.session.commit()
    return jsonify({"ok": True})

@hrm_bp.route("/marcaciones/<int:mid>/rechazar", methods=["PATCH"])
def rechazar_marcacion(mid):
    payload = request.get_json(silent=True) or {}
    obs = payload.get("Observacion")
    if not obs:
        return jsonify({"ok": False, "error": "Observación requerida para rechazar"}), 400

    m = Marcacion.query.get(mid)
    if not m:
        return jsonify({"ok": False, "error": "Marcación no encontrada"}), 404

    _set_if_attr(m, "Estado", "Rechazado")
    _set_if_attr(m, "Observacion", obs)
    _set_if_attr(m, "Observaciones", obs)
    _set_if_attr(m, "Validado_Por", _registrado_por())
    db.session.commit()
    return jsonify({"ok": True})

@hrm_bp.route("/marcaciones/aprobar-lote", methods=["POST"])
def aprobar_lote():
    """
    Aprueba en lote todas las marcaciones 'Pendiente' del filtro seleccionado.
    JSON opcional:
      {
        "func": 123,
        "dep": "Recepción",
        "y": 2025, "m": 11
      }
    """
    payload = request.get_json(silent=True) or {}
    func_id = payload.get("func") or request.args.get("func", type=int)
    dep     = payload.get("dep")  or request.args.get("dep")

    year  = payload.get("y") or request.args.get("y", type=int) or datetime.today().year
    month = payload.get("m") or request.args.get("m", type=int) or datetime.today().month

    start = date(int(year), int(month), 1)
    end   = (start.replace(day=28) + timedelta(days=4)).replace(day=1)

    q = db.session.query(Marcacion).join(
        Funcionario, Funcionario.Codigo_Funcionario == Marcacion.Codigo_Funcionario
    ).filter(
        Marcacion.Fecha >= start,
        Marcacion.Fecha < end,
        Marcacion.Estado == "Pendiente"
    )

    if func_id:
        q = q.filter(Marcacion.Codigo_Funcionario == func_id)
    if dep:
        q = q.filter(Funcionario.Departamento == dep)

    count = 0
    val_por = _registrado_por()
    for m in q.all():
        _set_if_attr(m, "Estado", "Aprobado")
        _set_if_attr(m, "Validado_Por", val_por)
        count += 1

    db.session.commit()
    return jsonify({"ok": True, "aprobadas": count})

