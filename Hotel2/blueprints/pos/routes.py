from flask import Blueprint, request, jsonify
from sqlalchemy import text
from datetime import date
from extensions import db



# =============================
# Helpers que ya existen en fin_cash
# =============================

def _month_key(d: date) -> str:
    return f"{d:%Y-%m}"

def _is_locked_effective(d: date) -> bool:
    """
    Devuelve True si el mes correspondiente a la fecha 'd'
    está marcado como cerrado en fin_period_lock (status='closed')
    según tu schema real.
    """
    row = db.session.execute(
        text("""
            SELECT 1
              FROM fin_period_lock
             WHERE period_key = :p
               AND status = 'closed'
             LIMIT 1
        """),
        {"p": _month_key(d)},
    ).fetchone()
    return bool(row)

def _get_open_session(fecha: date):
    """
    Trae una caja ABIERTA/REOPENED para esa fecha.
    Coincide con tu tabla fin_cash_session.
    """
    row = db.session.execute(
        text("""
            SELECT id_session
              FROM fin_cash_session
             WHERE fecha = :f
               AND status IN ('open','reopened')
             ORDER BY id_session DESC
             LIMIT 1
        """),
        {"f": fecha},
    ).fetchone()
    return (row[0] if row else None)

def _get_reserva(reserva_id: int):
    """
    Lee la reserva real desde tu tabla Reserva.
    Campos claves:
      - Monto_Total
      - Monto_Pagado
    """
    row = db.session.execute(
        text("""
            SELECT
              Codigo_Reserva,
              Monto_Total,
              Monto_Pagado
            FROM Reserva
            WHERE Codigo_Reserva = :rid
            LIMIT 1
        """),
        {"rid": reserva_id}
    ).mappings().fetchone()
    return row


# =============================
# Blueprint POS
# =============================

pos_bp = Blueprint("pos", __name__, url_prefix="/pos")


