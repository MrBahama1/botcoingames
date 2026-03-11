"""Authentication and CSRF decorators for Flask routes."""

import re
import functools
from flask import request, jsonify, g


# API key format validation
API_KEY_PATTERN = re.compile(r'^bk_[A-Za-z0-9]{20,80}$')


def require_auth(session_manager):
    """Decorator factory: requires valid session cookie."""
    def decorator(f):
        @functools.wraps(f)
        def wrapped(*args, **kwargs):
            session_id = request.cookies.get("session_id")
            if not session_id:
                return jsonify({"ok": False, "error": "Not authenticated"}), 401
            session = session_manager.get_session(session_id)
            if not session:
                return jsonify({"ok": False, "error": "Session expired"}), 401
            g.session = session
            g.session_id = session_id
            return f(*args, **kwargs)
        return wrapped
    return decorator


def csrf_protect(session_manager):
    """Decorator factory: validates CSRF token on POST/PUT/DELETE."""
    def decorator(f):
        @functools.wraps(f)
        def wrapped(*args, **kwargs):
            session_id = request.cookies.get("session_id")
            if not session_id:
                return jsonify({"ok": False, "error": "Not authenticated"}), 401
            expected = session_manager.get_csrf_token(session_id)
            if not expected:
                return jsonify({"ok": False, "error": "Session expired"}), 401
            provided = request.headers.get("X-CSRF-Token", "")
            if not provided or provided != expected:
                return jsonify({"ok": False, "error": "Invalid CSRF token"}), 403
            return f(*args, **kwargs)
        return wrapped
    return decorator


def origin_check():
    """Decorator: reject cross-origin POST requests to unauthenticated endpoints.

    Checks Origin and Referer headers to prevent CSRF on pre-auth endpoints
    (like /api/setup/connect) that don't have session-based CSRF tokens yet.
    """
    def decorator(f):
        @functools.wraps(f)
        def wrapped(*args, **kwargs):
            origin = request.headers.get("Origin", "")
            referer = request.headers.get("Referer", "")
            host = request.host_url.rstrip("/")

            # Allow same-origin requests (Origin matches host)
            if origin and origin.rstrip("/") == host:
                return f(*args, **kwargs)
            # Allow if Referer matches host (browsers send this on same-origin)
            if referer and referer.startswith(host):
                return f(*args, **kwargs)
            # Allow requests with no Origin/Referer (direct API calls, curl)
            # but require Content-Type: application/json (not sent by HTML forms)
            if not origin and not referer:
                ct = request.content_type or ""
                if "application/json" in ct:
                    return f(*args, **kwargs)
            return jsonify({"ok": False, "error": "Cross-origin request blocked"}), 403
        return wrapped
    return decorator


def validate_api_key(key: str) -> bool:
    """Check API key format without calling Bankr."""
    return bool(API_KEY_PATTERN.match(key))


def validate_email(email: str) -> bool:
    """Email format check. Disallows leading hyphens to prevent CLI argument injection."""
    return bool(re.match(r'^[a-zA-Z0-9][a-zA-Z0-9._%+\-]*@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}$', email)) and len(email) < 255


def validate_otp(code: str) -> bool:
    """OTP is a 4-8 character alphanumeric code."""
    return bool(re.match(r'^[a-zA-Z0-9]{4,8}$', code))


def sanitize_log(msg: str) -> str:
    """Redact API keys from log messages."""
    return re.sub(r'bk_[A-Za-z0-9]+', 'bk_***', msg)
