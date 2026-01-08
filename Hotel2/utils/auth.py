from functools import wraps

from flask import flash, jsonify, redirect, request, session, url_for


def _is_api_request() -> bool:
    """Heurística simple para decidir si la respuesta debe ser JSON."""
    try:
        if (request.path or "").startswith("/api/"):
            return True
        if request.is_json:
            return True
        best = request.accept_mimetypes.best
        return best == "application/json"
    except Exception:
        return False


def _norm_role_name(value) -> str | None:
    if not isinstance(value, str):
        return None
    return value.strip().lower()


def role_required(*allowed_roles):
    """Decorador de control de acceso por rol.

    **Compatibilidad:**
    - Esquema antiguo: session["rol"], session["rol_id"]
    - Esquema actual del proyecto: session["user_role"], session["user_id"]

    Acepta:
      - IDs de rol (int)
      - Nombres de rol (str)
    """

    allowed_norm = []
    for a in allowed_roles:
        if isinstance(a, str):
            allowed_norm.append(_norm_role_name(a))
        else:
            allowed_norm.append(a)

    def decorator(f):
        @wraps(f)
        def wrapped(*args, **kwargs):
            # 1) Leer rol desde cualquier esquema de sesión conocido
            rol_id = (
                session.get("rol_id")
                or session.get("user_role_id")
                or session.get("role_id")
            )
            rol_name = (
                session.get("rol")
                or session.get("user_role")
                or session.get("role")
            )

            # 2) Si no hay rol, no está autenticado (o sesión incompleta)
            if rol_id is None and not rol_name:
                if _is_api_request():
                    return jsonify({"ok": False, "error": "Rol requerido"}), 403

                next_url = request.full_path if request.query_string else request.path
                flash("Debes iniciar sesión para acceder a esta sección.", "warning")
                return redirect(url_for("login_html", next=next_url))

            # 3) Normalización
            try:
                rol_id_int = int(rol_id) if rol_id is not None else None
            except (TypeError, ValueError):
                rol_id_int = None

            rol_name_norm = _norm_role_name(rol_name)

            # 4) Evaluación de permisos
            permitido = False
            for allowed in allowed_norm:
                if isinstance(allowed, int) and rol_id_int == allowed:
                    permitido = True
                    break
                if isinstance(allowed, str) and rol_name_norm == allowed:
                    permitido = True
                    break

            if not permitido:
                if _is_api_request():
                    return jsonify({"ok": False, "error": "Acceso denegado"}), 403

                flash("No tienes permisos para acceder a esta sección.", "danger")
                return redirect(request.referrer or url_for("index_html"))

            return f(*args, **kwargs)

        return wrapped

    return decorator
