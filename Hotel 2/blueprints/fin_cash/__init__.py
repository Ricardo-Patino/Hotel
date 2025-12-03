# blueprints/fin_cash/__init__.py
from flask import Blueprint

fin_cash_bp = Blueprint(
    "fin_cash", __name__, url_prefix="/fin/caja", template_folder="../../templates"
)

from . import routes  # noqa

