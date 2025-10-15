# services/assignment.py
from datetime import date, timedelta
from typing import Optional, List, Dict, Any
from flask import current_app
from sqlalchemy import text
from extensions import db

def _overlap_clause():
    # NOT (nuevo_desde >= exist_hasta OR nuevo_hasta <= exist_desde)
    return """
      NOT (
        :ci >= E.Fecha_Hasta
        OR :co <= E.Fecha_Desde
      )
    """

def _fetch_user_prefs(codigo_cliente: int) -> Dict[str,str]:
    try:
        rows = db.session.execute(
            text("SELECT Clave, Valor FROM PreferenciaHuesped WHERE Codigo_Cliente=:c"),
            {"c": codigo_cliente}
        ).all()
        return {r[0]: r[1] for r in rows}
    except Exception:
        return {}

def _habitaciones_candidatas(tipo: Optional[str], ci: str, co: str) -> List[Dict[str,Any]]:
    # Excluye mantenimiento y ocupadas por solapamiento (ReservaEstancia + Reserva confirmada/pendiente)
    sql = text(f"""
      SELECT H.Codigo_Habitacion, H.Numero_Habitacion, H.Tipo, H.Precio_Noche
      FROM Habitacion H
      WHERE H.Estado <> 'Mantenimiento'
        AND (:tipo IS NULL OR H.Tipo = :tipo)
        AND H.Codigo_Habitacion NOT IN (
            SELECT E.Habitacion_Id
            FROM ReservaEstancia E
            JOIN Reserva R ON R.Codigo_Reserva = E.Codigo_Reserva
            WHERE R.Estado IN ('Confirmada','Pendiente')
              AND {_overlap_clause()}
        )
      ORDER BY H.Tipo, H.Precio_Noche ASC, H.Numero_Habitacion
    """)
    rows = db.session.execute(sql, {"tipo": tipo, "ci": ci, "co": co}).mappings().all()
    return [dict(r) for r in rows]

def _score_room(h: Dict[str,Any], prefs: Dict[str,str]) -> float:
    # Heurística simple: preferir tipo coincidente (ya filtrado), precio más bajo y num. menor
    base = 100.0
    # Bonus por prefs simples (ejemplos: 'vista'='mar' -> rooms 2xx? adapta si tienes metadata)
    # Aquí dejamos ganchos; si no hay metadatos, no afecta.
    return base - float(h.get("Precio_Noche") or 0) * 0.0001

def _insert_estancia(reserva_id: int, room_id: int, ci: str, co: str):
    db.session.execute(
        text("""
          INSERT INTO ReservaEstancia (Codigo_Reserva, Habitacion_Id, Fecha_Desde, Fecha_Hasta, Estado)
          VALUES (:r,:h,:ci,:co,'Asignada')
        """),
        {"r": reserva_id, "h": room_id, "ci": ci, "co": co}
    )

def _update_reserva_room_legacy(reserva_id: int, room_id: int):
    # Mantener compatibilidad: espejo en Reserva.Codigo_Habitacion
    db.session.execute(
        text("UPDATE Reserva SET Codigo_Habitacion=:h WHERE Codigo_Reserva=:r"),
        {"h": room_id, "r": reserva_id}
    )

