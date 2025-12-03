from flask import Blueprint

inv_bp = Blueprint("inv", __name__, url_prefix="/inv", template_folder="../../templates")

from . import routes # noqa: E402,F401



