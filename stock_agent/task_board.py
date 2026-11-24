"""Read-only research task board with one governed human-feedback endpoint.

First slice of plan W09 (read-only skeleton, stdlib only). GET routes render
task status, replay/live mode and model-call facts; the single POST route
records a human response through the task runtime with actor, expected
version and idempotency. A personal acknowledgement is stored as a personal
research acknowledgement only — it is never an institutional approval and
never touches the governance JSONL ledgers.
"""

from __future__ import annotations

import argparse
import hmac
import json
import secrets
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict, Optional, Tuple
from urllib.parse import urlparse

from .config import project_path
from .models import now_iso
from .task_runtime import (
    STATUS_AWAITING_HUMAN,
    STATUS_SUCCEEDED,
    TERMINAL_STATUSES,
    HUMAN_DECISIONS,
    TaskRuntimeError,
    TaskStore,
    TaskVersionConflictError,
    TaskIdempotencyConflictError,
)

_BOARD_VERSION = "1.0"
SSE_MAX_SECONDS = 300
SSE_POLL_SECONDS = 1.0
SSE_KEEPALIVE_SECONDS = 15.0


def _board_state(store: TaskStore, config: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    from .summary_review import review_state

    tasks = store.list_tasks()
    for task in tasks:
        task["is_replay"] = str(task.get("execution_mode") or "live") == "replay"
        task["summary_review"] = review_state(store, task["task_id"], config=config or {})
    return {
        "schema_version": _BOARD_VERSION,
        "generated_at": now_iso(),
        "summary": store.summary(),
        "tasks": tasks,
    }


def _action_buttons(task: Dict[str, Any], task_id: str, version: str, *,
                    logged_in: bool) -> str:
    """A93 row actions, shown ONLY to a logged-in principal (every target
    route is session-gated). The terminal set mirrors the backend's
    TERMINAL_STATUSES exactly — a rejected (blocked) row offers re-research,
    not a cancel the store would refuse. Plain buttons — no actor, no English
    decision strings, no JSON patch (taskbook R14 feedback rule)."""
    if not logged_in:
        return ""
    status = str(task.get("status") or "")
    buttons = []
    if status not in TERMINAL_STATUSES:
        buttons.append('<button type="button" class="act" data-ws="cancel" data-task="{0}" data-version="{1}">取消</button>'.format(task_id, version))
    if status in TERMINAL_STATUSES or status == STATUS_AWAITING_HUMAN:
        buttons.append('<button type="button" class="act" data-ws="rerun" data-task="{0}">重新研究</button>'.format(task_id))
    return '<span class="acts">{0}</span>'.format("".join(buttons)) if buttons else ""


def _feedback_row(task: Dict[str, Any], task_id: str, version: str) -> str:
    """A64/A90 plain-language feedback row (session mode, logged in only —
    the caller gates it).

    - awaiting_human: the model's question plus 签收/否定/追问补证;
    - succeeded without a pending question: the result is final, but the user
      can STILL proactively follow up (A90 — new linked task) and sign off
      (A50 — acknowledgement added to the preserved output).
    """
    status = str(task.get("status") or "")
    if status == STATUS_AWAITING_HUMAN:
        human_request = task.get("human_request") or {}
        question = _esc(str(human_request.get("question") or ""))
        if not question:
            return ""
        return (
            '<tr data-taskform="{0}"><td colspan="8"><div class="feedback"><em>{1}</em>'
            '<form data-task="{0}" data-version="{2}" class="ws-feedback">'
            '<textarea name="note" placeholder="追问或补充说明（普通文字）"></textarea>'
            '<button type="button" data-ws="acknowledge" data-task="{0}" data-version="{2}">签收（个人确认，非机构批准）</button>'
            '<button type="button" data-ws="reject" data-task="{0}" data-version="{2}">否定（理由写在上方）</button>'
            '<button type="button" data-ws="followup" data-task="{0}" data-version="{2}">提交追问/补证</button>'
            '</form></div></td></tr>'
        ).format(task_id, question, version)
    if status == STATUS_SUCCEEDED:
        output = task.get("output") if isinstance(task.get("output"), dict) else {}
        acknowledged = bool(isinstance(output.get("personal_acknowledgement"), dict)
                            and output.get("personal_acknowledgement"))
        buttons = ['<button type="button" data-ws="followup" data-task="{0}" data-version="{1}">提交追问（针对该结果）</button>'.format(task_id, version)]
        if not acknowledged:
            buttons.insert(0, '<button type="button" data-ws="signoff" data-task="{0}" data-version="{1}">签收（个人确认，非机构批准）</button>'.format(task_id, version))
        return (
            '<tr data-taskform="{0}"><td colspan="8"><div class="feedback">'
            '<form data-task="{0}" data-version="{2}" class="ws-feedback">'
            '<textarea name="note" placeholder="追问该结果的问题（普通文字，将作为新的关联研究）"></textarea>'
            '{3}</form></div></td></tr>'
        ).format(task_id, "", version, "".join(buttons))
    return ""


def _personal_panel_html(session_mode: bool, session_actor: str) -> Tuple[str, str]:
    """A93: the personal workspace panel, server-rendered so the FIRST paint
    already matches the session state (the JS only attaches handlers).

    Returns (inner_html, hidden_attr): the panel is hidden entirely on
    legacy (no-session) pages, which keep their original form."""
    if not session_mode:
        return "", " hidden"
    if not session_actor:
        return (
            '<form id="login-form">'
            '<input name="actor" placeholder="账号（真实个人）" required>'
            '<input name="symbols" placeholder="关注标的，逗号分隔（如 600519）" required>'
            '<button type="submit">登录个人工作台</button></form>'
            '<p class="hint">登录后即可仅用本页面完成创建研究、导入原件、追问、签收与取消。</p>'
        ), ""
    return (
        '<div class="who">已登录：' + _esc(session_actor) +
        ' <button type="button" id="logout-btn">退出</button></div>'
        '<form id="create-form">'
        '<textarea name="question" placeholder="研究问题（普通文字）" required></textarea>'
        '<input name="symbols" placeholder="标的，逗号分隔（如 600519）" required>'
        '<div class="docs" id="attach-docs"></div>'
        '<button type="submit">创建研究（replay 样本回放）</button></form>'
        '<details><summary>导入样本原件（个人命名空间，不发外部）</summary>'
        '<form id="import-form">'
        '<input type="file" name="file" required>'
        '<input name="source_url" placeholder="原文 HTTPS 链接" required>'
        '<input name="title" placeholder="标题" required>'
        '<input name="published_at" placeholder="发布时间（ISO，如 2026-10-23T18:00:00+08:00）" required>'
        '<button type="submit">导入</button></form>'
        '<p class="hint">导入仅进入你的个人证据命名空间（R12a），机构准入状态保持 NOT_REQUESTED。</p></details>'
        '<details open><summary>我的原件</summary><div class="docs" id="doc-list"></div></details>'
    ), ""


def _render_html(state: Dict[str, Any], csrf_token: str, *,
                 session_mode: bool = False, session_actor: str = "") -> str:
    logged_in = bool(session_mode and session_actor)
    rows = []
    for task in state.get("tasks", []):
        mode = "REPLAY" if task.get("is_replay") else "LIVE"
        task_id = _esc(str(task.get("task_id") or ""))
        version = _esc(str(task.get("version") or ""))
        # SSR rows carry the SAME task keys the client renderer uses so the
        # first client render recognizes them instead of duplicating rows.
        row = '<tr data-task="{7}" data-version="{5}"><td>{0}</td><td>{1}</td><td class=\'status-{2}\'>{2}</td><td>{3}</td><td>v{4}</td><td>#{5}</td><td>{6}</td><td>{8}</td></tr>'.format(
            _esc(str(task.get("task_id") or "")),
            _esc(str(mode)),
            _esc(str(task.get("status") or "")),
            _esc(str(task.get("title") or "")) + '<small class="summary-state">Summary: ' + _esc(str((task.get("summary_review") or {}).get("status") or "not_submitted")) + '</small>',
            _esc(str(task.get("input_version") or "")),
            version,
            _esc(str((task.get("model_call") or {}).get("error") or (task.get("last_error") or ""))[:120]),
            task_id,
            _action_buttons(task, task_id, version, logged_in=logged_in),
        )
        if logged_in:
            # A64/A90: plain-language personal feedback (taskbook: 反馈使用
            # 明确按钮和普通文字，不要求 actor/英文 decision/JSON patch).
            row += _feedback_row(task, task_id, version)
        elif (not session_mode and task.get("status") == STATUS_AWAITING_HUMAN
              and (task.get("human_request") or {}).get("question")):
            # Legacy rollback page only: session-mode pages never show the
            # form-actor path (a logged-out visitor has nothing that works).
            row += (
                '<tr data-taskform="{0}"><td colspan="8"><div class="feedback"><em>{1}</em>'
                '<form data-task="{0}" data-version="{2}">'
                '<input name="actor" placeholder="actor" required>'
                '<select name="decision"><option>acknowledge</option><option>reject</option><option>provide_input</option></select>'
                '<input name="input_patch" placeholder="JSON patch (provide_input)">'
                '<input name="notes" placeholder="notes"><button type="submit">Submit</button></form></div></td></tr>'
            ).format(task_id, _esc(str(task["human_request"].get("question") or "")), version)
        rows.append(row)
    panel_html, panel_hidden = _personal_panel_html(session_mode, session_actor)
    page = (_PAGE_TEMPLATE
            .replace("__ROWS__", "".join(rows))
            .replace("__CSRF_JSON__", json.dumps(str(csrf_token)))
            .replace("__SESSION_MODE_JSON__", "true" if session_mode else "false")
            .replace("__SESSION_ACTOR_JSON__", json.dumps(str(session_actor or "")))
            .replace("__PERSONAL_HIDDEN__", panel_hidden)
            .replace("__PERSONAL_PANEL__", panel_html))
    return page


_PAGE_TEMPLATE = """<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Research Task Board</title>
<style>
body{font-family:system-ui,sans-serif;margin:1.5rem;background:#111;color:#eee}
table{border-collapse:collapse;width:100%;min-width:760px;font-size:0.9rem}
.table-wrap{max-width:100%;overflow-x:auto}
td,th{border:1px solid #444;padding:0.4rem 0.6rem;text-align:left;vertical-align:top}
.status-succeeded{color:#5fd35f}.status-failed,.status-expired{color:#ff6b6b}
.status-awaiting_human{color:#ffc861}.status-running,.status-leased{color:#7fb4ff}
.status-queued{color:#bbb}.status-blocked{color:#e0a1e0}.status-cancelled{color:#888}
.replay{color:#ff9d5c;font-weight:bold}
.feedback{margin:.25rem 0 .75rem;padding:.4rem;border-left:3px solid #ffc861;background:#1c1c1c}
form{display:flex;gap:.35rem;flex-wrap:wrap;margin-top:.3rem}
input,select,button{background:#222;color:#eee;border:1px solid #555;padding:.25rem}
h1{font-size:1.2rem;display:flex;align-items:center;gap:.6rem}
#conn{font-size:.75rem;padding:.15rem .5rem;border-radius:.6rem;border:1px solid #555}
#conn.live{color:#5fd35f;border-color:#5fd35f}#conn.down{color:#ff6b6b;border-color:#ff6b6b}
p.note{color:#999;font-size:.8rem}
#msg{font-size:.8rem;color:#ffc861;min-height:1em}
.summary-state{display:block;color:#b5c6b8;font-size:.75rem;margin-top:.3rem}
#detail{position:fixed;top:0;right:0;bottom:0;width:min(480px,92vw);background:#161616;
border-left:1px solid #555;box-shadow:-4px 0 18px rgba(0,0,0,.5);padding:1rem 1.1rem;
overflow-y:auto;z-index:30}
#detail[hidden]{display:none}
#detail h2{font-size:.95rem;margin:.2rem 0 .6rem;display:flex;gap:.5rem;align-items:center;flex-wrap:wrap}
#detail .close{margin-left:auto;cursor:pointer}
#detail dl{margin:.4rem 0;display:grid;grid-template-columns:auto 1fr;gap:.2rem .6rem;font-size:.82rem}
#detail dt{color:#9a9a9a}#detail dd{margin:0;word-break:break-all}
#detail .sec{margin:.8rem 0 .2rem;font-size:.78rem;color:#ffc861;border-top:1px solid #333;padding-top:.5rem}
#detail .round{border-left:2px solid #444;padding:.15rem .5rem;margin:.25rem 0;font-size:.78rem}
#detail .scope{margin-top:1rem;color:#888;font-size:.72rem}
tr[data-task]{cursor:pointer}
.ovbar{display:flex;flex-wrap:wrap;gap:.4rem;margin:.4rem 0 .8rem;font-size:.78rem}
.ovbar .chip{border:1px solid #444;border-radius:.6rem;padding:.15rem .55rem;background:#1a1a1a}
.ovbar .chip b{color:#ffc861;font-weight:600}
.ovbar .chip .bad{color:#ff6b6b}
.ovbar .chip .ok{color:#5fd35f}
.evsec{border:1px solid #3a3a3a;border-radius:.4rem;margin:.2rem 0 .8rem;background:#141414}
.evsec summary{cursor:pointer;padding:.4rem .6rem;font-size:.85rem;color:#cfcfcf}
.evsec .ev{border-top:1px solid #2c2c2c;padding:.4rem .6rem;font-size:.8rem;display:flex;gap:.5rem;flex-wrap:wrap;align-items:baseline}
.evsec .ev .t{font-weight:600;max-width:46ch;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.evsec .rev{background:#5a2d2d;color:#ffb3b3;border-radius:.4rem;padding:0 .35rem;font-size:.72rem}
.evsec .src{background:#222;border-radius:.4rem;padding:0 .35rem;font-size:.72rem;color:#9fd0ff}
.evsec .tl{cursor:pointer;border-radius:.4rem;padding:0 .35rem;font-size:.72rem;border:1px solid #444}
.evsec .tl.succeeded{color:#5fd35f}.evsec .tl.queued{color:#bbb}
.evsec .tl.awaiting_human{color:#ffc861}.evsec .tl.failed,.evsec .tl.blocked{color:#ff6b6b}
.evsec .when{color:#777;font-size:.72rem;margin-left:auto}
.evsec .age{background:#3a2d10;color:#ffd27a;border-radius:.4rem;padding:0 .35rem;font-size:.72rem}
.evsec .why{color:#999;font-size:.72rem}
.evsec .blk{color:#ff9d9d}
.evsec .muted{color:#777}
.evsec .stale{color:#ffd27a}
#personal{border:1px solid #3a5a3a;border-radius:.5rem;margin:.4rem 0 .8rem;padding:.6rem .8rem;background:#14201a}
#personal .who{color:#9fd0ff;font-size:.85rem;margin-bottom:.4rem}
#personal form{margin:.2rem 0}
#personal textarea{flex:1 1 100%;min-height:2.4rem}
#personal .docs{margin:.4rem 0;display:grid;gap:.25rem}
#personal .doc{display:flex;gap:.5rem;align-items:baseline;font-size:.8rem;flex-wrap:wrap}
#personal .doc .src{max-width:38ch;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
#personal .hint{color:#888;font-size:.72rem}
.ws-feedback textarea{flex:1 1 100%;min-height:2.2rem}
button.act{cursor:pointer}
span.acts{display:inline-flex;gap:.3rem;flex-wrap:wrap}
</style></head><body>
<h1>Research Task Board <span id="conn" class="down">CONNECTING</span></h1>
<p class="note">REPLAY rows used recorded model responses, not live calls.
Personal acknowledgement is a personal research acknowledgement, not institutional approval.</p>
<p id="msg"></p>
<div id="personal"__PERSONAL_HIDDEN__ aria-label="Personal workspace">__PERSONAL_PANEL__</div>
<div id="overview" class="ovbar" hidden aria-label="Workbench overview"></div>
<details id="todo" class="evsec" open>
  <summary>待办<span id="todocount"></span></summary>
  <div id="todolist"></div>
</details>
<details id="events" class="evsec" open>
  <summary>事件（去重，72h）<span id="evcount"></span></summary>
  <div id="evlist"></div>
</details>
<details id="queries" class="evsec">
  <summary>数据查询（Kimi 账本，只读）<span id="kqcount"></span></summary>
  <div id="kqlist"></div>
</details>
<details id="ops" class="evsec">
  <summary>运行（引擎/预算/登记/投影）</summary>
  <div id="opslist"></div>
</details>
<div class="table-wrap" role="region" aria-label="Research tasks" tabindex="0"><table><thead><tr><th>task_id</th><th>mode</th><th>status</th><th>title</th><th>input_v</th><th>row_v</th><th>last error</th><th>操作</th></tr></thead>
<tbody id="tasks">__ROWS__
</tbody></table></div>
<aside id="detail" hidden aria-label="Task detail"></aside>
<script>
(function() {
  var CSRF = __CSRF_JSON__;
  var SESSION_MODE = __SESSION_MODE_JSON__;
  var SESSION_ACTOR = __SESSION_ACTOR_JSON__;
  var LOGGED_IN = SESSION_MODE && !!SESSION_ACTOR;
  // Mirrors the backend TERMINAL_STATUSES — blocked IS terminal, so a
  // rejected row offers re-research, never a cancel the store would refuse.
  var TERMINAL = ['succeeded', 'failed', 'blocked', 'cancelled', 'expired'];
  var tbody = document.getElementById('tasks');
  var conn = document.getElementById('conn');
  var msg = document.getElementById('msg');

  // ---- A93 personal workspace panel (session mode pages only) ----
  var personal = document.getElementById('personal');
  function wsPost(path, body) {
    return fetch(path, {
      method: 'POST',
      headers: {'Content-Type': 'application/json', 'X-Task-Board-CSRF': CSRF},
      body: JSON.stringify(body)
    }).then(function(r) { return r.json().then(function(j) { return {ok: r.ok, j: j}; }); });
  }
  // The panel MARKUP is server-rendered (first paint matches the session);
  // this side only wires the handlers to it.
  function renderPersonalPanel() {
    if (!SESSION_MODE || !personal) return;
    var loginForm = document.getElementById('login-form');
    if (loginForm) {
      loginForm.addEventListener('submit', function(ev) {
        ev.preventDefault();
        var fd = new FormData(loginForm);
        fetch('/api/v1/personal/login', {
          method: 'POST', headers: {'Content-Type': 'application/json'},
          body: JSON.stringify({actor: fd.get('actor'),
                                symbols: String(fd.get('symbols') || '').split(/[,，\\s]+/).filter(Boolean)})
        }).then(function(r) { return r.json().then(function(j) { return {ok: r.ok, j: j}; }); })
          .then(function(out) {
            if (out.ok) { window.location.reload(); return; }
            msg.textContent = '登录失败: ' + (out.j.error || '') + (out.j.detail ? ' — ' + out.j.detail : '');
          })
          .catch(function() { msg.textContent = '登录请求失败'; });
      });
      return;
    }
    var logout = document.getElementById('logout-btn');
    if (logout) logout.addEventListener('click', function() {
      wsPost('/api/v1/personal/logout', {}).then(function() { window.location.reload(); });
    });
    var importForm = document.getElementById('import-form');
    if (importForm) importForm.addEventListener('submit', function(ev) {
      ev.preventDefault();
      var fd = new FormData(importForm);
      var file = fd.get('file');
      if (!file) { msg.textContent = '请选择要导入的文件'; return; }
      file.arrayBuffer().then(function(buffer) {
        var binary = '';
        new Uint8Array(buffer).forEach(function(byte) { binary += String.fromCharCode(byte); });
        return wsPost('/api/v1/personal/import', {
          filename: file.name,
          content_base64: btoa(binary),
          source_url: String(fd.get('source_url') || ''),
          title: String(fd.get('title') || ''),
          published_at: String(fd.get('published_at') || '')
        });
      }).then(function(out) {
        if (out.ok) {
          msg.textContent = '已导入: ' + ((out.j.document || {}).title || '');
          importForm.reset();
          loadDocuments();
        } else {
          msg.textContent = '导入被拒绝: ' + (out.j.error || '') + (out.j.detail ? ' — ' + out.j.detail : '');
        }
      }).catch(function() { msg.textContent = '导入请求失败'; });
    });
    var createForm = document.getElementById('create-form');
    if (createForm) createForm.addEventListener('submit', function(ev) {
      ev.preventDefault();
      var fd = new FormData(createForm);
      var documentIds = [];
      createForm.querySelectorAll('input[name=document_id]:checked').forEach(function(box) {
        documentIds.push(box.value);
      });
      wsPost('/api/v1/workspace/create', {
        question: String(fd.get('question') || ''),
        symbols: String(fd.get('symbols') || '').split(/[,，\\s]+/).filter(Boolean),
        execution_mode: 'replay',
        document_ids: documentIds
      }).then(function(out) {
        if (out.ok) {
          msg.textContent = '已创建: ' + out.j.task_id + ' (' + out.j.outcome + ')';
          createForm.querySelector('textarea[name=question]').value = '';
          refresh();
        } else {
          msg.textContent = '创建失败: ' + (out.j.error || '') + (out.j.detail ? ' — ' + out.j.detail : '');
        }
      }).catch(function() { msg.textContent = '创建请求失败'; });
    });
    loadDocuments();
  }
  function loadDocuments() {
    if (!SESSION_MODE || !SESSION_ACTOR) return;
    fetch('/api/v1/personal/documents')
      .then(function(r) { if (!r.ok) throw new Error('documents request failed'); return r.json(); })
      .then(showDocuments)
      .catch(function() { /* documents stay empty; the board works */ });
  }
  function showDocuments(view) {
    if (!view || !Array.isArray(view.documents)) return;
    var attach = document.getElementById('attach-docs');
    var list = document.getElementById('doc-list');
    var html = view.documents.map(function(d) {
      return '<div class="doc"><input type="checkbox" name="document_id" value="' + esc(d.document_id) + '" id="doc-' + esc(d.document_id) + '">' +
        '<label for="doc-' + esc(d.document_id) + '">' + esc(d.title) + '</label>' +
        '<span class="src" title="' + esc(d.source_url) + '">' + esc(d.source_url) + '</span>' +
        '<span class="src">' + esc(String(d.published_at || '').replace('T', ' ').slice(0, 16)) + '</span></div>';
    }).join('');
    if (attach) attach.innerHTML = html || '<p class="hint">尚无原件；可先在下方导入样本公告。</p>';
    if (list) list.innerHTML = html || '<p class="hint">尚无原件。</p>';
  }
  // Delegated workspace actions: cancel / rerun row buttons plus the
  // plain-language awaiting feedback (acknowledge / reject / followup).
  // A submission whose response was lost can be resent: the SAME note on the
  // SAME form reuses the SAME request id, so the server deduplicates instead
  // of queueing a second task; editing the note or a confirmed success mints
  // a fresh id (a genuinely new user action).
  function pendingCommand(group, noteText) {
    if (!group) return '';
    if (group.dataset.pendingCommand && group.dataset.pendingNote === noteText) {
      return group.dataset.pendingCommand;
    }
    var cmd = (window.crypto && window.crypto.randomUUID)
      ? window.crypto.randomUUID()
      : 'cmd-' + Date.now() + '-' + Math.random().toString(16).slice(2);
    group.dataset.pendingCommand = cmd;
    group.dataset.pendingNote = noteText;
    return cmd;
  }
  function workspaceAction(button) {
    var kind = button.getAttribute('data-ws');
    var taskId = button.getAttribute('data-task');
    var version = parseInt(button.getAttribute('data-version') || '0', 10);
    var group = tbody.querySelector('tr[data-taskform="' + taskId + '"] form');
    var note = group && group.querySelector('textarea[name=note]');
    var noteText = note ? note.value : '';
    var body;
    if (kind === 'cancel') {
      body = {task_id: taskId, expected_version: version, reason: '网页取消'};
    } else if (kind === 'rerun') {
      body = {task_id: taskId, execution_mode: 'replay'};
    } else if (kind === 'followup') {
      if (!noteText.trim()) { msg.textContent = '追问前请先写下问题或补充说明'; return; }
      body = {task_id: taskId, expected_version: version,
              extra_note: noteText, command_id: pendingCommand(group, noteText)};
    } else if (kind === 'acknowledge' || kind === 'reject') {
      body = {task_id: taskId, expected_version: version,
              decision: kind === 'acknowledge' ? 'acknowledge' : 'reject',
              notes: noteText, command_id: pendingCommand(group, noteText), csrf_token: CSRF};
    } else if (kind === 'signoff') {
      // A90/A50: sign off an already-succeeded no-question result — the
      // workspace route ADDS the acknowledgement to the preserved output.
      body = {task_id: taskId, expected_version: version,
              notes: noteText, command_id: pendingCommand(group, noteText)};
    } else {
      return;
    }
    var path = (kind === 'cancel' || kind === 'rerun' || kind === 'followup')
      ? '/api/v1/workspace/' + kind
      : (kind === 'signoff' ? '/api/v1/workspace/acknowledge' : '/api/v1/human-responses');
    msg.textContent = '处理中…';
    wsPost(path, body).then(function(out) {
      if (out.ok) {
        if (group) {
          group.dataset.dirty = '';
          // Confirmed: the next submission on this form is a NEW action.
          group.dataset.pendingCommand = '';
          group.dataset.pendingNote = '';
        }
        msg.textContent = '已' + ({cancel: '取消', rerun: '创建重新研究', followup: '记录追问',
                                   acknowledge: '签收', reject: '否定', signoff: '签收'})[kind] +
          (out.j.task_id ? ': ' + out.j.task_id : '');
        refresh();
      } else {
        // Refused (or a lost-response retry arrives as an error): the pending
        // id STAYS, so resending the same note reuses the same request.
        msg.textContent = '操作被拒绝: ' + (out.j.error || '') + (out.j.detail ? ' — ' + out.j.detail : '');
      }
    }).catch(function() { msg.textContent = '操作请求失败'; });
  }
  document.addEventListener('click', function(ev) {
    var button = ev.target.closest ? ev.target.closest('button[data-ws]') : null;
    if (button) { ev.preventDefault(); workspaceAction(button); }
  });
  function esc(s) {
    return String(s == null ? '' : s).replace(/[&<>"']/g, function(c) {
      return {'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c];
    });
  }
  function actionButtons(t) {
    if (!LOGGED_IN) return '';
    var buttons = '';
    if (TERMINAL.indexOf(t.status) === -1) {
      buttons += '<button type="button" class="act" data-ws="cancel" data-task="' + esc(t.task_id) +
        '" data-version="' + esc(t.version) + '">取消</button>';
    }
    if (TERMINAL.indexOf(t.status) !== -1 || t.status === 'awaiting_human') {
      buttons += '<button type="button" class="act" data-ws="rerun" data-task="' + esc(t.task_id) + '">重新研究</button>';
    }
    return buttons ? '<span class="acts">' + buttons + '</span>' : '';
  }
  function feedbackRow(t) {
    // Mirrors the server-side _feedback_row: awaiting gets the question +
    // 签收/否定/追问; a succeeded no-question result STILL gets 追问 (A90,
    // new linked task) and 签收 until acknowledged (A50).
    if (t.status === 'awaiting_human' && t.human_request && t.human_request.question) {
      return '<tr data-taskform="' + esc(t.task_id) + '"><td colspan="8"><div class="feedback"><em>' + esc(t.human_request.question) + '</em>' +
        '<form data-task="' + esc(t.task_id) + '" data-version="' + esc(t.version) + '" class="ws-feedback">' +
        '<textarea name="note" placeholder="追问或补充说明（普通文字）"></textarea>' +
        '<button type="button" data-ws="acknowledge" data-task="' + esc(t.task_id) + '" data-version="' + esc(t.version) + '">签收（个人确认，非机构批准）</button>' +
        '<button type="button" data-ws="reject" data-task="' + esc(t.task_id) + '" data-version="' + esc(t.version) + '">否定（理由写在上方）</button>' +
        '<button type="button" data-ws="followup" data-task="' + esc(t.task_id) + '" data-version="' + esc(t.version) + '">提交追问/补证</button>' +
        '</form></div></td></tr>';
    }
    if (t.status === 'succeeded') {
      var output = t.output || {};
      var acknowledged = !!(output.personal_acknowledgement &&
        typeof output.personal_acknowledgement === 'object');
      var buttons = '<button type="button" data-ws="followup" data-task="' + esc(t.task_id) +
        '" data-version="' + esc(t.version) + '">提交追问（针对该结果）</button>';
      if (!acknowledged) {
        buttons = '<button type="button" data-ws="signoff" data-task="' + esc(t.task_id) +
          '" data-version="' + esc(t.version) + '">签收（个人确认，非机构批准）</button>' + buttons;
      }
      return '<tr data-taskform="' + esc(t.task_id) + '"><td colspan="8"><div class="feedback">' +
        '<form data-task="' + esc(t.task_id) + '" data-version="' + esc(t.version) + '" class="ws-feedback">' +
        '<textarea name="note" placeholder="追问该结果的问题（普通文字，将作为新的关联研究）"></textarea>' +
        buttons + '</form></div></td></tr>';
    }
    return '';
  }
  function rowHtml(t) {
    var mode = t.is_replay ? 'REPLAY' : 'LIVE';
    var err = ((t.model_call || {}).error || t.last_error || '').slice(0, 120);
    var base = '<tr data-task="' + esc(t.task_id) + '" data-version="' + esc(t.version) + '"><td>' + esc(t.task_id) + '</td><td>' + esc(mode) + '</td>' +
      '<td class="status-' + esc(t.status) + '">' + esc(t.status) + '</td><td>' + esc(t.title) +
      '<small class="summary-state">Summary: ' + esc((t.summary_review || {}).status || 'not_submitted') + '</small></td>' +
      '<td>v' + esc(t.input_version) + '</td><td>#' + esc(t.version) + '</td><td>' + esc(err) + '</td><td>' + actionButtons(t) + '</td></tr>';
    if (LOGGED_IN) {
      base += feedbackRow(t);
    } else if (!SESSION_MODE && t.status === 'awaiting_human' && t.human_request && t.human_request.question) {
      base += '<tr data-taskform="' + esc(t.task_id) + '"><td colspan="8"><div class="feedback"><em>' + esc(t.human_request.question) + '</em>' +
        '<form data-task="' + esc(t.task_id) + '" data-version="' + esc(t.version) + '">' +
        '<input name="actor" placeholder="actor" required>' +
        '<select name="decision"><option>acknowledge</option><option>reject</option><option>provide_input</option></select>' +
        '<input name="input_patch" placeholder="JSON patch (provide_input)">' +
        '<input name="notes" placeholder="notes"><button type="submit">Submit</button></form></div></td></tr>';
    }
    return base;
  }
  var applySeq = 0;
  var lastApplied = 0;
  function groupNodes(tid) {
    var tr = tbody.querySelector('tr[data-task="' + tid + '"]');
    if (!tr) return null;
    var formTr = tbody.querySelector('tr[data-taskform="' + tid + '"]');
    return { tr: tr, formTr: formTr };
  }
  function appendGroup(t) { tbody.insertAdjacentHTML('beforeend', rowHtml(t)); }
  function replaceGroup(t) {
    var g = groupNodes(t.task_id);
    // Focus continuity for CLEAN groups: record the focused input (name +
    // selection) and restore it inside the rebuilt form, so a post-submit
    // refresh updates the status while focus/selection survive.
    var focusInfo = null;
    if (g && document.activeElement && (g.tr.contains(document.activeElement) || (g.formTr && g.formTr.contains(document.activeElement)))) {
      var ae = document.activeElement;
      focusInfo = { name: ae.name || '', selStart: ae.selectionStart, selEnd: ae.selectionEnd };
    }
    var holder = document.createElement('tbody');
    holder.innerHTML = rowHtml(t);
    var anchor = g ? g.tr : null;
    // SNAPSHOT the node list first: moving children out of the live
    // NodeList while iterating skips every second node (which is how the
    // feedback form row used to disappear on version updates).
    var nodes = Array.prototype.slice.call(holder.childNodes);
    nodes.forEach(function(node) { tbody.insertBefore(node, anchor); });
    if (g) {
      if (g.tr) tbody.removeChild(g.tr);
      if (g.formTr) tbody.removeChild(g.formTr);
    }
    if (focusInfo && focusInfo.name) {
      var restored = tbody.querySelector('tr[data-taskform="' + t.task_id + '"] [name="' + focusInfo.name + '"]');
      if (restored) {
        restored.focus();
        try { restored.setSelectionRange(focusInfo.selStart, focusInfo.selEnd); } catch (e) { }
      }
    }
  }
  function containsActiveElement(node) {
    return node && document.activeElement && node.contains(document.activeElement);
  }
  function render(state) {
    // TRUE preservation: dirty form groups are never re-rendered, so the
    // live DOM nodes keep their .value; a version change keeps the form but
    // flags a conflict instead of binding old input to a new version. Clean
    // groups are rebuilt; vanished tasks are removed. Reordering only moves
    // groups that are actually misplaced AND never moves the group holding
    // the focus, so typing through a refresh keeps focus and selection.
    var byId = {};
    (state.tasks || []).forEach(function(t) { byId[t.task_id] = t; });
    Object.keys(byId).forEach(function(tid) {
      var g = groupNodes(tid);
      if (!g) { appendGroup(byId[tid]); return; }
      var summaryBadge = g.tr.querySelector('.summary-state');
      if (summaryBadge) summaryBadge.textContent = 'Summary: ' + ((byId[tid].summary_review || {}).status || 'not_submitted');
      var form = g.formTr && g.formTr.querySelector('form');
      var dirty = form && form.dataset.dirty === '1';
      if (dirty) {
        if (g.tr.getAttribute('data-version') !== String(byId[tid].version)) {
          g.formTr.className = 'stale-form';
          msg.textContent = 'conflict: task ' + tid + ' updated; your unsent input targets the old version';
        }
        return;  // keep the live nodes (and focus) untouched this round
      }
      if (g.tr.getAttribute('data-version') !== String(byId[tid].version)) { replaceGroup(byId[tid]); }
    });
    // Order-aware reorder: compare the desired group order with the current
    // one and only move groups that are genuinely misplaced.
    var desired = Object.keys(byId);
    var current = Array.prototype.map.call(
      tbody.querySelectorAll('tr[data-task]'), function(tr) { return tr.getAttribute('data-task'); }
    ).filter(function(tid) { return byId[tid]; });
    var orderChanged = desired.length !== current.length || desired.some(function(tid, i) { return tid !== current[i]; });
    if (orderChanged) {
      desired.forEach(function(tid) {
        var g = groupNodes(tid);
        if (!g) return;
        if (containsActiveElement(g.tr) || containsActiveElement(g.formTr)) return;
        tbody.appendChild(g.tr);
        if (g.formTr) tbody.appendChild(g.formTr);
      });
    }
    Array.prototype.forEach.call(tbody.querySelectorAll('tr[data-task]'), function(tr) {
      if (!byId[tr.getAttribute('data-task')]) tbody.removeChild(tr);
    });
    Array.prototype.forEach.call(tbody.querySelectorAll('tr[data-taskform]'), function(tr) {
      if (!byId[tr.getAttribute('data-taskform')]) tbody.removeChild(tr);
    });
  }
  function refresh() {
    var seq = ++applySeq;
    fetch('/api/v1/tasks').then(function(r) {
      if (!r.ok) throw new Error('snapshot request failed');
      return r.json();
    }).then(function(state) {
      if (seq < lastApplied) return;  // a newer snapshot already applied
      if (!state || !Array.isArray(state.tasks)) throw new Error('invalid task snapshot');
      lastApplied = seq;
      render(state);
    }).catch(function() {
      if (seq < lastApplied) return;
      msg.textContent = 'refresh failed; retrying on next event';
    });
  }
  tbody.addEventListener('input', function(ev) {
    var form = ev.target.closest ? ev.target.closest('form') : null;
    if (form) form.dataset.dirty = '1';
  });
  tbody.addEventListener('submit', function(ev) {
    ev.preventDefault();
    var form = ev.target;
    var fd = new FormData(form);
    var body = {
      task_id: form.getAttribute('data-task'),
      expected_version: parseInt(form.getAttribute('data-version'), 10),
      actor: fd.get('actor'), decision: fd.get('decision'), notes: fd.get('notes') || '',
      command_id: '', csrf_token: CSRF
    };
    var patch = fd.get('input_patch');
    if (patch && String(patch).trim()) { body.input_patch = String(patch); }
    fetch('/api/v1/human-responses', {
      method: 'POST',
      headers: {'Content-Type': 'application/json', 'X-Task-Board-CSRF': CSRF},
      body: JSON.stringify(body)
    }).then(function(r) { return r.json().then(function(j) { return {ok: r.ok, j: j}; }); })
      .then(function(out) {
        if (out.ok) { form.dataset.dirty = ''; }  // accepted: no longer unsent input
        msg.textContent = out.ok ? 'recorded: ' + (out.j.task && out.j.task.status) : ('error: ' + (out.j.error || 'rejected'));
        refresh();
      }).catch(function() { msg.textContent = 'submit failed'; });
  });
  // ---- workbench overview bar (read-only projection of /api/v1/overview) ----
  var overviewBar = document.getElementById('overview');
  // Last readable successful snapshot of /api/v1/overview. It is kept across
  // failed refreshes so the region can tell empty / failed / stale apart.
  var overviewSnapshot = null;  // {o, generatedAt, receivedAt}
  var overviewSeq = 0;
  function overviewIsEmpty(o) {
    var counts = (o.tasks || {}).status_counts || {};
    var total = Object.keys(counts).reduce(function(sum, key) { return sum + (Number(counts[key]) || 0); }, 0);
    return !Object.keys((o.market || {}).quotes || {}).length &&
      !((o.events || {}).eligible_count || 0) && !total && !((o.tasks || {}).awaiting_human || []).length;
  }
  function overviewClock(d) {
    function two(n) { return (n < 10 ? '0' : '') + n; }
    return two(d.getHours()) + ':' + two(d.getMinutes()) + ':' + two(d.getSeconds());
  }
  function showOverview(o) {
    if (!o || typeof o !== 'object') return;
    overviewSnapshot = {
      o: o,
      generatedAt: typeof o.generated_at === 'string' ? o.generated_at : '',
      receivedAt: new Date(),
    };
    renderOverview(overviewIsEmpty(o) ? 'empty' : 'fresh');
  }
  // region states: loading | fresh | empty | stale | failed
  function renderOverview(state, detail) {
    var snap = overviewSnapshot;
    var html = '';
    function chip(html) { return '<span class="chip">' + html + '</span>'; }
    if (snap) {
      var o = snap.o;
      var engine = (o.engine || {}).event_intake_mode || '?';
      var market = o.market || {};
      var quotes = market.quotes || {};
      var missing = market.quotes_missing || [];
      var events = o.events || {};
      var counts = (o.tasks || {}).status_counts || {};
      var awaiting = (o.tasks || {}).awaiting_human || [];
      var budget = o.budget || {};
      var data = '';
      data += chip('engine <b>' + esc(engine) + '</b>');
      data += chip('quotes <b class="' + (missing.length && !Object.keys(quotes).length ? 'bad' : 'ok') + '">'
        + Object.keys(quotes).length + '</b>' + (missing.length
          ? ' / missing <b class="bad">' + esc(missing.length) + '</b>' : ''));
      data += chip('events 24h <b>' + esc(events.eligible_count || 0) + '</b>');
      data += chip('queued <b>' + esc(counts.queued || 0) + '</b>');
      data += chip('awaiting human <b class="' + (awaiting.length ? 'bad' : 'ok') + '">'
        + esc(awaiting.length) + '</b>');
      data += chip('model calls today <b>' + esc(budget.calls_today || 0) + '</b>'
        + (budget.enabled ? ' / limit ' + esc(budget.limit) : ' <span class="ok">(budget off)</span>'));
      // display:contents keeps each chip a direct flex item of .ovbar
      // (its per-chip wrapping and gap), so the wrapper cannot split a chip.
      html += '<span data-ov-data style="display:contents">' + data + '</span>';
    }
    // The snapshot time is the server's own generated_at, shown verbatim.
    var stamp = snap && snap.generatedAt
      ? ' <time datetime="' + esc(snap.generatedAt) + '">' + esc(snap.generatedAt) + '</time>' : '';
    var received = snap ? '（本页 ' + esc(overviewClock(snap.receivedAt)) + ' 收到）' : '';
    var status = '';
    var tone = '';
    if (state === 'loading') status = '概览加载中…';
    else if (state === 'fresh') status = '已加载' + stamp;
    else if (state === 'empty') status = '已加载 · 暂无数据' + stamp;
    else if (state === 'stale') { status = '已过期 · 刷新失败（' + esc(detail || '请求失败') + '）· 以下为旧数据，截至' + stamp + received; tone = 'bad'; }
    else { status = '概览加载失败（' + esc(detail || '请求失败') + '）'; tone = 'bad'; }
    html += chip('<span data-ov-status class="' + tone + '">' + status + '</span>');
    overviewBar.innerHTML = html;
    overviewBar.setAttribute('data-state', state);
    overviewBar.hidden = false;
  }
  function fetchOverview() {
    // Only the newest request may settle the region.
    var seq = ++overviewSeq;
    if (!overviewSnapshot) renderOverview('loading');
    return fetch('/api/v1/overview')
      .then(function(r) { if (!r.ok) throw new Error('HTTP ' + r.status); return r.json(); })
      .then(function(o) {
        if (!o || typeof o !== 'object') throw new Error('bad payload');
        if (seq === overviewSeq) showOverview(o);
      })
      .catch(function(err) {
        // a failed refresh keeps the last readable snapshot, marked stale
        if (seq !== overviewSeq) return;
        var message = err && err.message;
        var reason = message === 'bad payload' ? '响应无效'
          : message && message.indexOf('HTTP ') === 0 ? message : '网络错误';
        renderOverview(overviewSnapshot ? 'stale' : 'failed', reason);
      });
  }

  // ---- todo section (what needs a human, from /api/v1/todo) ----
  var todoList = document.getElementById('todolist');
  var todoCount = document.getElementById('todocount');
  function showTodo(view) {
    if (!view || typeof view !== 'object') return;
    var counts = view.counts || {};
    var rq = view.review_queue || {};
    var total = (counts.awaiting_human || 0) + (counts.blocked || 0) + (rq.pending_count || 0);
    todoCount.textContent = '（' + total + '）';
    var html = '';
    (view.awaiting_human || []).forEach(function(item) {
      var age = item.age_minutes >= 60
        ? Math.floor(item.age_minutes / 60) + 'h' : item.age_minutes + 'm';
      html += '<div class="ev">'
        + '<span class="age">' + esc(age) + '</span>'
        + '<span class="t" title="' + esc(item.question) + '">' + esc(item.question) + '</span>'
        + (item.why_needed ? '<span class="why">' + esc(item.why_needed) + '</span>' : '')
        + '<span class="tl awaiting_human" data-tasklink="' + esc(item.task_id) + '"'
        + ' title="' + esc(item.task_id) + '">回答</span>'
        + '<span class="when">' + esc(String(item.updated_at || '').replace('T', ' ').slice(0, 16)) + '</span>'
        + '</div>';
    });
    (view.blocked || []).forEach(function(item) {
      html += '<div class="ev">'
        + '<span class="t blk" title="' + esc(item.error) + '">⚠ ' + esc(item.title) + '</span>'
        + '<span class="why">' + esc(item.error) + '</span>'
        + '<span class="tl blocked" data-tasklink="' + esc(item.task_id) + '"'
        + ' title="' + esc(item.task_id) + '">查看</span>'
        + '</div>';
    });
    if (rq.available && rq.pending_count) {
      html += '<div class="ev"><span class="t">复核队列</span>'
        + '<span class="why">pending ' + esc(rq.pending_count) + '（research-ops/review-queue 入口处理）</span></div>';
    }
    todoList.innerHTML = html;
  }
  function fetchTodo() {
    fetch('/api/v1/todo')
      .then(function(r) { if (!r.ok) throw new Error('todo request failed'); return r.json(); })
      .then(showTodo)
      .catch(function() { /* todo section stays empty; the board works */ });
  }

  // ---- ops section (run health from /api/v1/ops; projections only) ----
  var opsList = document.getElementById('opslist');
  function showOps(view) {
    if (!view || typeof view !== 'object') return;
    var engine = view.engine || {};
    var counts = engine.status_counts || {};
    var consumption = engine.consumption || {};
    var budget = view.budget || {};
    var registry = view.source_registry || {};
    var html = '';
    html += '<div class="ev"><span class="t">引擎 <b>' + esc(engine.event_intake_mode) + '</b></span>'
      + '<span class="why">queued ' + esc(counts.queued || 0)
      + ' · running ' + esc(counts.running || 0)
      + ' · succeeded ' + esc(counts.succeeded || 0)
      + ' · failed ' + esc(counts.failed || 0)
      + ' · blocked ' + esc(counts.blocked || 0)
      + ' · awaiting ' + esc(counts.awaiting_human || 0) + '</span>'
      + '<span class="when">outbox ' + esc(engine.outbox_events || 0) + '</span></div>';
    html += '<div class="ev"><span class="t">预算</span>'
      + '<span class="why">今日调用 ' + esc(budget.calls_today || 0)
      + (budget.enabled ? ' / 上限 ' + esc(budget.limit) : '（未启用）') + '</span>'
      + (consumption.orphan_tasks ? '<span class="stale">orphan ' + esc(consumption.orphan_tasks) + '</span>' : '')
      + '<span class="when">consumed ' + esc(consumption.consumed_events || 0) + '</span></div>';
    (view.recent_failures || []).forEach(function(item) {
      html += '<div class="ev">'
        + '<span class="t blk" title="' + esc(item.error) + '">⚠ ' + esc(item.title) + '</span>'
        + '<span class="why">' + esc(item.error) + '</span>'
        + '<span class="tl ' + esc(item.status) + '" data-tasklink="' + esc(item.task_id) + '">查看</span>'
        + '</div>';
    });
    if (registry.configured) {
      (registry.entries || []).forEach(function(entry) {
        html += '<div class="ev"><span class="t">' + esc(entry.source_id) + '</span>'
          + '<span class="why">' + esc(entry.kind) + ' · ' + esc(entry.approval_state)
          + (entry.expired ? ' · <span class="stale">已过期</span>' : '')
          + ' · 外发 ' + (entry.external_send_allowed ? '允许' : '否') + '</span></div>';
      });
    } else {
      html += '<div class="ev"><span class="t">来源登记</span><span class="muted">未配置</span></div>';
    }
    html += '<div class="ev"><span class="t">投影</span>'
      + (view.projections || []).map(function(p) {
        var text = p.present
          ? (p.age_minutes == null ? p.name : p.name + ' ' + p.age_minutes + 'm')
          : p.name + ' 缺失';
        return '<span class="' + (p.present ? 'src' : 'muted') + '">' + esc(text) + '</span>';
      }).join(' ') + '</div>';
    html += '<div class="ev"><span class="muted">' + esc(view.projections_note || '') + '</span></div>';
    html += '<div class="ev"><span class="muted">' + esc((view.source_registry || {}).note || '') + '</span></div>';
    opsList.innerHTML = html;
  }
  function fetchOps() {
    fetch('/api/v1/ops')
      .then(function(r) { if (!r.ok) throw new Error('ops request failed'); return r.json(); })
      .then(showOps)
      .catch(function() { /* ops section stays empty; the board works */ });
  }

  // ---- events section (deduped story events from /api/v1/events-view) ----
  var evList = document.getElementById('evlist');
  var evCount = document.getElementById('evcount');
  function showEvents(view) {
    if (!view || !Array.isArray(view.events)) return;
    evCount.textContent = '（' + (view.event_count || view.events.length) + '）';
    evList.innerHTML = view.events.map(function(ev) {
      var tasks = (ev.linked_tasks || []).map(function(t) {
        return '<span class="tl ' + esc(t.status) + '" data-tasklink="' + esc(t.task_id) + '"'
          + ' title="' + esc(t.task_id) + '">' + esc(t.status) + '</span>';
      }).join(' ');
      var sources = (ev.sources || []).map(function(s) { return '<span class="src">' + esc(s) + '</span>'; }).join(' ');
      return '<div class="ev" data-cluster="' + esc(ev.cluster_key) + '">'
        + '<span class="t" title="' + esc(ev.title) + '">' + esc(ev.title) + '</span>'
        + (ev.revision ? '<span class="rev">v' + esc((ev.versions || []).join('/')) + ' 修订</span>' : '')
        + sources
        + '<span class="src">' + esc((ev.symbols || []).join(',')) + '</span>'
        + '<span class="src">×' + esc(ev.report_count) + '</span>'
        + tasks
        + '<span class="when">' + esc(String(ev.last_published || '').replace('T', ' ').slice(0, 16)) + '</span>'
        + '</div>';
    }).join('');
  }
  function fetchEvents() {
    fetch('/api/v1/events-view')
      .then(function(r) { if (!r.ok) throw new Error('events request failed'); return r.json(); })
      .then(showEvents)
      .catch(function() { /* events section stays empty; the board works */ });
  }

  // ---- queries section (Kimi data-query ledger from /api/v1/queries-view) ----
  var kqList = document.getElementById('kqlist');
  var kqCount = document.getElementById('kqcount');
  function showQueries(view) {
    if (!view || !Array.isArray(view.queries)) return;
    kqCount.textContent = '（' + (view.query_count || view.queries.length) + '）';
    kqList.innerHTML = view.queries.map(function(q) {
      var result = q.result || {};
      var params = Object.keys(q.params || {}).map(function(k) {
        return '<span class="src">' + esc(k) + '=' + esc(q.params[k]) + '</span>';
      }).join(' ');
      return '<div class="ev">'
        + '<span class="t">' + esc(q.api_id || q.source_id) + '</span>'
        + '<span class="src">' + esc(q.data_kind) + '</span>'
        + (result.outcome
            ? '<span class="tl ' + (result.outcome === 'ok' ? 'succeeded' : 'failed') + '">'
              + esc(result.outcome_label || result.outcome) + '</span>'
              + '<span class="src">费用:' + esc(result.cost_status || 'unknown') + '</span>'
              + (result.attempts ? '<span class="src">×' + esc(result.attempts) + '</span>' : '')
            : '<span class="muted">无发送（门禁拦截或未启用）</span>')
        + params
        + '<span class="when">' + esc(String(q.created_at || '').replace('T', ' ').slice(0, 16)) + '</span>'
        + '</div>';
    }).join('');
  }
  function fetchQueries() {
    fetch('/api/v1/queries-view')
      .then(function(r) { if (!r.ok) throw new Error('queries request failed'); return r.json(); })
      .then(showQueries)
      .catch(function() { /* queries section stays empty; the board works */ });
  }
  evList.addEventListener('click', function(ev) {
    var link = ev.target.closest ? ev.target.closest('[data-tasklink]') : null;
    if (link) openDetail(link.getAttribute('data-tasklink'));
  });
  todoList.addEventListener('click', function(ev) {
    var link = ev.target.closest ? ev.target.closest('[data-tasklink]') : null;
    if (link) openDetail(link.getAttribute('data-tasklink'));
  });
  opsList.addEventListener('click', function(ev) {
    var link = ev.target.closest ? ev.target.closest('[data-tasklink]') : null;
    if (link) openDetail(link.getAttribute('data-tasklink'));
  });

  // ---- task detail drawer (read-only projection of /api/v1/tasks/{id}) ----
  var detail = document.getElementById('detail');
  function showDetail(d) {
    if (!d || !d.task_id) { msg.textContent = 'invalid detail payload'; return; }
    var rounds = d.research_rounds || [];
    var delivery = d.evidence_delivery || {};
    var model = d.model_call || {};
    var origin = d.origin || {};
    var html = '<h2><span class="status-' + esc(d.status) + '">' + esc(d.status) + '</span>' +
      '<span>(' + (d.is_replay ? 'REPLAY' : 'LIVE') + ')</span><span class="close" data-detail-close title="close (Esc)">✕</span></h2>' +
      '<dl><dt>task</dt><dd>' + esc(d.task_id) + '</dd>' +
      '<dt>title</dt><dd>' + esc(d.title || '') + '</dd>' +
      '<dt>versions</dt><dd>input v' + esc(d.input_version) + ' · row #' + esc(d.row_version) +
      ' · attempts ' + esc((d.attempt_count || 0) + '/' + (d.max_attempts || 0)) + '</dd>' +
      '<dt>origin</dt><dd>' + esc(origin.kind || 'unknown') +
      (origin.event_key ? ' · ' + esc(origin.event_key) : '') +
      (origin.supersedes_task_id ? ' · supersedes ' + esc(origin.supersedes_task_id) : '') +
      (origin.prior_task_id ? ' · 关联前案 <span class="tl" data-tasklink="' + esc(origin.prior_task_id) +
        '" title="打开关联案件">' + esc(origin.prior_task_id) + '</span>' : '') +
      (origin.research_occasion ? ' · ' + esc(origin.research_occasion) : '') + '</dd>' +
      '<dt>question</dt><dd>' + esc(d.research_question || '') + '</dd>' +
      '<dt>symbols</dt><dd>' + esc((d.symbols || []).join(', ')) + '</dd></dl>' +
      '<div class="sec">evidence actually delivered</div>' +
      '<dl><dt>quotes</dt><dd>' + esc((delivery.delivered_quotes || []).join(', ') || 'none') + '</dd>' +
      '<dt>evidence</dt><dd>' + esc((delivery.delivered_evidence || []).join(', ') || 'none') + '</dd>' +
      '<dt>dropped</dt><dd>' + esc(((delivery.dropped_quotes || []).concat(delivery.dropped_evidence || [])).join('; ') || 'none') + '</dd></dl>' +
      '<div class="sec">research rounds (' + rounds.length + ')</div>' +
      rounds.map(function(r) {
        var intents = r.intent_results || [];
        return '<div class="round">round ' + esc(r.round) + ' · ' + esc(r.actual_model || 'unknown') +
          ' · ' + (r.final ? 'final' : 'provisional') +
          (intents.length ? ' · intents ' + intents.map(function(i) {
            return esc(i.action || '?') + ':' + esc(i.status || '?');
          }).join(', ') : '') + '</div>';
      }).join('') +
      '<div class="sec">model</div>' +
      '<dl><dt>request</dt><dd>' + esc(model.request_model || '') + '</dd>' +
      '<dt>actual</dt><dd>' + esc(model.actual_model || '') + (model.called ? '' : ' (not called)') + '</dd>' +
      '<dt>usage</dt><dd>' + esc(JSON.stringify(model.usage || {})) + '</dd></dl>' +
      (d.output && d.output.summary ? '<div class="sec">AI output</div><div>' + esc(String(d.output.summary).slice(0, 400)) + '</div>' : '') +
      ((d.pending_human_questions || []).length ? '<div class="sec">pending questions</div>' +
        (d.pending_human_questions || []).map(function(q) {
          return '<div class="round">' + esc(q.question || '') + '</div>';
        }).join('') : '') +
      ((d.input_versions || []).length ? '<div class="sec">输入版本（' + d.input_versions.length + '，只读）</div>' +
        d.input_versions.map(function(v) {
          var docs = v.evidence_documents || [];
          return '<div class="round">v' + esc(v.input_version) + ' · ' + esc(v.reason || '') +
            ' · ' + esc(String(v.created_at || '').replace('T', ' ').slice(0, 19)) +
            (v.followup_note ? '<div class="why">追问原文：' + esc(v.followup_note) + '</div>' : '') +
            (docs.length ? '<div class="why">所选原件：' + docs.map(function(id) { return esc(id); }).join(' · ') + '</div>' : '') +
            '</div>';
        }).join('') : '') +
      (d.personal_acknowledgement ? '<div class="sec">个人签收（非机构批准）</div>' +
        '<dl><dt>by</dt><dd>' + esc(d.personal_acknowledgement.actor || '') + '</dd>' +
        '<dt>at</dt><dd>' + esc(String(d.personal_acknowledgement.at || '').replace('T', ' ').slice(0, 19)) + '</dd>' +
        (d.personal_acknowledgement.notes ? '<dt>备注</dt><dd>' + esc(d.personal_acknowledgement.notes) + '</dd>' : '') +
        '<dt>范围</dt><dd>' + esc(d.personal_acknowledgement.scope || '') + '</dd></dl>' : '') +
      (d.last_error ? '<div class="sec">error</div><div>' + esc(d.last_error) + '</div>' : '') +
      '<p class="scope">' + esc(d.scope_note || '') + '</p>';
    detail.innerHTML = html;
    detail.hidden = false;
  }
  function closeDetail() { detail.hidden = true; detail.innerHTML = ''; }
  function openDetail(tid) {
    fetch('/api/v1/tasks/' + encodeURIComponent(tid))
      .then(function(r) { if (!r.ok) throw new Error('detail request failed'); return r.json(); })
      .then(showDetail)
      .catch(function() { msg.textContent = 'detail fetch failed for ' + tid; });
  }
  tbody.addEventListener('click', function(ev) {
    if (ev.target.closest && ev.target.closest('form')) return;  // never hijack feedback inputs
    if (ev.target.closest && ev.target.closest('button[data-ws]')) return;  // workspace actions, not detail
    var tr = ev.target.closest ? ev.target.closest('tr[data-task]') : null;
    if (tr) openDetail(tr.getAttribute('data-task'));
  });
  detail.addEventListener('click', function(ev) {
    if (ev.target.closest && ev.target.closest('[data-detail-close]')) closeDetail();
    var link = ev.target.closest ? ev.target.closest('[data-tasklink]') : null;
    if (link) openDetail(link.getAttribute('data-tasklink'));
  });
  document.addEventListener('keydown', function(ev) { if (ev.key === 'Escape') closeDetail(); });
  var linkedTask = new URLSearchParams(window.location.search).get('task');
  if (linkedTask) openDetail(linkedTask);
  var es = new EventSource('/api/v1/events');
  es.onopen = function() { conn.textContent = 'LIVE'; conn.className = 'live'; refresh(); fetchOverview(); fetchEvents(); fetchQueries(); fetchTodo(); fetchOps(); };
  es.onerror = function() { conn.textContent = 'RECONNECTING'; conn.className = 'down'; };
  es.onmessage = function(ev) { refresh(); };
  // Read-only hook for automated browser verification of the render contract
  // (SSR keys, form preservation, focus retention), the detail drawer and the
  // overview bar. They never mutate state.
  window.__taskBoardRender = render;
  window.__taskBoardShowDetail = showDetail;
  window.__taskBoardOverview = showOverview;
  window.__taskBoardEvents = showEvents;
  window.__taskBoardTodo = showTodo;
  window.__taskBoardOps = showOps;
  window.__taskBoardQueries = showQueries;
  window.__taskBoardShowDocuments = showDocuments;
  renderPersonalPanel();
  fetchOverview();
  fetchEvents();
  fetchQueries();
  fetchTodo();
  fetchOps();
  refresh();
})();
</script></body></html>"""


def _esc(value: str) -> str:
    return (
        value.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")
    )


def _is_loopback_host(host: str) -> bool:
    lowered = str(host or "").lower().split(":")[0]
    return lowered in {"127.0.0.1", "localhost", "[::1]", "::1"}


def create_task_board_server(store: TaskStore, host: str = "127.0.0.1", port: int = 8768, *,
                             config: Optional[Dict[str, Any]] = None,
                             sse_max_seconds: Optional[int] = None,
                             sse_keepalive_seconds: Optional[float] = None,
                             session_store: Optional[Any] = None,
                             login_authenticator: Optional[Any] = None) -> ThreadingHTTPServer:
    server = ThreadingHTTPServer((host, port), _TaskBoardHandler)
    server.task_store = store
    server.csrf_token = secrets.token_urlsafe(32)
    server.board_lock = threading.Lock()
    server.board_config = config or {}
    # Production bounds stay the module defaults; tests may tighten them to
    # exercise the slow-consumer behavior quickly.
    server.sse_max_seconds = int(sse_max_seconds or SSE_MAX_SECONDS)
    server.sse_keepalive_seconds = float(sse_keepalive_seconds or SSE_KEEPALIVE_SECONDS)
    # R10 personal sessions: when a session store is attached, protected
    # writes require a logged-in session and REJECT form-supplied actors
    # (A46). Without one the legacy form-actor path stays available
    # (rollback); production attaches the store + a provider authenticator.
    server.session_store = session_store
    server.login_authenticator = login_authenticator
    return server


class _TaskBoardHandler(BaseHTTPRequestHandler):
    server: "ThreadingHTTPServer"

    def log_message(self, fmt: str, *args: Any) -> None:
        return

    def _send(self, code: int, body: bytes, content_type: str, *,
              set_cookie: str = "") -> None:
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        if set_cookie:
            self.send_header("Set-Cookie", set_cookie)
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _send_json(self, code: int, payload: Dict[str, Any], *,
                   set_cookie: str = "") -> None:
        self._send(code, json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8"),
                   "application/json; charset=utf-8", set_cookie=set_cookie)

    def _session_cookie(self, name: str) -> str:
        from http.cookies import SimpleCookie

        raw = self.headers.get("Cookie", "")
        try:
            jar = SimpleCookie()
            jar.load(raw)
            return str(jar[name].value) if name in jar else ""
        except Exception:
            return ""

    def _handle_personal_login(self) -> None:
        """R10: authenticate a real principal via the attached provider and
        mint one server-side session. Fails closed when no session store or
        no authenticator is attached (rollback stays the CLI/Windows path)."""
        from .identity_providers import SESSION_COOKIE_NAME, PersonalSessionError

        sessions = getattr(self.server, "session_store", None)
        authenticator = getattr(self.server, "login_authenticator", None)
        if sessions is None or authenticator is None:
            self._send_json(503, {"error": "personal_sessions_unavailable",
                                  "detail": "no session store/authenticator attached; use the CLI entry"})
            return
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0 or length > 16384:
            self._send_json(400, {"error": "invalid body size"})
            return
        try:
            body = json.loads(self.rfile.read(length).decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            self._send_json(400, {"error": "invalid JSON"})
            return
        actor = str(body.get("actor") or "").strip()
        symbols = [str(s) for s in (body.get("symbols") or []) if str(s).strip()]
        provider = str(body.get("provider") or "windows_local")
        if not actor or not symbols:
            self._send_json(400, {"error": "actor and symbols are required"})
            return
        try:
            binding = authenticator(self.server.board_config, actor, symbols)
            session = sessions.login(
                actor=binding["actor"], provider=binding["provider"],
                subject=binding["subject"],
                policy_sha256=str(binding.get("policy_sha256") or ""))
        except (PersonalSessionError, ValueError) as exc:
            self._send_json(403, {"error": "login_failed", "detail": str(exc)})
            return
        self._send_json(200, {"status": "logged_in", "session": session.as_dict()},
                        set_cookie="{0}={1}; HttpOnly; SameSite=Strict; Path=/".format(
                            SESSION_COOKIE_NAME, session.session_id))

    def _handle_personal_logout(self) -> None:
        """R10/A48: logout revokes NOW — the next protected action fails."""
        from .identity_providers import SESSION_COOKIE_NAME

        sessions = getattr(self.server, "session_store", None)
        if sessions is None:
            self._send_json(503, {"error": "personal_sessions_unavailable"})
            return
        cookie = self._session_cookie(SESSION_COOKIE_NAME)
        revoked = sessions.logout(cookie) if cookie else False
        self._send_json(200, {"status": "logged_out", "revoked": bool(revoked)},
                        set_cookie="{0}=; HttpOnly; SameSite=Strict; Path=/; Max-Age=0".format(
                            SESSION_COOKIE_NAME))

    def _resolve_session(self) -> Optional[Any]:
        """Resolve the caller's session, or None when absent/invalid."""
        from .identity_providers import SESSION_COOKIE_NAME, PersonalSessionError

        sessions = getattr(self.server, "session_store", None)
        if sessions is None:
            return None
        try:
            return sessions.resolve(self._session_cookie(SESSION_COOKIE_NAME))
        except PersonalSessionError:
            return None

    def _handle_personal_session(self) -> None:
        """Read-only login state for the page (A93): never mutates anything."""
        if getattr(self.server, "session_store", None) is None:
            self._send_json(200, {"logged_in": False, "sessions_available": False})
            return
        session = self._resolve_session()
        if session is None:
            self._send_json(200, {"logged_in": False, "sessions_available": True})
            return
        self._send_json(200, {"logged_in": True, "sessions_available": True,
                              "actor": session.actor, "provider": session.provider,
                              "expires_at": getattr(session, "expires_at", "")})

    def _handle_personal_documents(self) -> None:
        """A93: the owner's personal namespace documents (read-only list)."""
        from .personal_evidence import personal_documents, PersonalEvidenceError

        if getattr(self.server, "session_store", None) is None:
            self._send_json(503, {"error": "workspace_requires_sessions"})
            return
        session = self._resolve_session()
        if session is None:
            self._send_json(403, {"error": "session_required"})
            return
        try:
            view = personal_documents(self.server.board_config, session.actor)
        except PersonalEvidenceError as exc:
            self._send_json(400, {"error": "personal_documents_failed", "detail": str(exc)})
            return
        documents = [
            {
                "document_id": str(item.get("document_id") or ""),
                "title": str(item.get("title") or ""),
                "sha256": str(item.get("sha256") or ""),
                "source_url": str(item.get("source_url") or ""),
                "published_at": str(item.get("published_at") or ""),
                "content_type": str(item.get("content_type") or ""),
            }
            for item in view.get("documents") or []
        ]
        self._send_json(200, {"owner": view.get("owner"),
                              "namespace": view.get("namespace"),
                              "documents": documents})

    def _handle_library(self, timeline: bool = False) -> None:
        """R20 retrieval: shared research plus the session's OWN personal
        research, revocation-checked against the current authorized vaults
        on every request. The principal comes from the session; the caller
        cannot name an actor or widen the scope. Stale rows are returned
        only to an authorized auditor (``include_stale=1``), never on the
        strength of the request alone. ``timeline`` narrows one case to its
        original report and the continuations linked to it."""
        from urllib.parse import parse_qs as _parse_qs

        from .personal_evidence import PersonalEvidenceError
        from .research_library import (
            LibraryError,
            LibraryTimeWindowError,
            build_library_index,
            query_library,
        )

        if getattr(self.server, "session_store", None) is None:
            self._send_json(503, {"error": "workspace_requires_sessions"})
            return
        session = self._resolve_session()
        if session is None:
            self._send_json(403, {"error": "session_required"})
            return
        params = _parse_qs(urlparse(self.path).query, keep_blank_values=True)

        def first(name, limit=500):
            return str((params.get(name) or [""])[0])[:limit]

        if "actor" in params or "owner" in params:
            self._send_json(403, {"error": "actor_from_session_only"})
            return
        task_id = first("task_id", 120)
        if timeline and not task_id:
            self._send_json(400, {"error": "timeline requires task_id"})
            return
        symbols = [item.strip() for item in first("symbols", 400).split(",") if item.strip()]
        store: TaskStore = self.server.task_store
        with self.server.board_lock:
            try:
                result = query_library(
                    build_library_index(store), self.server.board_config, session.actor,
                    query=first("query"), symbols=symbols or None,
                    # A timeline is keyed by its case, not narrowed to it.
                    task_id="" if timeline else task_id,
                    since=first("since", 40), until=first("until", 40),
                    status=first("status", 40),
                    include_stale=first("include_stale", 8) in {"1", "true"},
                    timeline_of=task_id if timeline else "")
            except LibraryTimeWindowError as exc:
                # Contract: since/until are offset-bearing timestamps
                # (compared as instants) or bare calendar dates (the whole
                # day, inclusive); anything else is a 400, never a guess.
                self._send_json(400, {"error": "invalid_time_window", "detail": str(exc)})
                return
            except (LibraryError, PersonalEvidenceError) as exc:
                self._send_json(400, {"error": "library query failed", "detail": str(exc)})
                return
            except Exception as exc:  # the vault projection failed: no partial answer
                self._send_json(503, {"error": "library unavailable: {0}".format(type(exc).__name__)})
                return
        self._send_json(200, result)

    def _handle_personal_import(self) -> None:
        """A93 page wiring: import one file into the owner's personal
        namespace through the R12a service (never the institutional vault).

        The session names the owner; CSRF is enforced like every POST. The
        upload arrives as JSON (filename + base64 body) so the whole contract
        stays stdlib JSON, matching the other board routes."""
        import base64
        import binascii
        import uuid
        from pathlib import Path

        from .identity_providers import SESSION_COOKIE_NAME, PersonalSessionError
        from .personal_evidence import (
            PersonalEvidenceError,
            import_personal_document,
            personal_root,
        )

        sessions = getattr(self.server, "session_store", None)
        if sessions is None:
            self._send_json(503, {"error": "workspace_requires_sessions"})
            return
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0 or length > 8 * 1024 * 1024:
            self._send_json(400, {"error": "invalid body size"})
            return
        try:
            body = json.loads(self.rfile.read(length).decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            self._send_json(400, {"error": "invalid JSON"})
            return
        if not hmac.compare_digest(str(self.headers.get("X-Task-Board-CSRF", "")),
                                   str(self.server.csrf_token)):
            self._send_json(403, {"error": "csrf validation failed"})
            return
        try:
            session = sessions.resolve(self._session_cookie(SESSION_COOKIE_NAME))
        except PersonalSessionError as exc:
            self._send_json(403, {"error": "session_required", "detail": str(exc)})
            return
        if "actor" in body:
            self._send_json(403, {"error": "actor_from_session_only"})
            return
        try:
            decoded = base64.b64decode(
                str(body.get("content_base64") or ""), validate=True)
        except (binascii.Error, ValueError):
            self._send_json(400, {"error": "content_base64 is not valid base64"})
            return
        if not decoded:
            self._send_json(400, {"error": "content_base64 is empty"})
            return
        if len(decoded) > 2 * 1024 * 1024:
            self._send_json(400, {"error": "file exceeds the 2MB personal import limit"})
            return
        filename = Path(str(body.get("filename") or "announcement.html")).name
        source_url = str(body.get("source_url") or "").strip()
        title = str(body.get("title") or "").strip()
        published_at = str(body.get("published_at") or "").strip()
        if not (source_url.startswith("https://") and title and published_at):
            self._send_json(400, {"error": "source_url (https), title and published_at are required"})
            return
        incoming = personal_root(self.server.board_config, session.actor) / "incoming"
        incoming.mkdir(parents=True, exist_ok=True)
        staged = incoming / "{0}-{1}".format(uuid.uuid4().hex[:12], filename)
        staged.write_bytes(decoded)
        try:
            imported = import_personal_document(
                self.server.board_config, session.actor, str(staged),
                source_url=source_url, title=title, published_at=published_at)
        except (PersonalEvidenceError, ValueError) as exc:
            self._send_json(400, {"error": "personal_import_rejected",
                                  "detail": str(exc)})
            return
        finally:
            try:
                staged.unlink()
            except OSError:
                pass
        document = imported.get("document") or {}
        self._send_json(200, {"status": "imported",
                              "document": {
                                  "document_id": str(document.get("document_id") or ""),
                                  "title": str(document.get("title") or ""),
                                  "sha256": str(document.get("sha256") or ""),
                                  "content_type": str(document.get("content_type") or ""),
                              }})

    def _handle_workspace(self, action: str) -> None:
        """R14 workspace actions: create / cancel / followup / rerun.

        Session mode is REQUIRED for all four (the session names the owner);
        CSRF is enforced exactly like every other POST."""
        from .identity_providers import SESSION_COOKIE_NAME, PersonalSessionError
        from . import personal_workspace

        sessions = getattr(self.server, "session_store", None)
        if sessions is None:
            self._send_json(503, {"error": "workspace_requires_sessions"})
            return
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0 or length > 65536:
            self._send_json(400, {"error": "invalid body size"})
            return
        try:
            body = json.loads(self.rfile.read(length).decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            self._send_json(400, {"error": "invalid JSON"})
            return
        if not hmac.compare_digest(str(self.headers.get("X-Task-Board-CSRF", "")),
                                   str(self.server.csrf_token)):
            self._send_json(403, {"error": "csrf validation failed"})
            return
        try:
            session = sessions.resolve(self._session_cookie(SESSION_COOKIE_NAME))
        except PersonalSessionError as exc:
            self._send_json(403, {"error": "session_required", "detail": str(exc)})
            return
        if "actor" in body:
            # Same A46 rule: the principal comes from the session.
            self._send_json(403, {"error": "actor_from_session_only"})
            return
        store: TaskStore = self.server.task_store
        config = self.server.board_config
        try:
            if action == "create":
                result = personal_workspace.create_personal_research(
                    store, actor=session.actor,
                    question=str(body.get("question") or ""),
                    symbols=[str(s) for s in (body.get("symbols") or [])],
                    execution_mode=str(body.get("execution_mode") or "replay"),
                    config=config,
                    document_ids=[str(d) for d in (body.get("document_ids") or [])],
                    # R15: excerpt selectors the user approved from their own
                    # namespace; validated against the owner's personal vault
                    # view by the workspace service before anything is created.
                    fact_selectors=list(body.get("fact_selectors") or []))
            elif action == "cancel":
                result = {"task": personal_workspace.cancel_personal_task(
                    store, actor=session.actor,
                    task_id=str(body.get("task_id") or ""),
                    expected_version=int(body.get("expected_version") or 0),
                    reason=str(body.get("reason") or ""))}
            elif action == "followup":
                # A64 while awaiting (same task, input version bump) vs A90
                # on a finished no-question result (new linked task).
                follow_task = store.get_task(str(body.get("task_id") or ""))
                if follow_task["status"] == "awaiting_human":
                    result = {"task": personal_workspace.follow_up_awaiting(
                        store, actor=session.actor,
                        task_id=str(body.get("task_id") or ""),
                        expected_version=int(body.get("expected_version") or 0),
                        note=str(body.get("note") or ""),
                        extra_note=str(body.get("extra_note") or ""),
                        command_id=str(body.get("command_id") or ""))}
                else:
                    followed = personal_workspace.follow_up_completed_research(
                        store, actor=session.actor,
                        task_id=str(body.get("task_id") or ""),
                        note=str(body.get("extra_note") or body.get("note") or ""),
                        config=config,
                        # The page's request id: a lost-response retry of ONE
                        # submission reuses the task the intent already named.
                        intent_id=str(body.get("command_id") or ""))
                    # Shape like the intake result so the generic response
                    # tail reports outcome + the followed-up task as prior.
                    result = {"task": followed.get("task") or followed,
                              "status": followed.get("status"),
                              "supersedes_task_id": str(body.get("task_id") or "")}
            elif action == "acknowledge":
                # A90/A50: personal sign-off on an already-succeeded result
                # (the awaiting_human path stays on /api/v1/human-responses).
                result = {"task": personal_workspace.acknowledge_completed_research(
                    store, actor=session.actor,
                    task_id=str(body.get("task_id") or ""),
                    expected_version=int(body.get("expected_version") or 0),
                    note=str(body.get("notes") or body.get("note") or ""))}
            else:  # rerun
                result = personal_workspace.rerun_completed_research(
                    store, actor=session.actor,
                    task_id=str(body.get("task_id") or ""),
                    config=config,
                    execution_mode=str(body.get("execution_mode") or "replay"))
        except personal_workspace.WorkspaceError as exc:
            self._send_json(400, {"error": str(exc).split(":", 1)[0],
                                  "detail": str(exc)})
            return
        except (TaskVersionConflictError, TaskRuntimeError) as exc:
            self._send_json(409, {"error": type(exc).__name__, "detail": str(exc)})
            return
        task = result.get("task") or {}
        payload = {"status": "ok", "outcome": result.get("status"),
                   "task_id": str(task.get("task_id") or "")}
        if "supersedes_task_id" in result:
            payload["prior_task_id"] = result.get("supersedes_task_id")
        for count_key in ("documents_attached", "facts_attached"):
            if result.get(count_key) is not None:
                payload[count_key] = result[count_key]
        if task:
            payload["task"] = task
        self._send_json(200, payload)

    def _valid_loopback(self) -> bool:
        host = self.headers.get("Host", "")
        if not _is_loopback_host(host):
            return False
        origin = self.headers.get("Origin", "")
        if origin and not _is_loopback_host(urlparse(origin).netloc):
            return False
        return True

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        if not self._valid_loopback():
            self._send_json(403, {"error": "loopback only"})
            return
        store: TaskStore = self.server.task_store
        if parsed.path == "/health":
            self._send_json(200, {"status": "ok", "pid": __import__("os").getpid(), "board_version": _BOARD_VERSION})
            return
        if parsed.path == "/api/v1/tasks":
            with self.server.board_lock:
                self._send_json(200, _board_state(store, self.server.board_config))
            return
        if parsed.path == "/api/v1/overview":
            from .overview import build_overview

            with self.server.board_lock:
                try:
                    payload = build_overview(self.server.board_config, store=store)
                except Exception as exc:  # overview is a projection; degrade honestly
                    self._send_json(503, {"error": "overview unavailable: {0}".format(type(exc).__name__)})
                    return
            self._send_json(200, payload)
            return
        if parsed.path == "/api/v1/ops":
            from .ops_view import build_ops_view

            with self.server.board_lock:
                try:
                    payload = build_ops_view(self.server.board_config, store=store)
                except Exception as exc:  # projection; degrade honestly
                    self._send_json(503, {"error": "ops view unavailable: {0}".format(type(exc).__name__)})
                    return
            self._send_json(200, payload)
            return
        if parsed.path == "/api/v1/todo":
            from .todo_view import build_todo_view

            with self.server.board_lock:
                try:
                    payload = build_todo_view(self.server.board_config, store=store)
                except Exception as exc:  # projection; degrade honestly
                    self._send_json(503, {"error": "todo view unavailable: {0}".format(type(exc).__name__)})
                    return
            self._send_json(200, payload)
            return
        if parsed.path == "/api/v1/events-view":
            # NOT /api/v1/events: that URL is the SSE stream contract frozen
            # by ADR-001 and must keep streaming.
            from .events_view import build_events_view

            with self.server.board_lock:
                try:
                    payload = build_events_view(self.server.board_config, store=store)
                except Exception as exc:  # projection; degrade honestly
                    self._send_json(503, {"error": "events view unavailable: {0}".format(type(exc).__name__)})
                    return
            self._send_json(200, payload)
            return
        if parsed.path == "/api/v1/queries-view":
            # Kimi data-query ledger projection: read-only, never triggers a
            # gateway call or quota consumption.
            from .queries_view import build_queries_view

            with self.server.board_lock:
                try:
                    payload = build_queries_view(self.server.board_config)
                except Exception as exc:  # projection; degrade honestly
                    self._send_json(503, {"error": "queries view unavailable: {0}".format(type(exc).__name__)})
                    return
            self._send_json(200, payload)
            return
        # ---- R20 research library (session mode only) ----
        if parsed.path == "/api/v1/library":
            self._handle_library()
            return
        if parsed.path == "/api/v1/library/timeline":
            self._handle_library(timeline=True)
            return
        if parsed.path.startswith("/api/v1/tasks/"):
            task_id = parsed.path[len("/api/v1/tasks/"):]
            from .task_detail import task_detail

            with self.server.board_lock:
                try:
                    detail = task_detail(store, task_id, config=self.server.board_config)
                except TaskRuntimeError:
                    self._send_json(404, {"error": "unknown task_id"})
                    return
            self._send_json(200, detail)
            return
        # ---- A93 personal workspace wiring (session mode only) ----
        if parsed.path == "/api/v1/personal/session":
            self._handle_personal_session()
            return
        if parsed.path == "/api/v1/personal/documents":
            self._handle_personal_documents()
            return
        if parsed.path == "/api/v1/events":
            self._stream_events()
            return
        if parsed.path == "/":
            session_mode = getattr(self.server, "session_store", None) is not None
            session_actor = ""
            if session_mode:
                session = self._resolve_session()
                session_actor = str(getattr(session, "actor", "") or "")
            with self.server.board_lock:
                state = _board_state(store, self.server.board_config)
            self._send(200, _render_html(state, str(self.server.csrf_token),
                                         session_mode=session_mode,
                                         session_actor=session_actor).encode("utf-8"),
                       "text/html; charset=utf-8")
            return
        self._send_json(404, {"error": "not found"})

    def _stream_events(self) -> None:
        """Server-sent events from the task outbox with Last-Event-ID resume.

        ADR-001 stage one: only public task-domain projections are pushed; the
        stream is bounded (SSE_MAX_SECONDS) so a slow client reconnects with
        its last received id instead of pinning a worker thread forever. A
        write failure on a dead connection ends the stream immediately.
        """

        import time as _time

        store: TaskStore = self.server.task_store
        try:
            last_id = int(self.headers.get("Last-Event-ID") or 0)
        except ValueError:
            last_id = 0
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "close")
        self.end_headers()
        deadline = _time.monotonic() + getattr(self.server, "sse_max_seconds", SSE_MAX_SECONDS)
        keepalive = getattr(self.server, "sse_keepalive_seconds", SSE_KEEPALIVE_SECONDS)
        idle_since = _time.monotonic()
        try:
            while _time.monotonic() < deadline:
                try:
                    with self.server.board_lock:
                        events = store.events_after(last_id, limit=50)
                except Exception:
                    # The store was closed (server shutdown); end the stream.
                    return
                for event in events:
                    last_id = int(event["sequence"])
                    message = {
                        "sequence": event["sequence"],
                        "event_id": event["event_id"],
                        "event_type": event["event_type"],
                        "task_id": event["task_id"],
                        "actor": event["actor"],
                        "created_at": event["created_at"],
                    }
                    body = json.dumps(message, ensure_ascii=False, default=str)
                    # No named `event:` line: default (message) events let the
                    # browser's EventSource.onmessage handle every type with
                    # one handler; the type travels inside the JSON payload.
                    self.wfile.write("id: {0}\ndata: {1}\n\n".format(last_id, body).encode("utf-8"))
                    self.wfile.flush()
                    idle_since = _time.monotonic()
                if _time.monotonic() - idle_since >= keepalive:
                    self.wfile.write(b": keepalive\n\n")
                    self.wfile.flush()
                    idle_since = _time.monotonic()
                _time.sleep(SSE_POLL_SECONDS)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, OSError):
            return

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        if not self._valid_loopback():
            self._send_json(403, {"error": "loopback only"})
            return
        # ---- R10 personal session routes (fail closed without a store) ----
        if parsed.path == "/api/v1/personal/login":
            self._handle_personal_login()
            return
        if parsed.path == "/api/v1/personal/logout":
            self._handle_personal_logout()
            return
        # ---- A93: personal source import (session + CSRF, R12a service) ----
        if parsed.path == "/api/v1/personal/import":
            self._handle_personal_import()
            return
        # ---- R14 workspace routes (session mode enforced, CSRF required) ----
        if parsed.path == "/api/v1/workspace/create":
            self._handle_workspace("create")
            return
        if parsed.path == "/api/v1/workspace/cancel":
            self._handle_workspace("cancel")
            return
        if parsed.path == "/api/v1/workspace/followup":
            self._handle_workspace("followup")
            return
        if parsed.path == "/api/v1/workspace/acknowledge":
            self._handle_workspace("acknowledge")
            return
        if parsed.path == "/api/v1/workspace/rerun":
            self._handle_workspace("rerun")
            return
        if parsed.path != "/api/v1/human-responses":
            self._send_json(404, {"error": "not found"})
            return
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0 or length > 65536:
            self._send_json(400, {"error": "invalid body size"})
            return
        raw = self.rfile.read(length).decode("utf-8", "replace")
        header_token = self.headers.get("X-Task-Board-CSRF", "")
        expected = str(self.server.csrf_token)
        supplied = header_token
        if not supplied:
            try:
                from urllib.parse import parse_qs

                values = parse_qs(raw).get("csrf_token") or []
                supplied = values[-1] if values else ""
            except Exception:
                supplied = ""
        if not supplied or not expected or not hmac.compare_digest(str(supplied), expected):
            self._send_json(403, {"error": "csrf validation failed"})
            return
        try:
            if header_token:
                body = json.loads(raw)
            else:
                from urllib.parse import parse_qs

                form = {key: (vals[-1] if vals else "") for key, vals in parse_qs(raw).items()}
                body = form
        except json.JSONDecodeError:
            self._send_json(400, {"error": "body must be JSON or a form"})
            return
        task_id = str(body.get("task_id") or "")
        decision = str(body.get("decision") or "")
        actor = str(body.get("actor") or "")
        # ---- R10/A46: session mode makes the principal SERVER-SIDE ----
        if getattr(self.server, "session_store", None) is not None:
            from .identity_providers import SESSION_COOKIE_NAME, PersonalSessionError

            if actor:
                # A form actor is not merged, not honored — it is rejected so
                # a forged name can never ride a valid session (A46).
                self._send_json(403, {"error": "actor_from_session_only",
                                      "detail": "supply no actor field; the session names the principal"})
                return
            cookie = self._session_cookie(SESSION_COOKIE_NAME)
            try:
                session = self.server.session_store.resolve(cookie)
            except PersonalSessionError as exc:
                self._send_json(403, {"error": "session_required",
                                      "detail": str(exc)})
                return
            actor = session.actor
        try:
            expected_version = int(body.get("expected_version") or 0)
        except (TypeError, ValueError):
            self._send_json(400, {"error": "expected_version must be an integer"})
            return
        if decision not in HUMAN_DECISIONS:
            self._send_json(400, {"error": "decision must be one of {0}".format(sorted(HUMAN_DECISIONS))})
            return
        input_patch: Optional[Dict[str, Any]] = None
        if decision == "provide_input":
            patch_text = str(body.get("input_patch") or "")
            try:
                input_patch = json.loads(patch_text) if patch_text.strip() else None
            except json.JSONDecodeError:
                self._send_json(400, {"error": "input_patch must be a JSON object"})
                return
        store: TaskStore = self.server.task_store
        try:
            with self.server.board_lock:
                task = store.record_human_response(
                    task_id,
                    actor=actor,
                    decision=decision,
                    expected_version=expected_version,
                    command_id=str(body.get("command_id") or ""),
                    input_patch=input_patch,
                    notes=str(body.get("notes") or ""),
                )
        except (TaskVersionConflictError, TaskIdempotencyConflictError) as exc:
            self._send_json(409, {"error": str(exc)})
            return
        except TaskRuntimeError as exc:
            self._send_json(400, {"error": str(exc)})
            return
        self._send_json(200, {
            "status": "recorded",
            "task": task,
            "scope_note": "personal acknowledgement is not institutional approval",
        })


def _session_components():
    from .identity_providers import PersonalSessionStore, authenticate_with_provider
    from .identity_providers.sessions import DEFAULT_SESSION_TTL_SECONDS
    from .identity_providers.providers import PROVIDER_WINDOWS_LOCAL

    return DEFAULT_SESSION_TTL_SECONDS, PersonalSessionStore, authenticate_with_provider, PROVIDER_WINDOWS_LOCAL


def serve_task_board(config: Dict[str, Any], host: str = "127.0.0.1", port: int = 8768, *,
                   ready: Optional[Any] = None) -> None:
    """Serve the board until interrupted.

    ``ready(server)`` runs once the socket is bound and before the serve
    loop starts, so a caller (a test, a supervisor) can learn the bound port
    (``port=0``) and drive the real server."""
    db_path = project_path(
        str((config.get("task_runtime") or {}).get("db_path") or "data/task_runtime/tasks.sqlite3")
    )
    store = TaskStore(db_path)
    # Sessions are part of the supported start: the personal workspace and
    # the R20 research library both resolve the caller from a server-side
    # session, so the standard entry attaches the session store and the
    # provider-registry authenticator. ``task_board.sessions_enabled=false``
    # keeps the explicit rollback: no store, and every session route fails
    # closed (503) exactly as before.
    (DEFAULT_SESSION_TTL_SECONDS, PersonalSessionStore,
     authenticate_with_provider, PROVIDER_WINDOWS_LOCAL) = _session_components()
    board = config.get("task_board") or {}
    provider = str(board.get("identity_provider") or PROVIDER_WINDOWS_LOCAL)
    sessions = None
    if board.get("sessions_enabled", True):
        sessions = PersonalSessionStore(
            ttl_seconds=int(board.get("session_ttl_seconds") or DEFAULT_SESSION_TTL_SECONDS))
    server = create_task_board_server(
        store, host=host, port=port, config=config, session_store=sessions,
        login_authenticator=(lambda cfg, actor, symbols: authenticate_with_provider(
            provider, cfg, actor, symbols)) if sessions is not None else None)
    host_, port_ = server.server_address[:2]
    print("task board: http://{0}:{1}/ (sessions {2})".format(
        host_, port_, "on" if sessions is not None else "off"), flush=True)
    if ready is not None:
        ready(server)
    try:
        server.serve_forever()
    finally:
        server.server_close()
        store.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="Serve the read-only research task board")
    parser.add_argument("--config", default="config/watchlist.local.json")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8768)
    args = parser.parse_args()
    from .config import load_config

    serve_task_board(load_config(args.config), host=args.host, port=args.port)


if __name__ == "__main__":
    main()
