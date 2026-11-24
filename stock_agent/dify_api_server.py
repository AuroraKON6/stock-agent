"""DF-02: minimal loopback HTTP binding for the Dify-facing API.

A deliberately thin transport: stdlib ``ThreadingHTTPServer`` (same pattern
as the task board), bound to 127.0.0.1, no framework. Bearer service tokens
authenticate; every route maps 1:1 onto a ``DifyGateway`` method so the
service layer stays the single place where authorization, idempotency and
state checks live. The HTTP layer only maps exceptions to status codes:

- dify_access.DifyAccessError(credential_unknown)  -> 401
- other dify_access.DifyAccessError                -> 403
- DifySubmissionError / task-store conflicts       -> 409
- ValueError / malformed JSON                      -> 400

Human-protected actions are NOT special-cased here: the gateway already
requires a principal verification no Dify credential can mint.

The isolated R16 listener can additionally accept its two native HTTP-node
forms and expose validated receipt fields as response headers.
"""
from __future__ import annotations

import json
import re
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, Tuple
from urllib.parse import parse_qsl

from . import dify_access
from .dify_api import DifySubmissionError
from .task_runtime.store import (
    ALLOWED_TRANSITIONS,
    TaskRuntimeError,
    TaskIdempotencyConflictError,
    TaskTransitionError,
    TaskVersionConflictError,
)

SERVER_VERSION = "1.0"
_MAX_BODY_BYTES = 1024 * 1024
_LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1"}
_REPLAY_CREATE = "/api/dify/v1/create-research"
_REPLAY_STATUS = "/api/dify/v1/research-status"

# path -> (gateway method name, required keyword arguments)
_ROUTES: Dict[str, Tuple[str, Tuple[str, ...]]] = {
    _REPLAY_CREATE: (
        "create_research",
        ("title", "case_id", "input_version", "research_question",
         "dify_run_id", "workflow_version")),
    _REPLAY_STATUS: ("research_status", ("task_id",)),
    "/api/dify/v1/fact-bundle": ("read_fact_bundle", ("case_id", "input_version")),
    "/api/dify/v1/submit-result": (
        "submit_candidate_result",
        ("task_id", "dify_run_id", "workflow_version", "bundle_id", "citations", "result")),
    "/api/dify/v1/adopt-result": (
        "adopt_candidate_result",
        ("task_id", "dispatch_id", "result_id")),
    "/api/dify/v1/submit-feedback": (
        "submit_feedback",
        ("task_id", "command_id", "expected_version", "decision")),
    "/api/dify/v1/cancel": ("request_cancel", ("task_id", "expected_version", "reason")),
}


def _is_loopback_host(host: str) -> bool:
    host = str(host or "").strip().lower()
    if host in _LOOPBACK_HOSTS:
        return True
    # tolerate "127.0.0.1:8770" style Host headers
    return host.rsplit(":", 1)[0] in _LOOPBACK_HOSTS if ":" in host else False


def _replay_form_body(path: str, raw: bytes) -> dict:
    pairs = parse_qsl(raw.decode("ascii"), keep_blank_values=True, strict_parsing=True,
                      max_num_fields=8, encoding="utf-8", errors="strict")
    body = dict(pairs)
    if len(pairs) != len(body):
        raise ValueError("duplicate form fields")
    if path == _REPLAY_CREATE:
        if body.keys() != set(_ROUTES[path][1]) | {"business_intent_id", "execution_mode"}:
            raise ValueError("unexpected replay creation fields")
        version = body["input_version"]
        if not re.fullmatch(r"[1-9][0-9]{0,18}", version) or int(version) >= 2**63:
            raise ValueError("input_version must be a positive integer")
        return {**body, "input_version": int(version)}
    if body.keys() != {"task_id", "status_only"} or body["status_only"] != "true":
        raise ValueError("expected task_id and status_only=true")
    return {**body, "status_only": True}


def _replay_receipt_headers(path: str, request: dict, payload: dict) -> dict:
    task_id = payload.get("task_id", "")
    if not isinstance(task_id, str) or not re.fullmatch(r"task_[0-9a-f]{16}", task_id):
        raise ValueError("invalid task ID in replay receipt")
    if path == _REPLAY_CREATE:
        platform_run = payload.get("platform_run")
        if (payload.get("case_id") != request["case_id"]
                or not isinstance(platform_run, dict)
                or platform_run.get("run_id") != request["dify_run_id"]):
            raise ValueError("creation receipt does not match the requested case and Dify run")
        # Graphon's nested template selectors accept underscores in keys.
        return {"R16_Task_Id": task_id, "R16_Case_Path": "/?task=" + task_id}
    status = payload.get("status")
    if task_id != request["task_id"] or not isinstance(status, str) or status not in ALLOWED_TRANSITIONS:
        raise ValueError("invalid replay task status receipt")
    return {"R16_Status": status}


