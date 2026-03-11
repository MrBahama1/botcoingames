"""Server-side session store with encrypted API key storage.

API keys are encrypted at rest using Fernet (AES-128-CBC + HMAC-SHA256).
The encryption key is persisted to a protected directory so sessions survive
server restarts.
"""

import os
import time
import json
import secrets
import threading
from cryptography.fernet import Fernet


# Max concurrent sessions to prevent resource exhaustion
MAX_SESSIONS = 50
SESSION_TTL = 86400  # 24 hours

# Store session data in ~/.botcoin/ (protected, not world-readable /tmp/)
_DATA_DIR = os.environ.get("BOTCOIN_DATA_DIR", os.path.join(os.path.expanduser("~"), ".botcoin"))
SESSION_FILE = os.environ.get("SESSION_FILE", os.path.join(_DATA_DIR, "sessions.json"))
KEY_FILE = os.environ.get("SESSION_KEY_FILE", os.path.join(_DATA_DIR, "fernet.key"))


class SessionManager:
    def __init__(self):
        self._migrate_legacy_files()
        self._fernet = self._load_or_create_key()
        self._sessions: dict[str, dict] = {}
        self._lock = threading.Lock()
        self._load_sessions()

    @staticmethod
    def _migrate_legacy_files():
        """Migrate key/session files from /tmp/ to ~/.botcoin/ if they exist."""
        SessionManager._ensure_data_dir()
        for old, new in [("/tmp/botcoin_fernet.key", KEY_FILE),
                         ("/tmp/botcoin_sessions.json", SESSION_FILE)]:
            if old == new:
                continue
            try:
                if os.path.exists(old) and not os.path.exists(new):
                    import shutil
                    shutil.move(old, new)
                    os.chmod(new, 0o600)
                elif os.path.exists(old) and os.path.exists(new):
                    os.remove(old)  # Clean up old file
            except Exception:
                pass

    def _load_or_create_key(self) -> Fernet:
        """Load Fernet key from disk, or create and persist a new one."""
        self._ensure_data_dir()
        try:
            if os.path.exists(KEY_FILE):
                with open(KEY_FILE, "r") as f:
                    key = f.read().strip()
                if key:
                    return Fernet(key.encode())
        except Exception:
            pass
        key = Fernet.generate_key()
        try:
            with open(KEY_FILE, "w") as f:
                f.write(key.decode())
            os.chmod(KEY_FILE, 0o600)
        except Exception:
            pass
        return Fernet(key)

    @staticmethod
    def _ensure_data_dir():
        """Create data directory with owner-only permissions if it doesn't exist."""
        if not os.path.exists(_DATA_DIR):
            os.makedirs(_DATA_DIR, mode=0o700, exist_ok=True)
        else:
            # Tighten permissions if directory already exists
            try:
                os.chmod(_DATA_DIR, 0o700)
            except Exception:
                pass

    def _load_sessions(self):
        """Load sessions from disk."""
        try:
            if os.path.exists(SESSION_FILE):
                with open(SESSION_FILE, "r") as f:
                    data = json.load(f)
                now = time.time()
                for sid, sess in data.items():
                    # Skip expired sessions
                    if now - sess.get("created_at", 0) > SESSION_TTL:
                        continue
                    # encrypted_key is stored as string, convert back to bytes
                    sess["encrypted_key"] = sess["encrypted_key"].encode()
                    # Verify the key can be decrypted with current Fernet key
                    try:
                        self._fernet.decrypt(sess["encrypted_key"])
                        self._sessions[sid] = sess
                    except Exception:
                        pass  # Key from different Fernet instance, skip
        except Exception:
            pass

    def _save_sessions(self):
        """Persist sessions to disk. Must be called with lock held."""
        try:
            data = {}
            for sid, sess in self._sessions.items():
                data[sid] = {
                    "encrypted_key": sess["encrypted_key"].decode()
                        if isinstance(sess["encrypted_key"], bytes)
                        else sess["encrypted_key"],
                    "miner_address": sess.get("miner_address", ""),
                    "csrf_token": sess.get("csrf_token", ""),
                    "created_at": sess.get("created_at", 0),
                    "last_active": sess.get("last_active", 0),
                }
            with open(SESSION_FILE, "w") as f:
                json.dump(data, f)
            os.chmod(SESSION_FILE, 0o600)
        except Exception:
            pass

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
            self._save_sessions()
        return session_id

    def get_session(self, session_id: str) -> dict | None:
        """Get session metadata (without decrypted key). Returns None if expired/missing."""
        with self._lock:
            sess = self._sessions.get(session_id)
            if not sess:
                return None
            if time.time() - sess["created_at"] > SESSION_TTL:
                del self._sessions[session_id]
                self._save_sessions()
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
                self._save_sessions()
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
                self._save_sessions()

    def destroy_session(self, session_id: str):
        """Remove a session."""
        with self._lock:
            self._sessions.pop(session_id, None)
            self._save_sessions()

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
            self._save_sessions()
