import hashlib
import hmac
import time
from functools import wraps

from flask import current_app, request

from .errors import unauthorized

REPLAY_WINDOW_SECONDS = 60


def verify_service_request(req):
    """The one place that decides if a caller is the moderation
    service. Today: HMAC-SHA256 request signing with a timestamp
    replay window. Swap the body of this function — mTLS client
    identity, AWS SigV4, a token-issuing auth service — and every
    route below is unaffected."""
    secret = current_app.config.get('MODERATION_SERVICE_TOKEN')
    if not secret:
        return False
    timestamp = req.headers.get('X-Moderation-Timestamp')
    signature = req.headers.get('X-Moderation-Signature')
    if not timestamp or not signature:
        return False
    try:
        if abs(time.time() - int(timestamp)) > REPLAY_WINDOW_SECONDS:
            return False
    except ValueError:
        return False
    message = timestamp.encode('utf-8') + b'.' + req.get_data()
    expected = hmac.new(secret.encode('utf-8'), message, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature)


def moderation_auth_required(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        if not verify_service_request(request):
            return unauthorized('Invalid moderation service token')
        return f(*args, **kwargs)
    return wrapper
