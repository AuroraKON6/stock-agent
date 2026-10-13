"""Kimi gateway transport: fixed destinations, explicit credentials, typed outcomes.

F02/F06 remediation. Unlike the external kimi_gateway.py, this transport never
discovers keys (no env fallback chains, no desktop share scanning), never
accepts a base URL from the environment, and only ever talks to the exact
reviewed origin configured by the host. Credentials arrive as a provider
callable resolved by the configuration layer; their values are never placed in
error text, logs, or the request records. Importing this module has no side
effects. The urllib implementation reads at most max_response_bytes and never
follows redirects: any 3xx is a destination violation, not a hop.
"""
from __future__ import annotations

import json
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Protocol

DEFAULT_ORIGIN = "https://agent-gw.kimi.com"
DEFAULT_TOOLS_PATH = "/coding/v1/tools"
# Conservative PoC engineering caps (plan K02): not entitlements.
DEFAULT_TIMEOUT_SECONDS = 60.0
MAX_RESPONSE_BYTES = 5 * 1024 * 1024

# Outcome codes are separate namespaces for business, protocol, auth and
# transport-unknown failures (F05/F06): never collapse them into one "failed".
OUTCOME_OK = "ok"
OUTCOME_NOT_CONFIGURED = "not_configured"
OUTCOME_CREDENTIAL_MISSING = "credential_missing"
OUTCOME_DESTINATION_DENIED = "destination_denied"
OUTCOME_AUTH_REJECTED = "auth_rejected"
OUTCOME_PERMISSION_DENIED = "permission_denied"
OUTCOME_RATE_LIMITED = "rate_limited"
OUTCOME_RESPONSE_TOO_LARGE = "response_too_large"
OUTCOME_RESPONSE_INVALID = "response_invalid"
OUTCOME_TRANSPORT_ERROR = "transport_error"
OUTCOME_OUTCOME_UNKNOWN = "outcome_unknown"
OUTCOME_BUDGET_EXHAUSTED = "budget_exhausted"

_RETRYABLE_NEVER = frozenset({
    OUTCOME_NOT_CONFIGURED, OUTCOME_CREDENTIAL_MISSING, OUTCOME_DESTINATION_DENIED,
    OUTCOME_AUTH_REJECTED, OUTCOME_PERMISSION_DENIED, OUTCOME_BUDGET_EXHAUSTED,
    OUTCOME_RESPONSE_TOO_LARGE, OUTCOME_RESPONSE_INVALID,
})


@dataclass
class DestinationPolicy:
    """Exact-origin allowlist. scheme/host/port must match to the letter; the
    tools path is fixed, so a credential header can never leave this origin
    via a redirect (redirects are rejected outright)."""
    origin: str = DEFAULT_ORIGIN
    path: str = DEFAULT_TOOLS_PATH

    def url(self) -> str:
        return self.origin.rstrip("/") + self.path

    def matches(self, url: str) -> bool:
        return url.rstrip("/") == self.url().rstrip("/")


@dataclass
class TransportOutcome:
    outcome: str
    http_status: Optional[int] = None
    body: Optional[bytes] = None
    # Secret-free diagnostics only; never include headers or key material.
    detail: str = ""
    retry_after_seconds: Optional[float] = None

    @property
    def ok(self) -> bool:
        return self.outcome == OUTCOME_OK

    @property
    def retryable(self) -> bool:
        if self.outcome in _RETRYABLE_NEVER:
            return False
        if self.outcome == OUTCOME_RATE_LIMITED:
            return self.retry_after_seconds is not None
        return self.outcome in {OUTCOME_TRANSPORT_ERROR}


class GatewayTransport(Protocol):
    def post_tools(self, payload: bytes, headers: Dict[str, str],
                   timeout: float) -> TransportOutcome: ...


