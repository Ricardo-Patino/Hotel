# -*- coding: utf-8 -*-
from decimal import Decimal, InvalidOperation
from flask import render_template, request, redirect, url_for, flash, session
from sqlalchemy import func
from extensions import db
from . import inv_bp
from models_sql import InvCategoria, InvInsumo, InvMovimiento
from utils.auth import role_required

# Catálogo de unidades (una sola definición)
UNIDADES = ["un", "kg", "g", "lt", "ml", "paq", "caja", "rollo", "bolsa", "pz"]

def _actor():
    """Información del usuario autenticado para auditoría."""
    return {
        "id": session.get("user_id"),
        "name": session.get("user_name"),
        "email": session.get("user_email") or session.get("email"),
    }

# =========================================================
# CATEGORÍAS (INV-07-001)
# =========================================================

# Listado de categorías
@inv_bp.route("/categorias", methods=["GET"])
@role_required("Administrador", "Recepcionista")  # o amplía a otros roles si corresponde
def categorias_list():
    q = request.args.get("q", "").strip()
    solo_activas = request.args.get("solo_activas", "1") == "1"

    query = InvCategoria.query
    if q:
        like = f"%{q}%"
        query = query.filter(
            (InvCategoria.Nombre.ilike(like)) |
            (InvCategoria.Descripcion.ilike(like))
        )
    if solo_activas:
        query = query.filter(InvCategoria.Activa.is_(True))

    categorias = query.order_by(
        InvCategoria.Activa.desc(),
        InvCategoria.Nombre.asc()
    ).all()

    return render_template(
        "inv/categorias_list.html",
        categorias=categorias,
        q=q,
        solo_activas=solo_activas
    )

# Crear categoría
@inv_bp.route("/categorias/nueva", methods=["GET", "POST"])
@role_required("Administrador", "Recepcionista")
def categorias_new():
    if request.method == "POST":
        nombre = (request.form.get("Nombre") or "").strip()
        descripcion = (request.form.get("Descripcion") or "").strip()

        if not nombre:
            flash("El nombre es obligatorio.", "danger")
            return redirect(url_for("inv.categorias_new"))

        # Duplicado por nombre (case-insensitive)
        existe = InvCategoria.query.filter(
            db.func.lower(InvCategoria.Nombre) == nombre.lower()
        ).first()
        if existe:
            flash("Ya existe una categoría con ese nombre.", "warning")
            return redirect(url_for("inv.categorias_new"))

        cat = InvCategoria(Nombre=nombre, Descripcion=descripcion, Activa=True)
        db.session.add(cat)
        db.session.commit()
        flash("Categoría creada correctamente.", "success")
        return redirect(url_for("inv.categorias_list"))

    return render_template("inv/categorias_form.html", modo="new")

# Editar categoría
@inv_bp.route("/categorias/<int:cat_id>/editar", methods=["GET", "POST"])
@role_required("Administrador", "Recepcionista")
def categorias_edit(cat_id: int):
    cat = InvCategoria.query.get_or_404(cat_id)

    if request.method == "POST":
        nombre = (request.form.get("Nombre") or "").strip()
        descripcion = (request.form.get("Descripcion") or "").strip()

        if not nombre:
            flash("El nombre es obligatorio.", "danger")
            return redirect(url_for("inv.categorias_edit", cat_id=cat_id))

        # Duplicado excluyendo la misma Id
        existe = InvCategoria.query.filter(
            db.func.lower(InvCategoria.Nombre) == nombre.lower(),
            InvCategoria.Id != cat.Id
        ).first()
        if existe:
            flash("Ya existe otra categoría con ese nombre.", "warning")
            return redirect(url_for("inv.categorias_edit", cat_id=cat_id))

        cat.Nombre = nombre
        cat.Descripcion = descripcion
        db.session.commit()
        flash("Categoría actualizada.", "success")
        return redirect(url_for("inv.categorias_list"))

    return render_template("inv/categorias_form.html", modo="edit", cat=cat)

# Activar/Inactivar categoría
@inv_bp.route("/categorias/<int:cat_id>/toggle", methods=["POST"])
@role_required("Administrador", "Recepcionista")
def categorias_toggle(cat_id: int):
    cat = InvCategoria.query.get_or_404(cat_id)
    cat.Activa = not cat.Activa
    db.session.commit()
    flash(("Categoría activada." if cat.Activa else "Categoría inactivada."), "info")
    return redirect(url_for("inv.categorias_list"))

# =========================================================
# INSUMOS
# =========================================================

