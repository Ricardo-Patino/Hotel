from flask import Blueprint

mnt_bp = Blueprint(
    "mnt",
    __name__,
    url_prefix="/mnt",
    template_folder="../../templates"
)

from . import routes  # noqa
