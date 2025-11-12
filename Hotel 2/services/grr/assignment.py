# services/grr/assignment.py
from __future__ import annotations

import json
from datetime import date, timedelta
from typing import Optional, List, Dict, Any, Tuple

from sqlalchemy import text
from extensions import db


# -------------------------- Utilidades internas --------------------------

def _get_reserva(rid: int):
    """
    Obtiene datos mínimos de la reserva. No asume que ya tenga habitación asignada.
    Intenta traer Tipo de la habitación actual; si no hay, queda en None.
    """
    row = db.session.execute(
        text("""
            SELECT 
                r.Codigo_Reserva,
                r.Codigo_Cliente,
                r.Codigo_Habitacion,
                r.Fecha_Entrada   AS ci,
                r.Fecha_Salida    AS co,
                r.Estado          AS estado,
                h.Tipo            AS tipo_actual
            FROM Reserva r
            LEFT JOIN Habitacion h ON h.Codigo_Habitacion = r.Codigo_Habitacion
            WHERE r.Codigo_Reserva = :rid
            LIMIT 1
        """),
        {"rid": int(rid)}
    ).mappings().first()
    return row


def _habitaciones_candidatas(tipo: Optional[str], limit: int = 400) -> List[int]:
    """
    Devuelve IDs de habitaciones candidatas. Si existe la columna Tipo, filtra por ella
    cuando 'tipo' no es None; si no existe o hay error, hace fallback a todas.
    """
    # Intento con filtro por tipo
    try:
        rows = db.session.execute(
            text("""
                SELECT h.Codigo_Habitacion
                FROM Habitacion h
                WHERE (:t IS NULL OR COALESCE(h.Tipo,'') = :t)
                ORDER BY h.Codigo_Habitacion
                LIMIT :lim
            """),
            {"t": tipo, "lim": int(limit)}
        ).mappings().all()
        return [r["Codigo_Habitacion"] for r in rows]
    except Exception:
        # Fallback: ignorar columna Tipo
        rows = db.session.execute(
            text("""
                SELECT h.Codigo_Habitacion
                FROM Habitacion h
                ORDER BY h.Codigo_Habitacion
                LIMIT :lim
            """),
            {"lim": int(limit)}
        ).mappings().all()
        return [r["Codigo_Habitacion"] for r in rows]


def _hay_solape(habitacion_id: int, ci, co, exclude_reserva_id: Optional[int] = None) -> bool:
    """
    True si existe una reserva Confirmada/Pendiente que se solapa con [ci, co) en esa habitación.
    Usa solo la tabla Reserva (no depende de ReservaEstancia).
    """
    params = {"h": int(habitacion_id), "ci": ci, "co": co}
    cond_excl = ""
    if exclude_reserva_id:
        cond_excl = " AND r.Codigo_Reserva <> :rid "
        params["rid"] = int(exclude_reserva_id)

    row = db.session.execute(
        text(f"""
            SELECT 1
              FROM Reserva r
             WHERE r.Codigo_Habitacion = :h
               AND r.Estado IN ('Confirmada','Pendiente')
               AND DATE(r.Fecha_Entrada) < DATE(:co)
               AND DATE(r.Fecha_Salida)  > DATE(:ci)
               {cond_excl}
             LIMIT 1
        """),
        params
    ).first()
    return row is not None


def _asentar_estancia_si_existe(reserva_id: int, habitacion_id: int, ci, co) -> None:
    """
    Intenta escribir en ReservaEstancia si la tabla existe. Si no existe, ignora silencioso.
    """
    try:
        db.session.execute(
            text("""
                INSERT INTO ReservaEstancia
                    (Codigo_Reserva, Habitacion_Id, Fecha_Desde, Fecha_Hasta, Estado)
                VALUES (:r, :h, :ci, :co, 'Asignada')
            """),
            {"r": int(reserva_id), "h": int(habitacion_id), "ci": ci, "co": co}
        )
    except Exception:
        # Tabla no existe o no aplica en este proyecto: continuar sin fallar.
        pass


def _detalle_json(obj: Dict[str, Any]) -> Tuple[str, Dict[str, Any]]:
    """
    Serializa un dict a JSON (str) y devuelve el mapping de parámetros para CAST.
    Evita el error 'dict can not be used as parameter'.
    """
    return json.dumps(obj, ensure_ascii=False), obj  # el obj se devuelve por si se requiere reutilizar en memoria


