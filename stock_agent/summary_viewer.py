"""Account-issued, short-lived local viewing capability. Never a review writer."""
from __future__ import annotations

import hashlib
import hmac
import json
import secrets
import sqlite3
import time
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer

from .config import load_config, project_path, project_path_override
from .local_identity import authenticate_local_actor, local_actor_binding
from .summary_review import review_state
from .task_runtime import TaskRuntimeError, TaskStore

MAX_LIFETIME_SECONDS = 300
ASSURANCE = "local_account_issued_bearer_access_not_browser_identity_or_human_presence"
VALID_REVIEW_STATES = {"pending", "supported", "rejected", "changes_requested", "revoked"}


class SummaryViewerError(ValueError):
    pass


def _sha(data):
    return hashlib.sha256(data).hexdigest()


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _config_digest(path):
    with path.open("rb") as handle:
        raw = handle.read(1024 * 1024 + 1)
    if len(raw) > 1024 * 1024:
        raise SummaryViewerError("review config exceeds size limit")
    return _sha(raw)


class SummaryViewAccess:
    """Minted by the authenticated CLI, not by an HTTP-supplied actor or SID."""

    def __init__(self, config_path, task_id, actor, draft_id, expected_version, *, lifetime_seconds=300):
        if type(lifetime_seconds) is not int or not 1 <= lifetime_seconds <= MAX_LIFETIME_SECONDS:
            raise SummaryViewerError("view lifetime must be 1..300 seconds")
        if not isinstance(draft_id, str) or not draft_id or type(expected_version) is not int:
            raise SummaryViewerError("an explicit draft_id and integer task version are required")
        self.root = project_path(".").resolve()
        self.config_path = project_path(config_path).resolve()
        self.config_sha = _config_digest(self.config_path)
        self.config = load_config(str(self.config_path))
        if _config_digest(self.config_path) != self.config_sha:
            raise SummaryViewerError("config changed during authentication")
        self.db = project_path(str((self.config.get("task_runtime") or {}).get("db_path") or "data/task_runtime/tasks.sqlite3"))
        if not self.db.is_file():
            raise SummaryViewerError("task database does not exist; no files were initialized")
        self.task_id, self.draft_id = task_id, draft_id
        self.expected_version = expected_version
        store = TaskStore(self.db, read_only=True)
        try:
            task = store.get_task(task_id)
            self.binding = authenticate_local_actor(self.config, actor, task["input_payload"].get("symbols"), review=True)
        finally:
            store.close()
        self.closed = False
        self.deadline = time.monotonic() + lifetime_seconds
        self.expires_at = (datetime.now(timezone.utc) + timedelta(seconds=lifetime_seconds)).isoformat()
        self._snapshot_sha = ""
        initial = self.read_detail()
        self._snapshot_sha = _sha(_json(initial))
        self._bootstrap = secrets.token_urlsafe(32)
        self._session_sha = ""

    def remaining(self):
        return max(0.0, self.deadline - time.monotonic())

    def revoke(self):
        self.closed = True
        self._bootstrap = ""
        self._session_sha = ""

    def _active(self):
        if self.closed or self.remaining() <= 0:
            self.revoke()
            raise SummaryViewerError("view access ended")

    def exchange(self, bootstrap):
        self._active()
        if not isinstance(bootstrap, str) or not self._bootstrap or not hmac.compare_digest(_sha(bootstrap.encode()), _sha(self._bootstrap.encode())):
            raise SummaryViewerError("view access denied")
        self.read_detail()
        self._bootstrap = ""
        token = secrets.token_urlsafe(32)
        self._session_sha = _sha(token.encode())
        return {"session_token": token, "remaining_seconds": self.remaining()}

    def authorize(self, token):
        self._active()
        if not isinstance(token, str) or not token or not self._session_sha or not hmac.compare_digest(_sha(token.encode()), self._session_sha):
            raise SummaryViewerError("view access denied")

    def read_detail(self):
        self._active()
        try:
            with project_path_override(lambda value: self.root / value):
                return self._read_detail()
        except (OSError, ValueError, TypeError, KeyError, TaskRuntimeError, sqlite3.Error) as exc:
            self.revoke()
            raise SummaryViewerError("view is no longer valid; reopen from the authenticated CLI") from exc

    def _read_detail(self):
        if _config_digest(self.config_path) != self.config_sha:
            raise SummaryViewerError("config changed")
        store = TaskStore(self.db, read_only=True)
        try:
            # One SQLite read snapshot; never migrate or mutate the task/outbox tables.
            store._conn.execute("BEGIN")
            task = store.get_task(self.task_id)
            if task["version"] != self.expected_version:
                raise SummaryViewerError("task version changed")
            binding = local_actor_binding(self.config, self.binding["actor_id"], task["input_payload"].get("symbols"), review=True)
            if binding != self.binding:
                raise SummaryViewerError("account policy changed")
            state = review_state(store, self.task_id, config=self.config, draft_id=self.draft_id, include_local_content=True)
            if state["status"] not in VALID_REVIEW_STATES or not state.get("local_excerpts"):
                raise SummaryViewerError("draft or evidence is not currently valid")
            payload = state["local_draft"]
            detail = {
                "access": {"actor_id": binding["actor_id"], "assurance": ASSURANCE,
                           "expires_at": self.expires_at, "read_only": True},
                "task": {key: task[key] for key in ("task_id", "title", "role_id", "status", "version", "input_version")},
                "review": {key: state[key] for key in ("draft_id", "summary_id", "revision", "status", "execution_mode",
                           "model_called", "scope", "external_eligible", "institutional_approval", "expires_at")},
                "draft": {"text": payload["draft"]["text"], "content_kind": payload["draft"]["content_kind"],
                          "author": payload["author"]["actor_id"], "context_sha256": payload["context_sha256"],
                          "task_prompt_version": payload["task_prompt_version"], "model_prompt_version": payload["model_prompt_version"]},
                "excerpts": state["local_excerpts"],
                "history": [{"decision": item["decision"], "actor": item["reviewer"]["actor_id"],
                             "notes": item["notes"], "created_at": item["created_at"],
                             "summary_id": item["summary_id"]} for item in state["review_history"]],
            }
            if local_actor_binding(self.config, binding["actor_id"], task["input_payload"].get("symbols"), review=True) != binding:
                raise SummaryViewerError("policy changed during read")
            if _config_digest(self.config_path) != self.config_sha:
                raise SummaryViewerError("config changed during read")
            if self._snapshot_sha and _sha(_json(detail)) != self._snapshot_sha:
                raise SummaryViewerError("the bound view changed")
            self._active()
            return detail
        finally:
            store.close()


