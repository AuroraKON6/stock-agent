"""R18: connector policy — fail-closed manifests, tool visibility, egress
guards (exact origin, DNS pinning, no caller tokens)."""
from __future__ import annotations

import ipaddress
import json
import re
import threading
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Set
from urllib.parse import urlsplit

CONNECTOR_POLICY_SCHEMA = "1.0"
TRANSPORTS = {"mcp_stdio", "mcp_http", "http"}
MANIFEST_FIELDS = {
    "connector_id", "transport", "endpoint", "allowed_tools", "versions",
    "max_response_bytes", "max_calls_per_day", "requests_per_minute",
    "token_audience", "revoked", "expires_at",
}
_AUDIENCE_RE = re.compile(r"^[a-z0-9][a-z0-9._:/-]{3,128}$")
# A82: these keys are caller credentials wherever they appear — headers or
# (recursively) tool arguments. A connector authenticates as ITSELF; a caller
# credential must never ride the connector.
CALLER_CREDENTIAL_KEYS = frozenset({
    "authorization", "x-api-key", "cookie", "proxy-authorization",
    "api-key", "x-auth-token",
})


class PolicyViolation(ValueError):
    """reasons: malformed_manifest, unknown_connector, tool_not_configured,
    connector_revoked, caller_token_forbidden, unsafe_destination,
    unsafe_path, redirect_refused, endpoint_not_pinned,
    credential_not_bound, credential_audience_mismatch, credential_expired,
    credential_destination_mismatch"""


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise PolicyViolation("malformed_manifest: {0}".format(message))


def _origin(url: str) -> str:
    parts = urlsplit(str(url or ""))
    return "{0}://{1}".format(parts.scheme, parts.netloc)


def _parse_expiry(value: str) -> Optional[datetime]:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise PolicyViolation(
            "credential_not_bound: unparseable expires_at {0!r}".format(text)) from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


@dataclass(frozen=True)
class ConnectorCredential:
    """A82: the connector's OWN credential, bound by trusted config to one
    connector id, one audience and one approved destination origin. Never
    caller-supplied. ``token`` is redacted from repr so it cannot leak into
    logs. ``destination`` is an independent binding from the policy document:
    swapping the policy endpoint cannot make an old credential usable."""

    connector_id: str
    audience: str
    token: str = field(repr=False)
    destination: str = ""
    expires_at: str = ""

    def __repr__(self) -> str:
        return ("ConnectorCredential(connector_id={0!r}, audience={1!r}, "
                "token=<redacted>, destination={2!r}, expires_at={3!r})").format(
                    self.connector_id, self.audience, self.destination,
                    self.expires_at)


def bind_connector_credential(policy: "ConnectorPolicy",
                              credential: Any,
                              destination: str = "") -> Dict[str, str]:
    """A82: adjudicate the connector identity BEFORE any bytes move. The
    credential must be the trusted-config credential for THIS connector and
    THIS audience, unexpired, and bound to the approved destination given for
    this request. Returns the presentation handed to the transport adapter."""
    if not isinstance(credential, ConnectorCredential):
        raise PolicyViolation(
            "credential_not_bound: connector identity missing or not a "
            "trusted-config ConnectorCredential")
    if not str(credential.token or "").strip():
        raise PolicyViolation("credential_not_bound: empty connector token")
    if str(credential.connector_id or "") != policy.connector_id:
        raise PolicyViolation(
            "credential_not_bound: credential is for {0!r}, connector is {1!r}".format(
                credential.connector_id, policy.connector_id))
    if str(credential.audience or "") != str(policy.token_audience or ""):
        raise PolicyViolation(
            "credential_audience_mismatch: credential audience {0!r} != policy "
            "token_audience {1!r}".format(credential.audience, policy.token_audience))
    expiry = _parse_expiry(credential.expires_at)
    if expiry is not None and expiry <= datetime.now(timezone.utc):
        raise PolicyViolation(
            "credential_expired: connector credential expired at {0}".format(
                credential.expires_at))
    if destination:
        approved = str(credential.destination or "").strip()
        if not approved:
            raise PolicyViolation(
                "credential_destination_mismatch: credential has no approved "
                "destination binding")
        if _origin(approved) != _origin(destination):
            raise PolicyViolation(
                "credential_destination_mismatch: credential approved for {0}, "
                "request goes to {1}".format(_origin(approved), _origin(destination)))
        if _origin(destination) != _origin(policy.endpoint):
            raise PolicyViolation(
                "credential_destination_mismatch: request destination {0} is not "
                "the policy endpoint {1}".format(
                    _origin(destination), _origin(policy.endpoint)))
    return {
        "connector_id": policy.connector_id,
        "audience": str(policy.token_audience or ""),
        "destination": _origin(destination) if destination else "",
        "authorization": "Bearer {0}".format(credential.token),
    }



