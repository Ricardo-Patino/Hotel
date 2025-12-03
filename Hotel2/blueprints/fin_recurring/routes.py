# blueprints/fin_recurring/routes.py
from flask import request, jsonify, session
from datetime import date, datetime, timedelta

from sqlalchemy import text

from extensions import db
from models_sql import FinRecurringPlan, FinRecurringRun
from . import fin_recurring_bp


# ---------------------------------------------------------
# Utilidades internas
# ---------------------------------------------------------
def _today() -> date:
    return date.today()


def _parse_date(value):
    if isinstance(value, date):
        return value
    if not value:
        return None
    return datetime.strptime(str(value), "%Y-%m-%d").date()


def _next_charge_date(curr: date, frecuencia: str) -> date:
    """
    Calcula la siguiente fecha de cobro según la frecuencia.
    DAILY  -> +1 día
    WEEKLY -> +7 días
    MONTHLY -> mismo día del mes siguiente (ajustando días del mes)
    """
    frecuencia = (frecuencia or "").upper()
    if frecuencia == "DAILY":
        return curr + timedelta(days=1)
    if frecuencia == "WEEKLY":
        return curr + timedelta(weeks=1)
    if frecuencia == "MONTHLY":
        # incremento de mes "real" (no 30 días fijos)
        month = curr.month - 1 + 1
        year = curr.year + month // 12
        month = month % 12 + 1
        # días por mes considerando año bisiesto
        days_in_month = [
            31,
            29 if year % 4 == 0 and (year % 100 != 0 or year % 400 == 0) else 28,
            31, 30, 31, 30, 31, 31, 30, 31, 30, 31
        ]
        day = min(curr.day, days_in_month[month - 1])
        return date(year, month, day)
    raise ValueError(f"Frecuencia no soportada: {frecuencia}")


def _current_user_id() -> int:
    """
    Igual que en GRR/HRM:
    toma el user_id de sesión si existe, si no, 1.
    """
    try:
        return int(session.get("user_id") or 1)
    except Exception:
        return 1


# ---------------------------------------------------------
# Crear plan recurrente (FAC-07-008)
# ---------------------------------------------------------
@fin_recurring_bp.post("/plan")
def create_or_update_plan():
    """
    Crea un nuevo plan recurrente.

    Espera JSON con:
      - reserva_id (opcional)
      - cliente_id (opcional)
      - descripcion
      - frecuencia: DAILY / WEEKLY / MONTHLY
      - monto
      - currency
      - fecha_inicio (YYYY-MM-DD)
      - fecha_fin (opcional, YYYY-MM-DD)
      - numero_cuotas (opcional)
    """
    data = request.get_json(silent=True) or {}

    required = ["frecuencia", "monto", "fecha_inicio"]
    missing = [k for k in required if not data.get(k)]
    if missing:
        return (
            jsonify(
                {
                    "ok": False,
                    "error": f"Faltan campos obligatorios: {', '.join(missing)}",
                }
            ),
            400,
        )

    freq = str(data["frecuencia"]).upper()
    if freq not in ("DAILY", "WEEKLY", "MONTHLY"):
        return jsonify({"ok": False, "error": "Frecuencia inválida"}), 400

    fecha_inicio = _parse_date(data["fecha_inicio"])
    if not fecha_inicio:
        return jsonify({"ok": False, "error": "fecha_inicio inválida"}), 400

    plan = FinRecurringPlan(
        reserva_id=data.get("reserva_id"),
        cliente_id=data.get("cliente_id"),
        descripcion=data.get("descripcion"),
        frecuencia=freq,
        monto=data["monto"],
        currency=data.get("currency") or "CRC",
        fecha_inicio=fecha_inicio,
        fecha_fin=_parse_date(data.get("fecha_fin")),
        numero_cuotas=data.get("numero_cuotas"),
        proximo_cobro_en=fecha_inicio,
        estado="ACTIVE",
        creado_por=_current_user_id(),
    )
    db.session.add(plan)
    db.session.commit()

    return jsonify({"ok": True, "id_plan": plan.id_plan})


