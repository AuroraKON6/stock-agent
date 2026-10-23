"""R18: MCP thin client — JSON-RPC envelope facts only, transport injected.

The client speaks the minimal JSON-RPC surface MCP shares (initialize →
tools/list → tools/call) against an injected transport. The official SDK
adoption is NOT_RUN (dependency registration); when it lands it backs the
same transport interface. In-process fake servers drive every rule offline.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from .policy import ConnectorPolicy, PolicyViolation

MCP_PROTOCOL_VERSION = "2024-11-05"
CLIENT_INFO = {"name": "stock-agent-connector-gateway", "version": "1.0"}


class MCPToolError(RuntimeError):
    """reasons: handshake_failed, rpc_error, tool_not_configured,
    result_too_large, result_malformed, connection_lost_after_send,
    injection_is_data_only"""


class MemoryMCPTransport:
    """In-process fake server for offline verification. Implements the exact
    JSON-RPC envelope surface the client uses — no network, no subprocess."""

    def __init__(self, tools: Dict[str, Dict[str, Any]],
                 *, call_results: Optional[Dict[str, Any]] = None,
                 fail_calls_with: Optional[Exception] = None,
                 oversized_tool: str = ""):
        self.tools = tools                     # name -> {description, inputSchema}
        self.call_results = dict(call_results or {})
        self.fail_calls_with = fail_calls_with
        self.oversized_tool = oversized_tool
        self.calls: List[Dict[str, Any]] = []
        self.initialized = False
        self.sent_any_call = False             # for connection-lost semantics

    # -- server side of the wire -------------------------------------- #

    def post(self, request: Dict[str, Any]) -> Dict[str, Any]:
        method = str(request.get("method") or "")
        if method == "initialize":
            self.initialized = True
            return {"jsonrpc": "2.0", "id": request.get("id"),
                    "result": {"protocolVersion": MCP_PROTOCOL_VERSION,
                               "serverInfo": {"name": "fake-mcp"}}}
        if method == "tools/list":
            return {"jsonrpc": "2.0", "id": request.get("id"),
                    "result": {"tools": [
                        {"name": name, **spec} for name, spec in self.tools.items()]}}
        if method == "tools/call":
            self.sent_any_call = True
            self.calls.append(dict(request.get("params") or {}))
            if self.fail_calls_with is not None:
                raise self.fail_calls_with
            name = str((request.get("params") or {}).get("name") or "")
            if name == self.oversized_tool:
                return {"jsonrpc": "2.0", "id": request.get("id"),
                        "result": {"content": [
                            {"type": "text", "text": "x" * (8 * 1024 * 1024)}]}}
            result = self.call_results.get(name)
            if result is None:
                return {"jsonrpc": "2.0", "id": request.get("id"),
                        "error": {"code": -32602, "message": "unknown tool {0}".format(name)}}
            return {"jsonrpc": "2.0", "id": request.get("id"), "result": result}
        return {"jsonrpc": "2.0", "id": request.get("id"),
                "error": {"code": -32601, "message": "method not found"}}


class MCPClient:
    """Policy-bound client: every reply is filtered/checked against the
    manifest before it escapes this class."""

    def __init__(self, policy: ConnectorPolicy, transport: Any):
        self.policy = policy
        self.transport = transport
        self._request_id = 0
        self._handshake()

    # ------------------------------------------------------------------ #

    def _rpc(self, method: str, params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        self._request_id += 1
        request: Dict[str, Any] = {"jsonrpc": "2.0", "id": self._request_id,
                                   "method": method}
        if params is not None:
            request["params"] = params
        response = self.transport.post(request)
        if not isinstance(response, dict) or "error" in response:
            message = str((response or {}).get("error", {}).get("message")
                          if isinstance(response, dict) else response)
            raise MCPToolError("rpc_error: {0}".format(message))
        return response

    def _handshake(self) -> None:
        try:
            reply = self._rpc("initialize", {
                "protocolVersion": MCP_PROTOCOL_VERSION, "clientInfo": CLIENT_INFO})
        except MCPToolError as exc:
            raise MCPToolError("handshake_failed: {0}".format(exc)) from exc
        if str((reply.get("result") or {}).get("protocolVersion") or "") \
                != MCP_PROTOCOL_VERSION:
            raise MCPToolError("handshake_failed: protocol version mismatch")

    # ------------------------------------------------------------------ #

    def list_tools(self) -> List[Dict[str, Any]]:
        """A81: only whitelisted tools are visible — a server advertising
        extra tools (drift) yields them as INVISIBLE, never as new surface."""
        reply = self._rpc("tools/list")
        visible: List[Dict[str, Any]] = []
        for tool in (reply.get("result") or {}).get("tools") or []:
            name = str(tool.get("name") or "")
            if self.policy.tool_visible(name):
                visible.append({"name": name,
                                "description": str(tool.get("description") or ""),
                                "input_schema": tool.get("inputSchema") or {}})
        return visible

    def call_tool(self, tool: str, arguments: Optional[Dict[str, Any]] = None,
                  *, caller_headers: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        # A82: refuse caller credentials before anything is sent.
        self.policy.forbid_caller_tokens(caller_headers or {})
        self.policy.assert_callable(tool)
        try:
            reply = self._rpc("tools/call", {"name": tool,
                                             "arguments": dict(arguments or {})})
        except MCPToolError as exc:
            raise MCPToolError("rpc_error: {0}".format(exc)) from exc
        except Exception as exc:  # transport died mid-call
            if getattr(self.transport, "sent_any_call", False):
                # The call MAY have reached the server: never silently retry.
                raise MCPToolError(
                    "connection_lost_after_send: {0}; manual reconciliation".format(
                        type(exc).__name__)) from exc
            raise MCPToolError("connection_lost_before_send: {0}".format(
                type(exc).__name__)) from exc
        result = reply.get("result") or {}
        encoded = json.dumps(result, ensure_ascii=False, default=str)
        if len(encoded.encode("utf-8")) > self.policy.max_response_bytes:
            # A84: bounded refusal — the payload is dropped, never parsed.
            raise MCPToolError(
                "result_too_large: {0} > cap {1}".format(
                    len(encoded.encode("utf-8")), self.policy.max_response_bytes))
        content = result.get("content")
        if not isinstance(content, list):
            raise MCPToolError("result_malformed: content must be a list")
        return {"tool": tool, "content": content, "is_error": bool(result.get("isError")),
                # A85 note: text items are returned verbatim as DATA — nothing
                # here interprets instructions inside tool output.
                "content_is": "data_only_never_instructions"}
