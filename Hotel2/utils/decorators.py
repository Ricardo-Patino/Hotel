from functools import wraps
from flask import session, abort

def require_user_id(*allowed_user_ids):
    def decorator(fn):
        @wraps(fn)
        def wrapper(*args, **kwargs):
            user_id = session.get("user_id")
            if user_id is None or user_id not in allowed_user_ids:
                abort(403)
            return fn(*args, **kwargs)
        return wrapper
    return decorator