@dataclass(frozen=True)
class ConnectorPolicy:
    connector_id: str
    transport: str                    # mcp_stdio | mcp_http | http
    endpoint: str                     # https origin (remote) / command name (stdio)
    allowed_tools: frozenset          # tool whitelist — everything else invisible
    versions: tuple                   # approved connector versions
    max_response_bytes: int = 4 * 1024 * 1024
    max_calls_per_day: int = 0        # 0 = deny (fail closed)
    requests_per_minute: int = 0
    token_audience: str = ""          # required for remote transports
    revoked: bool = False
    expires_at: str = ""

    # ---- manifest -------------------------------------------------- #

    @classmethod
    def from_manifest(cls, entry: Dict[str, Any]) -> "ConnectorPolicy":
        _require(isinstance(entry, dict), "entry must be an object")
        unknown = set(entry) - MANIFEST_FIELDS
        _require(not unknown, "unknown fields: {0}".format(sorted(unknown)))
        connector_id = str(entry.get("connector_id") or "").strip()
        _require(bool(connector_id), "connector_id required")
        transport = str(entry.get("transport") or "")
        _require(transport in TRANSPORTS,
                 "transport must be one of {0}".format(sorted(TRANSPORTS)))
        endpoint = str(entry.get("endpoint") or "").strip()
        _require(bool(endpoint), "endpoint required")
        if transport != "mcp_stdio":
            parts = urlsplit(endpoint)
            _require(parts.scheme == "https", "remote endpoint must be HTTPS")
            _require(bool(parts.netloc) and not parts.query and not parts.fragment,
                     "remote endpoint is an exact origin (no query/fragment)")
        tools = entry.get("allowed_tools")
        _require(isinstance(tools, list)
                 and all(isinstance(t, str) and t.strip() for t in tools),
                 "allowed_tools must be a list of names")
        versions = entry.get("versions")
        _require(isinstance(versions, list) and versions,
                 "versions must be a non-empty list")
        audience = str(entry.get("token_audience") or "")
        if transport != "mcp_stdio":
            _require(bool(_AUDIENCE_RE.fullmatch(audience)),
                     "remote transports require a token_audience")
        return cls(
            connector_id=connector_id, transport=transport, endpoint=endpoint,
            allowed_tools=frozenset(str(t) for t in tools),
            versions=tuple(str(v) for v in versions),
            max_response_bytes=max(1024, int(entry.get("max_response_bytes") or 0)),
            max_calls_per_day=int(entry.get("max_calls_per_day") or 0),
            requests_per_minute=int(entry.get("requests_per_minute") or 0),
            token_audience=audience,
            revoked=bool(entry.get("revoked")),
            expires_at=str(entry.get("expires_at") or ""),
        )

    # ---- gates ------------------------------------------------------ #

    def tool_visible(self, tool: str) -> bool:
        """A81: visibility IS the whitelist — nothing else is listed or callable."""
        return (not self.revoked) and str(tool) in self.allowed_tools

    def assert_callable(self, tool: str) -> None:
        if self.revoked:
            raise PolicyViolation("connector_revoked: {0}".format(self.connector_id))
        if not self.tool_visible(tool):
            raise PolicyViolation(
                "tool_not_configured: {0} on {1}".format(tool, self.connector_id))

    def forbid_caller_tokens(self, supplied: Dict[str, Any]) -> None:
        """A82: caller credentials never ride the connector. The connector
        authenticates as ITSELF (policy-configured credential with the
        recorded audience); a caller-supplied Authorization/token is a
        programming error we refuse, not a header we forward."""
        keys = {str(k).lower() for k in (supplied or {})}
        if keys & CALLER_CREDENTIAL_KEYS:
            raise PolicyViolation(
                "caller_token_forbidden: connectors never forward caller credentials")

    def forbid_caller_tokens_deep(self, value: Any, path: str = "arguments") -> None:
        """A82: the args boundary. A caller credential hidden anywhere in the
        tool arguments (nested object/list) is refused before anything is
        sent — the same rule as the header boundary."""
        if isinstance(value, dict):
            for key, item in value.items():
                if str(key).strip().lower() in CALLER_CREDENTIAL_KEYS:
                    raise PolicyViolation(
                        "caller_token_forbidden: caller credential key {0!r} at "
                        "{1}; connectors never forward caller credentials".format(
                            key, path))
                self.forbid_caller_tokens_deep(item, "{0}.{1}".format(path, key))
        elif isinstance(value, (list, tuple)):
            for index, item in enumerate(value):
                self.forbid_caller_tokens_deep(item, "{0}[{1}]".format(path, index))


