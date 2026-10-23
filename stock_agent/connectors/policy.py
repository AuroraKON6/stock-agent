"""R18: connector policy — fail-closed manifests, tool visibility, egress
guards (exact origin, DNS pinning, no caller tokens)."""
from __future__ import annotations

import ipaddress
import json
import re
from dataclasses import dataclass, field
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


class PolicyViolation(ValueError):
    """reasons: malformed_manifest, unknown_connector, tool_not_configured,
    connector_revoked, caller_token_forbidden, unsafe_destination,
    redirect_refused, endpoint_not_pinned"""


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise PolicyViolation("malformed_manifest: {0}".format(message))


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
        if keys & {"authorization", "x-api-key", "cookie", "proxy-authorization"}:
            raise PolicyViolation(
                "caller_token_forbidden: connectors never forward caller credentials")


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
