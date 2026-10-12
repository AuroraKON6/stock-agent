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
from typing import Any, Dict, Optional
from urllib.parse import urlparse

from .config import project_path
from .models import now_iso
from .task_runtime import (
    STATUS_AWAITING_HUMAN,
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


def _render_html(state: Dict[str, Any], csrf_token: str) -> str:
    rows = []
    for task in state.get("tasks", []):
        mode = "REPLAY" if task.get("is_replay") else "LIVE"
        task_id = _esc(str(task.get("task_id") or ""))
        version = _esc(str(task.get("version") or ""))
        # SSR rows carry the SAME task keys the client renderer uses so the
        # first client render recognizes them instead of duplicating rows.
        row = '<tr data-task="{7}" data-version="{5}"><td>{0}</td><td>{1}</td><td class=\'status-{2}\'>{2}</td><td>{3}</td><td>v{4}</td><td>#{5}</td><td>{6}</td></tr>'.format(
            _esc(str(task.get("task_id") or "")),
            _esc(mode),
            _esc(str(task.get("status") or "")),
            _esc(str(task.get("title") or "")) + '<small class="summary-state">Summary: ' + _esc(str((task.get("summary_review") or {}).get("status") or "not_submitted")) + '</small>',
            _esc(str(task.get("input_version") or "")),
            version,
            _esc(str((task.get("model_call") or {}).get("error") or (task.get("last_error") or ""))[:120]),
            task_id,
        )
        human_request = task.get("human_request") or {}
        if task.get("status") == STATUS_AWAITING_HUMAN and human_request.get("question"):
            row += (
                '<tr data-taskform="{0}"><td colspan="7"><div class="feedback"><em>{1}</em>'
                '<form data-task="{0}" data-version="{2}">'
                '<input name="actor" placeholder="actor" required>'
                '<select name="decision"><option>acknowledge</option><option>reject</option><option>provide_input</option></select>'
                '<input name="input_patch" placeholder="JSON patch (provide_input)">'
                '<input name="notes" placeholder="notes"><button type="submit">Submit</button></form></div></td></tr>'
            ).format(task_id, _esc(str(human_request.get("question") or "")), version)
        rows.append(row)
    page = _PAGE_TEMPLATE.replace("__ROWS__", "".join(rows)).replace("__CSRF_JSON__", json.dumps(str(csrf_token)))
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
</style></head><body>
<h1>Research Task Board <span id="conn" class="down">CONNECTING</span></h1>
<p class="note">REPLAY rows used recorded model responses, not live calls.
Personal acknowledgement is a personal research acknowledgement, not institutional approval.</p>
<p id="msg"></p>
<div id="overview" class="ovbar" hidden aria-label="Workbench overview"></div>
<details id="todo" class="evsec" open>
  <summary>待办<span id="todocount"></span></summary>
  <div id="todolist"></div>
</details>
<details id="events" class="evsec" open>
  <summary>事件（去重，72h）<span id="evcount"></span></summary>
  <div id="evlist"></div>
</details>
<details id="ops" class="evsec">
  <summary>运行（引擎/预算/登记/投影）</summary>
  <div id="opslist"></div>
</details>
<div class="table-wrap" role="region" aria-label="Research tasks" tabindex="0"><table><thead><tr><th>task_id</th><th>mode</th><th>status</th><th>title</th><th>input_v</th><th>row_v</th><th>last error</th></tr></thead>
<tbody id="tasks">__ROWS__
</tbody></table></div>
<aside id="detail" hidden aria-label="Task detail"></aside>
<script>
(function() {
  var CSRF = __CSRF_JSON__;
  var tbody = document.getElementById('tasks');
  var conn = document.getElementById('conn');
  var msg = document.getElementById('msg');
  function esc(s) {
    return String(s == null ? '' : s).replace(/[&<>"']/g, function(c) {
      return {'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c];
    });
  }
  function rowHtml(t) {
    var mode = t.is_replay ? 'REPLAY' : 'LIVE';
    var err = ((t.model_call || {}).error || t.last_error || '').slice(0, 120);
    var base = '<tr data-task="' + esc(t.task_id) + '" data-version="' + esc(t.version) + '"><td>' + esc(t.task_id) + '</td><td>' + esc(mode) + '</td>' +
      '<td class="status-' + esc(t.status) + '">' + esc(t.status) + '</td><td>' + esc(t.title) +
      '<small class="summary-state">Summary: ' + esc((t.summary_review || {}).status || 'not_submitted') + '</small></td>' +
      '<td>v' + esc(t.input_version) + '</td><td>#' + esc(t.version) + '</td><td>' + esc(err) + '</td></tr>';
    if (t.status === 'awaiting_human' && t.human_request && t.human_request.question) {
      base += '<tr data-taskform="' + esc(t.task_id) + '"><td colspan="7"><div class="feedback"><em>' + esc(t.human_request.question) + '</em>' +
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
  function showOverview(o) {
    if (!o || typeof o !== 'object') return;
    var engine = (o.engine || {}).event_intake_mode || '?';
    var market = o.market || {};
    var quotes = market.quotes || {};
    var missing = market.quotes_missing || [];
    var events = o.events || {};
    var tasks = o.tasks || {};
    var counts = tasks.status_counts || {};
    var awaiting = tasks.awaiting_human || [];
    var budget = o.budget || {};
    function chip(html) { return '<span class="chip">' + html + '</span>'; }
    var html = '';
    html += chip('engine <b>' + esc(engine) + '</b>');
    html += chip('quotes <b class="' + (missing.length && !Object.keys(quotes).length ? 'bad' : 'ok') + '">'
      + Object.keys(quotes).length + '</b>' + (missing.length
        ? ' / missing <b class="bad">' + esc(missing.length) + '</b>' : ''));
    html += chip('events 24h <b>' + esc(events.eligible_count || 0) + '</b>');
    html += chip('queued <b>' + esc(counts.queued || 0) + '</b>');
    html += chip('awaiting human <b class="' + (awaiting.length ? 'bad' : 'ok') + '">'
      + esc(awaiting.length) + '</b>');
    html += chip('model calls today <b>' + esc(budget.calls_today || 0) + '</b>'
      + (budget.enabled ? ' / limit ' + esc(budget.limit) : ' <span class="ok">(budget off)</span>'));
    overviewBar.innerHTML = html;
    overviewBar.hidden = false;
  }
  function fetchOverview() {
    fetch('/api/v1/overview')
      .then(function(r) { if (!r.ok) throw new Error('overview request failed'); return r.json(); })
      .then(showOverview)
      .catch(function() { /* overview stays hidden; the board itself still works */ });
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
      (origin.supersedes_task_id ? ' · supersedes ' + esc(origin.supersedes_task_id) : '') + '</dd>' +
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
    var tr = ev.target.closest ? ev.target.closest('tr[data-task]') : null;
    if (tr) openDetail(tr.getAttribute('data-task'));
  });
  detail.addEventListener('click', function(ev) {
    if (ev.target.closest && ev.target.closest('[data-detail-close]')) closeDetail();
  });
  document.addEventListener('keydown', function(ev) { if (ev.key === 'Escape') closeDetail(); });
  var es = new EventSource('/api/v1/events');
  es.onopen = function() { conn.textContent = 'LIVE'; conn.className = 'live'; refresh(); fetchOverview(); fetchEvents(); fetchTodo(); fetchOps(); };
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
  fetchOverview();
  fetchEvents();
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


def create_task_board_server(store: TaskStore, host: str = "127.0.0.1", port: int = 8768, *, config: Optional[Dict[str, Any]] = None) -> ThreadingHTTPServer:
    server = ThreadingHTTPServer((host, port), _TaskBoardHandler)
    server.task_store = store
    server.csrf_token = secrets.token_urlsafe(32)
    server.board_lock = threading.Lock()
    server.board_config = config or {}
    return server


class _TaskBoardHandler(BaseHTTPRequestHandler):
    server: "ThreadingHTTPServer"

    def log_message(self, fmt: str, *args: Any) -> None:
        return

    def _send(self, code: int, body: bytes, content_type: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _send_json(self, code: int, payload: Dict[str, Any]) -> None:
        self._send(code, json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8"), "application/json; charset=utf-8")

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
        if parsed.path == "/api/v1/events":
            self._stream_events()
            return
        if parsed.path == "/":
            with self.server.board_lock:
                state = _board_state(store, self.server.board_config)
            self._send(200, _render_html(state, str(self.server.csrf_token)).encode("utf-8"), "text/html; charset=utf-8")
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
        deadline = _time.monotonic() + SSE_MAX_SECONDS
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
                if _time.monotonic() - idle_since >= SSE_KEEPALIVE_SECONDS:
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


def serve_task_board(config: Dict[str, Any], host: str = "127.0.0.1", port: int = 8768) -> None:
    db_path = project_path(
        str((config.get("task_runtime") or {}).get("db_path") or "data/task_runtime/tasks.sqlite3")
    )
    store = TaskStore(db_path)
    server = create_task_board_server(store, host=host, port=port, config=config)
    print("task board: http://{0}:{1}/".format(host, port))
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