# LISTA DE INSUMOS (endpoint único)
@inv_bp.route("/insumos", methods=["GET"])
@role_required("Administrador", "Recepcionista")
def insumos_list():
    q = (
        db.session.query(InvInsumo, InvCategoria)
        .join(InvCategoria, InvInsumo.Categoria_Id == InvCategoria.Id)
        .order_by(InvInsumo.Activo.desc(), InvCategoria.Nombre.asc(), InvInsumo.Nombre.asc())
    )
    rows = q.all()
    data = [{"insumo": i, "categoria": c} for (i, c) in rows]

    # Conteo de alertas (stock < mínimo)
    low_count = sum(1 for (i, _) in rows if (i.Stock_Actual or 0) < (i.Stock_Minimo or 0))

    # items y data para compatibilidad con vistas previas
    return render_template("inv/insumos_list.html", items=data, data=data, low_count=low_count)




# NUEVO INSUMO (INV-07-002)
@inv_bp.route("/insumos/nuevo", methods=["GET", "POST"])
@role_required("Administrador", "Recepcionista")
def insumo_new():
    categorias = InvCategoria.query.filter_by(Activa=True).order_by(InvCategoria.Nombre.asc()).all()

    if request.method == "POST":
        categoria_id = request.form.get("categoria_id")
        nombre       = (request.form.get("nombre") or "").strip()
        unidad       = (request.form.get("unidad") or "").strip().lower()
        stock_ini    = (request.form.get("stock_inicial") or "0").strip()
        stock_min    = (request.form.get("stock_minimo") or "0").strip()

        # Validaciones básicas
        if not categoria_id or not categoria_id.isdigit():
            flash("Selecciona una categoría válida.", "danger")
            return render_template("inv/insumos_form.html", categorias=categorias, unidades=UNIDADES)

        if not nombre or len(nombre) < 2:
            flash("Ingresa un nombre de insumo válido.", "danger")
            return render_template("inv/insumos_form.html", categorias=categorias, unidades=UNIDADES)

        if unidad not in UNIDADES:
            flash("Selecciona una unidad válida.", "danger")
            return render_template("inv/insumos_form.html", categorias=categorias, unidades=UNIDADES)

        try:
            stock_ini_dec = Decimal(stock_ini)
            stock_min_dec = Decimal(stock_min)
            if stock_ini_dec < 0 or stock_min_dec < 0:
                raise InvalidOperation()
        except InvalidOperation:
            flash("Stock inicial / mínimo inválido.", "danger")
            return render_template("inv/insumos_form.html", categorias=categorias, unidades=UNIDADES)

        cat_id = int(categoria_id)

        # Duplicados por (Categoría + Nombre)
        existe = (
            InvInsumo.query
            .filter(func.lower(InvInsumo.Nombre) == nombre.lower(),
                    InvInsumo.Categoria_Id == cat_id)
            .first()
        )
        if existe:
            flash("Ya existe un insumo con ese nombre en la categoría seleccionada.", "warning")
            return render_template("inv/insumos_form.html", categorias=categorias, unidades=UNIDADES)

        ins = InvInsumo(
            Categoria_Id = cat_id,
            Nombre       = nombre,
            Unidad       = unidad,
            Stock_Actual = stock_ini_dec,
            Stock_Minimo = stock_min_dec,
            Activo       = True,
        )
        db.session.add(ins)
        db.session.commit()

        flash("Insumo registrado correctamente.", "success")
        return redirect(url_for("inv.insumos_list"))

    # GET
    return render_template("inv/insumos_form.html", categorias=categorias, unidades=UNIDADES)

