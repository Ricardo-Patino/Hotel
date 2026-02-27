from flask import Blueprint

fin_invoices_bp = Blueprint(
    "fin_invoices",
    __name__,
    url_prefix="",  # importante: queremos /fin-invoices.html tal cual
    template_folder="../../templates"
)

from . import routes  # noqa