class _ReviewServer(HTTPServer):
    allow_reuse_address = False

    def get_request(self):
        connection, address = super().get_request()
        connection.settimeout(2)
        return connection, address

    def server_close(self):
        self.access.revoke()
        super().server_close()


def create_summary_view_server(config_path, task_id, actor, draft_id, expected_version, *, lifetime_seconds=300):
    # Authenticate and verify the exact draft BEFORE binding even a loopback socket.
    access = SummaryViewAccess(config_path, task_id, actor, draft_id, expected_version, lifetime_seconds=lifetime_seconds)
    server = _ReviewServer(("127.0.0.1", 0), _ReviewHandler, bind_and_activate=False)
    server.access = access
    try:
        server.server_bind()
        server.server_activate()
    except BaseException:
        server.server_close()
        raise
    server.timeout = 0.25
    server.origin = "http://127.0.0.1:" + str(server.server_port)
    server.nonce = secrets.token_urlsafe(24)
    return server


def serve_summary_viewer(config_path, task_id, actor, draft_id, expected_version):
    import webbrowser

    server = create_summary_view_server(config_path, task_id, actor, draft_id, expected_version)
    try:
        # The single-use fragment is not printed, logged, stored in project files,
        # or placed in an HTTP request URL. The page removes it before exchange.
        if not webbrowser.open(server.origin + "/#" + server.access._bootstrap, new=2):
            raise SummaryViewerError("browser could not open; no access link was printed")
        print("Local summary viewer opened; read-only, at most 5 minutes. Ctrl+C closes access.")
        while not server.access.closed and server.access.remaining() > 0:
            server.handle_request()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