@pos_bp.post("/checkout")
def pos_checkout():
    """
    Cobro POS / Caja (flujo de recepción).
    Esta ruta implementa la HU de cobro en caja,
    integrándose con reservas (GRR) y la caja diaria (fin_cash).

    Espera JSON así:
    {
      "reserva_id": 2,
      "monto": 30000.00,
      "metodo": "Efectivo" | "Tarjeta" | "Transferencia" | "Otro",
      "concepto": "Pago de estadía",
      "referencia": "VISA AUTH 12345",
      "created_by": 1
    }

    Retorna JSON con:
    - saldo pendiente actualizado
    - ids contables / de caja
    """
    payload = request.get_json(force=True, silent=True) or {}

    required = ("reserva_id", "monto", "metodo", "concepto", "created_by")
    if not all(k in payload for k in required):
        return jsonify({"ok": False, "error": "missing_fields"}), 400

    reserva_id = int(payload["reserva_id"])
    monto      = float(payload["monto"])
    metodo     = str(payload["metodo"]).strip()  # "Efectivo","Tarjeta","Transferencia","Otro"
    concepto   = str(payload["concepto"]).strip()
    referencia = (payload.get("referencia") or "").strip()
    usuario_id = int(payload["created_by"])

    # 1) Traer la reserva
    r = _get_reserva(reserva_id)
    if not r:
        return jsonify({"ok": False, "error": "reserva_not_found"}), 404

    total_reserva   = float(r["Monto_Total"] or 0.0)
    pagado_actual   = float(r["Monto_Pagado"] or 0.0)
    saldo_actual    = total_reserva - pagado_actual

    if monto <= 0:
        return jsonify({"ok": False, "error": "invalid_amount"}), 400

    # Política anti sobrepago. Si quieres permitir propina/crédito a favor, quita este if.
    if monto > saldo_actual + 0.01:
        return jsonify({
            "ok": False,
            "error": "overpay_blocked",
            "message": "El pago excede el saldo pendiente de la reserva."
        }), 409

    # 2) Validar que el periodo contable del día no esté cerrado
    hoy = date.today()
    if _is_locked_effective(hoy):
        return jsonify({
            "ok": False,
            "error": "period_locked",
            "message": f"El periodo {_month_key(hoy)} está bloqueado. No se pueden registrar cobros."
        }), 423

    # 3) Verificar caja abierta hoy
    cash_session_id = _get_open_session(hoy)
    if cash_session_id is None:
        return jsonify({
            "ok": False,
            "error": "no_open_cash",
            "message": "No hay caja abierta/reabierta para hoy. Abra caja antes de cobrar."
        }), 409

    # =============================
    # 4) Registrar movimiento en caja (fin_cash_movement)
    #    tipo = 'Ingreso' porque entra plata
    #    source = 'POS' para diferenciar de 'Manual'
    # =============================
    db.session.execute(
        text("""
            INSERT INTO fin_cash_movement
                (session_id, tipo, metodo, concepto, referencia, monto,
                 created_by, created_at, source, estado)
            VALUES
                (:sid, 'Ingreso', :met, :con, :ref, :monto,
                 :usr, NOW(), 'POS', 'Aplicado')
        """),
        {
            "sid": cash_session_id,
            "met": metodo,
            "con": concepto,
            "ref": referencia or None,
            "monto": monto,
            "usr": usuario_id
        }
    )

    # obtener id_move recién insertado
    mov_row = db.session.execute(
        text("""
            SELECT id_move
            FROM fin_cash_movement
            WHERE session_id = :sid
            ORDER BY id_move DESC
            LIMIT 1
        """),
        {"sid": cash_session_id}
    ).fetchone()
    id_move = mov_row[0] if mov_row else None

    # =============================
    # 5) Registrar recibo (fin_receipts)
    #    esto deja trazabilidad del pago con método (efectivo/tarjeta)
    # =============================
    # Generar un número de recibo simple (puedes reemplazarlo por consecutivo real)
    numero_recibo = f"RC-{hoy.strftime('%Y%m%d')}-{reserva_id}-{id_move or 'X'}"

    db.session.execute(
        text("""
            INSERT INTO fin_receipts
                (numero, reserva_id, tx_id, metodo, currency, monto, emitido_por, creado_en, estado)
            VALUES
                (:num, :rid, NULL, :met, 'CRC', :monto, :usr, NOW(), 'Emitido')
        """),
        {
            "num": numero_recibo,
            "rid": reserva_id,
            "met": metodo,
            "monto": monto,
            "usr": usuario_id
        }
    )

    rec_row = db.session.execute(
        text("""
            SELECT id_receipt
            FROM fin_receipts
            WHERE numero = :num
            LIMIT 1
        """),
        {"num": numero_recibo}
    ).fetchone()
    id_receipt = rec_row[0] if rec_row else None

    # opcional: amarrar el recibo al movimiento de caja
    if id_receipt and id_move:
        db.session.execute(
            text("""
                UPDATE fin_cash_movement
                   SET link_receipt = :rid
                 WHERE id_move = :mid
            """),
            {"rid": id_receipt, "mid": id_move}
        )

    # =============================
    # 6) Libro mayor (fin_ledger_tx / fin_ledger_line)
    #    Creamos una transacción contable resumida.
    #    Debit: Caja/Bancos
    #    Credit: CxC Huésped (o Ingresos diferidos)
    # =============================
    # Creamos la tx
    ext_id = f"POS-{reserva_id}-{id_move or 'X'}"
    db.session.execute(
        text("""
            INSERT INTO fin_ledger_tx
                (external_id, source, reserva_id, currency, total, status, created_at, meta)
            VALUES
                (:ext, 'POS', :rid, 'CRC', :total, 'posted', NOW(),
                 JSON_OBJECT('metodo', :met, 'recibo', :rec))
        """),
        {
            "ext": ext_id,
            "rid": reserva_id,
            "total": monto,
            "met": metodo,
            "rec": numero_recibo
        }
    )

    tx_row = db.session.execute(
        text("""
            SELECT id_tx
            FROM fin_ledger_tx
            WHERE external_id = :ext
            LIMIT 1
        """),
        {"ext": ext_id}
    ).fetchone()
    id_tx = tx_row[0] if tx_row else None

    if id_tx:
        # línea 1: Debit Caja/Banco
        cuenta_debito = "Caja" if metodo == "Efectivo" else "Banco/Tarjeta"
        db.session.execute(
            text("""
                INSERT INTO fin_ledger_line
                    (id_tx, line_no, account, debit, credit, description)
                VALUES
                    (:tx, 1, :acct, :debit, 0.00, :desc)
            """),
            {
                "tx": id_tx,
                "acct": cuenta_debito,
                "debit": monto,
                "desc": f"Cobro reserva {reserva_id} ({metodo})"
            }
        )
        # línea 2: Credit CxC Huésped
        db.session.execute(
            text("""
                INSERT INTO fin_ledger_line
                    (id_tx, line_no, account, debit, credit, description)
                VALUES
                    (:tx, 2, 'CxC Huesped', 0.00, :credit, :desc)
            """),
            {
                "tx": id_tx,
                "credit": monto,
                "desc": f"Cobro reserva {reserva_id} ({metodo})"
            }
        )

    # =============================
    # 7) Actualizar la reserva: sumar lo pagado
    #    En tu esquema real la columna es Monto_Pagado
    # =============================
    nuevo_pagado = pagado_actual + monto
    if nuevo_pagado < 0:
        nuevo_pagado = 0.0

    db.session.execute(
        text("""
            UPDATE Reserva
               SET Monto_Pagado = :pagado,
                   Fecha_Ultimo_Pago = NOW()
             WHERE Codigo_Reserva = :rid
        """),
        {
            "pagado": nuevo_pagado,
            "rid": reserva_id
        }
    )

    # Guardamos todo
    db.session.commit()

    saldo_restante = total_reserva - nuevo_pagado
    estado_pago = "pagado" if saldo_restante <= 0.01 else "pendiente"

    return jsonify({
        "ok": True,
        "reserva_id": reserva_id,

        "cash_session_id": cash_session_id,
        "cash_movement_id": id_move,
        "receipt_id": id_receipt,
        "ledger_tx_id": id_tx,

        "total_reserva": total_reserva,
        "pagado_acumulado": nuevo_pagado,
        "saldo_pendiente": saldo_restante,
        "estado_pago": estado_pago,

        "message": "Pago registrado en caja, recibo emitido y mayor actualizado."
    }), 201

