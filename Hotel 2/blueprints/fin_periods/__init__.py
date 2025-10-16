from flask import Blueprint
fin_periods_bp = Blueprint("fin_periods", __name__, url_prefix="/fin/periods")
from . import routes  # noqa