# EDITAR / INACTIVAR (INV-07-003)
@inv_bp.route("/insumos/<int:insumo_id>/editar", methods=["GET", "POST"])
@role_required("Administrador", "Recepcionista")
def insumo_edit(insumo_id: int):
    i = InvInsumo.query.filter_by(Id=insumo_id).first()
    if not i:
        flash("Insumo no encontrado.", "warning")
        return redirect(url_for("inv.insumos_list"))

    categorias = InvCategoria.query.filter_by(Activa=True).order_by(InvCategoria.Nombre.asc()).all()

    if request.method == "POST":
        motivo = (request.form.get("motivo") or "").strip()
        if not motivo:
            flash("Debes indicar un motivo del ajuste/edición.", "danger")
            return render_template("inv/insumos_edit.html", insumo=i, categorias=categorias, unidades=UNIDADES)

        nuevo_nombre   = (request.form.get("nombre") or "").strip()
        nueva_unidad   = (request.form.get("unidad") or "").strip().lower()
        nueva_cat_id_s = (request.form.get("categoria_id") or "").strip()
        nuevo_min_s    = (request.form.get("stock_minimo") or "").strip()
        nuevo_activo   = True if request.form.get("activo") == "on" else False

        # Normalizaciones
        try:
            nueva_cat_id = int(nueva_cat_id_s)
        except Exception:
            nueva_cat_id = i.Categoria_Id

        try:
            nuevo_min = Decimal(nuevo_min_s or "0")
            if nuevo_min < 0:
                raise InvalidOperation()
        except InvalidOperation:
            flash("Stock mínimo inválido.", "danger")
            return render_template("inv/insumos_edit.html", insumo=i, categorias=categorias, unidades=UNIDADES)

        # Registro de cambios
        changes = []
        actor = _actor()

        def log_change(campo, antes, despues, tipo="EDICION"):
            mov = InvMovimiento(
                Insumo_Id=i.Id,
                Tipo=tipo,
                Campo=campo,
                Valor_Antes=str(antes) if antes is not None else None,
                Valor_Despues=str(despues) if despues is not None else None,
                Delta=None,
                Motivo=motivo,
                Usuario_Id=actor["id"],
                Usuario_Nombre=actor["name"],
                Usuario_Email=actor["email"],
            )
            changes.append(mov)

        # Comparaciones (Nombre, Unidad, Categoria_Id, Stock_Minimo, Activo)
        if nuevo_nombre and nuevo_nombre != i.Nombre:
            log_change("Nombre", i.Nombre, nuevo_nombre)
            i.Nombre = nuevo_nombre

        if nueva_unidad and nueva_unidad != i.Unidad:
            if nueva_unidad not in UNIDADES:
                flash("Unidad inválida.", "danger")
                return render_template("inv/insumos_edit.html", insumo=i, categorias=categorias, unidades=UNIDADES)
            log_change("Unidad", i.Unidad, nueva_unidad)
            i.Unidad = nueva_unidad

        if nueva_cat_id != i.Categoria_Id:
            old_cat = InvCategoria.query.get(i.Categoria_Id)
            new_cat = InvCategoria.query.get(nueva_cat_id)
            log_change("Categoria_Id",
                       old_cat.Nombre if old_cat else i.Categoria_Id,
                       new_cat.Nombre if new_cat else nueva_cat_id)
            i.Categoria_Id = nueva_cat_id

        if nuevo_min != (i.Stock_Minimo or Decimal("0")):
            log_change("Stock_Minimo", i.Stock_Minimo, nuevo_min)
            i.Stock_Minimo = nuevo_min

        if nuevo_activo != bool(i.Activo):
            tipo = "INACTIVACION" if (bool(i.Activo) and not nuevo_activo) else "REACTIVACION"
            log_change("Activo", int(i.Activo), int(nuevo_activo), tipo=tipo)
            i.Activo = nuevo_activo

        if not changes:
            flash("No hay cambios para guardar.", "info")
            return redirect(url_for("inv.insumo_edit", insumo_id=i.Id))

        for m in changes:
            db.session.add(m)
        db.session.commit()

        flash("Insumo actualizado y cambios registrados en historial.", "success")
        return redirect(url_for("inv.insumos_list"))

    # GET
    return render_template("inv/insumos_edit.html", insumo=i, categorias=categorias, unidades=UNIDADES)

# AJUSTE DE STOCK (INV-07-003)
@inv_bp.route("/insumos/<int:insumo_id>/ajuste", methods=["GET", "POST"])
@role_required("Administrador", "Recepcionista")
def insumo_adjust(insumo_id: int):
    i = InvInsumo.query.filter_by(Id=insumo_id).first()
    if not i:
        flash("Insumo no encontrado.", "warning")
        return redirect(url_for("inv.insumos_list"))

    if request.method == "POST":
        delta_s = (request.form.get("delta") or "").strip()
        motivo  = (request.form.get("motivo") or "").strip()

        try:
            delta = Decimal(delta_s)
        except Exception:
            flash("Cantidad de ajuste inválida.", "danger")
            return render_template("inv/ajustes_form.html", insumo=i)

        if not motivo:
            flash("Debes indicar un motivo del ajuste.", "danger")
            return render_template("inv/ajustes_form.html", insumo=i)

        nuevo_stock = (i.Stock_Actual or Decimal("0")) + delta
        if nuevo_stock < 0:
            flash("El ajuste dejaría el stock negativo. Corrige la cantidad.", "danger")
            return render_template("inv/ajustes_form.html", insumo=i)

        actor = _actor()
        mov = InvMovimiento(
            Insumo_Id=i.Id,
            Tipo="AJUSTE",
            Campo="Stock_Actual",
            Valor_Antes=str(i.Stock_Actual),
            Valor_Despues=str(nuevo_stock),
            Delta=delta,
            Motivo=motivo,
            Usuario_Id=actor["id"],
            Usuario_Nombre=actor["name"],
            Usuario_Email=actor["email"],
        )
        i.Stock_Actual = nuevo_stock

        db.session.add(mov)
        db.session.commit()

        flash("Ajuste aplicado y registrado en historial.", "success")
        return redirect(url_for("inv.insumos_list"))

    # GET
    return render_template("inv/ajustes_form.html", insumo=i)