# =============================
# Gestión Contable y Facturación (FAC) 
#  Helpers contables / de caja
#Cada transacción que llegue al POS endpoint /pos/checkout:
#Crea movimiento en fin_cash_movement.
#Crea recibo en fin_receipts y lo enlaza al movimiento.
#Crea la transacción contable en fin_ledger_tx + fin_ledger_line.
#Actualiza Reserva.Monto_Pagado.
# =============================

def _month_key(d: date) -> str:
    """Devuelve la llave de periodo en formato YYYY-MM."""
    return f"{d:%Y-%m}"


def _is_locked_effective(d: date) -> bool:
    """
    Devuelve True si el mes correspondiente a la fecha 'd'
    está marcado como cerrado en fin_period_lock (status='closed').

    Si no hay fila para ese periodo, se asume que NO está cerrado.
    """
    row = db.session.execute(
        text("""
            SELECT 1
              FROM fin_period_lock
             WHERE period_key = :p
               AND status = 'closed'
             LIMIT 1
        """),
        {"p": _month_key(d)},
    ).fetchone()
    return bool(row)


def _get_open_session(fecha: date):
    """
    Trae una caja ABIERTA/REOPENED para esa fecha.
    Coincide con la tabla fin_cash_session.
    """
    row = db.session.execute(
        text("""
            SELECT id_session
              FROM fin_cash_session
             WHERE fecha = :f
               AND status IN ('open','reopened')
             ORDER BY id_session DESC
             LIMIT 1
        """),
        {"f": fecha},
    ).fetchone()
    return row[0] if row else None


def _get_reserva(reserva_id: int):
    """
    Lee la reserva real desde la tabla Reserva.
    Campos claves:
      - Monto_Total
      - Monto_Pagado
    """
    row = db.session.execute(
        text("""
            SELECT
              Codigo_Reserva,
              Monto_Total,
              Monto_Pagado
            FROM Reserva
            WHERE Codigo_Reserva = :rid
            LIMIT 1
        """),
        {"rid": reserva_id}
    ).mappings().fetchone()
    return row


# =============================
# FAC-07-002: Integración POS
# =============================

