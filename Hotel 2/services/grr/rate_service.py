# services/grr/rate_service.py
from decimal import Decimal
from models.grr import CodigoDescuento

class RateService:
    def apply_pricing(self, precio_noche: Decimal, noches: int, descuento_codigo: str | None):
        base = Decimal(precio_noche) * int(noches)
        dto = Decimal('0')
        cod_id = None
        if descuento_codigo:
            code = CodigoDescuento.query.filter_by(Codigo=descuento_codigo, Activo=True).first()
            if code:
                cod_id = code.Id
                if code.MontoFijo: dto += Decimal(code.MontoFijo)
                if code.Porcentaje: dto += (base * Decimal(code.Porcentaje) / Decimal('100'))
        total = max(Decimal('0.00'), base - dto)
        return {"base": float(base), "descuento": float(dto), "total": float(total), "codigo_id": cod_id}
