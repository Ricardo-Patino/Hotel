# blueprints/grr/__init__.py
from flask import Blueprint

grr_bp = Blueprint('grr', __name__, url_prefix='/grr')

from . import routes  # noqa
