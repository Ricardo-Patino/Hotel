from decimal import Decimal, InvalidOperation
from flask import render_template, request, redirect, url_for, flash
from sqlalchemy import func
from sqlalchemy import text
from extensions import db
from models import InvCategoria
from . import inv_bp
from models_sql import InvCategoria, InvInsumo
from utils.auth import role_required


# ---------- Listado ----------
@inv_bp.route("/categorias", methods=["GET"])
def categorias_list():
    q = request.args.get("q", "").strip()
    solo_activas = request.args.get("solo_activas", "1") == "1"

    query = InvCategoria.query
    if q:
        like = f"%{q}%"
        query = query.filter((InvCategoria.Nombre.ilike(like)) | (InvCategoria.Descripcion.ilike(like)))
    if solo_activas:
        query = query.filter(InvCategoria.Activa.is_(True))

    categorias = query.order_by(InvCategoria.Activa.desc(), InvCategoria.Nombre.asc()).all()
    return render_template("inv/categorias_list.html", categorias=categorias, q=q, solo_activas=solo_activas)

# ---------- Crear ----------
@inv_bp.route("/categorias/nueva", methods=["GET", "POST"])
def categorias_new():
    if request.method == "POST":
        nombre = (request.form.get("Nombre") or "").strip()
        descripcion = (request.form.get("Descripcion") or "").strip()

        if not nombre:
            flash("El nombre es obligatorio.", "danger")
            return redirect(url_for("inv.categorias_new"))

        # Validación de duplicado
        existe = InvCategoria.query.filter(db.func.lower(InvCategoria.Nombre) == nombre.lower()).first()
        if existe:
            flash("Ya existe una categoría con ese nombre.", "warning")
            return redirect(url_for("inv.categorias_new"))

        cat = InvCategoria(Nombre=nombre, Descripcion=descripcion, Activa=True)
        db.session.add(cat)
        db.session.commit()
        flash("Categoría creada correctamente.", "success")
        return redirect(url_for("inv.categorias_list"))

    return render_template("inv/categorias_form.html", modo="new")

# ---------- Editar ----------
@inv_bp.route("/categorias/<int:cat_id>/editar", methods=["GET", "POST"])
def categorias_edit(cat_id: int):
    cat = InvCategoria.query.get_or_404(cat_id)

    if request.method == "POST":
        nombre = (request.form.get("Nombre") or "").strip()
        descripcion = (request.form.get("Descripcion") or "").strip()

        if not nombre:
            flash("El nombre es obligatorio.", "danger")
            return redirect(url_for("inv.categorias_edit", cat_id=cat_id))

        # Validación de duplicado (excluyendo la misma Id)
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

# ---------- Activar/Inactivar (soft delete) ----------
@inv_bp.route("/categorias/<int:cat_id>/toggle", methods=["POST"])
def categorias_toggle(cat_id: int):
    cat = InvCategoria.query.get_or_404(cat_id)
    cat.Activa = not cat.Activa
    db.session.commit()
    flash(("Categoría activada." if cat.Activa else "Categoría inactivada.") , "info")
    return redirect(url_for("inv.categorias_list"))



# catálogo simple para la UI (puedes mover a tabla propia si es necesario)
UNIDADES = ["un", "kg", "g", "lt", "ml", "paq", "caja", "rollo", "bolsa", "pz"]

# ---------------------------
# LISTA DE INSUMOS
# ---------------------------
@inv_bp.route("/insumos", methods=["GET"])
@role_required("Administrador", "Recepcionista")
def insumos_list():
    q = (
        db.session.query(InvInsumo, InvCategoria)
        .join(InvCategoria, InvInsumo.Categoria_Id == InvCategoria.Id)
        .order_by(InvInsumo.Activo.desc(), InvCategoria.Nombre.asc(), InvInsumo.Nombre.asc())
    )
    data = [{"insumo": i, "categoria": c} for (i, c) in q.all()]
    return render_template("inv/insumos_list.html", items=data)

# ---------------------------
# NUEVO INSUMO (INV-07-002)
# ---------------------------
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
