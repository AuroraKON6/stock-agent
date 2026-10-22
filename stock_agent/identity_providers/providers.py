"""R10: provider registry — the Windows-local bridge stays first-class.

Providers AUTHENTICATE (prove a real principal); the session store then holds
the resulting binding. Registry lookups fail closed: an unknown provider name
is an error, never a pass-through. The default provider on Windows remains
``local_identity.authenticate_local_actor`` (the existing CLI-grade bridge);
container deployments register a stronger provider later without touching the
session layer.
"""
from __future__ import annotations

from typing import Any, Callable, Dict, List

from .. import local_identity

PROVIDER_WINDOWS_LOCAL = "windows_local"

# authenticator(config, actor, symbols) -> binding dict
#   {actor, provider, subject, binding_evidence_ref, policy_sha256, ...}
Authenticator = Callable[[Dict[str, Any], str, List[str]], Dict[str, Any]]


def _windows_local_authenticator(config: Dict[str, Any], actor: str,
                                  symbols: List[str]) -> Dict[str, Any]:
    return local_identity.authenticate_local_actor(config, actor, symbols)


_REGISTRY: Dict[str, Authenticator] = {
    PROVIDER_WINDOWS_LOCAL: _windows_local_authenticator,
}


def available_providers() -> List[str]:
    return sorted(_REGISTRY)


def register_provider(name: str, authenticator: Authenticator) -> None:
    name = str(name or "").strip()
    if not name:
        raise ValueError("provider name must not be empty")
    if name in _REGISTRY:
        raise ValueError("provider already registered: {0}".format(name))
    _REGISTRY[name] = authenticator


def authenticate_with_provider(provider: str, config: Dict[str, Any], actor: str,
                               symbols: List[str]) -> Dict[str, Any]:
    """Authenticate one principal through a NAMED provider; fail closed.

    The Windows bridge raises LocalIdentityError off-Windows — that is the
    documented fail-closed rollback path, not a bug to catch here."""
    authenticator = _REGISTRY.get(str(provider or ""))
    if authenticator is None:
        raise local_identity.LocalIdentityError(
            "unknown identity provider: {0!r} (available: {1})".format(
                provider, available_providers()))
    return authenticator(config, actor, symbols)
