from flask import Blueprint

rooms_bp = Blueprint("rooms", __name__, template_folder="templates")
from . import routes
