from flask import Blueprint
ace_bp = Blueprint('ace', __name__, template_folder='templates')
from . import routes  # noqa
