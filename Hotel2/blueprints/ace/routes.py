from flask import request, render_template, jsonify, abort, send_file, current_app, session
from werkzeug.utils import secure_filename
from datetime import datetime
import io

from models import db  # tu SQLAlchemy
from sqlalchemy import text, or_
import os

# -------- Helpers de rol --------
ROLE_ORDER = {'Cliente': 1, 'Recepcionista': 2, 'Administrador': 3}

def current_role():
    # Ajusta si tu sesión guarda el rol con otra clave
    return session.get('rol_nombre', 'Administrador')

def can_view(min_role):
    return ROLE_ORDER.get(current_role(), 0) >= ROLE_ORDER.get(min_role, 99)

# -------- ACE-01-001: Historial de reservas --------
@current_app.route('/ace/reservas/historial')
def ace_hist_reservas():
    q = """
      SELECT * FROM v_hist_reservas
      WHERE (:cli IS NULL OR Correo = :cli OR Cedula = :cli)
        AND (:desde IS NULL OR Fecha_Entrada >= :desde)
        AND (:hasta IS NULL OR Fecha_Salida  <= :hasta)
      ORDER BY Fecha_Entrada DESC, Codigo_Reserva DESC
    """
    params = {
        'cli': request.args.get('cliente'),
        'desde': request.args.get('desde'),
        'hasta': request.args.get('hasta'),
    }
    rows = db.session.execute(text(q), params).mappings().all()
    return render_template('ace/historial_reservas.html', rows=rows)

# -------- ACE-01-003: Historial de pagos --------
@current_app.route('/ace/pagos/historial')
def ace_hist_pagos():
    q = """
      SELECT p.*, r.Correo AS ClienteCorreo, concat(c.Nombre,' ',c.Apellido) AS Cliente
      FROM v_hist_pagos p
      LEFT JOIN Reserva rr ON rr.Codigo_Reserva = p.Codigo_Reserva
      LEFT JOIN Cliente c  ON c.Codigo_Cliente = rr.Codigo_Cliente
      LEFT JOIN Cliente c2 ON 1=0
      LEFT JOIN (SELECT Codigo_Cliente, Correo FROM Cliente) r ON r.Codigo_Cliente = rr.Codigo_Cliente
      WHERE (:cli IS NULL OR r.Correo = :cli)
        AND (:desde IS NULL OR p.fecha >= :desde)
        AND (:hasta IS NULL OR p.fecha <= :hasta)
      ORDER BY p.fecha DESC, p.origen
    """
    params = {
        'cli': request.args.get('cliente'),
        'desde': request.args.get('desde'),
        'hasta': request.args.get('hasta'),
    }
    rows = db.session.execute(text(q), params).mappings().all()
    return render_template('ace/historial_pagos.html', rows=rows)

# -------- ACE-01-007: Alta de cliente con validación de duplicados --------
@current_app.route('/ace/clientes/alta', methods=['POST'])
def ace_alta_cliente():
    data = request.form if request.form else request.json
    if not data:
        abort(400, 'Datos vacíos')

    cedula = data.get('Cedula')
    correo = data.get('Correo')

    dup = db.session.execute(
        text("SELECT 1 FROM Cliente WHERE Cedula=:ced OR Correo=:cor LIMIT 1"),
        {'ced': cedula, 'cor': correo}
    ).first()
    if dup:
        return jsonify({'ok': False, 'msg': 'Duplicado: cédula o correo ya existen'}), 409

    ins = text("""
      INSERT INTO Cliente (Cedula,Nombre,Apellido,Telefono,Correo,Fecha_Nacimiento)
      VALUES (:ced,:nom,:ape,:tel,:cor,:fnac)
    """)
    db.session.execute(ins, {
        'ced': cedula,
        'nom': data.get('Nombre'),
        'ape': data.get('Apellido'),
        'tel': data.get('Telefono'),
        'cor': correo,
        'fnac': data.get('Fecha_Nacimiento', '1990-01-01')
    })
    db.session.commit()

    cod = db.session.execute(text("SELECT LAST_INSERT_ID() AS id")).scalar_one()
    return jsonify({'ok': True, 'Codigo_Cliente': cod})

