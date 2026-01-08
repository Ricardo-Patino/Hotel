from flask import Blueprint

hrm_bp = Blueprint(
    "hrm",
    __name__,
    template_folder="templates",
    url_prefix="/hrm"
)

from . import routes


