# blueprints/hrm/routes.py
from datetime import datetime, date
from flask import request, jsonify, render_template, session
from sqlalchemy import func
from extensions import db
from . import hrm_bp
from models import Funcionario, FuncionarioHistorial


# -------------------------------------------------------------------
# Utilidades de serialización
# -------------------------------------------------------------------
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
    # Ajusta a como guardes al usuario en sesión
    # p.ej. session["Codigo_Usuario"] o session["user_id"]
    return session.get("Codigo_Usuario") or session.get("user_id")


# -------------------------------------------------------------------
# API JSON
# -------------------------------------------------------------------

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


# ---------- NUEVO: actualizar perfil (salario, puesto y departamento) ----------
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


# -------------------------------------------------------------------
# Interfaz HTML
# -------------------------------------------------------------------
@hrm_bp.route("/empleados-ui", methods=["GET"])
def empleados_ui():
    estado = request.args.get("estado", default="Activo")

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


