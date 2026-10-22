"""R10: server-side personal sessions with expiry and revocation."""
from __future__ import annotations

import secrets
import threading
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from ..research_runtime import AUTOMATED_ACTOR_PREFIXES

SESSION_COOKIE_NAME = "task_board_session"
DEFAULT_SESSION_TTL_SECONDS = 3600

# Service-credential actor formats this project actually mints (R03 audit
# trails use "dify:<credential_id>"; service accounts often read
# "name_service"/"svc-name"). A personal session must never belong to one.
_SERVICE_ACTOR_MARKERS = ("dify:", "service", "svc-", "system:", "automation")


class PersonalSessionError(ValueError):
    """reason codes: automated_principal, unknown_session, expired_session,
    revoked_session, login_failed"""


@dataclass
class PersonalSession:
    session_id: str
    actor: str
    provider: str
    subject: str
    issued_at: str
    expires_at: str
    revoked: bool = False
    policy_sha256: str = ""

    def as_dict(self) -> Dict[str, Any]:
        return {
            "session_id": self.session_id,
            "actor": self.actor,
            "provider": self.provider,
            "subject": self.subject,
            "issued_at": self.issued_at,
            "expires_at": self.expires_at,
            "revoked": self.revoked,
        }


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _iso(moment: datetime) -> str:
    return moment.isoformat(timespec="seconds")


class PersonalSessionStore:
    """Thread-safe in-process session store.

    Sessions are server-side state by design: a stolen cookie is one
    revocable handle, not a bearer identity; the actor can never be amended
    from the client after login."""

    def __init__(self, *, ttl_seconds: int = DEFAULT_SESSION_TTL_SECONDS,
                 clock: Optional[Any] = None):
        self._ttl = max(60, int(ttl_seconds))
        self._clock = clock or _utcnow
        self._lock = threading.Lock()
        self._sessions: Dict[str, PersonalSession] = {}

    # ------------------------------------------------------------------ #

    def login(self, *, actor: str, provider: str, subject: str,
              policy_sha256: str = "", now: Optional[datetime] = None) -> PersonalSession:
        actor = str(actor or "").strip()
        if not actor:
            raise PersonalSessionError("login_failed: actor required")
        lowered = actor.lower()
        if lowered.startswith(AUTOMATED_ACTOR_PREFIXES) or \
                any(marker in lowered for marker in _SERVICE_ACTOR_MARKERS):
            # A47: service tokens / bots / platform credentials never hold a
            # personal session, so they can never sign off personally.
            raise PersonalSessionError(
                "automated_principal: service identities cannot hold personal sessions")
        moment = now or self._clock()
        session = PersonalSession(
            session_id="ps_" + secrets.token_urlsafe(24),
            actor=actor,
            provider=str(provider or ""),
            subject=str(subject or ""),
            issued_at=_iso(moment),
            expires_at=_iso(moment + timedelta(seconds=self._ttl)),
            policy_sha256=str(policy_sha256 or ""))
        with self._lock:
            self._sessions[session.session_id] = session
        return session

    def resolve(self, session_id: str, *,
                now: Optional[datetime] = None) -> PersonalSession:
        """A48: unknown, expired and revoked sessions all fail HERE, on the
        next protected action — not at some later cleanup."""
        key = str(session_id or "")
        with self._lock:
            session = self._sessions.get(key)
            if session is None:
                raise PersonalSessionError("unknown_session")
            if session.revoked:
                raise PersonalSessionError("revoked_session")
        expires = datetime.fromisoformat(session.expires_at)
        if expires <= (now or self._clock()):
            raise PersonalSessionError("expired_session")
        return session

    def logout(self, session_id: str) -> bool:
        key = str(session_id or "")
        with self._lock:
            session = self._sessions.get(key)
            if session is None or session.revoked:
                return False
            session.revoked = True
        return True

    def revoke_all_for_actor(self, actor: str) -> int:
        actor = str(actor or "")
        count = 0
        with self._lock:
            for session in self._sessions.values():
                if session.actor == actor and not session.revoked:
                    session.revoked = True
                    count += 1
        return count

    def active_count(self) -> int:
        with self._lock:
            return sum(1 for s in self._sessions.values() if not s.revoked)
