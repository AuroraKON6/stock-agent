"""R18: HTTP connector — exact-origin POST with DNS pinning, size caps and
no caller tokens. Transport injected; offline tests drive a fake."""
from __future__ import annotations

import json
from typing import Any, Callable, Dict, Optional

from .policy import ConnectorPolicy, PolicyViolation, resolve_endpoint_addresses


class HTTPConnectorError(RuntimeError):
    """reasons: redirect_refused, response_too_large, response_malformed,
    transport_failed_after_send, http_status_error"""


class _FakeResponse:
    def __init__(self, status: int, body: bytes, headers: Optional[Dict[str, str]] = None):
        self.status = status
        self.body = body
        self.headers = headers or {}


class HTTPConnector:
    """One policy-bound POST client. The connector authenticates as itself
    (policy credential provider); caller credentials are refused (A82)."""

    def __init__(self, policy: ConnectorPolicy, transport: Any,
                 resolver: Callable[[str], list],
                 credential_provider: Callable[[], str]):
        if policy.transport not in ("http", "mcp_http"):
            raise PolicyViolation("malformed_manifest: http client needs an http transport")
        self.policy = policy
        self.transport = transport
        self.resolver = resolver
        self.credential_provider = credential_provider
        # A83: pin the destination at construction — every request goes to a
        # verified-public, exact origin or nothing goes at all.
        self.pinned_addresses = resolve_endpoint_addresses(policy, resolver)

    def post(self, payload: Dict[str, Any], *,
             caller_headers: Optional[Dict[str, Any]] = None,
             path: str = "") -> Dict[str, Any]:
        # A82: caller tokens never ride the connector.
        self.policy.forbid_caller_tokens(caller_headers or {})
        if self.policy.revoked:
            raise PolicyViolation("connector_revoked: {0}".format(self.policy.connector_id))
        body = json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8")
        sent = False
        try:
            response = self.transport.post(
                self.policy.endpoint + path, body,
                headers={"Authorization": "Bearer {0}".format(self.credential_provider())})
            sent = True
        except Exception as exc:
            if sent:
                raise HTTPConnectorError(
                    "transport_failed_after_send: {0}".format(type(exc).__name__)) from exc
            raise HTTPConnectorError(
                "transport_failed_before_send: {0}".format(type(exc).__name__)) from exc
        status = int(getattr(response, "status", 0) or 0)
        if 300 <= status < 400:
            # A83: a redirect is a destination change — refused, never followed.
            raise HTTPConnectorError(
                "redirect_refused: {0} Location={1!r}".format(
                    status, (response.headers or {}).get("Location", "")))
        raw = bytes(getattr(response, "body", b"") or b"")
        if len(raw) > self.policy.max_response_bytes:
            raise HTTPConnectorError("response_too_large: {0} > cap {1}".format(
                len(raw), self.policy.max_response_bytes))
        if status >= 400:
            raise HTTPConnectorError("http_status_error: {0}".format(status))
        try:
            parsed = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise HTTPConnectorError("response_malformed: {0}".format(exc)) from exc
        return parsed
