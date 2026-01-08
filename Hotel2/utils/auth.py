from functools import wraps
from flask import session, jsonify


def role_required(*allowed_roles):
    """
    Decorador de control de acceso por rol.
    Acepta:
      - IDs de rol (int)
      - Nombres de rol (str)

    Ejemplos:
      @role_required(1)                    # Administrador por ID
      @role_required(1, 2)                 # Admin o Recepción
      @role_required("Administrador")      # Admin por nombre
      @role_required(1, "Administrador")   # Mixto (ID o nombre)
    """
    def decorator(f):
        @wraps(f)
        def wrapped(*args, **kwargs):

            # Obtener datos de sesión
            rol_id = session.get("rol_id")
            rol_name = session.get("rol")

            # Validación base
            if rol_id is None and not rol_name:
                return jsonify({"ok": False, "error": "Rol requerido"}), 403

            # Normalización
            try:
                rol_id = int(rol_id) if rol_id is not None else None
            except (TypeError, ValueError):
                rol_id = None

            rol_name_norm = rol_name.strip() if isinstance(rol_name, str) else None

            # Evaluación de permisos
            permitido = False
            for allowed in allowed_roles:
                if isinstance(allowed, int) and rol_id == allowed:
                    permitido = True
                    break
                if isinstance(allowed, str) and rol_name_norm == allowed:
                    permitido = True
                    break

            if not permitido:
                return jsonify({"ok": False, "error": "Acceso denegado"}), 403

            return f(*args, **kwargs)

        return wrapped
    return decorator