# ---------------------------------------------------------
# Cambiar estado del plan: pause / resume / cancel
# ---------------------------------------------------------
@fin_recurring_bp.post("/plan/<int:plan_id>/pause")
def pause_plan(plan_id: int):
    plan = FinRecurringPlan.query.get_or_404(plan_id)
    plan.estado = "PAUSED"
    plan.updated_by = _current_user_id()
    plan.updated_at = datetime.utcnow()
    db.session.commit()
    return jsonify({"ok": True, "estado": plan.estado})


@fin_recurring_bp.post("/plan/<int:plan_id>/resume")
def resume_plan(plan_id: int):
    plan = FinRecurringPlan.query.get_or_404(plan_id)
    plan.estado = "ACTIVE"
    plan.updated_by = _current_user_id()
    plan.updated_at = datetime.utcnow()
    db.session.commit()
    return jsonify({"ok": True, "estado": plan.estado})


@fin_recurring_bp.post("/plan/<int:plan_id>/cancel")
def cancel_plan(plan_id: int):
    plan = FinRecurringPlan.query.get_or_404(plan_id)
    plan.estado = "CANCELLED"
    plan.updated_by = _current_user_id()
    plan.updated_at = datetime.utcnow()
    db.session.commit()
    return jsonify({"ok": True, "estado": plan.estado})


# ---------------------------------------------------------
# Listar planes por reserva (para usar en GRR / detalle reserva)
# ---------------------------------------------------------
@fin_recurring_bp.get("/plan/by-reservation/<int:reserva_id>")
def list_plans_by_reservation(reserva_id: int):
    plans = (
        FinRecurringPlan.query.filter(
            FinRecurringPlan.reserva_id == reserva_id
        )
        .order_by(FinRecurringPlan.created_at.desc())
        .all()
    )
    items = []
    for p in plans:
        items.append(
            {
                "id_plan": p.id_plan,
                "reserva_id": p.reserva_id,
                "cliente_id": p.cliente_id,
                "descripcion": p.descripcion,
                "frecuencia": p.frecuencia,
                "monto": float(p.monto),
                "currency": p.currency,
                "fecha_inicio": p.fecha_inicio.isoformat(),
                "fecha_fin": p.fecha_fin.isoformat() if p.fecha_fin else None,
                "numero_cuotas": p.numero_cuotas,
                "proximo_cobro_en": p.proximo_cobro_en.isoformat(),
                "estado": p.estado,
            }
        )
    return jsonify({"ok": True, "items": items})


# ---------------------------------------------------------
# Helper: crear asiento en fin_ledger_tx + fin_ledger_line
#            (ajustado a tu esquema real)
# ---------------------------------------------------------
def _create_ledger_tx_for_plan(plan: FinRecurringPlan, today: date, user_id: int) -> int:
    """
    Crea una transacción en fin_ledger_tx + dos líneas en fin_ledger_line
    para el cargo recurrente.

    - fin_ledger_tx:
        external_id: "REC-{plan_id}-{YYYYMMDD}"
        source: 'BACKOFFICE'
        reserva_id: plan.reserva_id
        currency: plan.currency
        total: plan.monto
        status: 'posted'
        created_at: NOW()
        meta: JSON con info del plan

    - fin_ledger_line:
        línea 1: Debit CxC Huesped
        línea 2: Credit Ingresos Recurrentes
    """
    # 1) Insert en fin_ledger_tx
    ext_id = f"REC-{plan.id_plan}-{today.strftime('%Y%m%d')}"
    meta = {
        "plan_id": plan.id_plan,
        "descripcion": plan.descripcion or "Cobro recurrente",
        "frecuencia": plan.frecuencia,
        "fecha_programada": plan.proximo_cobro_en.isoformat()
        if plan.proximo_cobro_en
        else None,
    }

    db.session.execute(
        text(
            """
            INSERT INTO fin_ledger_tx
                (external_id, source, reserva_id, currency, total, status, created_at, meta)
            VALUES
                (:ext, 'BACKOFFICE', :rid, :curr, :total, 'posted', NOW(),
                 JSON_OBJECT('plan_id', :pid, 'descripcion', :desc, 'frecuencia', :freq, 'fecha_programada', :fprog))
            """
        ),
        {
            "ext": ext_id,
            "rid": plan.reserva_id,
            "curr": plan.currency,
            "total": float(plan.monto),
            "pid": plan.id_plan,
            "desc": plan.descripcion or "Cobro recurrente",
            "freq": plan.frecuencia,
            "fprog": plan.proximo_cobro_en.isoformat()
            if plan.proximo_cobro_en
            else None,
        },
    )

    # 2) Obtener id_tx recién creado
    tx_row = db.session.execute(
        text(
            """
            SELECT id_tx
            FROM fin_ledger_tx
            WHERE external_id = :ext
            LIMIT 1
            """
        ),
        {"ext": ext_id},
    ).fetchone()
    id_tx = tx_row[0] if tx_row else None
    if not id_tx:
        raise RuntimeError("No se pudo recuperar id_tx para el cobro recurrente")

    # 3) Crear líneas contables (doble partida)
    #    Aquí planteamos:
    #    - Debe  : CxC Huesped
    #    - Haber : Ingresos Recurrentes
    monto = float(plan.monto)
    desc_linea = f"Cargo recurrente plan {plan.id_plan}"

    # Línea 1: Debe CxC Huesped
    db.session.execute(
        text(
            """
            INSERT INTO fin_ledger_line
                (id_tx, line_no, account, debit, credit, description)
            VALUES
                (:tx, 1, 'CxC Huesped', :debit, 0.00, :desc)
            """
        ),
        {"tx": id_tx, "debit": monto, "desc": desc_linea},
    )

    # Línea 2: Haber Ingresos Recurrentes
    db.session.execute(
        text(
            """
            INSERT INTO fin_ledger_line
                (id_tx, line_no, account, debit, credit, description)
            VALUES
                (:tx, 2, 'Ingresos Recurrentes', 0.00, :credit, :desc)
            """
        ),
        {"tx": id_tx, "credit": monto, "desc": desc_linea},
    )

    return int(id_tx)


