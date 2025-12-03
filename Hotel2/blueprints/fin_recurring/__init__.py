from flask import Blueprint

# Blueprint de Finanzas - Cobros recurrentes (FAC-07-008)
fin_recurring_bp = Blueprint(
    "fin_recurring",
    __name__,
    url_prefix="/fin/recurring"
)

from . import routes  # noqa: E402,F401

