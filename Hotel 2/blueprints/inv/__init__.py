from flask import Blueprint

inv_bp = Blueprint("inv", __name__, url_prefix="/inv")  # único blueprint

from . import routes  # noqa: E402,F401



