"""R10: identity providers — server-side sessions bound to real principals.

The board's legacy human-response path trusts an ``actor`` FORM FIELD: anyone
who can reach the loopback port can type any name (F17/A46). This package
introduces the session layer that replaces it:

- ``PersonalSessionStore``: server-side sessions created ONLY by a successful
  provider authentication (the Windows-local bridge today; a container-grade
  provider later). Protected actions resolve the actor FROM THE SESSION —
  form-supplied actors are rejected, not merged (A46).
- Automated principals (service tokens, bots, ``dify:`` credentials) can
  never log in, so they can never sign off personally (A47).
- Logout/expiry/revocation take effect on the NEXT protected action (A48).
- The provider registry keeps the Windows CLI bridge available (rollback
  path); when no provider authenticates, protected writes FAIL CLOSED.
"""
from .providers import authenticate_with_provider, available_providers
from .sessions import (
    PersonalSession,
    PersonalSessionError,
    PersonalSessionStore,
    SESSION_COOKIE_NAME,
)

__all__ = [
    "PersonalSession",
    "PersonalSessionError",
    "PersonalSessionStore",
    "SESSION_COOKIE_NAME",
    "authenticate_with_provider",
    "available_providers",
]
