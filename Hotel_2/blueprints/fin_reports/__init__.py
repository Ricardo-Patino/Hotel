# blueprints/fin_reports/__init__.py
from flask import Blueprint

fin_reports_bp = Blueprint(
    "fin_reports", __name__, url_prefix="/fin/reports", template_folder="../../templates"
)

from . import routes  # noqa