def _registrar_decision(reserva_id: int, tipo: str, resultado: str, detalle: Dict[str, Any]) -> None:
    """
    Inserta traza en AsignacionDecision.Detalle (JSON), haciendo CAST seguro.
    Si la tabla no existe, ignora silencioso.
    """
    try:
        detalle_json, _ = _detalle_json(detalle)
        db.session.execute(
            text("""
                INSERT INTO AsignacionDecision (Codigo_Reserva, Tipo, Resultado, Detalle)
                VALUES (:r, :t, :res, CAST(:det AS JSON))
            """),
            {"r": int(reserva_id), "t": tipo, "res": resultado, "det": detalle_json}
        )
    except Exception:
        # Tabla/columna puede no existir en algunos entornos; no bloquear el flujo.
        pass


# -------------------------- API pública --------------------------

def auto_assign_for_reserva(reserva_id: int,
                            preferir_tipo: bool = True,
                            allow_split: bool = False) -> Dict[str, Any]:
    """
    Asignación automática simple:
      1) Si la reserva ya tiene habitación → no hace nada (ok=True).
      2) Busca una habitación libre para todo el rango [ci, co).
      3) Si no hay libre y allow_split=True → (opcional) partir por días e intentar tramos (no recomendado).
    - No pasa dicts como parámetros SQL (usa json.dumps + CAST(:det AS JSON)).
    - Solo usa Reserva para verificar solapes; ReservaEstancia es opcional.
    """
    r = _get_reserva(int(reserva_id))
    if not r:
        return {"ok": False, "error": "not_found"}

    estado = (r.get("estado") or "").strip()
    if estado not in ("Confirmada", "Pendiente"):
        _registrar_decision(reserva_id, "auto", "no_asignable",
                            {"motivo": "estado_no_asignable", "estado": estado})
        return {"ok": True, "resultado": "no_asignable"}

    if r.get("Codigo_Habitacion"):
        _registrar_decision(reserva_id, "auto", "ya_asignada",
                            {"habitacion": int(r["Codigo_Habitacion"])})
        return {"ok": True, "resultado": "ya_asignada"}

    ci, co = r.get("ci"), r.get("co")
    tipo = r.get("tipo_actual") if preferir_tipo else None

    candidatos = _habitaciones_candidatas(tipo=tipo, limit=500)
    elegida = None
    for hid in candidatos:
        if not _hay_solape(hid, ci, co, exclude_reserva_id=int(reserva_id)):
            elegida = int(hid)
            break

    plan: List[Dict[str, Any]] = []
    resultado = "sin_disponibilidad"

    if elegida is not None:
        # Asignar
        db.session.execute(
            text("""
                UPDATE Reserva
                   SET Codigo_Habitacion = :hid
                 WHERE Codigo_Reserva   = :rid
            """),
            {"hid": elegida, "rid": int(reserva_id)}
        )
        # (opcional) reflejar en ReservaEstancia si existe
        _asentar_estancia_si_existe(int(reserva_id), elegida, ci, co)

        db.session.commit()
        plan = [{"desde": str(ci), "hasta": str(co), "habitacion_id": elegida}]
        resultado = "ok"

    elif allow_split:
        # Intentar por días (simple): solo si se requiere
        d = date.fromisoformat(str(ci))
        end = date.fromisoformat(str(co))
        tmp_plan: List[Dict[str, Any]] = []
        cobertura_ok = True

        while d < end:
            d2 = d + timedelta(days=1)
            dia_ok = False
            for hid in candidatos:
                if not _hay_solape(int(hid), d, d2, exclude_reserva_id=int(reserva_id)):
                    tmp_plan.append({"desde": d.isoformat(), "hasta": d2.isoformat(), "habitacion_id": int(hid)})
                    dia_ok = True
                    break
            if not dia_ok:
                cobertura_ok = False
                break
            d = d2

        if cobertura_ok and tmp_plan:
            # Consolidar tramos contiguos de misma habitación
            merged: List[Dict[str, Any]] = []
            for t in tmp_plan:
                if not merged or merged[-1]["habitacion_id"] != t["habitacion_id"]:
                    merged.append(t)
                else:
                    merged[-1]["hasta"] = t["hasta"]
            plan = merged

            # Persistir estancias si existe la tabla y reflejar la primera en Reserva
            try:
                # borrar estancias anteriores
                db.session.execute(text("DELETE FROM ReservaEstancia WHERE Codigo_Reserva = :r"),
                                   {"r": int(reserva_id)})
                for t in plan:
                    _asentar_estancia_si_existe(int(reserva_id), int(t["habitacion_id"]), t["desde"], t["hasta"])
            except Exception:
                # si no existe la tabla, ignorar
                pass

            # Espejo en Reserva: primera habitación del plan
            db.session.execute(
                text("UPDATE Reserva SET Codigo_Habitacion = :h WHERE Codigo_Reserva = :r"),
                {"h": int(plan[0]["habitacion_id"]), "r": int(reserva_id)}
            )
            db.session.commit()
            resultado = "fallback_split"
        else:
            resultado = "sin_disponibilidad"

    # Traza (sin pasar dict como parámetro)
    detalle = {
        "tipo_preferido": tipo,
        "candidatos": candidatos[:50],  # no saturar el JSON
        "plan": plan
    }
    _registrar_decision(int(reserva_id), "auto", resultado, detalle)

    return {"ok": True, "resultado": resultado, "plan": plan}