def load_connector_registry(path: Path) -> Dict[str, ConnectorPolicy]:
    """Fail-closed manifest load: unknown/duplicate ids and any malformed
    entry refuse the WHOLE registry (no partial availability)."""
    text = Path(path).read_text(encoding="utf-8")
    document = json.loads(text)
    if not isinstance(document, dict) or document.get("schema_version") != CONNECTOR_POLICY_SCHEMA:
        raise PolicyViolation("malformed_manifest: unsupported schema")
    entries = document.get("connectors")
    if not isinstance(entries, list):
        raise PolicyViolation("malformed_manifest: connectors must be a list")
    registry: Dict[str, ConnectorPolicy] = {}
    for entry in entries:
        policy = ConnectorPolicy.from_manifest(entry)
        if policy.connector_id in registry:
            raise PolicyViolation(
                "malformed_manifest: duplicate connector_id {0}".format(policy.connector_id))
        registry[policy.connector_id] = policy
    return registry


class ConnectorRegistry:
    """A81: a live registry. Clients hold ``registry.provider(id)`` instead of
    a frozen policy snapshot, so a manifest reload or an explicit revocation
    takes effect on the SAME live client instance — visibility and calls
    stop immediately, not after the client is rebuilt."""

    def __init__(self, path: Path):
        self.path = Path(path)
        self._lock = threading.Lock()
        self._policies: Dict[str, ConnectorPolicy] = {}
        self.reload()

    def reload(self) -> Dict[str, ConnectorPolicy]:
        policies = load_connector_registry(self.path)
        with self._lock:
            self._policies = policies
        return policies

    def provider(self, connector_id: str) -> Callable[[], ConnectorPolicy]:
        return lambda: self.policy(connector_id)

    def policy(self, connector_id: str) -> ConnectorPolicy:
        with self._lock:
            policy = self._policies.get(connector_id)
        if policy is None:
            raise PolicyViolation("unknown_connector: {0}".format(connector_id))
        return policy

    def revoke(self, connector_id: str) -> ConnectorPolicy:
        """A81: revocation without a file round-trip; the next lookup on an
        existing client sees it."""
        with self._lock:
            current = self._policies.get(connector_id)
            if current is None:
                raise PolicyViolation("unknown_connector: {0}".format(connector_id))
            revoked = replace(current, revoked=True)
            next_policies = dict(self._policies)
            next_policies[connector_id] = revoked
            self._policies = next_policies
        return revoked


def resolve_endpoint_addresses(policy: ConnectorPolicy,
                               resolver: Callable[[str], List[str]]) -> List[str]:
    """A83: pin the destination BEFORE any request — resolve the endpoint
    host and refuse private/loopback/link-local addresses. The caller passes
    the resolver so tests can pin deterministic answers and production can
    wrap the SDK's resolution."""
    if policy.transport == "mcp_stdio":
        return []
    host = urlsplit(policy.endpoint).hostname or ""
    addresses = list(resolver(host) or [])
    if not addresses:
        raise PolicyViolation("endpoint_not_pinned: {0} did not resolve".format(host))
    for address in addresses:
        try:
            parsed = ipaddress.ip_address(address)
        except ValueError as exc:
            raise PolicyViolation(
                "endpoint_not_pinned: unparseable address {0!r}".format(address)) from exc
        if parsed.is_private or parsed.is_loopback or parsed.is_link_local \
                or parsed.is_multicast or parsed.is_reserved:
            raise PolicyViolation(
                "unsafe_destination: {0} resolves to non-public {1}".format(host, address))
    return addresses


def support_matrix() -> Dict[str, str]:
    """A80: report what THIS implementation actually supports — no aspirational
    rows. stdio/http are verified against in-process fakes; SSE is honestly
    NOT_RUN until the SDK-backed transport lands."""
    return {
        "mcp_stdio": "VERIFIED_O_inprocess_fake",
        "http": "VERIFIED_O_inprocess_fake",
        "mcp_http": "NOT_RUN_pending_sdk_adoption",
        "mcp_sse": "NOT_RUN_pending_sdk_adoption",
        "official_mcp_sdk": "NOT_RUN_dependency_registration_required",
    }