# =========================================================
# INV-07-004: ENTRADA por COMPRA o DEVOLUCIÓN
# =========================================================
@inv_bp.route("/entradas/nueva", methods=["GET", "POST"])
@role_required("Administrador", "Recepcionista")
def entradas_new():
    # Combos
    insumos = InvInsumo.query.filter_by(Activo=True).order_by(InvInsumo.Nombre.asc()).all()
    categorias = InvCategoria.query.filter_by(Activa=True).order_by(InvCategoria.Nombre.asc()).all()

    if request.method == "POST":
        insumo_id = request.form.get("insumo_id", type=int)
        tipo = request.form.get("tipo")  # 'COMPRA' | 'DEVOLUCION'
        cantidad = request.form.get("cantidad")
        motivo = request.form.get("motivo", "").strip()
        doc_tipo = (request.form.get("doc_tipo", "") or None)
        doc_numero = (request.form.get("doc_numero", "") or None)
        proveedor = (request.form.get("proveedor", "") or None)

        # Validaciones
        if not insumo_id or not tipo or not cantidad or not motivo:
            flash("Completa insumo, tipo, cantidad y motivo.", "warning")
            return render_template("inv/entradas_form.html",
                                   insumos=insumos, categorias=categorias, modo="new")

        try:
            cantidad = Decimal(str(cantidad))
        except Exception:
            flash("Cantidad inválida.", "warning")
            return render_template("inv/entradas_form.html",
                                   insumos=insumos, categorias=categorias, modo="new")

        if cantidad <= 0:
            flash("Cantidad debe ser mayor a cero.", "warning")
            return render_template("inv/entradas_form.html",
                                   insumos=insumos, categorias=categorias, modo="new")

        ins = InvInsumo.query.get(insumo_id)
        if not ins or not ins.Activo:
            flash("Insumo no válido o inactivo.", "danger")
            return redirect(url_for("inv.insumos_list"))

        # Transacción
        stock_antes = Decimal(ins.Stock_Actual or 0)
        stock_despues = stock_antes + cantidad  # entradas suman

        ins.Stock_Actual = stock_despues

        mov = InvMovimiento(
            Insumo_Id=ins.Id,
            Tipo=("ENTRADA_COMPRA" if tipo == "COMPRA" else "ENTRADA_DEVOLUCION"),
            Campo="Stock_Actual",
            Valor_Antes=str(stock_antes),
            Valor_Despues=str(stock_despues),
            Delta=cantidad,
            Motivo=motivo,
            Doc_Tipo=doc_tipo,
            Doc_Numero=doc_numero,
            Proveedor=proveedor,
            Usuario_Id=session.get("user_id"),
            Usuario_Nombre=session.get("user_name"),
            Usuario_Email=session.get("user_email"),
        )
        db.session.add(mov)
        db.session.commit()

        flash("Entrada registrada y stock actualizado.", "success")
        return redirect(url_for("inv.insumos_list"))

    # GET
    return render_template("inv/entradas_form.html",
                           insumos=insumos, categorias=categorias, modo="new")

# =========================================================
# INV-07-005 – Alertas de stock 
# =========================================================
@inv_bp.get("/alertas/stock")
@role_required("Administrador", "Recepcionista")
def alertas_stock():
    low_q = (
        InvInsumo.query
        .filter(InvInsumo.Activo.is_(True))
        .filter((InvInsumo.Stock_Actual or 0) < (InvInsumo.Stock_Minimo or 0))
    )
    # En SQLAlchemy, la comparación con None/Decimal requiere cuidado; usa una sub-expresión segura:
    items = []
    for ins in InvInsumo.query.filter_by(Activo=True).all():
        try:
            a = Decimal(ins.Stock_Actual or 0)
            m = Decimal(ins.Stock_Minimo or 0)
        except Exception:
            a, m = Decimal(0), Decimal(0)
        if a < m:
            items.append({
                "Id": ins.Id,
                "Nombre": ins.Nombre,
                "Stock_Actual": float(a),
                "Stock_Minimo": float(m),
                "Unidad": ins.Unidad,
            })

    return {
        "count": len(items),
        "items": sorted(items, key=lambda x: x["Nombre"])[:10]  # máximo 10 para la UI
    }
# =========================================================
