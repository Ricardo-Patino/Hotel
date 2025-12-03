# services/grr/reservation_service.py
# Servicio de dominio para Reservas (GRR-01-001).
# - Crear / Editar / Cancelar / Listar / Obtener
# - Disponibilidad por solapes (rango [entrada, salida))
# - Precio robusto con COALESCE(Precio_Noche, Precio_Base)
# - Guarda Observaciones y Número_Comprobante
# - PDF mínimo opcional + fallbacks de auditoría / notificación / housekeeping

from __future__ import annotations

import os
from datetime import date, datetime
from decimal import Decimal
from typing import Any, Dict, List, Tuple

from sqlalchemy import and_
from sqlalchemy.orm import Query

from extensions import db
from models_sql import Habitacion, Reserva, Cliente, DocumentoSQL as Documento, ReservaDocumento

from extensions import db
from models_sql import Reserva
from services.grr.housekeeping_sync import on_checkout_transition


# ========= Fallbacks opcionales (no bloqueantes) =========
class _AuditServiceFallback:
    def log(self, usuario: str | None, entidad: str, entidad_id: int | None,
            accion: str, detalle: Dict[str, Any] | None = None) -> None:
        print(f"[AUDIT] usuario={usuario} entidad={entidad} id={entidad_id} "
              f"accion={accion} detalle={detalle}")


class _NotificationServiceFallback:
    def send_confirmation(self, correo: str | None, telefono: str | None, numero: str) -> None:
        print(f"[NOTIFY] confirmación -> correo={correo} tel={telefono} nro={numero}")


class _HousekeepingSyncFallback:
    def publish(self, event: str, payload: Dict[str, Any]) -> None:
        print(f"[HK] event={event} payload={payload}")


try:
    from services.grr.audit_service import AuditService as _Audit  # type: ignore
except Exception:
    _Audit = _AuditServiceFallback  # type: ignore

try:
    from services.grr.notification_service import NotificationService as _Notify  # type: ignore
except Exception:
    _Notify = _NotificationServiceFallback  # type: ignore

try:
    from services.grr.housekeeping_sync import HousekeepingSync as _HK  # type: ignore
except Exception:
    _HK = _HousekeepingSyncFallback  # type: ignore


# ========= PDF mínimo (sin dependencias pesadas) =========
def _minimal_pdf_bytes(text_lines: List[str]) -> bytes:
    lines = []
    y = 770
    for t in text_lines:
        t = t.replace("(", r"\(").replace(")", r"\)")
        lines.append(f"1 0 0 1 50 {y} Tm ({t}) Tj")
        y -= 18

    content_stream = "BT /F1 12 Tf " + " ".join(lines) + " ET"
    resources = "/Font << /F1 2 0 R >>"
    contents_len = len(content_stream.encode("latin-1", errors="ignore"))

    pdf = f"""%PDF-1.4
1 0 obj << /Type /Catalog /Pages 3 0 R >> endobj
2 0 obj << /Type /Font /Subtype /Type1 /Name /F1 /BaseFont /Helvetica >> endobj
3 0 obj << /Type /Pages /Kids [4 0 R] /Count 1 >> endobj
4 0 obj << /Type /Page /Parent 3 0 R /MediaBox [0 0 595 842] /Resources {resources} /Contents 5 0 R >> endobj
5 0 obj << /Length {contents_len} >> stream
{content_stream}
endstream endobj
xref
0 6
0000000000 65535 f 
0000000010 00000 n 
0000000063 00000 n 
0000000172 00000 n 
0000000233 00000 n 
0000000357 00000 n 
trailer << /Size 6 /Root 1 0 R >>
startxref
{357 + contents_len + 63}
%%EOF
"""
    return pdf.encode("latin-1", errors="ignore")