class _ReviewHandler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        return  # Request headers/bodies contain ephemeral capabilities; never log them.

    def _send(self, code, payload, *, html=False):
        body = payload.encode("utf-8") if html else _json(payload)
        self.send_response(code)
        self.send_header("Content-Type", "text/html; charset=utf-8" if html else "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Content-Security-Policy", "default-src 'none'; script-src 'nonce-" + self.server.nonce +
                         "'; style-src 'nonce-" + self.server.nonce + "'; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'")
        self.end_headers()
        self.wfile.write(body)

    def _origin_ok(self, *, post=False):
        expected_host = self.server.origin.removeprefix("http://")
        hosts = self.headers.get_all("Host") or []
        origins = self.headers.get_all("Origin") or []
        if hosts != [expected_host] or self.client_address[0] != "127.0.0.1":
            return False
        if origins and origins != [self.server.origin]:
            return False
        if post and (origins != [self.server.origin] or self.headers.get("X-Review-Request") != "1"):
            return False
        site = self.headers.get("Sec-Fetch-Site", "same-origin")
        return site == "same-origin" or (not post and self.path == "/" and site == "none")

    def _token(self):
        values = self.headers.get_all("Authorization") or []
        if len(values) != 1 or not values[0].startswith("Bearer "):
            return ""
        return values[0][7:]

    def do_GET(self):
        if not self._origin_ok():
            self._send(403, {"error": "origin denied"})
            return
        if self.path == "/":
            self._send(200, _PAGE.replace("__NONCE__", self.server.nonce), html=True)
        elif self.path == "/api/detail":
            try:
                self.server.access.authorize(self._token())
                detail = self.server.access.read_detail()
            except SummaryViewerError:
                self._send(403, {"error": "view access unavailable"})
                return
            self._send(200, detail)
        else:
            self._send(404, {"error": "not found"})

    def _drain_request_body(self) -> bool:
        """Consume the declared request body so the connection can be reused
        or closed without Windows RST-on-unread-data aborting the client's
        read of our rejection. Returns False when draining is impossible."""
        sizes = self.headers.get_all("Content-Length") or []
        if len(sizes) != 1 or not sizes[0].isascii() or not sizes[0].isdigit():
            return False
        remaining = int(sizes[0])
        if remaining < 0 or remaining > 64 * 1024:
            return False
        while remaining > 0:
            chunk = self.rfile.read(min(remaining, 4096))
            if not chunk:
                return False
            remaining -= len(chunk)
        return True

    def do_POST(self):
        if not self._origin_ok(post=True):
            # Drain before rejecting: closing (or reusing) a connection with
            # an unread body makes Windows reset the socket, racing away the
            # 403 the client is about to read.
            if not self._drain_request_body():
                self.close_connection = True
            self._send(403, {"error": "origin denied"})
            return
        if self.path not in {"/session", "/close"}:
            if not self._drain_request_body():
                self.close_connection = True
            self._send(404, {"error": "not found"})
            return
        try:
            sizes = self.headers.get_all("Content-Length") or []
            if len(sizes) != 1 or not sizes[0].isascii() or not sizes[0].isdigit() or not 0 < int(sizes[0]) <= 1024:
                raise ValueError("invalid size")
            if self.headers.get("Transfer-Encoding") or self.headers.get("Content-Type") != "application/json":
                raise ValueError("invalid framing")
            body_text = self.rfile.read(int(sizes[0])).decode("utf-8")
        except (ValueError, UnicodeError):
            # Framing failed BEFORE the body was read: drain the declared
            # bytes so the connection can close or be reused without an
            # unread-data RST racing away this 400.
            if not self._drain_request_body():
                self.close_connection = True
            self._send(400, {"error": "invalid request"})
            return
        try:
            body = json.loads(body_text)
            if not isinstance(body, dict) or set(body) != ({"bootstrap"} if self.path == "/session" else set()):
                raise ValueError("unexpected body")
        except ValueError:
            # The declared body was fully consumed above; the connection is
            # byte-aligned again, so a normal keep-alive response is safe.
            self._send(400, {"error": "invalid request"})
            return
        try:
            if self.path == "/session":
                self._send(200, self.server.access.exchange(body["bootstrap"]))
            else:
                self.server.access.authorize(self._token())
                self.server.access.revoke()
                self._send(200, {"status": "closed"})
        except SummaryViewerError:
            self._send(403, {"error": "view access unavailable"})


