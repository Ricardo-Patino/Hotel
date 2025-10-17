# blueprints/inv/routes.py
from flask import Blueprint, render_template, request, redirect, url_for, flash
from sqlalchemy import text
from extensions import db
from models import InvCategoria

inv_bp = Blueprint("inv", __name__, template_folder="../../templates")

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