def _save_pdf_reserva(reserva: Reserva, habitacion: Habitacion) -> Tuple[bytes, str, str]:
    folder = os.path.join(os.getcwd(), "booking")
    os.makedirs(folder, exist_ok=True)
    numero = reserva.Numero_Comprobante or f"R-{reserva.Codigo_Reserva:08d}"
    filename = f"Comprobante_{numero}.pdf"
    path = os.path.join(folder, filename)

    cliente = getattr(reserva, "Cliente", None)
    cliente_nombre = (
        f"{cliente.Nombre} {cliente.Apellido}"
        if cliente else f"Cliente {reserva.Codigo_Cliente}"
    )
    hab_num = getattr(habitacion, "Numero_Habitacion", habitacion.Codigo_Habitacion)

    lines = [
        "Hotel Villa Grace - Comprobante de Reserva",
        f"Reserva: {numero}",
        f"Habitación: {hab_num} ({habitacion.Tipo})",
        f"Cliente: {cliente_nombre}",
        f"Entrada: {reserva.Fecha_Entrada.isoformat()}",
        f"Salida : {reserva.Fecha_Salida.isoformat()}",
        f"Monto  : {reserva.Monto_Total}",
        f"Estado : {reserva.Estado}",
        f"Canal  : {reserva.Canal or 'N/A'}",
        f"Generado: {datetime.utcnow().isoformat()}Z",
    ]
    blob = _minimal_pdf_bytes(lines)
    with open(path, "wb") as f:
        f.write(blob)
    return blob, path, "application/pdf"


def marcar_checkout(reserva_id: int):
    r = Reserva.query.get(reserva_id)
    if not r:
        raise ValueError("Reserva no existe")
    r.Estado = 'Finalizada'
    on_checkout_transition(r)
    db.session.commit()
    return r


