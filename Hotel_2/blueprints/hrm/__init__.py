from flask import Blueprint

hrm_bp = Blueprint("hrm", __name__, url_prefix="/hrm")

from . import routes  # importa las rutas para que se registren


