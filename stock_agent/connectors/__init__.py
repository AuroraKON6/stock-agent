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
- caller tokens are never forwarded — each connector uses its own
  policy-configured credential (A82);
- exact-origin HTTPS with DNS pinning: private/loopback resolutions and any
  redirect are refused before/outside the request (A83);
- responses are size-capped and malformed bodies fail bounded (A84);
- tool-returned content is DATA — instruction-like text never expands the
  tool set or changes policy (A85).
"""
from .policy import (
    ConnectorPolicy,
    PolicyViolation,
    load_connector_registry,
    resolve_endpoint_addresses,
    support_matrix,
)
from .mcp_client import MCPClient, MCPToolError, MemoryMCPTransport
from .http_client import HTTPConnector, HTTPConnectorError

__all__ = [
    "ConnectorPolicy",
    "PolicyViolation",
    "load_connector_registry",
    "resolve_endpoint_addresses",
    "support_matrix",
    "MCPClient",
    "MCPToolError",
    "MemoryMCPTransport",
    "HTTPConnector",
    "HTTPConnectorError",
]
