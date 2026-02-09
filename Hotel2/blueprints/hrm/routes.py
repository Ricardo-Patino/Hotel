# blueprints/hrm/routes.py
from __future__ import annotations

from datetime import datetime, date, timedelta
from io import BytesIO
from flask import request, jsonify, render_template, session, redirect, url_for, render_template, request, jsonify, send_file
from sqlalchemy import func, or_, text
from extensions import db
from . import hrm_bp
from datetime import datetime
from models import Funcionario

from models.usuario import Usuario
from models.ausencia import Ausencia
from models.amonestacion import Amonestacion
from utils.decorators import require_user_id
from decimal import Decimal

from flask import send_file
from io import BytesIO
from decimal import Decimal, ROUND_HALF_UP
from datetime import datetime

from reportlab.lib.pagesizes import A4
from reportlab.lib import colors
from reportlab.lib.units import cm
from reportlab.platypus import (
    SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle, HRFlowable
)
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.enums import TA_LEFT, TA_CENTER, TA_RIGHT



#from models import HoraExtra

# Modelos principales HRM
from models import Funcionario, FuncionarioHistorial
# Modelo de marcaciones (HU-08-002)
from models import Marcacion

from functools import wraps
from flask import abort

def require_roles(*roles):
    def decorator(f):
        @wraps(f)
        def wrapper(*args, **kwargs):
            rol = session.get("user_role")
            if rol not in roles:
                return abort(403)
            return f(*args, **kwargs)
        return wrapper
    return decorator


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
    - 1) session["Codigo_Funcionario"]
    - 2) si viene ?func=ID (testing)
    - 3) si hay session["user_id"], busca Funcionario por Codigo_Usuario y lo guarda en sesión
    """
    fid = session.get("Codigo_Funcionario")
    if fid:
        try:
            return int(fid)
        except Exception:
            pass

    fid_qs = request.args.get("func", type=int)
    if fid_qs:
        session["Codigo_Funcionario"] = int(fid_qs)
        return int(fid_qs)

    uid = session.get("user_id")
    if uid:
        funci = Funcionario.query.filter_by(Codigo_Usuario=uid).first()
        if funci:
            session["Codigo_Funcionario"] = int(funci.Codigo_Funcionario)
            session["Departamento"] = funci.Departamento
            return int(funci.Codigo_Funcionario)

    return None





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
@require_roles("Limpieza")
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


from datetime import datetime

@hrm_bp.route("/marcar/entrada", methods=["POST"])
@require_roles("Limpieza")
def marcar_entrada():
    fid = _current_funcionario_id()
    if not fid:
        return jsonify({"ok": False, "error": "La entrada no fue registrada, ingrese al sistema y haga su marca"}), 401

    ahora = datetime.now()
    hoy = ahora.date()

    abierto = (
        Marcacion.query
        .filter(
            Marcacion.Codigo_Funcionario == fid,
            Marcacion.Fecha == hoy,
            Marcacion.Hora_Salida.is_(None)
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
    _set_if_attr(m, "Observaciones", "Marcación de entrada")
    _set_if_attr(m, "Registrado_Por", _registrado_por())

    # ✅ ESTAS DOS LÍNEAS SON LA CLAVE
    _set_if_attr(m, "Fecha_Creacion", ahora)
    _set_if_attr(m, "Fecha_Actualiza", ahora)

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

    m = (
        Marcacion.query
        .filter(
            Marcacion.Codigo_Funcionario == fid,
            Marcacion.Fecha == hoy,
            Marcacion.Hora_Salida.is_(None)
        )
        .first()
    )

    if not m:
        return jsonify({"ok": False, "error": "No hay entrada abierta hoy"}), 400

    dt_in = m.Hora_Entrada
    horas = round((ahora - dt_in).total_seconds() / 3600, 2)

    m.Hora_Salida = ahora
    m.Horas = horas
    m.Total_Horas = horas
    m.Horas_Regulares = horas
    m.Estado = "Pendiente"
    m.Observaciones = "Marcación de salida"

    db.session.commit()

    return jsonify({"ok": True, "horas": horas})



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
@require_user_id(1)
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
            _set_if_attr(m, "Horas_Validadas", horas_f)
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

    if getattr(m, "Horas_Validadas", None) is None:
        hv = _horas_val(m)
        if hv is not None:
            _set_if_attr(m, "Horas_Validadas", hv)

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

def q(sql, **params):
    return db.session.execute(text(sql), params).mappings().all()
def exec_(sql, **params):
    db.session.execute(text(sql), params); db.session.commit()

    # ================= HRM-UI =================

@hrm_bp.get("/dashboard")
def dashboard():
    # mini-resumen para tarjetas
    resumen = q("""
        SELECT 
          (SELECT COUNT(*) 
             FROM Funcionario 
            WHERE Estado_Empleado='Activo') AS activos,

          -- dejamos esto aunque no se use ahora
          (SELECT COUNT(*) 
             FROM HRM_VacationRequest 
            WHERE Estado='PENDIENTE') AS vac_pend,

          -- TOTAL de amonestaciones registradas
          (SELECT COUNT(*) 
             FROM HRM_Warning) AS amon_pend,

          -- TOTAL de ausencias registradas
          (SELECT COUNT(*) 
             FROM HRM_Absence) AS aus_hoy
    """)[0]

    periodos = q("""
        SELECT * 
        FROM HRM_PayrollPeriod 
        ORDER BY Fecha_Desde DESC 
        LIMIT 6
    """)

    return render_template(
        "hrm-dashboard.html",
        resumen=resumen,
        periodos=periodos
    )


# =============== HRM-08-005 ===============
@hrm_bp.post("/absences")
def add_absence():
    payload = request.get_json(silent=True) or {}
    exec_("""
      INSERT INTO HRM_Absence (Codigo_Funcionario, Fecha, Horas, Motivo, Registrado_Por)
      VALUES (:f,:fe,:h,:m,:u)
    """, f=payload["func"], fe=payload["fecha"], h=payload.get("horas"), m=payload.get("motivo"), u=payload.get("user_id"))
    return jsonify({"ok": True})

# =============== HRM-08-006 ===============
@hrm_bp.post("/vacations")
def create_vacation():
    p = request.get_json(silent=True) or {}
    exec_("""
      INSERT INTO HRM_VacationRequest (Codigo_Funcionario, Fecha_Desde, Fecha_Hasta, Dias, Estado, Comentario)
      VALUES (:f,:d,:h,:dias,'PENDIENTE',:c)
    """, f=p["func"], d=p["desde"], h=p["hasta"], dias=p["dias"], c=p.get("comentario"))
    return jsonify({"ok": True})

@hrm_bp.post("/vacations/<int:vac_id>/approve")
def approve_vacation(vac_id):
    # aprueba y notifica por email vía SAC_Outbox
    vac = q("SELECT v.*, f.Nombre, f.Apellido FROM HRM_VacationRequest v JOIN Funcionario f ON f.Codigo_Funcionario=v.Codigo_Funcionario WHERE v.Id=:id", id=vac_id)[0]
    exec_("""UPDATE HRM_VacationRequest SET Estado='APROBADA', Aprobado_Por=:u, Aprobado_En=NOW() WHERE Id=:id""", u=1, id=vac_id)
    # usa SAC_Outbox para notificar (si hay email en Usuario vinculado o en otra tabla; aquí ejemplo simple)
    exec_("""
      INSERT INTO SAC_Outbox (Canal, Para, Asunto, Cuerpo, Estado, Programado_At, Ref_Entidad, Ref_Id)
      VALUES ('email', :para, 'Vacaciones aprobadas', 
        :cuerpo, 'PENDIENTE', NOW(), 'HRM_Vacation', :id)
    """, para='recepcion@hotel.test',
         cuerpo=f"Estimado/a {vac['Nombre']} {vac['Apellido']}, su solicitud de vacaciones del {vac['Fecha_Desde']} al {vac['Fecha_Hasta']} ha sido APROBADA.",
         id=vac_id)
    return jsonify({"ok": True})

@hrm_bp.post("/vacations/<int:vac_id>/reject")
def reject_vacation(vac_id):
    p = request.get_json(silent=True) or {}
    exec_("""UPDATE HRM_VacationRequest SET Estado='RECHAZADA', Aprobado_Por=:u, Aprobado_En=NOW(), Comentario=:c WHERE Id=:id""",
          u=1, c=p.get("comentario",""), id=vac_id)
    exec_("""
      INSERT INTO SAC_Outbox (Canal, Para, Asunto, Cuerpo, Estado, Programado_At, Ref_Entidad, Ref_Id)
      VALUES ('email', :para, 'Vacaciones rechazadas', 
        :cuerpo, 'PENDIENTE', NOW(), 'HRM_Vacation', :id)
    """, para='recepcion@hotel.test',
         cuerpo=f"Su solicitud de vacaciones fue RECHAZADA. Motivo: {p.get('comentario','')}", id=vac_id)
    return jsonify({"ok": True})

@hrm_bp.post("/warnings")
def create_warning():
    p = request.get_json(silent=True) or {}
    # registrar
    exec_("""
      INSERT INTO HRM_Warning (Codigo_Funcionario, Severidad, Motivo, Estado, Registrada_Por)
      VALUES (:f,:s,:m,'REGISTRADA',:u)
    """, f=p["func"], s=p.get("sev","LEVE"), m=p["motivo"], u=p.get("user_id"))
    # notificar
    func = q("SELECT Nombre, Apellido FROM Funcionario WHERE Codigo_Funcionario=:f", f=p["func"])[0]
    exec_("""
      INSERT INTO SAC_Outbox (Canal, Para, Asunto, Cuerpo, Estado, Programado_At, Ref_Entidad)
      VALUES ('email','recepcion@hotel.test','Amonestación registrada',
        :cuerpo,'PENDIENTE',NOW(),'HRM_Warning')
    """, cuerpo=f"Se registró una amonestación ({p.get('sev','LEVE')}) para {func['Nombre']} {func['Apellido']}: {p['motivo']}")
    return jsonify({"ok": True})

# =============== HRM-08-007 ===============
@hrm_bp.post("/config/social")
def save_social():
    p = request.get_json(silent=True) or {}
    exec_("""
      INSERT INTO HRM_ConfigSocial (Vigente_Desde, CCSS_Pct, IVM_Pct, BP_Pct, Renta_Pct, Activa)
      VALUES (:v, :ccss, :ivm, :bp, :renta, 1)
      ON DUPLICATE KEY UPDATE CCSS_Pct=:ccss, IVM_Pct=:ivm, BP_Pct=:bp, Renta_Pct=:renta, Activa=1
    """, v=p.get("vigente_desde", str(date.today().replace(day=1))),
         ccss=p.get("ccss",5.50), ivm=p.get("ivm",2.67), bp=p.get("bp",1.0), renta=p.get("renta",10.0))
    return jsonify({"ok": True})

# =============== HRM-08-008 ===============
@hrm_bp.post("/voluntary")
def add_voluntary():
    p = request.get_json(silent=True) or {}
    exec_("""
      INSERT INTO HRM_VoluntaryDed (Codigo_Funcionario, Tipo, Monto_Fijo, Porcentaje, Activa, Observacion)
      VALUES (:f,:t,:m,:p,1,:o)
    """, f=p["func"], t=p["tipo"], m=p.get("monto"), p=p.get("porcentaje"), o=p.get("obs"))
    return jsonify({"ok": True})

# =============== HRM-08-009 ===============
@hrm_bp.post("/incapacity")
def add_incapacity():
    p = request.get_json(silent=True) or {}
    exec_("""
      INSERT INTO HRM_Incapacity (Codigo_Funcionario, Fuente, Porcentaje_Rebajo, Fecha_Desde, Fecha_Hasta, Comprobante, Observacion)
      VALUES (:f,:fuente,:pct,:d,:h,:c,:o)
    """, f=p["func"], fuente=p["fuente"], pct=p["porcentaje"], d=p["desde"], h=p["hasta"], c=p.get("comp"), o=p.get("obs"))
    return jsonify({"ok": True})

# =============== HRM-08-010 ===============
@hrm_bp.post("/bank")
def add_bank():
    p = request.get_json(silent=True) or {}
    if p.get("predeterminada"):
        # desactiva otras predeterminadas del colaborador
        exec_("UPDATE HRM_BankAccount SET Predeterminada=0 WHERE Codigo_Funcionario=:f", f=p["func"])
    exec_("""
      INSERT INTO HRM_BankAccount (Codigo_Funcionario, Banco, IBAN, Predeterminada, Activa)
      VALUES (:f,:b,:i, :pre, 1)
    """, f=p["func"], b=p["banco"], i=p["iban"], pre=1 if p.get("predeterminada") else 0)
    return jsonify({"ok": True})

# =============== HRM-08-011/012 ===============
@hrm_bp.get("/payroll")
def payroll_home():
    periods = q("SELECT * FROM HRM_PayrollPeriod ORDER BY Fecha_Desde DESC")
    return render_template("hrm-payroll.html", periods=periods)

@hrm_bp.post("/payroll/period")
def create_period():
    p = request.get_json(silent=True) or {}
    exec_("""
      INSERT INTO HRM_PayrollPeriod (Periodo_Key, Quincena, Fecha_Desde, Fecha_Hasta, Estado)
      VALUES (:pk, :q, :d, :h, 'ABIERTO')
      ON DUPLICATE KEY UPDATE Fecha_Desde=:d, Fecha_Hasta=:h
    """, pk=p["periodo_key"], q=p["quincena"], d=p["desde"], h=p["hasta"])
    return jsonify({"ok": True})

# =============================
# Helpers renta (CR, salario 2026)
# =============================

def _d(x) -> Decimal:
    """Decimal seguro"""
    if x is None:
        return Decimal("0")
    try:
        return Decimal(str(x))
    except Exception:
        return Decimal("0")

def _get_int(dct, *keys, default=0) -> int:
    for k in keys:
        if k in dct and dct[k] is not None and str(dct[k]) != "":
            try:
                return int(dct[k])
            except Exception:
                pass
    return default

def _get_bool(dct, *keys, default=False) -> bool:
    for k in keys:
        if k in dct and dct[k] is not None:
            v = dct[k]
            if isinstance(v, bool):
                return v
            s = str(v).strip().lower()
            if s in ("1", "true", "t", "si", "sí", "yes", "y"):
                return True
            if s in ("0", "false", "f", "no", "n"):
                return False
    return default

def calc_renta_salario_mensual_2026(base_gravable_mensual: Decimal,
                                   hijos: int = 0,
                                   conyuge: bool = False) -> Decimal:
    """
    Impuesto al salario CR (mensual) por tramos 2026.
    Tramos (mensual):
      - Hasta 918,000: 0%
      - 918,000 a 1,347,000: 10%
      - 1,347,000 a 2,364,000: 15%
      - 2,364,000 a 4,727,000: 20%
      - Exceso de 4,727,000: 25%
    Créditos (mensual): 1,710 por hijo; 2,590 por cónyuge.
    """
    x = base_gravable_mensual
    if x <= 0:
        return Decimal("0.00")

    # Límites 2026 (CRC)
    t0 = Decimal("918000")
    t1 = Decimal("1347000")
    t2 = Decimal("2364000")
    t3 = Decimal("4727000")

    impuesto = Decimal("0")

    # tramo 10%
    if x > t0:
        base = min(x, t1) - t0
        if base > 0:
            impuesto += base * Decimal("0.10")

    # tramo 15%
    if x > t1:
        base = min(x, t2) - t1
        if base > 0:
            impuesto += base * Decimal("0.15")

    # tramo 20%
    if x > t2:
        base = min(x, t3) - t2
        if base > 0:
            impuesto += base * Decimal("0.20")

    # tramo 25%
    if x > t3:
        base = x - t3
        if base > 0:
            impuesto += base * Decimal("0.25")

    # créditos fiscales (mensual)
    cred_hijo = Decimal("1710")   # por hijo
    cred_cony = Decimal("2590")   # por cónyuge

    creditos = (Decimal(hijos) * cred_hijo) + (cred_cony if conyuge else Decimal("0"))

    impuesto = impuesto - creditos
    if impuesto < 0:
        impuesto = Decimal("0")

    return impuesto.quantize(Decimal("0.01"))


from flask import request, jsonify
from decimal import Decimal
from datetime import date

# ==========================================================
# Helpers seguros
# ==========================================================
def _get_int(row, *keys, default=0):
    for k in keys:
        if k in row and row[k] is not None and str(row[k]).strip() != "":
            try:
                return int(row[k])
            except Exception:
                pass
    return default

def _get_bool(row, *keys, default=False):
    for k in keys:
        if k in row and row[k] is not None:
            v = row[k]
            if isinstance(v, bool):
                return v
            s = str(v).strip().lower()
            if s in ("1", "true", "t", "si", "sí", "yes", "y"):
                return True
            if s in ("0", "false", "f", "no", "n"):
                return False
    return default

def _safe_decimal(x, default="0.00"):
    try:
        if x is None or x == "":
            return Decimal(default)
        return Decimal(str(x))
    except Exception:
        return Decimal(default)

# ==========================================================
# Renta por tramos (motor genérico)
# Espera tramos con: Desde, Hasta (nullable), Tarifa_Pct
# ==========================================================
def calc_renta_por_tramos(base_m, tramos):
    base_m = _safe_decimal(base_m)
    impuesto = Decimal("0.00")

    for t in tramos:
        desde = _safe_decimal(t.get("Desde", 0))
        hasta_raw = t.get("Hasta", None)
        tasa = _safe_decimal(t.get("Tarifa_Pct", 0)) / Decimal("100")

        if base_m <= desde:
            continue

        if hasta_raw is None or str(hasta_raw).strip() == "":
            gravable = base_m - desde
        else:
            hasta = _safe_decimal(hasta_raw)
            gravable = min(base_m, hasta) - desde

        if gravable > 0:
            impuesto += (gravable * tasa)

    return impuesto.quantize(Decimal("0.01"))

# ==========================================================
# Renta mensual (CR) - por tramos desde DB (recomendado)
# - Si NO hay tabla o no hay tramos vigentes -> retorna 0
#   y luego el endpoint puede aplicar fallback % si querés.
# ==========================================================
def calc_renta_salario_mensual_2026(base_gravable_m, hijos=0, conyuge=False, vigencia=None):
    vig = vigencia or date.today()

    # 1) Intentar cargar tramos desde una tabla (si existe)
    tramos = []
    try:
        # 👉 Si NO tenés esta tabla, no pasa nada: cae al except.
        # Estructura sugerida:
        # HRM_RentaBracket(Desde, Hasta, Tarifa_Pct, Vigente_Desde, Vigente_Hasta)
        tramos = q("""
            SELECT Desde, Hasta, Tarifa_Pct
            FROM HRM_RentaBracket
            WHERE (Vigente_Desde IS NULL OR Vigente_Desde <= :v)
              AND (Vigente_Hasta IS NULL OR Vigente_Hasta >= :v)
            ORDER BY Desde ASC
        """, v=vig) or []
    except Exception:
        tramos = []

    impuesto = Decimal("0.00")
    if tramos:
        impuesto = calc_renta_por_tramos(base_gravable_m, tramos)

    # 2) Créditos (si en tu sistema NO los manejás, dejalos en 0)
    # Si algún día los querés: podés leerlos de una tabla de config.
    credito_hijo = Decimal("0.00")
    credito_conyuge = Decimal("0.00")

    impuesto = impuesto - (credito_hijo * Decimal(int(hijos or 0))) - (credito_conyuge if conyuge else Decimal("0.00"))
    if impuesto < 0:
        impuesto = Decimal("0.00")

    return impuesto.quantize(Decimal("0.01"))


# ==========================================================
# ENDPOINT: Generar nómina (con recalcular force)
# Body:
#  { "period_id": 123, "force": true/false }
# ==========================================================
@hrm_bp.post("/payroll/generate")
def payroll_generate():
    p = request.get_json(silent=True) or {}

    if "period_id" not in p:
        return jsonify({"ok": False, "error": "Falta period_id"}), 400

    period_id = p["period_id"]
    force = bool(p.get("force", False))

    period_rows = q(
        "SELECT * FROM HRM_PayrollPeriod WHERE Id=:id",
        id=period_id
    )
    if not period_rows:
        return jsonify({"ok": False, "error": "Periodo no existe"}), 404

    period = period_rows[0]
    estado = (period.get("Estado") or "").upper()

    # ---- Reglas de seguridad ----
    if estado in ("PAGADO", "CERRADO"):
        return jsonify({"ok": False, "error": f"No se puede recalcular: periodo {estado}."}), 409

    # Si NO es force, solo permitir calcular cuando está ABIERTO
    if (not force) and estado != "ABIERTO":
        return jsonify({
            "ok": False,
            "error": f"El periodo debe estar ABIERTO para calcular. Estado actual: {estado}. "
                     f"Use force=true para recalcular si está CALCULADO."
        }), 409

    # ---- Si force=true: borrar nómina previa del periodo ----
    if force:
        # Si existe HRM_PaymentRecord, borrarlo primero para no quedar colgando
        try:
            exec_("""
                DELETE FROM HRM_PaymentRecord
                WHERE Payroll_Id IN (SELECT Id FROM HRM_Payroll WHERE Period_Id = :pid)
            """, pid=period_id)
        except Exception:
            pass

        exec_("DELETE FROM HRM_Payroll WHERE Period_Id = :pid", pid=period_id)
        exec_("UPDATE HRM_PayrollPeriod SET Estado='ABIERTO' WHERE Id=:pid", pid=period_id)

        # recargar periodo
        period = q("SELECT * FROM HRM_PayrollPeriod WHERE Id=:id", id=period_id)[0]

    # Configuración social vigente
    cfg_rows = q("""
        SELECT *
        FROM HRM_ConfigSocial
        WHERE Activa = 1
          AND Vigente_Desde <= :h
        ORDER BY Vigente_Desde DESC
        LIMIT 1
    """, h=period["Fecha_Hasta"])
    if not cfg_rows:
        return jsonify({"ok": False, "error": "No hay configuración social vigente"}), 400

    cfg = cfg_rows[0]

    funcs = q("""
        SELECT *
        FROM Funcionario
        WHERE Estado_Empleado = 'Activo'
    """)

    # -----------------------------
    # Helper internos seguros (no rompen si campos no existen)
    # -----------------------------
    def _get_int(obj, *keys, default=0):
        for k in keys:
            if k in obj and obj[k] is not None and str(obj[k]).strip() != "":
                try:
                    return int(obj[k])
                except Exception:
                    pass
        return int(default)

    def _get_bool(obj, *keys, default=False):
        for k in keys:
            if k in obj and obj[k] is not None:
                v = str(obj[k]).strip().lower()
                if v in ("1", "true", "t", "si", "sí", "y", "yes"):
                    return True
                if v in ("0", "false", "f", "no", "n"):
                    return False
        return bool(default)

    for f in funcs:

        # =============================
        # 1. HORAS APROBADAS (VISTA)
        # =============================
        horas_q = q("""
            SELECT IFNULL(horas_aprobadas, 0) AS h
            FROM vw_nomina_quincenal
            WHERE Codigo_Funcionario = :f
              AND periodo = :p
              AND quincena = :q
        """,
        f=f["Codigo_Funcionario"],
        p=period["Periodo_Key"],
        q=period["Quincena"]
        )

        horas_aprobadas = float(horas_q[0]["h"]) if horas_q else 0.0

        # =============================
        # 2. SALARIO PROPORCIONAL
        # =============================
        base_dec = Decimal(f["Salario_Base_Mensual"] or 0)

        salario_quincenal = base_dec / Decimal("2")
        valor_hora = salario_quincenal / Decimal("92")

        horas_dec = Decimal(str(horas_aprobadas))
        bruto_q = (valor_hora * horas_dec).quantize(Decimal("0.01"))

        # =============================
        # 3. AUSENCIAS
        # =============================
        aus = q("""
            SELECT
                IFNULL(SUM(
                    CASE
                        WHEN a.Horas IS NULL OR a.Horas = 0 THEN 1
                        ELSE 0
                    END
                ), 0) AS dias,
                IFNULL(SUM(
                    CASE
                        WHEN a.Horas IS NOT NULL AND a.Horas > 0 THEN a.Horas
                        ELSE 0
                    END
                ), 0) AS horas
            FROM HRM_Absence a
            WHERE a.Codigo_Funcionario = :f
              AND a.Fecha BETWEEN :d AND :h
        """,
        f=f["Codigo_Funcionario"],
        d=period["Fecha_Desde"],
        h=period["Fecha_Hasta"]
        )[0]

        base_dec = Decimal(f["Salario_Base_Mensual"] or 0)

        diario = base_dec / Decimal("30")
        por_hora = diario / Decimal("8")

        dias_aus = Decimal(aus["dias"] or 0)
        horas_aus = Decimal(aus["horas"] or 0)

        reb_aus = (dias_aus * diario + horas_aus * por_hora).quantize(Decimal("0.01"))

        # =============================
        # 4. INCAPACIDADES
        # =============================
        inc = q("""
            SELECT
                IFNULL(SUM(
                    DATEDIFF(
                        LEAST(:h, Fecha_Hasta),
                        GREATEST(:d, Fecha_Desde)
                    ) + 1
                ), 0) AS dias,
                IFNULL(MAX(Porcentaje_Rebajo), 0) AS pct
            FROM HRM_Incapacity
            WHERE Codigo_Funcionario = :f
              AND Fecha_Hasta >= :d
              AND Fecha_Desde <= :h
        """,
        f=f["Codigo_Funcionario"],
        d=period["Fecha_Desde"],
        h=period["Fecha_Hasta"]
        )[0]

        dias_inc = Decimal(inc["dias"] or 0)
        pct_inc  = Decimal(inc["pct"] or 0) / Decimal("100")

        reb_inc = (dias_inc * diario * pct_inc).quantize(Decimal("0.01"))

        # =============================
        # 5. CARGAS SOCIALES
        # =============================
        ccss_pct  = Decimal(cfg["CCSS_Pct"] or 0) / Decimal("100")
        ivm_pct   = Decimal(cfg["IVM_Pct"] or 0) / Decimal("100")
        bp_pct    = Decimal(cfg["BP_Pct"] or 0) / Decimal("100")

        # Se mantiene lectura por compatibilidad (solo si usas PORCENTAJE)
        renta_pct = Decimal(cfg.get("Renta_Pct", 0) or 0) / Decimal("100")

        ccss  = (bruto_q * ccss_pct).quantize(Decimal("0.01"))
        ivm   = (bruto_q * ivm_pct).quantize(Decimal("0.01"))
        bp    = (bruto_q * bp_pct).quantize(Decimal("0.01"))

        # =============================
        # 5.1 RENTA (CONTROLADA: TRAMOS / PORCENTAJE / OFF)
        # =============================
        renta_mode = (cfg.get("Renta_Mode") or "TRAMOS").upper()  # TRAMOS | PORCENTAJE | OFF

        if renta_mode in ("OFF", "NO", "NONE", "0"):
            renta = Decimal("0.00")

        elif renta_mode == "PORCENTAJE":
            renta = (bruto_q * renta_pct).quantize(Decimal("0.01"))

        else:
            # TRAMOS (mensual) y prorrateo a quincena
            hijos = _get_int(f, "Cantidad_Hijos", "Hijos", "Num_Hijos", default=0)
            conyuge = _get_bool(f, "Tiene_Conyuge", "Conyuge", "TieneConyuge", default=False)

            base_gravable_q = bruto_q - ccss - ivm - bp
            if base_gravable_q < 0:
                base_gravable_q = Decimal("0.00")

            base_gravable_m = (base_gravable_q * Decimal("2")).quantize(Decimal("0.01"))

            renta_m = calc_renta_salario_mensual_2026(
                base_gravable_m,
                hijos=hijos,
                conyuge=conyuge,
                vigencia=period["Fecha_Hasta"]
            )
            renta = (renta_m / Decimal("2")).quantize(Decimal("0.01"))

        # ✅ IMPORTANTE:
        # Eliminado el fallback que te rebajaba renta aunque por tramos diera 0:
        # if renta == 0 and renta_pct > 0: ...

        # =============================
        # 6. DEDUCCIONES VOLUNTARIAS
        # =============================
        vol = q("""
            SELECT *
            FROM HRM_VoluntaryDed
            WHERE Codigo_Funcionario = :f
              AND Activa = 1
        """, f=f["Codigo_Funcionario"])

        vol_total = Decimal("0.00")

        for d in vol:
            if d.get("Monto_Fijo"):
                vol_total += Decimal(d["Monto_Fijo"])
            elif d.get("Porcentaje"):
                pct = Decimal(d["Porcentaje"]) / Decimal("100")
                vol_total += (bruto_q * pct)

        vol_total = vol_total.quantize(Decimal("0.01"))

        # =============================
        # 7. NETO A PAGAR
        # =============================
        neto = (
            bruto_q
            - reb_aus
            - reb_inc
            - ccss
            - ivm
            - bp
            - renta
            - vol_total
        )

        if neto < 0:
            neto = Decimal("0.00")

        neto = neto.quantize(Decimal("0.01"))

        # =============================
        # 8. UPSERT NÓMINA
        # =============================
        exec_("""
            INSERT INTO HRM_Payroll (
                Period_Id,
                Codigo_Funcionario,
                Salario_Base_Mensual,
                Bruto_Quincena,
                Rebajo_Ausencias,
                Rebajo_Incap,
                Deduccion_CCSS,
                Deduccion_IVM,
                Deduccion_BP,
                Deduccion_Renta,
                Deduccion_Vol,
                Neto_Pagar
            )
            VALUES (
                :pid, :f, :base, :b,
                :ra, :ri, :cc, :ivm,
                :bp, :r, :vol, :net
            )
            ON DUPLICATE KEY UPDATE
                Salario_Base_Mensual = :base,
                Bruto_Quincena       = :b,
                Rebajo_Ausencias     = :ra,
                Rebajo_Incap         = :ri,
                Deduccion_CCSS       = :cc,
                Deduccion_IVM        = :ivm,
                Deduccion_BP         = :bp,
                Deduccion_Renta      = :r,
                Deduccion_Vol        = :vol,
                Neto_Pagar           = :net
        """,
        pid=period["Id"],
        f=f["Codigo_Funcionario"],
        base=base_dec,
        b=bruto_q,
        ra=reb_aus,
        ri=reb_inc,
        cc=ccss,
        ivm=ivm,
        bp=bp,
        r=renta,
        vol=vol_total,
        net=neto
        )

    exec_("""
        UPDATE HRM_PayrollPeriod
        SET Estado = 'CALCULADO'
        WHERE Id = :id
    """, id=period["Id"])

    return jsonify({"ok": True, "recalculo": force})


@hrm_bp.get("/payroll/<int:pay_id>/pdf")
def payroll_pdf(pay_id):
    # ----------------------------------------------------------
    # 1) Cargar datos (incluye periodo para que el PDF sea completo)
    # ----------------------------------------------------------
    rows = q("""
        SELECT
            p.*,
            f.Nombre, f.Apellido, f.Cedula, f.Departamento,
            per.Periodo_Key, per.Quincena, per.Fecha_Desde, per.Fecha_Hasta, per.Estado AS Periodo_Estado
        FROM HRM_Payroll p
        JOIN Funcionario f ON f.Codigo_Funcionario = p.Codigo_Funcionario
        JOIN HRM_PayrollPeriod per ON per.Id = p.Period_Id
        WHERE p.Id = :id
        LIMIT 1
    """, id=pay_id)

    if not rows:
        return jsonify({"ok": False, "error": "Nómina no encontrada"}), 404

    row = rows[0]

    # ----------------------------------------------------------
    # 2) Helpers de formato
    # ----------------------------------------------------------
    def _dec(x):
        try:
            if x is None or x == "":
                return Decimal("0.00")
            return Decimal(str(x))
        except Exception:
            return Decimal("0.00")

    def money(x):
        v = _dec(x).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        return f"CRC {v:,.2f}"


    def safe_str(x, default="-"):
        return default if x is None or str(x).strip() == "" else str(x)

    # Valores
    salario_base = _dec(row.get("Salario_Base_Mensual"))
    bruto        = _dec(row.get("Bruto_Quincena"))
    reb_aus      = _dec(row.get("Rebajo_Ausencias"))
    reb_inc      = _dec(row.get("Rebajo_Incap"))
    d_ccss       = _dec(row.get("Deduccion_CCSS"))
    d_ivm        = _dec(row.get("Deduccion_IVM"))
    d_bp         = _dec(row.get("Deduccion_BP"))
    d_renta      = _dec(row.get("Deduccion_Renta"))
    d_vol        = _dec(row.get("Deduccion_Vol"))
    neto         = _dec(row.get("Neto_Pagar"))

    # Fechas / periodo
    periodo_key = safe_str(row.get("Periodo_Key"))
    quincena    = safe_str(row.get("Quincena"))
    f_desde     = row.get("Fecha_Desde")
    f_hasta     = row.get("Fecha_Hasta")
    generado_en = row.get("Generado_En") or datetime.now()

    # ----------------------------------------------------------
    # 3) Estilos Villa Grace (A4)
    # ----------------------------------------------------------
    # Colores (ajustables)
    VG_GREEN = colors.HexColor("#0F3D2E")   # verde profundo
    VG_GOLD  = colors.HexColor("#C9A24D")   # dorado
    VG_SOFT  = colors.HexColor("#F3F6F4")   # fondo suave
    VG_GRAY  = colors.HexColor("#6B7280")   # gris texto

    styles = getSampleStyleSheet()

    title = ParagraphStyle(
        "VGTitle",
        parent=styles["Title"],
        fontName="Helvetica-Bold",
        fontSize=18,
        textColor=VG_GREEN,
        alignment=TA_LEFT,
        spaceAfter=6
    )

    subtitle = ParagraphStyle(
        "VGSub",
        parent=styles["Normal"],
        fontName="Helvetica",
        fontSize=10,
        textColor=VG_GRAY,
        leading=13,
        spaceAfter=10
    )

    label = ParagraphStyle(
        "VGLabel",
        parent=styles["Normal"],
        fontName="Helvetica-Bold",
        fontSize=9,
        textColor=VG_GREEN,
        leading=12
    )

    normal = ParagraphStyle(
        "VGNormal",
        parent=styles["Normal"],
        fontName="Helvetica",
        fontSize=9,
        textColor=colors.black,
        leading=12
    )

    small = ParagraphStyle(
        "VGSmall",
        parent=styles["Normal"],
        fontName="Helvetica",
        fontSize=8,
        textColor=VG_GRAY,
        leading=11
    )

    right = ParagraphStyle(
        "VGRight",
        parent=normal,
        alignment=TA_RIGHT
    )

    # ----------------------------------------------------------
    # 4) Construcción del PDF (Platypus)
    # ----------------------------------------------------------
    buf = BytesIO()
    doc = SimpleDocTemplate(
        buf,
        pagesize=A4,
        leftMargin=2.0 * cm,
        rightMargin=2.0 * cm,
        topMargin=1.6 * cm,
        bottomMargin=1.6 * cm
    )

    story = []

    # Header “banda” (tabla con fondo verde)
    header_tbl = Table(
        [[
            Paragraph("<b>Hotel Villa Grace</b><br/><font size=9>Comprobante de pago</font>", ParagraphStyle(
                "HeaderLeft",
                fontName="Helvetica-Bold",
                fontSize=14,
                textColor=colors.white,
                leading=16
            )),
            Paragraph(
                f"<font size=9>{safe_str(periodo_key)} · {safe_str(quincena)}</font><br/>"
                f"<font size=8>Generado: {safe_str(generado_en)}</font>",
                ParagraphStyle("HeaderRight", fontName="Helvetica", fontSize=9, textColor=colors.white, alignment=TA_RIGHT, leading=12)
            )
        ]],
        colWidths=[11.5 * cm, 4.5 * cm]
    )
    header_tbl.setStyle(TableStyle([
        ("BACKGROUND", (0,0), (-1,-1), VG_GREEN),
        ("BOX", (0,0), (-1,-1), 0, VG_GREEN),
        ("LEFTPADDING", (0,0), (-1,-1), 12),
        ("RIGHTPADDING", (0,0), (-1,-1), 12),
        ("TOPPADDING", (0,0), (-1,-1), 10),
        ("BOTTOMPADDING", (0,0), (-1,-1), 10),
    ]))
    story.append(header_tbl)
    story.append(Spacer(1, 10))

    # Línea dorada fina
    story.append(HRFlowable(width="100%", thickness=2, color=VG_GOLD, spaceBefore=2, spaceAfter=10))

    # Info colaborador / periodo
    nombre_full = f"{safe_str(row.get('Nombre'))} {safe_str(row.get('Apellido'))}"
    info_tbl = Table([
        [Paragraph("Colaborador", label), Paragraph(nombre_full, normal),
         Paragraph("Cédula", label), Paragraph(safe_str(row.get("Cedula")), normal)],
        [Paragraph("Departamento", label), Paragraph(safe_str(row.get("Departamento")), normal),
         Paragraph("Periodo", label), Paragraph(f"{safe_str(periodo_key)} / {safe_str(quincena)}", normal)],
        [Paragraph("Rango", label), Paragraph(f"{safe_str(f_desde)} → {safe_str(f_hasta)}", normal),
         Paragraph("Estado", label), Paragraph(safe_str(row.get("Periodo_Estado")), normal)],
    ], colWidths=[2.6*cm, 6.4*cm, 2.1*cm, 4.9*cm])

    info_tbl.setStyle(TableStyle([
        ("BACKGROUND", (0,0), (-1,-1), VG_SOFT),
        ("BOX", (0,0), (-1,-1), 0.5, colors.HexColor("#D6E1DA")),
        ("INNERGRID", (0,0), (-1,-1), 0.25, colors.HexColor("#D6E1DA")),
        ("LEFTPADDING", (0,0), (-1,-1), 10),
        ("RIGHTPADDING", (0,0), (-1,-1), 10),
        ("TOPPADDING", (0,0), (-1,-1), 8),
        ("BOTTOMPADDING", (0,0), (-1,-1), 8),
        ("VALIGN", (0,0), (-1,-1), "MIDDLE"),
    ]))
    story.append(info_tbl)
    story.append(Spacer(1, 14))

    story.append(Paragraph("Resumen de nómina", title))
    story.append(Paragraph("Detalle de ingresos y deducciones aplicadas para esta quincena.", subtitle))

    # Tabla de resumen
    resumen = [
        ["Concepto", "Monto"],
        ["Salario base mensual", money(salario_base)],
        ["Bruto quincena", money(bruto)],
        ["Rebajo ausencias", f"- {money(reb_aus)}" if reb_aus > 0 else money(0)],
        ["Rebajo incapacidades", f"- {money(reb_inc)}" if reb_inc > 0 else money(0)],
        ["Deducción CCSS", f"- {money(d_ccss)}" if d_ccss > 0 else money(0)],
        ["Deducción IVM", f"- {money(d_ivm)}" if d_ivm > 0 else money(0)],
        ["Deducción BP", f"- {money(d_bp)}" if d_bp > 0 else money(0)],
        ["Deducción Renta", f"- {money(d_renta)}" if d_renta > 0 else money(0)],
        ["Deducciones voluntarias", f"- {money(d_vol)}" if d_vol > 0 else money(0)],
    ]

    t = Table(resumen, colWidths=[11.5*cm, 4.5*cm])
    t.setStyle(TableStyle([
        ("BACKGROUND", (0,0), (-1,0), VG_GREEN),
        ("TEXTCOLOR", (0,0), (-1,0), colors.white),
        ("FONTNAME", (0,0), (-1,0), "Helvetica-Bold"),
        ("FONTSIZE", (0,0), (-1,0), 10),
        ("ALIGN", (1,1), (1,-1), "RIGHT"),
        ("FONTNAME", (0,1), (-1,-1), "Helvetica"),
        ("FONTSIZE", (0,1), (-1,-1), 9),
        ("ROWBACKGROUNDS", (0,1), (-1,-1), [colors.white, VG_SOFT]),
        ("INNERGRID", (0,0), (-1,-1), 0.25, colors.HexColor("#D6E1DA")),
        ("BOX", (0,0), (-1,-1), 0.5, colors.HexColor("#D6E1DA")),
        ("LEFTPADDING", (0,0), (-1,-1), 10),
        ("RIGHTPADDING", (0,0), (-1,-1), 10),
        ("TOPPADDING", (0,0), (-1,-1), 8),
        ("BOTTOMPADDING", (0,0), (-1,-1), 8),
    ]))
    story.append(t)
    story.append(Spacer(1, 14))

    # Neto destacado
    net_tbl = Table(
        [[Paragraph("NETO A PAGAR", ParagraphStyle("NetLabel", fontName="Helvetica-Bold", fontSize=12, textColor=colors.white)),
          Paragraph(money(neto), ParagraphStyle("NetValue", fontName="Helvetica-Bold", fontSize=12, textColor=colors.white, alignment=TA_RIGHT))]],
        colWidths=[11.5*cm, 4.5*cm]
    )
    net_tbl.setStyle(TableStyle([
        ("BACKGROUND", (0,0), (-1,-1), VG_GREEN),
        ("BOX", (0,0), (-1,-1), 0, VG_GREEN),
        ("LEFTPADDING", (0,0), (-1,-1), 12),
        ("RIGHTPADDING", (0,0), (-1,-1), 12),
        ("TOPPADDING", (0,0), (-1,-1), 10),
        ("BOTTOMPADDING", (0,0), (-1,-1), 10),
    ]))
    story.append(net_tbl)
    story.append(Spacer(1, 12))

    # Footer
    story.append(HRFlowable(width="100%", thickness=1, color=colors.HexColor("#D6E1DA"), spaceBefore=6, spaceAfter=8))
    story.append(Paragraph(
        "Documento generado automáticamente por el sistema de RRHH de Hotel Villa Grace. "
        "Si requiere una corrección, contacte a administración.",
        small
    ))

    # Build
    doc.build(story)
    buf.seek(0)

    filename = f"Comprobante_{periodo_key}_{quincena}_{safe_str(row.get('Apellido'),'')}_{pay_id}.pdf".replace(" ", "_")

    # as_attachment=False lo abre bonito en el navegador (tu botón ya abre target=_blank)
    return send_file(
        buf,
        as_attachment=False,
        download_name=filename,
        mimetype="application/pdf"
    )

@hrm_bp.post("/payroll/<int:pay_id>/send")
def payroll_send(pay_id):
    # inserta en SAC_Outbox para que tu proceso de correo lo despache
    row = q("""
      SELECT p.Id, f.Nombre, f.Apellido
      FROM HRM_Payroll p JOIN Funcionario f ON f.Codigo_Funcionario=p.Codigo_Funcionario
      WHERE p.Id=:id
    """, id=pay_id)[0]
    exec_("""
      INSERT INTO SAC_Outbox (Canal, Para, Asunto, Cuerpo, Estado, Programado_At, Ref_Entidad, Ref_Id)
      VALUES ('email','recepcion@hotel.test','Comprobante de pago',
        :cuerpo,'PENDIENTE',NOW(),'HRM_Payroll',:id)
    """, cuerpo=f"Estimado/a {row['Nombre']} {row['Apellido']}, adjuntaremos su comprobante de pago. (Ruta /hrm/payroll/{pay_id}/pdf)", id=pay_id)
    exec_("UPDATE HRM_Payroll SET Email_Enviado=1 WHERE Id=:id", id=pay_id)
    return jsonify({"ok": True})

@hrm_bp.post("/payroll/<int:pay_id>/pago")
def payroll_pago(pay_id):
    p = request.get_json(silent=True) or {}
    exec_("""
      INSERT INTO HRM_PaymentRecord (Payroll_Id, Fecha_Pago, Comprobante, Banco, Cuenta_Destino, Emitido_Por)
      VALUES (:id, NOW(), :comp, :ban, :cta, :usr)
    """, id=pay_id, comp=p.get("comprobante"), ban=p.get("banco"), cta=p.get("cuenta"), usr=p.get("user_id"))
    exec_("UPDATE HRM_PayrollPeriod SET Estado='PAGADO' WHERE Id=(SELECT Period_Id FROM HRM_Payroll WHERE Id=:id)", id=pay_id)
    return jsonify({"ok": True})

# =============== HRM-08-013 ===============
@hrm_bp.post("/aguinaldo/generate")
def aguinaldo_generate():
    p = request.get_json(silent=True) or {}
    anio = int(p.get("anio", date.today().year))
    # suma de BRUTO por colaborador últimos 12 meses / 12
    data = q("""
      SELECT Codigo_Funcionario, ROUND(SUM(Bruto_Quincena)/2.0,2) AS promedio_mensual
      FROM HRM_Payroll
      WHERE Generado_En >= DATE_SUB(CONCAT(:anio,'-12-01'), INTERVAL 12 MONTH)
        AND Generado_En <  CONCAT(:anio,'-12-01')
      GROUP BY Codigo_Funcionario
    """, anio=anio)
    # Creamos un periodo especial "AGUINALDO"
    exec_("""
      INSERT INTO HRM_PayrollPeriod (Periodo_Key, Quincena, Fecha_Desde, Fecha_Hasta, Estado)
      VALUES (:pk,'Q2', :d, :h, 'CALCULADO')
      ON DUPLICATE KEY UPDATE Estado='CALCULADO'
    """, pk=f"{anio}-AG", d=f"{anio}-12-01", h=f"{anio}-12-31")
    per = q("SELECT Id FROM HRM_PayrollPeriod WHERE Periodo_Key=:pk", pk=f"{anio}-AG")[0]
    for r in data:
        base = q("SELECT Salario_Base_Mensual FROM Funcionario WHERE Codigo_Funcionario=:f", f=r["Codigo_Funcionario"])
        base = float(base[0]["Salario_Base_Mensual"]) if base else 0.0
        bruto = float(r["promedio_mensual"])
        exec_("""
          INSERT INTO HRM_Payroll (Period_Id, Codigo_Funcionario, Salario_Base_Mensual, Bruto_Quincena,
                                   Rebajo_Ausencias, Rebajo_Incap, Deduccion_CCSS, Deduccion_IVM,
                                   Deduccion_BP, Deduccion_Renta, Deduccion_Vol, Neto_Pagar)
          VALUES (:pid,:f,:base,:b,0,0,0,0,0,0,0,:b)
          ON DUPLICATE KEY UPDATE Bruto_Quincena=:b, Neto_Pagar=:b
        """, pid=per["Id"], f=r["Codigo_Funcionario"], base=base, b=bruto)
    return jsonify({"ok": True})

@hrm_bp.route("/", methods=["GET"])
def rrhh_ui():
    resumen = {
        "activos": db.session.query(Funcionario)
            .filter(Funcionario.Estado_Empleado == "Activo")
            .count(),

        "vac_pend": db.session.query(HRMAbsence)
            .filter(HRMAbsence.Tipo == "VACACIONES")
            .count(),

        "amon_pend": db.session.query(text("HRM_Warning"))
            .filter(text("Estado='REGISTRADA'"))
            .count(),

        "aus_hoy": db.session.query(HRMAbsence)
            .filter(HRMAbsence.Fecha == func.curdate())
            .count(),
    }

    periodos = (
        db.session.query(HRMPayrollPeriod)
        .order_by(HRMPayrollPeriod.Fecha_Desde.desc())
        .limit(5)
        .all()
    )

    return render_template(
        "rrhh.html",
        resumen=resumen,
        periodos=periodos,
    )

@hrm_bp.get("/payroll/period/<int:period_id>/rows")
def payroll_period_rows(period_id):
    # trae la nómina calculada para ese periodo
    rows = q("""
        SELECT
          p.Id,
          p.Codigo_Funcionario,
          p.Bruto_Quincena,
          p.Neto_Pagar,
          f.Nombre,
          f.Apellido
        FROM HRM_Payroll p
        JOIN Funcionario f
          ON f.Codigo_Funcionario = p.Codigo_Funcionario
        WHERE p.Period_Id = :pid
        ORDER BY f.Nombre, f.Apellido
    """, pid=period_id)

    def _to_float(x):
        try:
            return float(x or 0)
        except Exception:
            return 0.0

    data = []
    for r in rows:
        data.append({
            "Id": r["Id"],  # <-- ESTE es el payId que usa el link /hrm/payroll/<payId>/pdf
            "Codigo_Funcionario": r["Codigo_Funcionario"],
            "Nombre": r.get("Nombre"),
            "Apellido": r.get("Apellido"),
            "Bruto_Quincena": _to_float(r.get("Bruto_Quincena")),
            "Neto_Pagar": _to_float(r.get("Neto_Pagar")),
        })

    return jsonify({"ok": True, "data": data})

