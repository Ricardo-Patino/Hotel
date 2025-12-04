from .room import Room
from .inv import InvCategoria
from .hrm import Funcionario, FuncionarioHistorial
# models/__init__.py
from .hrm import Funcionario, FuncionarioHistorial, Marcacion   # <-- agrega Marcacion aquí


from .evt import (
    EvtSalon, EvtRecurso, EvtEvento, EvtEventoRecurso, EvtPlantilla,
    EvtAsistente, EvtWaitlist, EvtPresupuesto, EvtCosto, EvtEncuesta,
    EvtNotificacion, EvtWorkOrder
)

from extensions import db