def reassign_reserva(reserva_id: int, nueva_habitacion_id: int) -> Dict[str, Any]:
    """
    Reasigna forzadamente si la nueva habitación no tiene solapes.
    No pasa dicts como parámetros y registra decisión.
    """
    r = _get_reserva(int(reserva_id))
    if not r:
        return {"ok": False, "error": "not_found"}

    ci, co = r.get("ci"), r.get("co")
    if _hay_solape(int(nueva_habitacion_id), ci, co, exclude_reserva_id=int(reserva_id)):
        _registrar_decision(int(reserva_id), "reassign", "conflict",
                            {"habitacion": int(nueva_habitacion_id), "ci": str(ci), "co": str(co)})
        return {"ok": False, "error": "conflict"}

    db.session.execute(
        text("""
            UPDATE Reserva
               SET Codigo_Habitacion = :hid
             WHERE Codigo_Reserva   = :rid
        """),
        {"hid": int(nueva_habitacion_id), "rid": int(reserva_id)}
    )
    _asentar_estancia_si_existe(int(reserva_id), int(nueva_habitacion_id), ci, co)
    db.session.commit()

    _registrar_decision(int(reserva_id), "reassign", "ok",
                        {"habitacion": int(nueva_habitacion_id), "ci": str(ci), "co": str(co)})
    return {"ok": True}


def split_reserva(reserva_id: int, tramos: List[Dict[str, str]]) -> Dict[str, Any]:
    """
    Divide manualmente una reserva en tramos (si la tabla ReservaEstancia existe).
    'tramos': [{habitacion_id, desde, hasta}, ...]
    """
    try:
        db.session.execute(text("DELETE FROM ReservaEstancia WHERE Codigo_Reserva = :r"),
                           {"r": int(reserva_id)})
        for t in tramos:
            _asentar_estancia_si_existe(int(reserva_id),
                                        int(t["habitacion_id"]),
                                        t["desde"],
                                        t["hasta"])
        if tramos:
            db.session.execute(
                text("UPDATE Reserva SET Codigo_Habitacion = :h WHERE Codigo_Reserva = :r"),
                {"h": int(tramos[0]["habitacion_id"]), "r": int(reserva_id)}
            )
        db.session.commit()
    except Exception:
        # Si no existe la tabla, simplemente registra la intención
        pass

    _registrar_decision(int(reserva_id), "split", "ok", {"tramos": tramos})
    return {"ok": True}


def merge_reserva(reserva_id: int, room_id: int) -> Dict[str, Any]:
    """
    Une tramos, dejando una sola estancia (si existe la tabla) y asignando 'room_id'.
    """
    r = _get_reserva(int(reserva_id))
    if not r:
        return {"ok": False, "error": "not_found"}

    ci, co = r.get("ci"), r.get("co")
    # Validar conflictos antes de fusionar
    if _hay_solape(int(room_id), ci, co, exclude_reserva_id=int(reserva_id)):
        _registrar_decision(int(reserva_id), "merge", "conflict",
                            {"habitacion": int(room_id), "ci": str(ci), "co": str(co)})
        return {"ok": False, "error": "conflict"}

    try:
        db.session.execute(text("DELETE FROM ReservaEstancia WHERE Codigo_Reserva = :r"),
                           {"r": int(reserva_id)})
        _asentar_estancia_si_existe(int(reserva_id), int(room_id), ci, co)
    except Exception:
        # Si no existe la tabla, continuar
        pass

    db.session.execute(
        text("UPDATE Reserva SET Codigo_Habitacion = :h WHERE Codigo_Reserva = :r"),
        {"h": int(room_id), "r": int(reserva_id)}
    )
    db.session.commit()

    _registrar_decision(int(reserva_id), "merge", "ok",
                        {"habitacion": int(room_id), "ci": str(ci), "co": str(co)})
    return {"ok": True}