# -------- ACE-01-005/006: Carga de documentos por huésped + visibilidad por rol --------
def allowed_ext(filename):
    ALLOWED = {'.pdf', '.jpg', '.jpeg', '.png'}
    _, ext = os.path.splitext(filename.lower())
    return ext in ALLOWED

@current_app.route('/ace/clientes/<int:cliente_id>/documentos', methods=['POST'])
def ace_upload_doc(cliente_id):
    if current_role() not in ROLE_ORDER:
        abort(403)

    f = request.files.get('file')
    if not f or f.filename == '':
        abort(400, 'Archivo requerido')

    if not allowed_ext(f.filename):
        abort(415, 'Formato no permitido (.pdf, .jpg, .jpeg, .png)')

    min_rol = request.form.get('Min_Rol', 'Cliente')
    if min_rol not in ROLE_ORDER:
        min_rol = 'Cliente'

    upload_dir = current_app.config.get('UPLOAD_FOLDER_CLIENTES', 'uploads/clientes')
    os.makedirs(os.path.join(upload_dir, str(cliente_id)), exist_ok=True)

    safe = secure_filename(f.filename)
    stored_path = os.path.join(upload_dir, str(cliente_id), safe)
    f.save(stored_path)

    # Persistimos en Documento + ClienteDocumento
    ins_doc = text("""
      INSERT INTO Documento (Tipo, Ruta, MimeType, TamanoBytes)
      VALUES (:tipo, :ruta, :mime, :size)
    """)
    mime = 'application/pdf' if safe.lower().endswith('.pdf') else 'image/jpeg'
    size = os.path.getsize(stored_path)

    db.session.execute(ins_doc, {
        'tipo': request.form.get('Tipo', 'OTRO'),
        'ruta': stored_path,
        'mime': mime,
        'size': size
    })
    doc_id = db.session.execute(text("SELECT LAST_INSERT_ID()")).scalar_one()

    db.session.execute(text("""
      INSERT INTO ClienteDocumento (Codigo_Cliente, Documento_Id, Tipo, Min_Rol, Visible)
      VALUES (:cli,:doc,:tipo,:rol,1)
    """), {
        'cli': cliente_id,
        'doc': doc_id,
        'tipo': request.form.get('Tipo', 'OTRO'),
        'rol': min_rol
    })
    db.session.commit()

    return jsonify({'ok': True, 'Documento_Id': doc_id})

# Descarga con control de rol (solo si cumple Min_Rol)
@current_app.route('/ace/clientes/<int:cliente_id>/documentos/<int:doc_id>')
def ace_get_doc(cliente_id, doc_id):
    row = db.session.execute(text("""
      SELECT d.Ruta, cd.Min_Rol, cd.Visible
      FROM ClienteDocumento cd
      JOIN Documento d ON d.Id = cd.Documento_Id
      WHERE cd.Codigo_Cliente=:cli AND cd.Documento_Id=:doc
      LIMIT 1
    """), {'cli': cliente_id, 'doc': doc_id}).mappings().first()

    if not row or row['Visible'] != 1:
        abort(404)

    if not can_view(row['Min_Rol']):
        abort(403)

    return send_file(row['Ruta'], as_attachment=True)

# -------- ACE-01-008: Exportación vCard --------
@current_app.route('/ace/clientes/<int:cliente_id>/vcard')
def ace_vcard(cliente_id):
    cli = db.session.execute(text("""
      SELECT Nombre, Apellido, Correo, Telefono
      FROM Cliente WHERE Codigo_Cliente=:id
    """), {'id': cliente_id}).mappings().first()
    if not cli:
        abort(404)

    vcard = f"""BEGIN:VCARD
VERSION:3.0
N:{cli['Apellido']};{cli['Nombre']};;;
FN:{cli['Nombre']} {cli['Apellido']}
EMAIL;TYPE=INTERNET:{cli['Correo'] or ''}
TEL;TYPE=CELL:{cli['Telefono'] or ''}
END:VCARD
"""
    return send_file(
        io.BytesIO(vcard.encode('utf-8')),
        mimetype='text/vcard',
        as_attachment=True,
        download_name=f"cliente_{cliente_id}.vcf"
    )
