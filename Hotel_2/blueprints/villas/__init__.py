from flask import Blueprint

villas_bp = Blueprint("villas", __name__, url_prefix="")

from . import routes  # noqa