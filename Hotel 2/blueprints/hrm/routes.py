from datetime import datetime, date
from flask import request, jsonify
from extensions import db
from . import hrm_bp
from models import Funcionario, FuncionarioHistorial


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


def _historial_to_dict(h: FuncionarioHistorial):
    return {
        "Id_Historial": h.Id_Historial,
        "Fecha_Evento": h.Fecha_Evento.isoformat() if h.Fecha_Evento else None,
        "Tipo_Evento": h.Tipo_Evento,
        "Detalle": h.Detalle,
        "Valor_Anterior": h.Valor_Anterior,
        "Valor_Nuevo": h.Valor_Nuevo,
        "Registrado_Por": h.Registrado_Por,
    }


# ----------------------------------------
# GET /hrm/empleados?estado=Activo|Inactivo|Suspendido|todos
# ----------------------------------------
@hrm_bp.route("/empleados", methods=["GET"])
def listar_empleados():
    estado = request.args.get("estado", default="Activo")

    query = Funcionario.query
    if estado != "todos":
        query = query.filter(Funcionario.Estado_Empleado == estado)

    empleados = query.order_by(Funcionario.Nombre, Funcionario.Apellido).all()
    data = [_funcionario_to_dict(f) for f in empleados]

    return jsonify({"ok": True, "data": data})


# ----------------------------------------
# GET /hrm/empleados/<id>  (detalle + historial)
# ----------------------------------------
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
        "historial": [_historial_to_dict(h) for h in hist],
    })


# ----------------------------------------
# POST /hrm/empleados
# Crea un colaborador nuevo y registra evento "Ingreso"
# ----------------------------------------
@hrm_bp.route("/empleados", methods=["POST"])
def crear_empleado():
    payload = request.get_json(silent=True) or {}

    # Si no mandan Fecha_Ingreso, usamos hoy
    fecha_ingreso = payload.get("Fecha_Ingreso")
    if not fecha_ingreso:
        fecha_ingreso = date.today().isoformat()

    nuevo = Funcionario(
        Cedula               = payload.get("Cedula"),
        Nombre               = payload.get("Nombre"),
        Apellido             = payload.get("Apellido"),
        Puesto               = payload.get("Puesto"),
        Departamento         = payload.get("Departamento"),
        Fecha_Nacimiento     = payload.get("Fecha_Nacimiento"),
        Fecha_Ingreso        = fecha_ingreso,
        Salario_Base_Mensual = payload.get("Salario_Base_Mensual", 0.00),
        Estado_Empleado      = payload.get("Estado_Empleado", "Activo"),
        Tipo_Contrato        = payload.get("Tipo_Contrato", "Tiempo completo"),
        Cuenta_Bancaria      = payload.get("Cuenta_Bancaria"),
        Banco                = payload.get("Banco"),
        Fecha_modificacion   = datetime.utcnow(),
        Fecha_mod_alta       = datetime.utcnow(),
    )

    db.session.add(nuevo)
    db.session.flush()  # para obtener Codigo_Funcionario antes del commit

    historial_ingreso = FuncionarioHistorial(
        Codigo_Funcionario = nuevo.Codigo_Funcionario,
        Fecha_Evento       = datetime.utcnow(),
        Tipo_Evento        = "Ingreso",
        Detalle            = "Ingreso al hotel",
        Valor_Anterior     = None,
        Valor_Nuevo        = f"Salario inicial {payload.get('Salario_Base_Mensual', 0.00)}",
        Registrado_Por     = payload.get("Registrado_Por")  # opcional: Código_Usuario del que creó
    )
    db.session.add(historial_ingreso)

    db.session.commit()

    return jsonify({"ok": True, "empleado": _funcionario_to_dict(nuevo)}), 201


# ----------------------------------------
# PUT /hrm/empleados/<id>
# Actualiza datos. Si cambia Puesto o Salario → se guarda en historial
# ----------------------------------------
@hrm_bp.route("/empleados/<int:codigo_func>", methods=["PUT"])
def actualizar_empleado(codigo_func):
    payload = request.get_json(silent=True) or {}

    f = Funcionario.query.get(codigo_func)
    if not f:
        return jsonify({"ok": False, "error": "Empleado no encontrado"}), 404

    cambios_historial = []

    # Cambio de Puesto
    nuevo_puesto = payload.get("Puesto", f.Puesto)
    if nuevo_puesto != f.Puesto:
        cambios_historial.append({
            "Tipo_Evento": "Cambio Puesto",
            "Detalle": "Actualización de puesto",
            "Valor_Anterior": f.Puesto,
            "Valor_Nuevo": nuevo_puesto
        })
        f.Puesto = nuevo_puesto

    # Cambio de Salario
    nuevo_salario = payload.get("Salario_Base_Mensual", f.Salario_Base_Mensual)
    if str(nuevo_salario) != str(f.Salario_Base_Mensual):
        cambios_historial.append({
            "Tipo_Evento": "Cambio Salario",
            "Detalle": "Ajuste salarial",
            "Valor_Anterior": str(f.Salario_Base_Mensual),
            "Valor_Nuevo": str(nuevo_salario)
        })
        f.Salario_Base_Mensual = nuevo_salario

    # Otros campos normales
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

    # Guardar las entradas de historial generadas
    for cambio in cambios_historial:
        db.session.add(FuncionarioHistorial(
            Codigo_Funcionario = f.Codigo_Funcionario,
            Fecha_Evento       = datetime.utcnow(),
            Tipo_Evento        = cambio["Tipo_Evento"],
            Detalle            = cambio["Detalle"],
            Valor_Anterior     = cambio["Valor_Anterior"],
            Valor_Nuevo        = cambio["Valor_Nuevo"],
            Registrado_Por     = payload.get("Registrado_Por")
        ))

    db.session.commit()

    return jsonify({"ok": True, "empleado": _funcionario_to_dict(f)})


# ----------------------------------------
# DELETE /hrm/empleados/<id>
# No borra el registro. Lo marca Inactivo + guarda evento "Salida".
# ----------------------------------------
@hrm_bp.route("/empleados/<int:codigo_func>", methods=["DELETE"])
def desactivar_empleado(codigo_func):
    payload = request.get_json(silent=True) or {}

    f = Funcionario.query.get(codigo_func)
    if not f:
        return jsonify({"ok": False, "error": "Empleado no encontrado"}), 404

    if f.Estado_Empleado != "Inactivo":
        f.Estado_Empleado    = "Inactivo"
        f.Fecha_modificacion = datetime.utcnow()
        f.Fecha_mod_alta     = datetime.utcnow()

        db.session.flush()

        salida = FuncionarioHistorial(
            Codigo_Funcionario = f.Codigo_Funcionario,
            Fecha_Evento       = datetime.utcnow(),
            Tipo_Evento        = "Salida",
            Detalle            = "Colaborador desactivado",
            Valor_Anterior     = None,
            Valor_Nuevo        = "Estado Inactivo",
            Registrado_Por     = payload.get("Registrado_Por")
        )
        db.session.add(salida)

    db.session.commit()

    return jsonify({"ok": True, "empleado": _funcionario_to_dict(f)})




