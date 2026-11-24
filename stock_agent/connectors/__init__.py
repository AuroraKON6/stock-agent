"""R18: outbound Connector Gateway — policy, registry, MCP/HTTP thin clients.

Offline slice: the governance layer plus protocol-thin clients verified
against IN-PROCESS fake servers (no network, no subprocess — the isolation
guard stays satisfied). The official MCP SDK adoption is NOT_RUN: it is a new
runtime dependency that needs its own source/license registration and a
locked install; until then this module implements only the JSON-RPC envelope
facts both share, and the client code is written against a transport
interface the SDK can back later.

Stop conditions honored by construction (taskbook R18):
- tools not configured in the manifest are INVISIBLE and uncallable (A81);
- a revoked connector stops visible and callable on the SAME live instance
  via ConnectorRegistry (A81);
- caller credentials are refused at the header AND tool-argument boundary;
  each connector authenticates only with its own trusted-config credential,
  bound to one connector id, audience and destination (A82);
- exact-origin HTTPS with DNS pinning: the request URL is rebuilt from the
  parsed origin and a validated path, addresses are handed to the transport,
  private/loopback resolutions and any redirect are refused (A83);
- responses are size-capped, must satisfy the MCP content-item / HTTP
  top-level object contract, and malformed bodies fail bounded (A84);
- a possibly-delivered HTTP failure is reported as after_send or
  delivery_unknown with a reconciliation hint, never as before_send, and is
  never retried; MCP disconnects do the same (A83/A85);
- tool-returned content is DATA — instruction-like text never expands the
  tool set or changes policy (A85).
"""
from .policy import (
    CALLER_CREDENTIAL_KEYS,
    ConnectorCredential,
    ConnectorPolicy,
    ConnectorRegistry,
    PolicyViolation,
    bind_connector_credential,
    load_connector_registry,
    resolve_endpoint_addresses,
    support_matrix,
)
from .mcp_client import MCPClient, MCPToolError, MemoryMCPTransport
from .http_client import (
    HTTPConnector,
    HTTPConnectorError,
    HTTPTransportError,
    approved_request_url,
)

__all__ = [
    "CALLER_CREDENTIAL_KEYS",
    "ConnectorCredential",
    "ConnectorPolicy",
    "ConnectorRegistry",
    "PolicyViolation",
    "bind_connector_credential",
    "load_connector_registry",
    "resolve_endpoint_addresses",
    "support_matrix",
    "MCPClient",
    "MCPToolError",
    "MemoryMCPTransport",
    "HTTPConnector",
    "HTTPConnectorError",
    "HTTPTransportError",
    "approved_request_url",
]