_PAGE = """<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>Local Summary Review</title>
<style nonce="__NONCE__">
*{box-sizing:border-box}body{margin:0;background:#171717;color:#ededed;font:14px/1.5 system-ui,sans-serif;letter-spacing:0}
header,main{max-width:1120px;margin:auto;padding:20px 24px}header{display:flex;align-items:center;justify-content:space-between;gap:16px;border-bottom:1px solid #484848}
h1{font-size:20px;margin:0}h2{font-size:16px;margin:0 0 12px}h3{font-size:14px;margin:0 0 8px}
p{margin:8px 0}button{font:inherit;background:#303030;color:#fff;border:1px solid #737373;border-radius:4px;padding:6px 12px;cursor:pointer}
button:focus-visible{outline:2px solid #8be0bc;outline-offset:3px}button:disabled{opacity:.4;cursor:default}
#state{color:#ffd08a;margin:0}#state[data-state=active]{color:#8be0bc}#state[data-state=locked]{color:#ffaaaa}
.meta,dt,.source-meta{color:#bcbcbc;font-size:12px}.context{border-bottom:1px solid #484848;padding-bottom:18px;margin-bottom:22px}
.grid{display:grid;grid-template-columns:minmax(0,1fr) minmax(0,1fr);gap:32px}section{min-width:0}
dl{margin:12px 0;display:grid;grid-template-columns:125px minmax(0,1fr);gap:6px 12px}dd{margin:0}
pre{font:inherit;white-space:pre-wrap;margin:10px 0;overflow-wrap:anywhere}dd,p,h3,.source-meta{overflow-wrap:anywhere}
article{padding:14px 0;border-top:1px solid #484848}.source-text{border-left:3px solid #8be0bc;padding-left:12px}
.history{margin-top:28px;padding-top:20px;border-top:1px solid #484848}[hidden]{display:none!important}
@media(max-width:680px){header,main{padding:16px}.grid{grid-template-columns:minmax(0,1fr);gap:24px}dl{grid-template-columns:98px minmax(0,1fr)}h1{font-size:18px}}
</style></head><body>
<header><div><h1>Local Summary Review</h1><p class="meta">Read only / No institutional approval</p></div><button id="close" disabled>Close access</button></header>
<main><p id="state" role="status" aria-live="polite">Verifying access</p>
<div id="content" hidden><section class="context"><h2 id="title"></h2><p id="provenance"></p><dl id="metadata"></dl></section>
<div class="grid"><section><h2>Research summary</h2><p id="kind" class="meta"></p><pre id="summary"></pre></section>
<section><h2>Retained source excerpts</h2><div id="excerpts"></div></section></div>
<section class="history"><h2>Review history</h2><div id="history"></div></section></div></main>
<script nonce="__NONCE__">
(function(){
  'use strict';
  let bootstrap = location.hash.slice(1);
  history.replaceState(null, '', '/');
  let token = '', stopped = false, expiryTimer, pollTimer;
  const state = document.getElementById('state'), content = document.getElementById('content'), close = document.getElementById('close');
  function lock(message){
    stopped = true; token = ''; bootstrap = ''; clearTimeout(expiryTimer); clearTimeout(pollTimer);
    content.replaceChildren(); content.hidden = true; close.disabled = true;
    state.textContent = message; state.dataset.state = 'locked';
  }
  async function request(path, body){
    const controller = new AbortController(), timeout = setTimeout(() => controller.abort(), 4000);
    const headers = {'X-Review-Request':'1'};
    if(token) headers.Authorization = 'Bearer ' + token;
    if(body !== undefined) headers['Content-Type'] = 'application/json';
    try{
      const response = await fetch(path, {method:body === undefined ? 'GET':'POST', headers,
        body:body === undefined ? undefined : JSON.stringify(body), cache:'no-store', credentials:'omit', signal:controller.signal});
      if(!response.ok) throw new Error('access unavailable');
      return await response.json();
    }finally{clearTimeout(timeout);}
  }
  function node(tag, text, cls){const element = document.createElement(tag); element.textContent = String(text == null ? '' : text); if(cls) element.className = cls; return element;}
  function render(detail){
    if(stopped) return;
    document.getElementById('title').textContent = detail.task.title;
    document.getElementById('provenance').textContent = detail.review.execution_mode.toUpperCase() + ' / Model called: ' + detail.review.model_called + ' / Review: ' + detail.review.status;
    const metadata = document.getElementById('metadata'); metadata.replaceChildren();
    [['Task',detail.task.task_id],['Task version',detail.task.version],['Draft',detail.review.draft_id],
      ['Summary ID',detail.review.summary_id],['Role',detail.task.role_id],['Author',detail.draft.author],
      ['Access holder',detail.access.actor_id],['Access expires',detail.access.expires_at],
      ['Context SHA-256',detail.draft.context_sha256]].forEach(pair => metadata.append(node('dt',pair[0]),node('dd',pair[1])));
    document.getElementById('kind').textContent = detail.draft.content_kind;
    document.getElementById('summary').textContent = detail.draft.text;
    const excerpts = document.getElementById('excerpts'); excerpts.replaceChildren();
    detail.excerpts.forEach(excerpt => {
      const article = node('article',''); article.append(node('h3',excerpt.document_title),node('pre',excerpt.text,'source-text'),
        node('p',excerpt.source_url,'source-meta'),node('p','Lines ' + excerpt.line_start + '-' + excerpt.line_end + ' / [' + excerpt.start + ', ' + excerpt.end + ')','source-meta'),
        node('p','Document: ' + excerpt.document_id,'source-meta'),node('p','SHA-256: ' + excerpt.document_sha256,'source-meta'),node('p','Excerpt: ' + excerpt.excerpt_sha256,'source-meta'));
      excerpts.append(article);
    });
    const historyList = document.getElementById('history'); historyList.replaceChildren();
    if(!detail.history.length) historyList.append(node('p','No decisions recorded.','meta'));
    detail.history.forEach(item => {const article = node('article',''); article.append(node('h3',item.decision + ' / ' + item.actor),node('p',item.created_at,'meta'),node('pre',item.notes)); historyList.append(article);});
    content.hidden = false; close.disabled = false; state.textContent = 'Verified local view'; state.dataset.state = 'active';
  }
  async function refresh(){
    try{const detail = await request('/api/detail'); if(stopped) return; render(detail); pollTimer = setTimeout(refresh, 3000);}
    catch(error){lock('Access ended or source changed.');}
  }
  close.addEventListener('click', function(){
    const closing = request('/close', {}); lock('Access closed.'); closing.catch(function(){});
  });
  // Clear sensitive DOM when leaving, including back/forward cache restoration.
  addEventListener('pagehide', function(){lock('Access closed.');});
  (async function(){
    if(!bootstrap){lock('No active access.');return;}
    try{
      const started = performance.now();
      const session = await request('/session', {bootstrap}); bootstrap = '';
      if(stopped) return;
      token = session.session_token;
      const remaining = session.remaining_seconds * 1000 - (performance.now() - started);
      if(remaining <= 0){lock('Access expired.');return;}
      expiryTimer = setTimeout(() => lock('Access expired.'), remaining);
      await refresh();
    }catch(error){lock('Access unavailable.');}
  })();
})();
</script></body></html>"""