@dataclass
class UrllibTransport:
    """Production transport over stdlib urllib. HTTPS-only hard check; any
    redirect (3xx) is destination denial; response body is bounded."""
    policy: DestinationPolicy = field(default_factory=DestinationPolicy)
    max_response_bytes: int = MAX_RESPONSE_BYTES
    opener: Optional[Any] = None

    def post_tools(self, payload: bytes, headers: Dict[str, str],
                   timeout: float) -> TransportOutcome:
        url = self.policy.url()
        if not url.startswith("https://"):
            return TransportOutcome(OUTCOME_DESTINATION_DENIED,
                                    detail="non-HTTPS destination refused")
        request = urllib.request.Request(url, data=payload, headers=headers, method="POST")
        opener = self.opener or urllib.request.build_opener(_NoRedirectHandler)
        try:
            with opener.open(request, timeout=timeout) as response:
                status = getattr(response, "status", None) or response.getcode()
                if status is not None and 300 <= int(status) < 400:
                    return TransportOutcome(OUTCOME_DESTINATION_DENIED, http_status=int(status),
                                            detail="redirect refused without following")
                body = response.read(self.max_response_bytes + 1)
        except urllib.error.HTTPError as exc:
            return _http_error_outcome(exc, self.max_response_bytes)
        except urllib.error.URLError as exc:
            reason = getattr(exc, "reason", None)
            # A URLError after the request was written leaves the result
            # unknown only when bytes may already have reached the server;
            # connection-setup failures are plain transport errors.
            if isinstance(reason, (ConnectionResetError, TimeoutError)):
                return TransportOutcome(OUTCOME_OUTCOME_UNKNOWN,
                                        detail="connection failed after send; result unknown")
            return TransportOutcome(OUTCOME_TRANSPORT_ERROR, detail=_safe_reason(reason))
        except (ConnectionResetError, TimeoutError):
            return TransportOutcome(OUTCOME_OUTCOME_UNKNOWN,
                                    detail="connection failed after send; result unknown")
        except OSError as exc:
            return TransportOutcome(OUTCOME_TRANSPORT_ERROR, detail=_safe_reason(exc))
        if len(body) > self.max_response_bytes:
            return TransportOutcome(OUTCOME_RESPONSE_TOO_LARGE,
                                    detail="response exceeded {0} bytes".format(self.max_response_bytes))
        return TransportOutcome(OUTCOME_OK, http_status=int(status or 0), body=body)


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # type: ignore[override]
        return None


def _http_error_outcome(exc: urllib.error.HTTPError, cap: int) -> TransportOutcome:
    try:
        exc.read(cap + 1)
    except Exception:
        pass
    status = int(exc.code)
    if status in (401,):
        return TransportOutcome(OUTCOME_AUTH_REJECTED, http_status=status,
                                detail="gateway rejected credentials")
    if status in (403,):
        return TransportOutcome(OUTCOME_PERMISSION_DENIED, http_status=status,
                                detail="gateway refused access for this account/route")
    if status == 429:
        retry_after = _parse_retry_after(exc.headers.get("Retry-After") if exc.headers else None)
        return TransportOutcome(OUTCOME_RATE_LIMITED, http_status=status,
                                detail="rate limited", retry_after_seconds=retry_after)
    if 500 <= status < 600:
        # Server-side failure after acceptance: the call may have consumed
        # quota, so the cost status stays unknown rather than "free failure".
        return TransportOutcome(OUTCOME_OUTCOME_UNKNOWN, http_status=status,
                                detail="5xx after send; result unknown")
    return TransportOutcome(OUTCOME_TRANSPORT_ERROR, http_status=status,
                            detail="unexpected HTTP status {0}".format(status))


def _parse_retry_after(value: Optional[str]) -> Optional[float]:
    if not value:
        return None
    try:
        seconds = float(value)
    except ValueError:
        return None
    return seconds if seconds >= 0 else None


def _safe_reason(reason: Any) -> str:
    text = str(reason or "network error")
    return text[:160]


def authorization_header(api_key: str) -> Dict[str, str]:
    return {"Content-Type": "application/json", "Authorization": "Bearer " + api_key}


def build_tools_payload(method: str, params: Dict[str, Any]) -> bytes:
    return json.dumps({"method": method, "params": params},
                      ensure_ascii=False).encode("utf-8")


def call_data_source_params(data_source_name: str, api_name: str,
                            params: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "data_source_name": data_source_name,
        "api_name": api_name,
        "params": params,
    }


def resolve_credential(provider: Optional[Callable[[], str]]) -> str:
    """Explicit provider only. Missing or placeholder values block with
    credential_missing; there is no fallback chain to another account (F02)."""
    if provider is None:
        raise CredentialMissing("no credential provider configured")
    value = provider()
    if not isinstance(value, str) or len(value.strip()) < 16 or "not_provided" in value:
        raise CredentialMissing("credential provider returned no usable value")
    return value.strip()


class CredentialMissing(RuntimeError):
    pass