def auto_assign_for_reserva(reserva_id: int, preferir_tipo: bool = True, allow_split: bool = True) -> Dict[str,Any]:
    """
    Ejecuta reglas simples:
      1) Intentar 1 sola habitación para todo el rango.
      2) Si no hay, y allow_split=True: dividir en tramos contiguos con distintas habitaciones.
    Guarda AsignacionDecision con el detalle.
    """
    r = db.session.execute(
        text("""
          SELECT Codigo_Reserva, Codigo_Cliente, Fecha_Entrada ci, Fecha_Salida co,
                 Huespedes, Observaciones, Estado,
                 H.Tipo AS TipoHabitacionDeseada
          FROM Reserva R
          JOIN Habitacion H ON H.Codigo_Habitacion = R.Codigo_Habitacion
          WHERE R.Codigo_Reserva=:id
          LIMIT 1
        """), {"id": reserva_id}
    ).mappings().first()
    if not r:
        return {"ok": False, "error": "not_found"}

    ci, co = str(r["ci"]), str(r["co"])
    prefs = _fetch_user_prefs(int(r["Codigo_Cliente"]))

    # 1) Candidatas para todo el rango
    candidates = _habitaciones_candidatas(r["TipoHabitacionDeseada"] if preferir_tipo else None, ci, co)
    scoring = [{"room": c, "score": _score_room(c, prefs)} for c in candidates]
    scoring.sort(key=lambda x: x["score"], reverse=True)

    selected = None
    if scoring:
        selected = scoring[0]["room"]
        _insert_estancia(reserva_id, int(selected["Codigo_Habitacion"]), ci, co)
        _update_reserva_room_legacy(reserva_id, int(selected["Codigo_Habitacion"]))
        resultado = "ok"
        plan = [{"desde": ci, "hasta": co, "habitacion_id": int(selected["Codigo_Habitacion"])}]
    else:
        # 2) Intentar split (día a día) si está habilitado
        if not allow_split:
            resultado = "sin_disponibilidad"
            plan = []
        else:
            # crear tramos día a día buscando mejor candidata disponible para cada día
            d = date.fromisoformat(ci)
            end = date.fromisoformat(co)
            plan = []
            resultado = "fallback"
            while d < end:
                d2 = d + timedelta(days=1)
                cands = _habitaciones_candidatas(r["TipoHabitacionDeseada"] if preferir_tipo else None, d.isoformat(), d2.isoformat())
                if not cands:
                    resultado = "sin_disponibilidad"
                    plan = []  # aborta: no se pudo cubrir todo el rango
                    break
                cands_scored = sorted(
                    [{"room": c, "score": _score_room(c, prefs)} for c in cands],
                    key=lambda x: x["score"], reverse=True
                )
                pick = cands_scored[0]["room"]
                plan.append({"desde": d.isoformat(), "hasta": d2.isoformat(), "habitacion_id": int(pick["Codigo_Habitacion"])})
                d = d2

            # consolidar tramos contiguos misma habitación
            if plan:
                merged = []
                for tramo in plan:
                    if not merged or merged[-1]["habitacion_id"] != tramo["habitacion_id"]:
                        merged.append(tramo)
                    else:
                        merged[-1]["hasta"] = tramo["hasta"]
                plan = merged
                for tramo in plan:
                    _insert_estancia(reserva_id, tramo["habitacion_id"], tramo["desde"], tramo["hasta"])
                # Por compatibilidad, setear la primera
                _update_reserva_room_legacy(reserva_id, plan[0]["habitacion_id"])

    # Trazabilidad
    db.session.execute(
        text("""
          INSERT INTO AsignacionDecision (Codigo_Reserva, Tipo, Resultado, Detalle)
          VALUES (:r, :t, :res, :det)
        """),
        {
            "r": reserva_id,
            "t": "auto",
            "res": resultado,
            "det": {
                "prefs": prefs,
                "candidatos": [s["room"] for s in scoring][:20],  # recorte
                "plan": plan
            }
        }
    )
    db.session.commit()
    return {"ok": True, "resultado": resultado, "plan": plan}

def reassign_reserva(reserva_id: int, room_id: int, ci: Optional[str]=None, co: Optional[str]=None):
    # Si no se pasan fechas, re-asigna todas las estancias al rango completo de la reserva (una sola estancia)
    r = db.session.execute(
        text("SELECT Fecha_Entrada ci, Fecha_Salida co FROM Reserva WHERE Codigo_Reserva=:id"),
        {"id": reserva_id}
    ).first()
    if not r:
        return {"ok": False, "error": "not_found"}
    ci = ci or str(r[0]); co = co or str(r[1])

    # Validar conflictos
    conflict = db.session.execute(
        text(f"""
          SELECT 1
          FROM ReservaEstancia E
          JOIN Reserva R ON R.Codigo_Reserva=E.Codigo_Reserva
          WHERE E.Habitacion_Id=:h AND R.Estado IN ('Confirmada','Pendiente')
            AND {_overlap_clause()}
            AND R.Codigo_Reserva <> :rid
          LIMIT 1
        """), {"h": room_id, "ci": ci, "co": co, "rid": reserva_id}
    ).first()
    if conflict:
        return {"ok": False, "error": "conflict"}

    # Borrar estancias previas y crear única
    db.session.execute(text("DELETE FROM ReservaEstancia WHERE Codigo_Reserva=:r"), {"r": reserva_id})
    _insert_estancia(reserva_id, room_id, ci, co)
    _update_reserva_room_legacy(reserva_id, room_id)

    db.session.execute(
        text("""INSERT INTO AsignacionDecision (Codigo_Reserva, Tipo, Resultado, Detalle)
                VALUES (:r, 'reassign', 'ok', JSON_OBJECT('room_id', :h, 'ci', :ci, 'co', :co))"""),
        {"r": reserva_id, "h": room_id, "ci": ci, "co": co}
    )
    db.session.commit()
    return {"ok": True}

def split_reserva(reserva_id: int, tramos: List[Dict[str,str]]):
    # tramos: [{habitacion_id, desde, hasta}, ...]
    db.session.execute(text("DELETE FROM ReservaEstancia WHERE Codigo_Reserva=:r"), {"r": reserva_id})
    for t in tramos:
        _insert_estancia(reserva_id, int(t["habitacion_id"]), t["desde"], t["hasta"])
    # setear primera habitación por compatibilidad
    if tramos:
        _update_reserva_room_legacy(reserva_id, int(tramos[0]["habitacion_id"]))
    db.session.execute(
        text("INSERT INTO AsignacionDecision (Codigo_Reserva, Tipo, Resultado, Detalle) VALUES (:r,'split','ok', :det)"),
        {"r": reserva_id, "det": {"tramos": tramos}}
    )
    db.session.commit()
    return {"ok": True}

def merge_reserva(reserva_id: int, room_id: int):
    r = db.session.execute(
        text("SELECT Fecha_Entrada ci, Fecha_Salida co FROM Reserva WHERE Codigo_Reserva=:id"),
        {"id": reserva_id}
    ).first()
    if not r:
        return {"ok": False, "error": "not_found"}
    return reassign_reserva(reserva_id, room_id, str(r[0]), str(r[1]))
