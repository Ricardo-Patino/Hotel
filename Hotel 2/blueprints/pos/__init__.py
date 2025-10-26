# blueprints/pos/__init__.py
from flask import Blueprint

pos_bp = Blueprint("pos", __name__, url_prefix="/pos")

from . import routes  # importa las rutas reales