def create_dify_api_server(gateway: Any, host: str = "127.0.0.1", port: int = 8770, *,
                           replay_forms: bool = False) -> ThreadingHTTPServer:
    class _DifyApiHandler(BaseHTTPRequestHandler):
        server_version = "stock-agent-dify-api/{0}".format(SERVER_VERSION)

        def log_message(self, *args: Any) -> None:  # keep stdout clean in tests
            return

        # ---------------------------------------------------------------- #
        # helpers                                                          #
        # ---------------------------------------------------------------- #

        def _reject(self, status: int, reason: str, detail: str = "") -> None:
            body = json.dumps({"ok": False, "error": reason, "detail": detail},
                              ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _respond(self, payload: Dict[str, Any], headers: dict | None = None) -> None:
            body = json.dumps({"ok": True, "data": payload},
                              ensure_ascii=False, default=str).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            for name, value in (headers or {}).items():
                self.send_header(name, value)
            self.end_headers()
            self.wfile.write(body)

        def _loopback_ok(self) -> bool:
            host = self.headers.get("Host") or ""
            origin = self.headers.get("Origin")
            if not _is_loopback_host(host):
                return False
            if origin and not _is_loopback_host(
                    origin.replace("http://", "").replace("https://", "")):
                return False
            return True

        def _bearer_token(self) -> str:
            header = str(self.headers.get("Authorization") or "")
            if header.lower().startswith("bearer "):
                return header[7:].strip()
            return ""

        # ---------------------------------------------------------------- #
        # routes                                                           #
        # ---------------------------------------------------------------- #

        def do_GET(self) -> None:  # noqa: N802 (http.server API)
            if not self._loopback_ok():
                self._reject(403, "non_loopback_request")
                return
            if self.path == "/health":
                self._respond({"status": "ok", "service": "dify-api",
                               "version": SERVER_VERSION})
                return
            # R09 probes: liveness is process-alive only; readiness additionally
            # verifies the task store opens READ-ONLY. Zero collection, zero
            # model calls, zero business writes — a probe must never mutate
            # anything (A45).
            if self.path == "/livez":
                self._respond({"status": "ok", "probe": "livez",
                               "version": SERVER_VERSION})
                return
            if self.path == "/readyz":
                from .task_runtime.store import TaskStore

                ready = {"status": "ok", "probe": "readyz",
                         "store_read_only_open": True}
                try:
                    db_path = Path(getattr(gateway, "_db_path"))
                    probe = TaskStore(db_path, read_only=True)
                    probe.close()
                except Exception as exc:  # noqa: BLE001 — readiness reports, never crashes
                    ready = {"status": "unavailable", "probe": "readyz",
                             "store_read_only_open": False,
                             "reason": "{0}: {1}".format(type(exc).__name__, str(exc)[:200])}
                    self._respond(ready)
                    return
                self._respond(ready)
                return
            self._reject(404, "not_found", self.path)

        def do_POST(self) -> None:  # noqa: N802 (http.server API)
            if not self._loopback_ok():
                self._reject(403, "non_loopback_request")
                return
            route = _ROUTES.get(self.path)
            if route is None:
                self._reject(404, "not_found", self.path)
                return
            method_name, required = route
            try:
                length = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                length = 0
            if length <= 0 or length > _MAX_BODY_BYTES:
                self._reject(400, "invalid_body", "missing or oversized body")
                return
            raw = self.rfile.read(length)
            use_replay_form = (
                replay_forms and self.path in {_REPLAY_CREATE, _REPLAY_STATUS}
                and self.headers.get_content_type() == "application/x-www-form-urlencoded"
            )
            try:
                body = _replay_form_body(self.path, raw) if use_replay_form else json.loads(raw.decode("utf-8"))
            except ValueError:
                self._reject(400, "invalid_replay_form" if use_replay_form else "invalid_json")
                return
            if not isinstance(body, dict):
                self._reject(400, "invalid_body", "body must be a JSON object")
                return
            missing = [field for field in required if field not in body]
            if missing:
                self._reject(400, "missing_fields", ", ".join(missing))
                return
            token = self._bearer_token()
            method = getattr(gateway, method_name)
            try:
                payload = method(token, **body)
            except dify_access.DifyAccessError as exc:
                status = 401 if exc.reason == "credential_unknown" else 403
                self._reject(status, exc.reason, exc.detail)
                return
            except DifySubmissionError as exc:
                self._reject(409, exc.reason, exc.detail)
                return
            except (TaskVersionConflictError, TaskIdempotencyConflictError, TaskTransitionError) as exc:
                self._reject(409, type(exc).__name__, str(exc))
                return
            except (ValueError, TaskRuntimeError) as exc:
                self._reject(400, "invalid_request", str(exc))
                return
            try:
                headers = _replay_receipt_headers(self.path, body, payload) if use_replay_form else None
            except ValueError:
                self._reject(500, "invalid_replay_receipt")
                return
            self._respond(payload, headers)

    class _DifyApiServer(ThreadingHTTPServer):
        daemon_threads = True
        allow_reuse_address = True

    return _DifyApiServer((host, port), _DifyApiHandler)


def serve_dify_api(gateway: Any, host: str = "127.0.0.1", port: int = 8770, *,
                   replay_forms: bool = False) -> None:
    server = create_dify_api_server(gateway, host=host, port=port, replay_forms=replay_forms)
    try:
        server.serve_forever()
    finally:
        server.server_close()
