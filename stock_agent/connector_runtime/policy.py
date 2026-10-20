"""R04: connector execution policy — purpose gate, destinations, cache TTL.

Everything here refuses BEFORE bytes are sent (A24/A25):

- ``assert_purpose_registered``: the requested purpose must be listed in the
  registry entry's machine-readable ``purposes`` allowlist. Entries without
  the field allow NOTHING (fail closed): a description string is not a grant.
- ``resolve_destination`` / ``assert_transport_destination``: the only
  approved destination is the exact origin+path from the HOST integration
  config (HTTPS enforced). A caller-constructed ``DestinationPolicy`` on a
  real transport is a refusal, not a detour (F11).
- ``cache_ttl_seconds`` / ``cache_rights_revision``: cache freshness and the
  rights revision a cached document must still match at read time (A21/A22).
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, Tuple

DEFAULT_CACHE_TTL_SECONDS = 24 * 3600


class DestinationRefused(RuntimeError):
    pass


def quota_group_for(upstream: str) -> str:
    """Kimi-routed and direct interfaces of the SAME upstream share one quota
    group (A23): the transport is not the source identity."""
    return "upstream::{0}".format(str(upstream or "").strip().lower())


def assert_purpose_registered(entry: Dict[str, Any], purpose: str) -> Tuple[bool, str]:
    purposes = entry.get("purposes") if isinstance(entry, dict) else None
    requested = str(purpose or "").strip()
    if not isinstance(purposes, list) or not purposes:
        return False, "purpose_not_registered"
    registered = {str(item).strip() for item in purposes if str(item or "").strip()}
    if requested and requested in registered:
        return True, "ok"
    return False, "purpose_not_registered"


def resolve_destination(integration: Any) -> Any:
    """Build the ONLY approved destination from the host integration config."""
    from ..integrations.kimi import transport as kimi_transport

    origin = str(getattr(integration, "origin", "") or "").strip()
    path = str(getattr(integration, "tools_path", "") or "").strip()
    if not origin or not path:
        raise DestinationRefused("integration config names no destination origin")
    if not origin.lower().startswith("https://"):
        raise DestinationRefused("non-HTTPS destination refused: {0}".format(origin))
    return kimi_transport.DestinationPolicy(origin=origin, path=path)


def assert_transport_destination(transport: Any, integration: Any) -> Tuple[bool, str]:
    """A real (urllib) transport must already point at the approved exact
    origin+path. Injected fakes are the offline test seam and carry no
    destination of their own — production wiring is reviewed separately."""
    from ..integrations.kimi import transport as kimi_transport

    approved = resolve_destination(integration)
    if not isinstance(transport, kimi_transport.UrllibTransport):
        return True, "offline_seam"
    if transport.policy.url().rstrip("/") != approved.url().rstrip("/"):
        return False, "transport_destination_not_approved"
    return True, "ok"


def cache_ttl_seconds(config: Dict[str, Any]) -> int:
    raw = ((config or {}).get("kimi_sources") or {}).get("cache_ttl_seconds")
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return DEFAULT_CACHE_TTL_SECONDS
    return value if value > 0 else DEFAULT_CACHE_TTL_SECONDS


def cache_rights_revision(entry: Dict[str, Any]) -> str:
    """Revision of the rights that produced a cached document: re-approval or
    expiry changes it, invalidating every older cache entry (A21)."""
    approval = entry.get("approval") if isinstance(entry, dict) else None
    approved_at = str((approval or {}).get("approved_at") or "").strip()
    if approved_at:
        return approved_at
    return str(entry.get("expires_at") or "").strip()


def cache_expiry(now: Any = None, ttl_seconds: int = DEFAULT_CACHE_TTL_SECONDS) -> str:
    moment = now or datetime.now(timezone.utc)
    from datetime import timedelta

    return (moment + timedelta(seconds=max(1, int(ttl_seconds)))).isoformat(timespec="seconds")
