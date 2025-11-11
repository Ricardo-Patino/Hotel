from flask import Blueprint
arep_bp = Blueprint("arep", __name__, url_prefix="/arep")
from . import routes  # noqa
