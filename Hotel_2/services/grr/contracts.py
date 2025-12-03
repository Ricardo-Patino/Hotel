# services/grr/contracts.py
from abc import ABC, abstractmethod
from typing import Tuple

class IAvailabilityService(ABC):
    @abstractmethod
    def validate(self, fecha_entrada, fecha_salida, tipo, capacidad, excluir_reserva_id=None) -> Tuple[bool, list]:
        ...

class IReservationService(ABC):
    @abstractmethod
    def create(self, payload: dict) -> dict: ...
    @abstractmethod
    def update(self, reserva_id: int, payload: dict) -> dict: ...
    @abstractmethod
    def cancel(self, reserva_id: int, motivo: str, usuario: str | None = None) -> dict: ...
    @abstractmethod
    def checkin(self, reserva_id: int, doc_front: bytes | None, doc_back: bytes | None, mime: str = 'image/jpeg') -> dict: ...
    @abstractmethod
    def checkout(self, reserva_id: int) -> dict: ...