# =========================
# Servicio principal
# =========================
class ReservationService:
    """Capa de dominio para Reservas."""

    def __init__(self, habitacion_model=Habitacion, reserva_model=Reserva, cliente_model=Cliente):
        self.H = habitacion_model
        self.R = reserva_model
        self.C = cliente_model
        self.audit = _Audit()
        self.notify = _Notify()
        self.hk = _HK()

    # ---------- Utilidades ----------
    @staticmethod
    def _coalesce_price(h: Habitacion) -> Decimal:
        """
        Devuelve el precio unitario por noche usando:
        - Precio_Noche si existe (no None)
        - Si no, Precio_Base
        - Si tampoco existe, 0
        """
        # getattr con default None para no explotar si la columna no existe en el modelo
        pn = getattr(h, "Precio_Noche", None)
        if pn is not None:
            try:
                return Decimal(pn)
            except Exception:
                pass
        pb = getattr(h, "Precio_Base", None)
        try:
            return Decimal(pb if pb is not None else 0)
        except Exception:
            return Decimal(0)

    def _overlap_clause(self, entrada: date, salida: date):
        """Solape: [entrada, salida) con [r.Fecha_Entrada, r.Fecha_Salida)"""
        R = self.R
        return and_(R.Fecha_Entrada < salida, R.Fecha_Salida > entrada)

    @staticmethod
    def _parse_dates(payload: Dict[str, Any]) -> Tuple[date, date] | None:
        try:
            ent = payload["Fecha_Entrada"]
            sal = payload["Fecha_Salida"]
            if isinstance(ent, str):
                ent = date.fromisoformat(ent)
            if isinstance(sal, str):
                sal = date.fromisoformat(sal)
            return ent, sal
        except Exception:
            return None

    # ---------- Disponibilidad ----------
    def _find_available_rooms(
        self,
        entrada: date,
        salida: date,
        tipo: str,
        excluir_reserva_id: int | None = None,
    ) -> List[Habitacion]:
        subq: Query = (
            self.R.query.with_entities(self.R.Codigo_Habitacion)
            .filter(self._overlap_clause(entrada, salida))
        )
        if excluir_reserva_id:
            subq = subq.filter(self.R.Codigo_Reserva != excluir_reserva_id)

        q: Query = (
            self.H.query
            .filter(
                self.H.Tipo == tipo,
                # No mostrar las que estén en mantenimiento (si la columna existe)
                getattr(self.H, "Estado", None) != "Mantenimiento"
                if hasattr(self.H, "Estado") else True,
                ~self.H.Codigo_Habitacion.in_(subq),
            )
            .order_by(self.H.Codigo_Habitacion.asc())
        )
        return list(q.all())

    # ---------- Crear ----------
    def create(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        """
        Requiere:
          - Codigo_Cliente (int)
          - Codigo_Funcionario (int)
          - Fecha_Entrada (YYYY-MM-DD)
          - Fecha_Salida  (YYYY-MM-DD)
          - Tipo (str) o Codigo_Habitacion (int)
        Opcionales:
          - Canal, Huespedes, Codigo_Descuento, Observaciones
        """
        base_required = ["Codigo_Cliente", "Codigo_Funcionario", "Fecha_Entrada", "Fecha_Salida"]
        missing = [k for k in base_required if k not in payload or payload[k] in (None, "")]
        if not payload.get("Codigo_Habitacion") and not payload.get("Tipo"):
            missing.append("Tipo")
        if missing:
            return {"ok": False, "error": f"Faltan campos: {', '.join(missing)}"}

        parsed = self._parse_dates(payload)
        if not parsed:
            return {"ok": False, "error": "Formato de fecha inválido. Use YYYY-MM-DD"}
        entrada, salida = parsed
        if salida <= entrada:
            return {"ok": False, "error": "Fecha_Salida debe ser mayor que Fecha_Entrada"}

        canal = payload.get("Canal", "Web")
        descuento = payload.get("Codigo_Descuento")
        huespedes = int(payload.get("Huespedes", 1))
        observaciones = payload.get("Observaciones")

        # Determinar habitación
        if payload.get("Codigo_Habitacion"):
            habitacion = self.H.query.get(int(payload["Codigo_Habitacion"]))
            if not habitacion:
                return {"ok": False, "error": "Habitación no encontrada"}

            # Validar solapes
            existe_solape = (
                self.R.query.filter(
                    self.R.Codigo_Habitacion == habitacion.Codigo_Habitacion,
                    self._overlap_clause(entrada, salida)
                ).first()
            )
            if existe_solape:
                return {"ok": False, "error": "La habitación indicada no está disponible en esas fechas"}
        else:
            tipo = str(payload["Tipo"])
            disponibles = self._find_available_rooms(entrada, salida, tipo)
            if not disponibles:
                return {"ok": False, "error": "Sin disponibilidad para el rango solicitado"}
            habitacion = disponibles[0]

        noches = (salida - entrada).days
        precio_noche = self._coalesce_price(habitacion)
        total = (precio_noche * noches).quantize(Decimal("0.01"))

        r = self.R(
            Codigo_Cliente=int(payload["Codigo_Cliente"]),
            Codigo_Habitacion=habitacion.Codigo_Habitacion,
            Codigo_Funcionario=int(payload["Codigo_Funcionario"]),
            Fecha_Entrada=entrada,
            Fecha_Salida=salida,
            Monto_Total=total,
            Estado="Confirmada",
            Canal=canal,
            Descuento_Id=descuento,
            Observaciones=observaciones,
        )

        try:
            db.session.add(r)

            # Hint de estado para operación (si existe la columna)
            try:
                if hasattr(habitacion, "Estado"):
                    habitacion.Estado = "Ocupada"
            except Exception:
                pass

            db.session.flush()  # obtener PK
            r.Numero_Comprobante = f"R-{r.Codigo_Reserva:08d}"

            # Documento de comprobante (opcional)
            try:
                blob, pdf_path, mime = _save_pdf_reserva(r, habitacion)
                doc = Documento(Tipo="Comprobante", Ruta=pdf_path, MimeType=mime, TamanoBytes=len(blob))
                db.session.add(doc)
                db.session.flush()
                db.session.add(ReservaDocumento(Codigo_Reserva=r.Codigo_Reserva, Documento_Id=doc.Id))
            except Exception as e_doc:
                print(f"[WARN] No se pudo registrar comprobante: {e_doc}")

            db.session.commit()

            # Post-commit no críticos
            try:
                self.audit.log(
                    payload.get("Usuario"),
                    "Reserva",
                    r.Codigo_Reserva,
                    "CREATE",
                    {
                        "canal": canal,
                        "noches": noches,
                        "monto": float(total),
                        "habitacion": getattr(habitacion, "Numero_Habitacion", habitacion.Codigo_Habitacion),
                        "huespedes": huespedes,
                    },
                )
            except Exception:
                pass

            try:
                cli = self.C.query.get(r.Codigo_Cliente)
                self.notify.send_confirmation(
                    getattr(cli, "Correo", None),
                    getattr(cli, "Telefono", None),
                    r.Numero_Comprobante
                )
            except Exception:
                pass

            try:
                self.hk.publish(
                    "reservation_created",
                    {
                        "reserva_id": r.Codigo_Reserva,
                        "habitacion": getattr(habitacion, "Numero_Habitacion", habitacion.Codigo_Habitacion),
                        "entrada": entrada.isoformat(),
                        "salida": salida.isoformat(),
                    },
                )
            except Exception:
                pass

            return {
                "ok": True,
                "reserva_id": r.Codigo_Reserva,
                "numero": r.Numero_Comprobante,
                "monto": float(r.Monto_Total),
                "habitacion": getattr(habitacion, "Numero_Habitacion", habitacion.Codigo_Habitacion),
            }
        except Exception as e:
            db.session.rollback()
            return {"ok": False, "error": f"Error al crear reserva: {e}"}

    # ---------- Obtener ----------
    def get(self, reserva_id: int) -> Dict[str, Any]:
        r: Reserva | None = self.R.query.get(reserva_id)
        if not r:
            return {"ok": False, "error": "Reserva no encontrada"}
        hab_num = r.Habitacion.Numero_Habitacion if r.Habitacion else None
        cli_nom = f"{r.Cliente.Nombre} {r.Cliente.Apellido}" if r.Cliente else None

        return {
            "ok": True,
            "data": {
                "Codigo_Reserva": r.Codigo_Reserva,
                "Codigo_Cliente": r.Codigo_Cliente,
                "Cliente_Nombre": cli_nom,
                "Codigo_Habitacion": r.Codigo_Habitacion,
                "Habitacion_Numero": hab_num,
                "Tipo": r.Habitacion.Tipo if r.Habitacion else None,
                "Fecha_Entrada": r.Fecha_Entrada.isoformat(),
                "Fecha_Salida": r.Fecha_Salida.isoformat(),
                "Monto_Total": float(r.Monto_Total),
                "Estado": r.Estado,
                "Canal": r.Canal,
                "Numero_Comprobante": r.Numero_Comprobante,
                "Observaciones": getattr(r, "Observaciones", None),
            },
        }

    # ---------- Editar ----------
    def update(self, reserva_id: int, payload: Dict[str, Any]) -> Dict[str, Any]:
        r: Reserva | None = self.R.query.get(reserva_id)
        if not r:
            return {"ok": False, "error": "Reserva no encontrada"}

        entrada = payload.get("Fecha_Entrada", r.Fecha_Entrada)
        salida = payload.get("Fecha_Salida", r.Fecha_Salida)
        if isinstance(entrada, str):
            entrada = date.fromisoformat(entrada)
        if isinstance(salida, str):
            salida = date.fromisoformat(salida)
        if salida <= entrada:
            return {"ok": False, "error": "Fecha_Salida debe ser mayor que Fecha_Entrada"}

        nuevo_tipo = payload.get("Tipo", r.Habitacion.Tipo if r.Habitacion else None)

        # Revalidar disponibilidad si cambian fechas o tipo
        if (r.Habitacion and nuevo_tipo and nuevo_tipo != r.Habitacion.Tipo) or \
           entrada != r.Fecha_Entrada or salida != r.Fecha_Salida:
            opciones = self._find_available_rooms(
                entrada, salida, nuevo_tipo or r.Habitacion.Tipo, excluir_reserva_id=reserva_id
            )
            if not opciones:
                return {"ok": False, "code": "conflict", "error": "Sin disponibilidad para los cambios solicitados"}
            ids = [h.Codigo_Habitacion for h in opciones]
            if r.Codigo_Habitacion not in ids:
                r.Codigo_Habitacion = ids[0]

        r.Fecha_Entrada = entrada
        r.Fecha_Salida = salida
        if "Canal" in payload:
            r.Canal = payload["Canal"]
        if "Estado" in payload:
            r.Estado = payload["Estado"]
        if "Observaciones" in payload:
            r.Observaciones = payload["Observaciones"]

        try:
            db.session.commit()
            try:
                self.audit.log(payload.get("Usuario"), "Reserva", r.Codigo_Reserva, "UPDATE",
                               {"fields": list(payload.keys())})
            except Exception:
                pass
            return {"ok": True, "reserva_id": r.Codigo_Reserva}
        except Exception as e:
            db.session.rollback()
            return {"ok": False, "error": f"Error al actualizar: {e}"}

    # ---------- Cancelar ----------
    def cancel(self, reserva_id: int, motivo: str, usuario: str | None = None) -> Dict[str, Any]:
        r: Reserva | None = self.R.query.get(reserva_id)
        if not r:
            return {"ok": False, "error": "Reserva no encontrada"}

        try:
            r.Estado = "Cancelada"
            # Liberar estado de la habitación si existe la columna
            try:
                if r.Habitacion and hasattr(r.Habitacion, "Estado"):
                    r.Habitacion.Estado = "Disponible"
            except Exception:
                pass

            db.session.commit()

            try:
                self.audit.log(usuario, "Reserva", r.Codigo_Reserva, "CANCEL", {"motivo": motivo})
            except Exception:
                pass

            try:
                self.hk.publish(
                    "reservation_cancelled",
                    {
                        "reserva_id": r.Codigo_Reserva,
                        "habitacion": r.Habitacion.Numero_Habitacion if r.Habitacion else None,
                    },
                )
            except Exception:
                pass

            return {"ok": True}
        except Exception as e:
            db.session.rollback()
            return {"ok": False, "error": f"Error al cancelar: {e}"}

    # ---------- Listado con filtros ----------
    def list(self, filters: Dict[str, Any] | None = None, page: int = 1, page_size: int = 20) -> Dict[str, Any]:
        filters = filters or {}
        q: Query = self.R.query

        if filters.get("desde"):
            ini = filters["desde"]
            if isinstance(ini, str):
                ini = date.fromisoformat(ini)
            q = q.filter(self.R.Fecha_Entrada >= ini)

        if filters.get("hasta"):
            fin = filters["hasta"]
            if isinstance(fin, str):
                fin = date.fromisoformat(fin)
            q = q.filter(self.R.Fecha_Salida <= fin)

        if filters.get("estado"):
            q = q.filter(self.R.Estado == filters["estado"])

        if filters.get("canal"):
            q = q.filter(self.R.Canal == filters["canal"])

        q = q.order_by(self.R.Fecha_Entrada.desc())

        total = q.count()
        page = max(page, 1)
        page_size = max(page_size, 1)

        rows = q.limit(page_size).offset((page - 1) * page_size).all()

        items: List[Dict[str, Any]] = []
        for r in rows:
            cliente_nombre = f"{r.Cliente.Nombre} {r.Cliente.Apellido}" if r.Cliente else str(r.Codigo_Cliente)
            items.append(
                {
                    "Codigo_Reserva": r.Codigo_Reserva,
                    "Cliente": cliente_nombre,
                    "Codigo_Cliente": r.Codigo_Cliente,
                    "Habitacion": r.Habitacion.Numero_Habitacion if r.Habitacion else None,
                    "Tipo": r.Habitacion.Tipo if r.Habitacion else None,
                    "Entrada": r.Fecha_Entrada.isoformat(),
                    "Salida": r.Fecha_Salida.isoformat(),
                    "Estado": r.Estado,
                    "Canal": r.Canal,
                    "Numero_Comprobante": r.Numero_Comprobante,
                    "Monto_Total": float(r.Monto_Total),
                    "Observaciones": getattr(r, "Observaciones", None),
                }
            )
        return {"ok": True, "items": items, "page": page, "page_size": page_size, "total": total}

    # ---------- Helper público para el front (fallback de precios) ----------
    def public_price_of(self, codigo_habitacion: int) -> float:
        """
        Devuelve el precio por noche (float) aplicando COALESCE.
        Útil como Fallback en booking-details cuando el precio llega vacío.
        """
        h = self.H.query.get(int(codigo_habitacion))
        if not h:
            return 0.0
        return float(self._coalesce_price(h))


from datetime import date
from models_sql import TarifaTemporada, Temporada

def precio_noche_vigente(hab_id: int, fecha: date, precio_base: float) -> float:
    t = (db.session.query(TarifaTemporada)
         .join(Temporada, TarifaTemporada.Temporada_Id == Temporada.Id)
         .filter(TarifaTemporada.Codigo_Habitacion == hab_id,
                 Temporada.Fecha_Inicio <= fecha,
                 Temporada.Fecha_Fin   >= fecha)
         .first())
    return float(t.Precio_Noche) if t else float(precio_base)

