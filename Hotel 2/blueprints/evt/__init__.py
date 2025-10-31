from flask import Blueprint
evt_bp = Blueprint("evt", __name__, url_prefix="/evt")
from . import routes  # noqa
