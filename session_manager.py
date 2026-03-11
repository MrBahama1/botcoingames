"""Server-side session store with encrypted API key storage.

API keys are encrypted at rest using Fernet (AES-128-CBC + HMAC-SHA256).
The encryption key is ephemeral — generated at startup, held only in memory.
All sessions are invalidated on server restart (by design).
"""

import os
import time
import uuid
import secrets
import threading
from cryptography.fernet import Fernet


# Max concurrent sessions to prevent resource exhaustion
MAX_SESSIONS = 50
SESSION_TTL = 86400  # 24 hours


class SessionManager:
    def __init__(self):
        self._fernet = Fernet(Fernet.generate_key())
        self._sessions: dict[str, dict] = {}
        self._lock = threading.Lock()

    def create_session(self, api_key: str, miner_address: str = "") -> str:
        """Create a new session. Returns session_id."""
        session_id = secrets.token_urlsafe(32)
        csrf_token = secrets.token_hex(32)
        encrypted_key = self._fernet.encrypt(api_key.encode("utf-8"))

        with self._lock:
            # Evict expired sessions first
            self._cleanup_expired_locked()
            if len(self._sessions) >= MAX_SESSIONS:
                # Evict oldest session
                oldest = min(self._sessions, key=lambda k: self._sessions[k]["last_active"])
                del self._sessions[oldest]

            self._sessions[session_id] = {
                "encrypted_key": encrypted_key,
                "miner_address": miner_address,
                "csrf_token": csrf_token,
                "created_at": time.time(),
                "last_active": time.time(),
            }
        return session_id

    def get_session(self, session_id: str) -> dict | None:
        """Get session metadata (without decrypted key). Returns None if expired/missing."""
        with self._lock:
            sess = self._sessions.get(session_id)
            if not sess:
                return None
            if time.time() - sess["created_at"] > SESSION_TTL:
                del self._sessions[session_id]
                return None
            sess["last_active"] = time.time()
            return {
                "session_id": session_id,
                "miner_address": sess["miner_address"],
                "csrf_token": sess["csrf_token"],
                "created_at": sess["created_at"],
            }

    def get_api_key(self, session_id: str) -> str | None:
        """Decrypt and return the API key for this session. Returns None if invalid."""
        with self._lock:
            sess = self._sessions.get(session_id)
            if not sess:
                return None
            if time.time() - sess["created_at"] > SESSION_TTL:
                del self._sessions[session_id]
                return None
            sess["last_active"] = time.time()
        # Decrypt outside lock to minimize lock duration
        try:
            return self._fernet.decrypt(sess["encrypted_key"]).decode("utf-8")
        except Exception:
            return None

    def get_csrf_token(self, session_id: str) -> str | None:
        """Get CSRF token for this session."""
        with self._lock:
            sess = self._sessions.get(session_id)
            if not sess:
                return None
            return sess["csrf_token"]

    def update_miner_address(self, session_id: str, address: str):
        """Update the miner address for a session."""
        with self._lock:
            sess = self._sessions.get(session_id)
            if sess:
                sess["miner_address"] = address

    def destroy_session(self, session_id: str):
        """Remove a session."""
        with self._lock:
            self._sessions.pop(session_id, None)

    def _cleanup_expired_locked(self):
        """Remove expired sessions. Must be called with lock held."""
        now = time.time()
        expired = [k for k, v in self._sessions.items()
                   if now - v["created_at"] > SESSION_TTL]
        for k in expired:
            del self._sessions[k]

    def cleanup_expired(self):
        """Public cleanup method."""
        with self._lock:
            self._cleanup_expired_locked()
