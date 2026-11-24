"""R18: HTTP connector — exact-origin POST with DNS pinning, size caps,
validated responses and a bound connector identity. Transport injected;
offline tests drive a fake."""
from __future__ import annotations

import json
from typing import Any, Callable, Dict, Optional
from urllib.parse import urlsplit

from .policy import (
    ConnectorPolicy,
    PolicyViolation,
    bind_connector_credential,
    resolve_endpoint_addresses,
)


class HTTPConnectorError(RuntimeError):
    """reasons: redirect_refused, response_too_large, response_malformed,
    http_status_error, transport_failed_before_send,
    transport_failed_after_send, transport_delivery_unknown"""


class HTTPTransportError(RuntimeError):
    """Raised by a transport adapter that KNOWS the delivery phase.

    phase: ``before_send`` (nothing left the process), ``after_send`` (the
    request was written and the reply was lost), ``unknown`` (cannot tell).
    The connector never retries; after_send/unknown always carry a manual
    reconciliation instruction (A83/A85)."""

    PHASES = {"before_send", "after_send", "unknown"}

    def __init__(self, message: str, *, phase: str = "unknown"):
        super().__init__(message)
        self.phase = phase if phase in self.PHASES else "unknown"


class _FakeResponse:
    def __init__(self, status: int, body: bytes, headers: Optional[Dict[str, str]] = None):
        self.status = status
        self.body = body
        self.headers = headers or {}


def approved_request_url(policy: ConnectorPolicy, path: str = "") -> str:
    """A83: build the URL from the parsed origin plus a validated path and
    re-assert the final origin. String concatenation can no longer move the
    request onto another host (e.g. ``path="@evil.example/x"``)."""
    origin = str(policy.endpoint or "")
    parts = urlsplit(origin)
    if parts.scheme != "https" or not parts.netloc:
        raise PolicyViolation("unsafe_destination: endpoint is not an HTTPS origin")
    request_path = str(path or "")
    if request_path:
        if not request_path.startswith("/") or request_path.startswith("//"):
            raise PolicyViolation(
                "unsafe_path: request path must start with a single '/': {0!r}".format(
                    request_path))
        if any(ch in request_path for ch in ("@", "#", "\\")) \
                or any(ord(ch) < 32 for ch in request_path):
            raise PolicyViolation(
                "unsafe_path: request path is not allowed: {0!r}".format(request_path))
    url = origin + request_path
    final = urlsplit(url)
    if (final.scheme != parts.scheme or final.netloc != parts.netloc
            or final.hostname != parts.hostname):
        raise PolicyViolation(
            "unsafe_path: request moved off the approved origin {0}".format(origin))
    return url


class HTTPConnector:
    """One policy-bound POST client. The connector authenticates as itself
    with its trusted-config credential (A82); caller credentials are refused.
    ``policy`` may be a live provider so revocation reaches this instance
    (A81); ``resolver`` answers DNS for the construction-time pin (A83)."""

    def __init__(self, policy: Any, transport: Any,
                 resolver: Callable[[str], list],
                 credential_provider: Callable[[], Any]):
        self._policy_source = (policy if callable(policy)
                               else (lambda policy=policy: policy))
        current = self.policy
        if current.transport not in ("http", "mcp_http"):
            raise PolicyViolation("malformed_manifest: http client needs an http transport")
        self.transport = transport
        self.resolver = resolver
        self.credential_provider = credential_provider
        # A83: pin the destination at construction — every request goes to a
        # verified-public, exact origin or nothing goes at all.
        self.pinned_addresses = resolve_endpoint_addresses(current, resolver)
        self._pinned_endpoint = current.endpoint

    @property
    def policy(self) -> ConnectorPolicy:
        return self._policy_source()

    def _approved_addresses(self, policy: ConnectorPolicy):
        if policy.endpoint != self._pinned_endpoint:
            self.pinned_addresses = resolve_endpoint_addresses(policy, self.resolver)
            self._pinned_endpoint = policy.endpoint
        return self.pinned_addresses

    def post(self, payload: Dict[str, Any], *,
             caller_headers: Optional[Dict[str, Any]] = None,
             path: str = "") -> Dict[str, Any]:
        policy = self.policy
        # A82: caller tokens never ride the connector.
        policy.forbid_caller_tokens(caller_headers or {})
        # A81: live revocation applies to this same instance.
        if policy.revoked:
            raise PolicyViolation("connector_revoked: {0}".format(policy.connector_id))
        # A83: constrain the final origin/path and the approved addresses.
        url = approved_request_url(policy, path)
        addresses = self._approved_addresses(policy)
        # A82: the connector presents its OWN bound identity, never caller
        # credentials. Binding/audience/destination failures stop here.
        presentation = bind_connector_credential(
            policy, self.credential_provider(), destination=url)
        identity = {"connector_id": presentation["connector_id"],
                    "audience": presentation["audience"]}
        body = json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8")
        try:
            response = self.transport.post(
                url, body,
                headers={"Authorization": presentation["authorization"]},
                addresses=addresses, identity=identity)
        except Exception as exc:
            # A83/A85: one attempt only. Classify honestly; never claim
            # "before_send" unless the adapter says so.
            phase = str(getattr(exc, "phase", "unknown") or "unknown")
            detail = "{0}: {1}".format(type(exc).__name__, exc)
            if phase == "before_send":
                raise HTTPConnectorError(
                    "transport_failed_before_send: {0}".format(detail)) from exc
            if phase == "after_send":
                raise HTTPConnectorError(
                    "transport_failed_after_send: {0}; manual reconciliation".format(
                        detail)) from exc
            raise HTTPConnectorError(
                "transport_delivery_unknown: {0}; manual reconciliation".format(
                    detail)) from exc
        status = int(getattr(response, "status", 0) or 0)
        if 300 <= status < 400:
            # A83: a redirect is a destination change — refused, never followed.
            raise HTTPConnectorError(
                "redirect_refused: {0} Location={1!r}".format(
                    status, (response.headers or {}).get("Location", "")))
        raw = bytes(getattr(response, "body", b"") or b"")
        if len(raw) > policy.max_response_bytes:
            raise HTTPConnectorError("response_too_large: {0} > cap {1}".format(
                len(raw), policy.max_response_bytes))
        if status >= 400:
            raise HTTPConnectorError("http_status_error: {0}".format(status))
        try:
            parsed = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise HTTPConnectorError("response_malformed: {0}".format(exc)) from exc
        # A84: the response contract is a top-level JSON object.
        if not isinstance(parsed, dict):
            raise HTTPConnectorError(
                "response_malformed: top-level must be a JSON object, got {0}".format(
                    type(parsed).__name__))
        return parsed