# ---------------------------------------------------------
# Job diario: ejecutar cobros recurrentes vencidos
# ---------------------------------------------------------
@fin_recurring_bp.post("/run_daily")
def run_daily_recurring_charges():
    """
    Endpoint para ser llamado por cron una vez al día.
    Procesa todos los planes con proximo_cobro_en <= hoy y estado ACTIVE.
    """
    today = _today()
    user_id = _current_user_id()

    plans = (
        FinRecurringPlan.query.filter(
            FinRecurringPlan.estado == "ACTIVE",
            FinRecurringPlan.proximo_cobro_en <= today,
        ).all()
    )

    total = 0
    ok = 0
    failed = 0
    runs = []

    for plan in plans:
        total += 1
        run = FinRecurringRun(
            plan_id=plan.id_plan,
            fecha_programada=plan.proximo_cobro_en,
            estado="PENDING",
        )
        db.session.add(run)
        try:
            # 1) crear asiento contable (tx principal + líneas)
            tx_id = _create_ledger_tx_for_plan(plan, today, user_id)
            run.tx_id = tx_id
            run.fecha_ejecucion = datetime.utcnow()
            run.estado = "EXECUTED"

            # 2) actualizar próximo cobro
            plan.proximo_cobro_en = _next_charge_date(
                plan.proximo_cobro_en,
                plan.frecuencia,
            )

            # 3) verificar si ya completó número de cuotas
            if plan.numero_cuotas is not None:
                ejecutados = (
                    FinRecurringRun.query.filter(
                        FinRecurringRun.plan_id == plan.id_plan,
                        FinRecurringRun.estado == "EXECUTED",
                    ).count()
                )
                if ejecutados >= plan.numero_cuotas:
                    plan.estado = "FINISHED"

            ok += 1
            runs.append(
                {
                    "id_plan": plan.id_plan,
                    "id_run": run.id_run,
                    "status": "EXECUTED",
                }
            )
        except Exception as exc:
            run.estado = "FAILED"
            run.mensaje_error = str(exc)[:250]
            failed += 1
            runs.append(
                {
                    "id_plan": plan.id_plan,
                    "id_run": run.id_run,
                    "status": "FAILED",
                    "error": str(exc),
                }
            )

    db.session.commit()
    return jsonify(
        {
            "ok": True,
            "date": today.isoformat(),
            "total_plans": total,
            "executed": ok,
            "failed": failed,
            "runs": runs,
        }
    )






