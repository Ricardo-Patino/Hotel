# blueprints/fin_kpi/__init__.py
from flask import Blueprint

# Endpoint base para los KPIs contables
fin_kpi_bp = Blueprint(
    "fin_kpi",
    __name__,
    url_prefix="/api/kpi/contabilidad"
)

from . import routes  # noqa: F401