@pos_bp.post("/checkout")
def pos_checkout():
    """
    FAC-07-002 Integrar con POS:
    Cada transacción del POS enviará automáticamente sus líneas contables
    a fin_ledger (fin_ledger_tx / fin_ledger_line) y actualizará la reserva
    asociada.

    Request JSON esperado:
    {
      "reserva_id": 2,
      "monto": 30000.00,
      "metodo": "Efectivo" | "Tarjeta" | "Transferencia" | "Otro",
      "concepto": "Pago de estadía",
      "referencia": "VISA AUTH 12345",
      "created_by": 1
    }
    """

    payload = request.get_json(force=True, silent=True) or {}

    required = ("reserva_id", "monto", "metodo", "concepto", "created_by")
    if not all(k in payload for k in required):
        return jsonify({"ok": False, "error": "missing_fields"}), 400

    # Normalización de campos
    try:
        reserva_id = int(payload["reserva_id"])
        monto = float(payload["monto"])
    except (TypeError, ValueError):
        return jsonify({"ok": False, "error": "invalid_reserva_or_amount"}), 400

    metodo = str(payload["metodo"]).strip()  # "Efectivo","Tarjeta","Transferencia","Otro"
    concepto = str(payload["concepto"]).strip()
    referencia = (payload.get("referencia") or "").strip()
    try:
        usuario_id = int(payload["created_by"])
    except (TypeError, ValueError):
        return jsonify({"ok": False, "error": "invalid_user"}), 400

    if monto <= 0:
        return jsonify({"ok": False, "error": "invalid_amount"}), 400

    hoy = date.today()

    # 1) Validar que el periodo contable NO esté cerrado
    if _is_locked_effective(hoy):
        return jsonify({
            "ok": False,
            "error": "period_locked",
            "message": "El período contable de este mes está cerrado. No se permiten más movimientos POS."
        }), 409

    # 2) Traer la reserva
    r = _get_reserva(reserva_id)
    if not r:
        return jsonify({"ok": False, "error": "reserva_not_found"}), 404

    total_reserva = float(r["Monto_Total"] or 0.0)
    pagado_actual = float(r["Monto_Pagado"] or 0.0)

    # 3) Obtener sesión de caja abierta para hoy
    cash_session_id = _get_open_session(hoy)
    if not cash_session_id:
        return jsonify({
            "ok": False,
            "error": "cash_session_not_open",
            "message": "No hay sesión de caja abierta para hoy. Abre caja antes de registrar cobros POS."
        }), 409

    # =============================
    # 4) Registrar movimiento de caja (fin_cash_movement)
    #    tipo = 'Ingreso', source = 'POS'
    # =============================
    db.session.execute(
        text("""
            INSERT INTO fin_cash_movement
                (session_id, tipo, metodo, concepto, referencia, monto,
                 created_by, created_at, source, estado)
            VALUES
                (:sid, 'Ingreso', :met, :con, :ref, :monto,
                 :usr, NOW(), 'POS', 'Aplicado')
        """),
        {
            "sid": cash_session_id,
            "met": metodo,
            "con": concepto,
            "ref": referencia or None,
            "monto": monto,
            "usr": usuario_id
        }
    )

    mov_row = db.session.execute(
        text("""
            SELECT id_move
              FROM fin_cash_movement
             WHERE session_id = :sid
             ORDER BY id_move DESC
             LIMIT 1
        """),
        {"sid": cash_session_id}
    ).fetchone()
    id_move = mov_row[0] if mov_row else None

    # =============================
    # 5) Registrar recibo (fin_receipts)
    # =============================
    numero_recibo = f"RC-{hoy.strftime('%Y%m%d')}-{reserva_id}-{id_move or 'X'}"

    db.session.execute(
        text("""
            INSERT INTO fin_receipts
                (numero, reserva_id, tx_id, metodo, currency, monto,
                 emitido_por, creado_en, estado)
            VALUES
                (:num, :rid, NULL, :met, 'CRC', :monto,
                 :usr, NOW(), 'Emitido')
        """),
        {
            "num": numero_recibo,
            "rid": reserva_id,
            "met": metodo,
            "monto": monto,
            "usr": usuario_id
        }
    )

    rec_row = db.session.execute(
        text("""
            SELECT id_receipt
              FROM fin_receipts
             WHERE numero = :num
             LIMIT 1
        """),
        {"num": numero_recibo}
    ).fetchone()
    id_receipt = rec_row[0] if rec_row else None

    # Amarrar recibo al movimiento de caja (link_receipt)
    if id_receipt and id_move:
        db.session.execute(
            text("""
                UPDATE fin_cash_movement
                   SET link_receipt = :rid
                 WHERE id_move = :mid
            """),
            {"rid": id_receipt, "mid": id_move}
        )

    # =============================
    # 6) Libro mayor (fin_ledger_tx / fin_ledger_line)
    # =============================
    ext_id = f"POS-{reserva_id}-{id_move or 'X'}"

    db.session.execute(
        text("""
            INSERT INTO fin_ledger_tx
                (external_id, source, reserva_id, currency, total, status, created_at, meta)
            VALUES
                (:ext, 'POS', :rid, 'CRC', :total, 'posted', NOW(),
                 JSON_OBJECT('metodo', :met, 'recibo', :rec))
        """),
        {
            "ext": ext_id,
            "rid": reserva_id,
            "total": monto,
            "met": metodo,
            "rec": numero_recibo
        }
    )

    tx_row = db.session.execute(
        text("""
            SELECT id_tx
              FROM fin_ledger_tx
             WHERE external_id = :ext
             LIMIT 1
        """),
        {"ext": ext_id}
    ).fetchone()
    id_tx = tx_row[0] if tx_row else None

    if id_tx:
        # Línea 1: Débito Caja/Banco
        cuenta_debito = "Caja" if metodo == "Efectivo" else "Banco/Tarjeta"
        db.session.execute(
            text("""
                INSERT INTO fin_ledger_line
                    (id_tx, line_no, account, debit, credit, description)
                VALUES
                    (:tx, 1, :acct, :debit, 0.00, :desc)
            """),
            {
                "tx": id_tx,
                "acct": cuenta_debito,
                "debit": monto,
                "desc": f"Cobro reserva {reserva_id} ({metodo})"
            }
        )

        # Línea 2: Crédito CxC Huésped
        db.session.execute(
            text("""
                INSERT INTO fin_ledger_line
                    (id_tx, line_no, account, debit, credit, description)
                VALUES
                    (:tx, 2, 'CxC Huesped', 0.00, :credit, :desc)
            """),
            {
                "tx": id_tx,
                "credit": monto,
                "desc": f"Cobro reserva {reserva_id} ({metodo})"
            }
        )

    # =============================
    # 7) Actualizar la reserva: sumar lo pagado
    # =============================
    nuevo_pagado = pagado_actual + monto
    if nuevo_pagado < 0:
        nuevo_pagado = 0.0

    db.session.execute(
        text("""
            UPDATE Reserva
               SET Monto_Pagado = :pagado
             WHERE Codigo_Reserva = :rid
        """),
        {
            "pagado": nuevo_pagado,
            "rid": reserva_id
        }
    )

#frontend actualiza en tiempo real
    db.session.commit()

    saldo_restante = total_reserva - nuevo_pagado

    # Nunca mostramos saldo negativo al usuario, si se pasa, se corta en 0
    saldo_mostrar = saldo_restante if saldo_restante > 0 else 0.0

    # Estado de pago y porcentaje (para FAC-07-007 - abonos)
    estado_pago = "pagado" if saldo_mostrar <= 0.01 else "pendiente"
    if total_reserva > 0:
        porcentaje_pagado = min(max((nuevo_pagado / total_reserva) * 100, 0), 100)
    else:
        porcentaje_pagado = 100.0

    return jsonify({
        "ok": True,
        "reserva_id": reserva_id,

        "cash_session_id": cash_session_id,
        "cash_movement_id": id_move,
        "receipt_id": id_receipt,
        "ledger_tx_id": id_tx,

        "total_reserva": total_reserva,
        "pagado_acumulado": nuevo_pagado,
        "saldo_pendiente": saldo_mostrar,
        "estado_pago": estado_pago,
        "porcentaje_pagado": porcentaje_pagado,

        "message": "Pago registrado en caja, recibo emitido y mayor actualizado."
    }), 201






