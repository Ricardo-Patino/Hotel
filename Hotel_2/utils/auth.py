from functools import wraps
from flask import session, abort

def role_required(*roles):
    def deco(fn):
        @wraps(fn)
        def wrapper(*args, **kwargs):
            if session.get("user_role") not in roles:
                abort(403)
            return fn(*args, **kwargs)
        return wrapper
    return deco
