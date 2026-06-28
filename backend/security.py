from __future__ import annotations

import hmac
import os
import secrets
import time
from dataclasses import dataclass
from threading import RLock


@dataclass
class Session:
    role: str
    expires_at: float


class AuthManager:
    def __init__(self, ttl_seconds: int = 12 * 60 * 60):
        self.ttl_seconds = ttl_seconds
        self._sessions: dict[str, Session] = {}
        self._lock = RLock()

    @property
    def admin_password(self) -> str:
        return os.environ.get("MUSICLIGHT_ADMIN_PASSWORD", "admin")

    @property
    def user_password(self) -> str:
        return os.environ.get("MUSICLIGHT_USER_PASSWORD", "user")

    def login(self, role: str, password: str) -> str | None:
        expected = self.admin_password if role == "admin" else self.user_password if role == "user" else None
        if expected is None:
            return None
        if not hmac.compare_digest(str(password), expected):
            return None

        token = secrets.token_urlsafe(32)
        with self._lock:
            self._prune_locked()
            self._sessions[token] = Session(role=role, expires_at=time.time() + self.ttl_seconds)
        return token

    def role_for_token(self, token: str) -> str | None:
        with self._lock:
            self._prune_locked()
            session = self._sessions.get(token)
            return session.role if session else None

    def logout(self, token: str) -> None:
        with self._lock:
            self._sessions.pop(token, None)

    def _prune_locked(self) -> None:
        now = time.time()
        expired = [token for token, session in self._sessions.items() if session.expires_at <= now]
        for token in expired:
            self._sessions.pop(token, None)
