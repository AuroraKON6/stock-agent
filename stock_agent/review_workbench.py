from __future__ import annotations

import hmac
import html
import json
import os
import secrets
import socket
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict, List, Sequence, Tuple
from urllib.parse import parse_qs, quote_plus, unquote_plus, urlparse

from .alerts import generate_notifications
from .config import project_path
from .dashboard import generate_dashboard
from .llm_governance import load_latest_governance_summary
from .models import now_iso
from .research_runtime import (
    DEFAULT_COMPLIANCE_CHECKS,
    FORBIDDEN_ACTION_TERMS,
    SAFETY_BOUNDARY,
    STAGE_ORDER,
    VALID_ACTIONS,
    load_research_runtime,
    record_runtime_action,
    summarize_research_runtime,
)
from .research_operations import build_research_operations_snapshot, operations_config
from .review import compute_review_summary, write_review_summary
from .review_priority import build_priority_queue, write_priority_queue
from .review_queue import OPEN_STATUSES, TRANSITIONS, build_review_queue, record_review_queue_action
from .production_pilot import load_latest_production_pilot
from .pi_runtime import pi_runtime_status
from .source_remediation import (
    attach_evidence_url,
    attach_structured_fields,
    defer_remediation_task,
    load_latest_remediation_summary,
    reject_remediation_task,
    resolve_remediation_task,
)


ALLOWED_WORKBENCH_ACTIONS = {
    "attach_evidence_url",
    "attach_structured_fields",
    "resolve_remediation",
    "reject_remediation",
    "defer_remediation",
}

TRADING_TERMS = ("buy", "sell", "order", "rebalance", "trade")

MAX_FORM_BODY_BYTES = 64 * 1024  # 64 KiB

# Upper bound on how long the handler waits for a declared request body to
# arrive. Normal UI posts are small and arrive in a single segment on loopback,
# so this is never exercised in practice; it only prevents a truncated or
# never-sent declared body from blocking the handler indefinitely.
REQUEST_BODY_READ_TIMEOUT = 2.0  # seconds
REVIEW_QUEUE_PAGE_SIZE = 25

CSP_HEADER_VALUE = (
    "default-src 'none'; "
    "style-src 'unsafe-inline'; "
    "img-src data:; "
    "frame-ancestors 'none'; "
    "base-uri 'none'; "
    "form-action 'self'; "
    "script-src 'none'"
)


def load_workbench_state(status: str = "open", task_id: str = "", risk: str = "all") -> Dict[str, Any]:
    selected_status = _normalize_status(status)
    selected_risk = _normalize_risk(risk)
    summary = load_latest_remediation_summary()
    priority_queue = build_priority_queue()
    queue = list(priority_queue.get("tasks", []) or summary.get("remediation_queue", []) or [])
    status_counts = {key: 0 for key in ["open", "resolved", "rejected", "deferred"]}
    for task in queue:
        task_status = str(task.get("status") or "open")
        status_counts[task_status] = status_counts.get(task_status, 0) + 1
    tasks = queue if selected_status == "all" else [task for task in queue if str(task.get("status") or "open") == selected_status]
    if selected_risk != "all":
        tasks = [task for task in tasks if str(task.get("evidence_risk_level") or "low") == selected_risk]
    selected_task = None
    if task_id:
        for task in queue:
            if task.get("task_id") == task_id:
                selected_task = task
                break
    return {
        "generated_at": now_iso(),
        "status": selected_status,
        "risk": selected_risk,
        "status_counts": status_counts,
        "risk_counts": priority_queue.get("risk_counts", {}),
        "tasks": tasks,
        "all_tasks": queue,
        "selected_task": selected_task,
        "priority_queue": priority_queue,
        "recent_records": list(summary.get("remediation_decision_records", []) or [])[-20:],
        "summary": summary,
    }


def render_workbench_html(status: str = "open", task_id: str = "", risk: str = "all", message: str = "", csrf_token: str = "") -> str:
    state = load_workbench_state(status=status, task_id=task_id, risk=risk)
    body = [
        _render_top_tabs("remediation"),
        "<h1>Stock Agent Review Workbench</h1>",
        _render_status_tabs(state),
        _render_risk_tabs(state),
        _render_priority_top10(state),
    ]
    if message:
        body.append('<div class="notice">{0}</div>'.format(_esc(message)))
    body.append(_render_task_list(state))
    body.append(_render_task_detail(state.get("selected_task"), csrf_token=csrf_token))
    body.append(_render_recent_records(state.get("recent_records", [])))
    body.append('<footer>Generated at {0}. Manual review only. No automatic trading, position change, or order execution.</footer>'.format(_esc(state.get("generated_at", ""))))
    return """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<link rel="icon" href="data:,">
<title>Stock Agent Review Workbench</title>
<style>{css}</style>
</head>
<body>
<main>{body}</main>
</body>
</html>""".format(css=_css(), body="\n".join(body))


def submit_workbench_action(form: Dict[str, Any], config: Dict[str, Any] | None = None) -> Dict[str, Any]:
    action = _form_value(form, "action")
    if action not in ALLOWED_WORKBENCH_ACTIONS or _contains_trading_term(action):
        raise ValueError("Unsupported workbench action: {0}".format(action))
    task_id = _form_value(form, "task_id")
    actor = _form_value(form, "actor") or "user"
    notes = _form_value(form, "notes")

    if action == "attach_evidence_url":
        evidence_url = _form_value(form, "evidence_url")
        if not evidence_url:
            raise ValueError("evidence_url is required")
        record = attach_evidence_url(task_id, evidence_url, actor=actor, notes=notes)
    elif action == "attach_structured_fields":
        fields = _parse_structured_fields(_form_value(form, "structured_fields"))
        if not fields:
            raise ValueError("structured_fields is required")
        record = attach_structured_fields(task_id, fields, actor=actor, notes=notes)
    elif action == "resolve_remediation":
        record = resolve_remediation_task(task_id, notes=notes, actor=actor)
    elif action == "reject_remediation":
        record = reject_remediation_task(task_id, reason=notes, actor=actor)
    else:
        record = defer_remediation_task(task_id, reason=notes, actor=actor)

    dashboard_path = generate_dashboard(config or {})
    review_summary = compute_review_summary()
    review_summary_path, review_summary_json_path = write_review_summary(review_summary)
    return {
        "record": record,
        "dashboard_path": dashboard_path,
        "review_summary_path": review_summary_path,
        "review_summary_json_path": review_summary_json_path,
    }


def write_workbench_snapshot(status: str = "open", task_id: str = "", risk: str = "all") -> str:
    out_dir = project_path("reports/workbench")
    out_dir.mkdir(parents=True, exist_ok=True)
    write_priority_queue(build_priority_queue())
    html_text = render_workbench_html(status=status, task_id=task_id, risk=risk)
    stamp = now_iso().replace(":", "").replace("-", "").replace("+", "_").split(".")[0]
    latest = out_dir / "latest.html"
    stamped = out_dir / "workbench_{0}.html".format(stamp)
    latest.write_text(html_text, encoding="utf-8-sig")
    stamped.write_text(html_text, encoding="utf-8-sig")
    return str(latest)


class _WorkbenchServer(ThreadingHTTPServer):
    allow_reuse_address = False
    daemon_threads = True


class _IPv6WorkbenchServer(_WorkbenchServer):
    """HTTPServer variant that binds a dual-stack-independent IPv6 loopback."""

    address_family = socket.AF_INET6


def create_workbench_server(host: str = "127.0.0.1", port: int = 8767, config: Dict[str, Any] | None = None) -> HTTPServer:
    parsed_host = _parse_loopback_authority(host)
    if parsed_host is None:
        raise ValueError("Workbench server only binds to loopback addresses (127.0.0.1, localhost, ::1)")
    hostname, _ = parsed_host
    server_cls = _IPv6WorkbenchServer if hostname == "::1" else _WorkbenchServer
    server = server_cls((hostname, int(port)), _WorkbenchHandler)
    server.csrf_token = secrets.token_urlsafe(32)
    server.instance_id = secrets.token_urlsafe(24)
    server.workbench_config = config or {}
    return server


def serve_workbench(host: str = "127.0.0.1", port: int = 8767, config: Dict[str, Any] | None = None) -> None:
    server = create_workbench_server(host=host, port=port, config=config)
    try:
        server.serve_forever()
    finally:
        server.server_close()


def load_runtime_workbench_state(
    stage: str = "all",
    priority: str = "all",
    case_id: str = "",
    config: Dict[str, Any] | None = None,
) -> Dict[str, Any]:
    runtime = load_research_runtime()
    summary = summarize_research_runtime(runtime)
    all_cases = [item for item in runtime.get("cases", []) if isinstance(item, dict)]
    selected_stage = _normalize_stage(stage)
    selected_priority = _normalize_priority(priority)
    filtered = all_cases
    if selected_stage != "all":
        filtered = [item for item in filtered if str(item.get("stage")) == selected_stage]
    if selected_priority != "all":
        filtered = [item for item in filtered if str(item.get("top_priority")) == selected_priority]
    selected_case = None
    if case_id:
        selected_case = next((item for item in all_cases if str(item.get("case_id")) == str(case_id)), None)
    degradation = runtime.get("degradation", {}) or {}
    operations = build_research_operations_snapshot(config or {}, runtime=runtime, write=False)
    llm_governance = load_latest_governance_summary(config or {})
    operations_by_case = {
        str(item.get("case_id") or ""): item for item in operations.get("cases", []) or []
    }
    return {
        "generated_at": now_iso(),
        "summary": summary,
        "runtime": runtime,
        "cases": all_cases,
        "filtered_cases": filtered,
        "selected_case": selected_case,
        "stage": selected_stage,
        "priority": selected_priority,
        "case_id": str(case_id or ""),
        "degradation": degradation,
        "preserved_cases": set(str(item) for item in (degradation.get("preserved_cases", []) or [])),
        "config": config or {},
        "operations": operations,
        "llm_governance": llm_governance,
        "operations_by_case": operations_by_case,
        "selected_case_operations": operations_by_case.get(str(case_id or ""), {}),
    }


def render_runtime_workbench_html(
    stage: str = "all",
    priority: str = "all",
    case_id: str = "",
    message: str = "",
    config: Dict[str, Any] | None = None,
    csrf_token: str = "",
) -> str:
    state = load_runtime_workbench_state(stage=stage, priority=priority, case_id=case_id, config=config)
    body = [
        _render_top_tabs("runtime"),
        _render_runtime_kpi_band(state),
        _render_operations_readiness(state),
        _render_llm_governance_readiness(state),
        _render_degradation_notice(state),
    ]
    if message:
        body.append('<div class="notice">{0}</div>'.format(_esc(message)))
    body.append(_render_runtime_filter_bar(state))
    body.append(_render_runtime_queue(state))
    body.append(_render_runtime_case_detail(state, config or {}, csrf_token))
    body.append('<footer>{0}</footer>'.format(_esc(SAFETY_BOUNDARY)))
    return """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<link rel="icon" href="data:,">
<title>Research Runtime Workbench</title>
<style>{css}</style>
</head>
<body>
<main>{body}</main>
</body>
</html>""".format(css=_css(), body="\n".join(body))


def submit_runtime_workbench_action(form: Dict[str, Any], config: Dict[str, Any] | None = None) -> Dict[str, Any]:
    action = _form_value(form, "action")
    if action not in VALID_ACTIONS or action in FORBIDDEN_ACTION_TERMS or _contains_trading_term(action):
        raise ValueError("Unsupported or unsafe research runtime action: {0}".format(action))
    case_id = _form_value(form, "case_id")
    actor = _form_value(form, "actor")
    actor_role = _form_value(form, "actor_role")
    if not case_id:
        raise ValueError("case_id is required")
    if not actor or not actor_role:
        raise ValueError("actor and actor_role are required for every runtime action")
    notes = _form_value(form, "notes")
    task_id = _form_value(form, "task_id") or _form_value(form, "request_id")
    assignee = _form_value(form, "assignee")
    references = _parse_references(form)
    checks = _parse_checks(form)

    result = record_runtime_action(
        case_id,
        action,
        actor,
        actor_role,
        notes=notes,
        task_id=task_id,
        assignee=assignee,
        references=references,
        checks=checks,
        config=config or {},
    )
    # The action is already durably recorded and the runtime report refreshed by
    # record_runtime_action. Dashboard and notification refreshes are best-effort
    # audits; a refresh failure never converts a recorded action into an error.
    result["refresh_error"] = ""
    try:
        result["dashboard_path"] = generate_dashboard(config or {})
    except Exception as exc:
        result["refresh_error"] = _append_error(result["refresh_error"], "dashboard", exc)
    try:
        result["notification_path"] = generate_notifications()
    except Exception as exc:
        result["refresh_error"] = _append_error(result["refresh_error"], "notification", exc)
    return result


def load_review_queue_workbench_state(
    status: str = "outstanding",
    priority: str = "all",
    signal_id: str = "",
    config: Dict[str, Any] | None = None,
    page: int = 1,
    page_size: int = REVIEW_QUEUE_PAGE_SIZE,
) -> Dict[str, Any]:
    queue = build_review_queue(config or {})
    selected_status = _normalize_review_status(status)
    selected_priority = _normalize_priority(priority)
    all_items = [item for item in queue.get("items", []) if isinstance(item, dict)]
    filtered = all_items
    if selected_status == "outstanding":
        filtered = [item for item in filtered if item.get("status") in OPEN_STATUSES]
    elif selected_status != "all":
        filtered = [item for item in filtered if str(item.get("status") or "pending") == selected_status]
    if selected_priority != "all":
        filtered = [item for item in filtered if str(item.get("priority") or "P3") == selected_priority]
    safe_page_size = max(1, min(int(page_size or REVIEW_QUEUE_PAGE_SIZE), 100))
    total_items = len(filtered)
    total_pages = max(1, (total_items + safe_page_size - 1) // safe_page_size)
    try:
        requested_page = int(page)
    except (TypeError, ValueError):
        requested_page = 1
    selected_page = max(1, min(requested_page, total_pages))
    page_start = (selected_page - 1) * safe_page_size
    page_items = filtered[page_start:page_start + safe_page_size]
    selected = next((item for item in all_items if str(item.get("signal_id") or "") == str(signal_id or "")), None)
    return {
        "queue": queue,
        "summary": queue.get("summary", {}),
        "items": page_items,
        "filtered_items": filtered,
        "filtered_count": total_items,
        "page": selected_page,
        "page_size": safe_page_size,
        "page_count": total_pages,
        "page_start": page_start,
        "all_items": all_items,
        "selected_item": selected,
        "status": selected_status,
        "priority": selected_priority,
        "signal_id": str(signal_id or ""),
    }


def render_review_queue_workbench_html(
    status: str = "outstanding",
    priority: str = "all",
    signal_id: str = "",
    message: str = "",
    config: Dict[str, Any] | None = None,
    csrf_token: str = "",
    page: int = 1,
) -> str:
    state = load_review_queue_workbench_state(status, priority, signal_id, config, page=page)
    body = [
        _render_top_tabs("review"),
        "<h1>Human Signal Review Queue</h1>",
        _render_review_queue_kpis(state),
        _render_review_queue_filters(state),
    ]
    if message:
        body.append('<div class="notice">{0}</div>'.format(_esc(message)))
    body.append(_render_review_queue_detail(state.get("selected_item"), csrf_token))
    body.append(_render_review_queue_table(state))
    body.append("<footer>Human review support only. No automatic assignment, approval, publication, trading, order placement, or position change.</footer>")
    return """<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<link rel="icon" href="data:,"><title>Human Signal Review Queue</title><style>{css}</style></head>
<body><main>{body}</main></body></html>""".format(css=_css(), body="\n".join(body))


def load_production_pilot_workbench_state(config: Dict[str, Any] | None = None) -> Dict[str, Any]:
    pilot = load_latest_production_pilot(config or {})
    return {
        "generated_at": now_iso(),
        "pilot": pilot,
        "gates": pilot.get("gates", []) if pilot else [],
        "blockers": pilot.get("go_live_blockers", []) if pilot else [],
        "providers": (pilot.get("provider_registry") or {}).get("providers", {}) if pilot else {},
        "review_capacity": pilot.get("review_capacity", {}) if pilot else {},
        "shadow_evidence": pilot.get("shadow_evidence", {}) if pilot else {},
        "incidents": pilot.get("incidents", {}) if pilot else {},
        "pi_runtime": pi_runtime_status(config or {}),
    }


def render_production_pilot_workbench_html(
    message: str = "",
    config: Dict[str, Any] | None = None,
) -> str:
    state = load_production_pilot_workbench_state(config)
    pilot = state.get("pilot", {}) or {}
    body = [_render_top_tabs("pilot"), "<h1>Controlled Production Pilot</h1>"]
    if message:
        body.append('<div class="notice">{0}</div>'.format(_esc(message)))
    if not pilot:
        body.append('<section class="section"><h2>Status</h2><div class="body empty">No pilot snapshot.</div></section>')
        return _workbench_document("Production Pilot", body)
    decision = str(pilot.get("go_live_decision") or "NO_GO")
    body.append(
        '<div class="notice {warning}"><strong>{decision}</strong> · technical={technical} · candidate={candidate} · effective mode={mode}</div>'.format(
            warning="warning" if decision != "GO" else "",
            decision=_esc(decision),
            technical=_esc(pilot.get("technical_readiness", "UNKNOWN")),
            candidate=_esc(pilot.get("candidate_readiness", "UNKNOWN")),
            mode=_esc(pilot.get("effective_mode", "shadow")),
        )
    )
    pi_status = state.get("pi_runtime", {}) or {}
    body.append(
        '<section class="section"><h2>Pi Agent Runtime</h2><div class="body">'
        '<p>status={status} · version={installed}/{expected} · isolated settings={settings} · provider credential={credential}</p>'
        '<p>tools=disabled · project trust=denied · sessions=disabled · external packages=none</p>'
        '</div></section>'.format(
            status="READY" if pi_status.get("runtime_ready") else "BLOCKED",
            installed=_esc(pi_status.get("installed_version", "missing")),
            expected=_esc(pi_status.get("expected_version", "")),
            settings="PASS" if pi_status.get("settings_safe") else "FAIL",
            credential="present" if pi_status.get("api_key_present") else "missing",
        )
    )
    gate_rows = "".join(
        '<tr><td>{gate}</td><td>{status}</td><td>{detail}</td></tr>'.format(
            gate=_esc(item.get("gate", "")), status=_esc(item.get("status", "")), detail=_esc(item.get("detail", ""))
        )
        for item in state.get("gates", [])
    ) or '<tr><td colspan="3">none</td></tr>'
    body.append('<section class="section"><h2>Go/No-Go Gates</h2><div class="body"><table class="pilot-gates"><tr><th>Gate</th><th>Status</th><th>Detail</th></tr>{0}</table></div></section>'.format(gate_rows))
    provider_rows = "".join(
        '<tr><td>{provider}</td><td>{status}</td><td>{scopes}</td><td>{updated}</td></tr>'.format(
            provider=_esc(provider_id), status=_esc(item.get("status", "")),
            scopes=_esc(", ".join(item.get("service_scopes", [])) or "none"), updated=_esc(item.get("last_event_at", "")),
        )
        for provider_id, item in sorted(state.get("providers", {}).items())
    ) or '<tr><td colspan="4">none</td></tr>'
    body.append('<section class="section"><h2>Provider Registry</h2><div class="body"><table><tr><th>Provider</th><th>Status</th><th>Scopes</th><th>Updated</th></tr>{0}</table></div></section>'.format(provider_rows))
    capacity = state.get("review_capacity", {}) or {}
    shadow = state.get("shadow_evidence", {}) or {}
    incidents = state.get("incidents", {}) or {}
    body.append(
        '<section class="section"><h2>Operating Capacity</h2><div class="body"><p>review outstanding={outstanding} · overdue={overdue} · eligible humans={humans} · WIP={wip} · suggested batches={batches}</p><p>shadow qualifying runs={runs}/{min_runs} · days={days}/{min_days} · unreconciled={unreconciled}</p><p>incidents open={open_count} · critical={critical} · high={high}</p></div></section>'.format(
            outstanding=_esc(capacity.get("outstanding_count", 0)), overdue=_esc(capacity.get("overdue_count", 0)),
            humans=_esc(capacity.get("eligible_human_reviewer_count", 0)), wip=_esc(capacity.get("available_wip_capacity", 0)),
            batches=_esc(capacity.get("suggested_batch_count", 0)), runs=_esc(shadow.get("qualifying_run_count", 0)),
            min_runs=_esc(shadow.get("minimum_runs", 0)), days=_esc(shadow.get("qualifying_trading_day_count", 0)),
            min_days=_esc(shadow.get("minimum_trading_days", 0)), unreconciled=_esc(shadow.get("unreconciled_run_count", 0)),
            open_count=_esc(incidents.get("open_count", 0)), critical=_esc(incidents.get("open_critical_count", 0)),
            high=_esc(incidents.get("open_high_count", 0)),
        )
    )
    blocker_html = "".join("<li>{0}</li>".format(_esc(item)) for item in state.get("blockers", [])) or "<li>none</li>"
    body.append('<section class="section"><h2>Blocking Actions</h2><div class="body"><ul>{0}</ul></div></section>'.format(blocker_html))
    body.append('<footer>Read-only production control view. Protected actions require registered human actors and append-only evidence.</footer>')
    return _workbench_document("Production Pilot", body)


def _workbench_document(title: str, body: Sequence[str]) -> str:
    return """<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<link rel="icon" href="data:,"><title>{title}</title><style>{css}</style></head>
<body><main>{body}</main></body></html>""".format(title=_esc(title), css=_css(), body="\n".join(body))


def submit_review_queue_workbench_action(form: Dict[str, Any], config: Dict[str, Any] | None = None) -> Dict[str, Any]:
    action = _form_value(form, "action")
    signal_id = _form_value(form, "signal_id")
    actor = _form_value(form, "actor")
    actor_role = _form_value(form, "actor_role")
    supported = {item for transitions in TRANSITIONS.values() for item in transitions}
    if action not in supported or _contains_trading_term(action):
        raise ValueError("Unsupported or unsafe review queue action: {0}".format(action))
    if not signal_id or not actor or not actor_role:
        raise ValueError("signal_id, actor, and actor_role are required")
    fields = {
        "decision_taken": _form_value(form, "decision_taken"),
        "actual_price_move": _form_value(form, "actual_price_move"),
        "actual_news_followup": _form_value(form, "actual_news_followup"),
        "was_effective": _form_value(form, "was_effective") or "unknown",
        "false_positive_reason": _form_value(form, "false_positive_reason"),
        "notes": _form_value(form, "completion_notes"),
    }
    result = record_review_queue_action(
        config or {}, signal_id, action, actor, actor_role,
        notes=_form_value(form, "notes"), fields=fields,
    )
    result["refresh_error"] = ""
    try:
        result["dashboard_path"] = generate_dashboard(config or {})
    except Exception as exc:
        result["refresh_error"] = _append_error(result["refresh_error"], "dashboard", exc)
    try:
        result["notification_path"] = generate_notifications(config or {})
    except Exception as exc:
        result["refresh_error"] = _append_error(result["refresh_error"], "notification", exc)
    return result


def _render_review_queue_kpis(state: Dict[str, Any]) -> str:
    summary = state.get("summary", {}) or {}
    rows = [
        ("Outstanding", summary.get("outstanding_count", 0)),
        ("Overdue", summary.get("overdue_count", 0)),
        ("At Risk", summary.get("at_risk_count", 0)),
        ("Unassigned", summary.get("unassigned_count", 0)),
        ("Duplicate Rows", summary.get("pending_duplicate_rows", 0)),
        ("Human Principals", summary.get("human_principal_count", 0)),
    ]
    values = "".join(
        '<div class="kpi"><span class="kpi-label">{0}</span><b>{1}</b></div>'.format(_esc(label), _esc(value))
        for label, value in rows
    )
    return '<div class="kpi-band">{0}<span class="stage-pill">assignment=suggest_only</span><span class="stage-pill">auto assignment=false</span></div>'.format(values)


def _render_review_queue_filters(state: Dict[str, Any]) -> str:
    statuses = ["outstanding", "pending", "in_review", "waiting_evidence", "deferred", "completed", "superseded", "all"]
    status_options = "".join(
        '<option value="{0}"{1}>{0}</option>'.format(_esc(value), " selected" if value == state.get("status") else "")
        for value in statuses
    )
    priorities = ["all", "P0", "P1", "P2", "P3"]
    priority_options = "".join(
        '<option value="{0}"{1}>{0}</option>'.format(_esc(value), " selected" if value == state.get("priority") else "")
        for value in priorities
    )
    return ('<form class="filters" method="get" action="/"><input type="hidden" name="view" value="review">'
            '<label>Status<select name="review_status">{0}</select></label>'
            '<label>Priority<select name="priority">{1}</select></label><button type="submit">Apply</button></form>').format(
        status_options, priority_options
    )


def _render_review_queue_table(state: Dict[str, Any]) -> str:
    items = state.get("items", []) or []
    if not items:
        return '<section class="section"><h2>Queue</h2><div class="body empty">No matching review items.</div></section>'
    rows = []
    for item in items:
        rows.append(
            '<tr><td>{esc}</td><td>{priority}</td><td><a href="/?view=review&amp;review_status={status_filter}&amp;priority={priority_filter}&amp;page={page}&amp;signal_id={signal_id}#review-detail">{signal_label}</a></td>'
            '<td>{title}</td><td>{status}</td><td>{sla}</td><td>{due}</td><td>{assigned}</td><td>{suggested}</td></tr>'.format(
                esc=_esc(item.get("escalation_level", 0)), priority=_esc(item.get("priority", "P3")),
                status_filter=_esc(state.get("status", "outstanding")), priority_filter=_esc(state.get("priority", "all")),
                page=_esc(state.get("page", 1)),
                signal_id=quote_plus(str(item.get("signal_id") or "")), signal_label=_esc(item.get("signal_id", "")),
                title=_esc(item.get("title", "")), status=_esc(item.get("status", "pending")),
                sla=_esc(item.get("sla_status", "unknown")), due=_esc(item.get("due_at", "")),
                assigned=_esc(item.get("assigned_to", "") or "unassigned"),
                suggested=_esc(item.get("assignment_recommendation", "") or "none"),
            )
        )
    header = "<tr><th>Esc</th><th>Priority</th><th>Signal</th><th>Title</th><th>Status</th><th>SLA</th><th>Due</th><th>Assigned</th><th>Suggested</th></tr>"
    start = int(state.get("page_start", 0)) + 1
    end = int(state.get("page_start", 0)) + len(items)
    total = int(state.get("filtered_count", len(items)))
    heading = "Queue ({0}-{1} of {2})".format(start, end, total)
    pager = _render_review_queue_pagination(state)
    return '<section class="section"><h2>{0}</h2><div class="body table-wrap"><table>{1}{2}</table></div>{3}</section>'.format(
        _esc(heading), header, "".join(rows), pager,
    )


def _render_review_queue_pagination(state: Dict[str, Any]) -> str:
    current = int(state.get("page", 1))
    page_count = int(state.get("page_count", 1))
    if page_count <= 1:
        return ""
    base = "/?view=review&amp;review_status={0}&amp;priority={1}&amp;page=".format(
        _esc(state.get("status", "outstanding")), _esc(state.get("priority", "all")),
    )
    previous = (
        '<a rel="prev" href="{0}{1}">Previous</a>'.format(base, current - 1)
        if current > 1 else '<span aria-disabled="true">Previous</span>'
    )
    following = (
        '<a rel="next" href="{0}{1}">Next</a>'.format(base, current + 1)
        if current < page_count else '<span aria-disabled="true">Next</span>'
    )
    return '<nav class="pagination" aria-label="Review queue pages">{0}<span>Page {1} of {2}</span>{3}</nav>'.format(
        previous, current, page_count, following,
    )


def _render_review_queue_detail(item: Dict[str, Any] | None, csrf_token: str) -> str:
    if not item:
        return '<section class="section" id="review-detail"><h2>Review Detail</h2><div class="body empty">Select a signal.</div></section>'
    questions = "".join("<li>{0}</li>".format(_esc(value)) for value in item.get("review_questions", []) or []) or "<li>none</li>"
    forms = []
    for action in item.get("allowed_actions", []) or []:
        role = "lead_analyst" if action in {"start_review", "request_more_data", "defer_review", "complete_review"} else "research_coordinator"
        completion = ""
        if action == "complete_review":
            completion = ('<label>Decision taken<input name="decision_taken" required></label>'
                          '<label>Was effective<select name="was_effective"><option value="unknown">unknown</option><option value="true">true</option><option value="false">false</option></select></label>'
                          '<label>Actual price move<input name="actual_price_move"></label>'
                          '<label>Actual news follow-up<textarea name="actual_news_followup"></textarea></label>'
                          '<label>False-positive reason<input name="false_positive_reason"></label>'
                          '<label>Completion notes<textarea name="completion_notes"></textarea></label>')
        forms.append(
            '<form method="post" action="/review-action">{csrf}<input type="hidden" name="signal_id" value="{signal_id}">'
            '<input type="hidden" name="action" value="{action}"><label>Actor ID<input name="actor" required autocomplete="off"></label>'
            '<label>Actor role<input name="actor_role" value="{role}" required></label><label>Notes<textarea name="notes"></textarea></label>{completion}'
            '<button type="submit">{action}</button></form>'.format(
                csrf=_csrf_hidden(csrf_token), signal_id=_esc(item.get("signal_id", "")), action=_esc(action),
                role=_esc(role), completion=completion,
            )
        )
    meta = ('<div class="kvs"><div class="kv"><span>Status</span><b>{status}</b></div><div class="kv"><span>SLA</span><b>{sla}</b></div>'
            '<div class="kv"><span>Assigned</span><b>{assigned}</b></div><div class="kv"><span>Suggested</span><b>{suggested}</b></div></div>').format(
        status=_esc(item.get("status", "")), sla=_esc(item.get("sla_status", "")),
        assigned=_esc(item.get("assigned_to", "") or "unassigned"), suggested=_esc(item.get("assignment_recommendation", "") or "none"),
    )
    return '<section class="section" id="review-detail"><h2>Review Detail: {0}</h2><div class="body">{1}<h2>Questions</h2><ul>{2}</ul><h2>Human Actions</h2>{3}</div></section>'.format(
        _esc(item.get("signal_id", "")), meta, questions, "".join(forms) or '<div class="empty">No valid transitions.</div>'
    )


def _parse_loopback_authority(value: Any) -> Tuple[str, str] | None:
    """Strictly parse a loopback authority into (hostname, port).

    Accepts exactly ``127.0.0.1``, ``localhost``, or ``::1`` with an optional
    valid port. Rejects userinfo, path, query, fragment, suffix hosts, malformed
    IPv6 brackets, and bare IPv6 literals carrying a port (``[::1]:8767`` is the
    only valid IPv6 form with a port). Ports must be numeric in 1..65535.
    Returns ``None`` on any deviation so callers can fail closed.
    """
    text = str(value or "").strip().lower()
    if not text:
        return None
    if any(token in text for token in ("@", "/", "?", "#")):
        return None
    bracketed = text.startswith("[")
    host = text
    port = ""
    has_port = False
    if bracketed:
        closing = text.find("]")
        if closing == -1:
            return None
        host = text[1:closing]
        rest = text[closing + 1:]
        if rest:
            if not rest.startswith(":"):
                return None
            port = rest[1:]
            has_port = True
    elif text.count(":") == 1:
        host, port = text.split(":", 1)
        has_port = True
    if host == "::1":
        # A bare IPv6 literal cannot carry a port; only the bracketed form may.
        if not bracketed and has_port:
            return None
    elif host not in {"127.0.0.1", "localhost"}:
        return None
    if has_port and not _valid_port(port):
        return None
    return (host, port)


def _valid_port(port: str) -> bool:
    return bool(port) and port.isdigit() and 1 <= int(port) <= 65535


def _effective_port(port: str, scheme: str) -> int:
    if port:
        return int(port)
    return 443 if scheme == "https" else 80


def _is_loopback_host(value: Any) -> bool:
    return _parse_loopback_authority(value) is not None


def _is_loopback_origin(value: Any, request_host: Any) -> bool:
    """Reject a cross-origin POST.

    A plain HTTP server must only accept an explicit ``Origin`` that is http,
    authority-parseable, and identical in normalized hostname and effective port
    to the request Host. The absence of an Origin is still acceptable because the
    CSRF token remains mandatory.
    """
    origin = str(value or "").strip()
    if not origin:
        return False
    try:
        parsed = urlparse(origin)
    except ValueError:
        return False
    if parsed.scheme != "http":
        return False
    if not parsed.netloc:
        return False
    if parsed.username is not None or parsed.password is not None or "@" in parsed.netloc:
        return False
    if parsed.path or parsed.params or parsed.query or parsed.fragment:
        return False
    origin_authority = _parse_loopback_authority(parsed.netloc)
    request_authority = _parse_loopback_authority(request_host)
    if origin_authority is None or request_authority is None:
        return False
    origin_hostname, origin_port = origin_authority
    request_hostname, request_port = request_authority
    if origin_hostname != request_hostname:
        return False
    return _effective_port(origin_port, parsed.scheme) == _effective_port(request_port, "http")


def _form_value(form: Dict[str, Any], key: str) -> str:
    value = form.get(key, "")
    if isinstance(value, list):
        return str(value[-1]) if value else ""
    return str(value or "").strip()


def _form_values(form: Dict[str, Any], key: str) -> List[str]:
    value = form.get(key)
    if value is None:
        return []
    if isinstance(value, list):
        return [str(item or "").strip() for item in value]
    return [str(value or "").strip()]


def _parse_references(form: Dict[str, Any]) -> List[str]:
    parsed: List[str] = []
    for value in _form_values(form, "reference") + _form_values(form, "references"):
        for line in str(value).replace("\r", "\n").split("\n"):
            line = line.strip()
            if line:
                parsed.append(line)
    return _unique(parsed)


def _parse_checks(form: Dict[str, Any]) -> List[str]:
    parsed: List[str] = []
    for value in _form_values(form, "check") + _form_values(form, "checks"):
        for line in str(value).replace("\r", "\n").split("\n"):
            line = line.strip()
            if line:
                parsed.append(line)
    return _unique(parsed)


def _unique(values: Sequence[Any]) -> List[str]:
    rows: List[str] = []
    seen = set()
    for value in values:
        text = str(value or "").strip()
        if text and text not in seen:
            seen.add(text)
            rows.append(text)
    return rows


def _append_error(current: str, component: str, exc: Exception) -> str:
    # Best-effort refresh failures must never leak raw exception text, which may
    # embed filesystem paths, URLs, request headers, or secrets. Surface only
    # the component that failed and the exception class.
    text = "{0} refresh failed ({1})".format(component, type(exc).__name__)
    return "{0}; {1}".format(current, text) if current else text


def _normalize_stage(stage: str) -> str:
    value = str(stage or "all").strip().lower()
    return value if value == "all" or value in STAGE_ORDER else "all"


def _normalize_priority(priority: str) -> str:
    value = str(priority or "all").strip().upper()
    return value if value in {"all", "P0", "P1", "P2", "P3"} else "all"


def _normalize_review_status(status: str) -> str:
    value = str(status or "outstanding").strip().lower()
    allowed = {"outstanding", "all", "pending", "in_review", "waiting_evidence", "deferred", "completed", "superseded"}
    return value if value in allowed else "outstanding"


def _render_top_tabs(active: str) -> str:
    runtime_cls = "active" if active == "runtime" else ""
    remed_cls = "active" if active == "remediation" else ""
    review_cls = "active" if active == "review" else ""
    pilot_cls = "active" if active == "pilot" else ""
    return '<nav class="tabs top-tabs"><a class="{runtime_cls}" href="/?view=runtime">Research Cases</a><a class="{review_cls}" href="/?view=review&amp;review_status=outstanding">Signal Review</a><a class="{remed_cls}" href="/?status=open&amp;view=remediation">Remediation</a><a class="{pilot_cls}" href="/?view=pilot">Production Pilot</a></nav>'.format(
        runtime_cls=runtime_cls,
        remed_cls=remed_cls,
        review_cls=review_cls,
        pilot_cls=pilot_cls,
    )


def _render_runtime_kpi_band(state: Dict[str, Any]) -> str:
    summary = state.get("summary", {}) or {}
    operations_summary = (state.get("operations", {}) or {}).get("summary", {}) or {}
    governance_summary = (state.get("llm_governance", {}) or {}).get("summary", {}) or {}
    kpis = [
        ("Cases", summary.get("case_count", 0)),
        ("Blocked", summary.get("blocked_count", 0)),
        ("Pending Human", summary.get("pending_human_count", 0)),
        ("Monitoring", summary.get("monitoring_count", 0)),
        ("SLA Overdue", operations_summary.get("overdue_count", 0)),
        ("At Risk", operations_summary.get("at_risk_count", 0)),
        ("Unstaffed", operations_summary.get("unstaffed_count", 0)),
        ("LLM Egress Denied", governance_summary.get("denied_count", 0)),
        ("LLM Output Blocked", governance_summary.get("output_blocked_count", 0)),
    ]
    kpi_html = "".join(
        '<div class="kpi"><span class="kpi-label">{label}</span><b>{value}</b></div>'.format(
            label=_esc(label), value=_esc(value)
        )
        for label, value in kpis
    )
    stage_html = "".join(
        '<span class="stage-pill">{stage}={count}</span>'.format(stage=_esc(stage), count=_esc(count))
        for stage, count in (summary.get("stage_counts", {}) or {}).items()
    )
    return '<div class="kpi-band">{kpi_html}{stage_html}</div>'.format(kpi_html=kpi_html, stage_html=stage_html)


def _render_operations_readiness(state: Dict[str, Any]) -> str:
    operations = state.get("operations", {}) or {}
    roster = operations.get("roster", {}) or {}
    missing = roster.get("missing_mandatory_human_capabilities", []) or []
    status = "READY" if roster.get("valid") and not missing else "BLOCKED"
    detail = "none" if not missing else ", ".join(str(item) for item in missing)
    css_class = "notice" if status == "READY" else "notice warning"
    return '<div class="{css}"><strong>Research Operations: {status}</strong> · roster {humans} human / {services} service · missing: {missing} · assignment mode: suggest_only</div>'.format(
        css=css_class,
        status=_esc(status),
        humans=_esc(roster.get("human_count", 0)),
        services=_esc(roster.get("service_count", 0)),
        missing=_esc(detail),
    )


def _render_llm_governance_readiness(state: Dict[str, Any]) -> str:
    governance = state.get("llm_governance", {}) or {}
    if not governance:
        return '<div class="notice warning"><strong>LLM Governance: UNKNOWN</strong> · run agent-run to create an enforced audit snapshot</div>'
    summary = governance.get("summary", {}) or {}
    status = str(governance.get("status") or "UNKNOWN")
    css_class = "notice" if status == "OK" else "notice warning"
    return (
        '<div class="{css}"><strong>LLM Governance: {status}</strong> · public-only egress · '
        'approved {approved} · denied {denied} · output blocked {blocked} · pending human exceptions {pending}</div>'
    ).format(
        css=css_class,
        status=_esc(status),
        approved=_esc(summary.get("approved_count", 0)),
        denied=_esc(summary.get("denied_count", 0)),
        blocked=_esc(summary.get("output_blocked_count", 0)),
        pending=_esc(summary.get("pending_exception_count", 0)),
    )


def _render_degradation_notice(state: Dict[str, Any]) -> str:
    degradation = state.get("degradation", {}) or {}
    if not degradation.get("degraded"):
        return ""
    lines = ['<div class="notice warning"><strong>Runtime degraded:</strong> signal backfill was incomplete or prior verified state was preserved.']
    for reason in degradation.get("degradation_reasons", []) or []:
        lines.append('<div>{0}</div>'.format(_esc(reason)))
    preserved = state.get("preserved_cases") or set()
    if preserved:
        lines.append('<div>Preserved cases (verified state kept): {0}</div>'.format(_esc(", ".join(sorted(preserved)))))
    lines.append("</div>")
    return "\n".join(lines)


def _render_runtime_filter_bar(state: Dict[str, Any]) -> str:
    stages = ["all"] + list(STAGE_ORDER)
    stage_options = "".join(
        '<option value="{value}"{selected}>{value}</option>'.format(
            value=_esc(value), selected=" selected" if value == state.get("stage") else ""
        )
        for value in stages
    )
    priorities = ["all", "P0", "P1", "P2", "P3"]
    priority_options = "".join(
        '<option value="{value}"{selected}>{value}</option>'.format(
            value=_esc(value), selected=" selected" if value == state.get("priority") else ""
        )
        for value in priorities
    )
    return """<form class="filters" method="get" action="/">
<input type="hidden" name="view" value="runtime">
<label>Stage <select name="stage">{stage_options}</select></label>
<label>Priority <select name="priority">{priority_options}</select></label>
<button type="submit">Filter</button>
</form>""".format(stage_options=stage_options, priority_options=priority_options)


def _render_runtime_queue(state: Dict[str, Any]) -> str:
    cases = state.get("filtered_cases", []) or []
    preserved = state.get("preserved_cases") or set()
    operations_by_case = state.get("operations_by_case", {}) or {}
    if not cases:
        return '<div class="section"><h2>Case Queue</h2><div class="body"><p class="empty">No cases match the selected filters.</p></div></div>'
    rows = []
    for case in cases:
        case_id = str(case.get("case_id", ""))
        cls = " preserved" if case_id in preserved else ""
        operations = operations_by_case.get(case_id, {}) or {}
        rows.append('<tr class="{cls}"><td>{escalation}</td><td>{priority}</td><td><a href="/?view=runtime&amp;stage={stage}&amp;priority={priority_filter}&amp;case_id={case_id}">{label}</a></td><td>{title}</td><td>{stage_cell}</td><td>{sla}</td><td>{owner}</td><td>{recommendation}</td><td>{blocked}</td></tr>'.format(
            cls=cls.strip(),
            escalation=_esc(operations.get("escalation_level", "")),
            priority=_esc(case.get("top_priority", "")),
            stage=_esc(state.get("stage", "all")),
            priority_filter=_esc(state.get("priority", "all")),
            case_id=quote_plus(case_id),
            label=_esc(case_id),
            title=_esc(case.get("title", "")),
            stage_cell=_esc(case.get("stage", "")),
            entity=_esc(case.get("entity", "")),
            sla=_esc(operations.get("sla_status", "")),
            owner=_esc(operations.get("required_role", "")),
            recommendation=_esc(operations.get("assignment_recommendation", "") or "none"),
            blocked=_esc(str(len(case.get("blockers") or []))),
        ))
    return '<div class="section"><h2>Case Queue ({count})</h2><div class="body table-wrap"><table><tr><th>Esc</th><th>Pr</th><th>Case ID</th><th>Title</th><th>Stage</th><th>SLA</th><th>Next Owner</th><th>Recommendation</th><th>Blk</th></tr>{rows}</table></div></div>'.format(
        count=_esc(len(cases)), rows="\n".join(rows)
    )


def _render_runtime_case_detail(state: Dict[str, Any], config: Dict[str, Any], csrf_token: str) -> str:
    case = state.get("selected_case")
    if not case:
        return '<div class="section"><h2>Case Detail</h2><div class="body"><p class="empty">Select a case to inspect or operate on.</p></div></div>'
    preserved_cls = " preserved" if str(case.get("case_id")) in (state.get("preserved_cases") or set()) else ""
    sections = [
        _render_case_meta(case),
        _render_case_operations(state.get("selected_case_operations", {})),
        _render_case_assignments(case),
        _render_case_gates(case),
        _render_case_workstreams(case),
        _render_case_workpapers(case),
        _render_case_evidence_requests(case),
        _render_case_approvals(case),
        _render_case_blockers_next(case),
        _render_case_action_history(case),
        _render_runtime_forms(case, config, csrf_token, state.get("selected_case_operations", {})),
    ]
    return '<div class="case-detail{preserved_cls}">{sections}</div>'.format(
        preserved_cls=preserved_cls, sections="\n".join(sections)
    )


def _render_case_meta(case: Dict[str, Any]) -> str:
    rows = [
        ("Case ID", case.get("case_id", "")),
        ("Title", case.get("title", "")),
        ("Entity", case.get("entity", "")),
        ("Topic", case.get("topic", "")),
        ("Priority", case.get("top_priority", "")),
        ("Stage", case.get("stage", "")),
        ("Status", case.get("status", "")),
        ("Created", case.get("created_at", "")),
        ("Updated", case.get("updated_at", "")),
        ("External publication", "enabled" if case.get("external_publication_allowed") else "disabled"),
    ]
    cells = "".join(
        '<div class="kv"><span>{key}</span><b>{value}</b></div>'.format(key=_esc(k), value=_esc(v))
        for k, v in rows
    )
    return '<div class="section"><h2>Case {case_id}</h2><div class="body kvs">{cells}</div></div>'.format(
        case_id=_esc(case.get("case_id", "")), cells=cells
    )


def _render_case_operations(operations: Dict[str, Any]) -> str:
    if not operations:
        return ""
    rows = [
        ("Access", operations.get("access_classification", "")),
        ("Escalation", operations.get("escalation_level", "")),
        ("SLA", operations.get("sla_status", "")),
        ("Due", operations.get("due_at", "")),
        ("Next task", operations.get("next_task_id", "")),
        ("Required role", operations.get("required_role", "")),
        ("Suggested assignee", operations.get("assignment_recommendation", "") or "none"),
        ("Staffing gaps", ", ".join(operations.get("staffing_gaps", []) or []) or "none"),
    ]
    cells = "".join(
        '<div class="kv"><span>{key}</span><b>{value}</b></div>'.format(key=_esc(key), value=_esc(value))
        for key, value in rows
    )
    reasons = "; ".join(str(item) for item in operations.get("escalation_reasons", []) or [])
    return '<div class="section"><h2>Operations Control</h2><div class="body kvs">{cells}</div><div class="body"><strong>Escalation reasons:</strong> {reasons}</div></div>'.format(
        cells=cells, reasons=_esc(reasons or "none")
    )
def _render_case_assignments(case: Dict[str, Any]) -> str:
    assignments = case.get("assignments", {}) or {}
    rows = [
        ("Lead Analyst", assignments.get("lead_analyst", "")),
        ("Quality Reviewer", assignments.get("quality_reviewer", "")),
        ("Compliance Officer", assignments.get("compliance_officer", "")),
        ("Publication Approver", assignments.get("publication_approver", "")),
    ]
    cells = "".join(
        '<div class="kv"><span>{key}</span><b>{value}</b></div>'.format(key=_esc(k), value=_esc(v or "unassigned"))
        for k, v in rows
    )
    return '<div class="section"><h2>Assignments</h2><div class="body kvs">{0}</div></div>'.format(cells)


def _render_case_gates(case: Dict[str, Any]) -> str:
    gates = case.get("gates", {}) or {}
    gate_names = ["intake", "triage", "evidence", "analysis", "challenge", "quality_review", "compliance_review", "publication"]
    rows = []
    for name in gate_names:
        gate = gates.get(name, {}) or {}
        status = str(gate.get("status") or "PENDING")
        reasons = gate.get("reasons", []) or []
        rows.append("<tr><td>{name}</td><td class=\"gate-{status}\">{status}</td><td>{reasons}</td></tr>".format(
            name=_esc(name),
            status=_esc(status.lower()),
            reasons=_esc("; ".join(str(item) for item in reasons)),
        ))
    return '<div class="section"><h2>Gates</h2><div class="body table-wrap"><table><tr><th>Gate</th><th>Status</th><th>Reasons</th></tr>{0}</table></div></div>'.format(
        "\n".join(rows)
    )


def _render_case_workstreams(case: Dict[str, Any]) -> str:
    rows = []
    for task in case.get("workstreams", []) or []:
        rows.append("<tr><td>{task_id}</td><td>{stage}</td><td>{owner}</td><td>{assignee}</td><td>{status}</td><td>{blockers}</td></tr>".format(
            task_id=_esc(task.get("task_id", "")),
            stage=_esc(task.get("stage", "")),
            owner=_esc(task.get("owner_role", "")),
            assignee=_esc(task.get("assignee", "")),
            status=_esc(task.get("status", "")),
            blockers=_esc("; ".join(str(item) for item in task.get("blockers", []) or [])),
        ))
    if not rows:
        return '<div class="section"><h2>Workstreams</h2><div class="body"><p class="empty">No workstreams.</p></div></div>'
    return '<div class="section"><h2>Workstreams</h2><div class="body table-wrap"><table><tr><th>Task</th><th>Stage</th><th>Owner Role</th><th>Assignee</th><th>Status</th><th>Blockers</th></tr>{0}</table></div></div>'.format(
        "\n".join(rows)
    )


def _render_case_workpapers(case: Dict[str, Any]) -> str:
    manifest = case.get("workpaper_manifest", []) or []
    if not manifest:
        return '<div class="section"><h2>Workpapers</h2><div class="body"><p class="empty">No retained workpapers.</p></div></div>'
    rows = []
    for wp in manifest:
        origin = wp.get("origin", "") or "{0} {1}".format(wp.get("attached_by", ""), wp.get("attached_at", "")).strip()
        rows.append("<tr><td>{kind}</td><td>{reference}</td><td>{origin}</td><td>{notes}</td></tr>".format(
            kind=_esc(wp.get("kind", "")),
            reference=_esc(wp.get("reference", "")),
            origin=_esc(origin),
            notes=_esc(wp.get("notes", "")),
        ))
    return '<div class="section"><h2>Workpapers</h2><div class="body table-wrap"><table><tr><th>Kind</th><th>Reference</th><th>Origin</th><th>Notes</th></tr>{0}</table></div></div>'.format(
        "\n".join(rows)
    )


def _render_case_evidence_requests(case: Dict[str, Any]) -> str:
    requests = case.get("manual_evidence_requests", []) or []
    if not requests:
        return '<div class="section"><h2>Evidence Requests</h2><div class="body"><p class="empty">No open evidence requests.</p></div></div>'
    rows = []
    for item in requests:
        rows.append("<tr><td>{request_id}</td><td>{request}</td><td>{status}</td><td>{requested_by}</td><td>{resolved_at}</td><td>{references}</td></tr>".format(
            request_id=_esc(item.get("request_id", "")),
            request=_esc(item.get("request", "")),
            status=_esc(item.get("status", "")),
            requested_by=_esc(item.get("requested_by", "")),
            resolved_at=_esc(item.get("resolved_at", "")),
            references=_esc("; ".join(str(ref) for ref in item.get("references", []) or [])),
        ))
    return '<div class="section"><h2>Evidence Requests</h2><div class="body table-wrap"><table><tr><th>Request ID</th><th>Request</th><th>Status</th><th>Requested By</th><th>Resolved At</th><th>References</th></tr>{0}</table></div></div>'.format(
        "\n".join(rows)
    )


def _render_case_approvals(case: Dict[str, Any]) -> str:
    approvals = case.get("approvals", {}) or {}
    rows = []
    for key in ["quality_review", "compliance_review", "publication"]:
        appr = approvals.get(key, {}) or {}
        rows.append("<tr><td>{key}</td><td>{status}</td><td>{actor}</td><td>{actor_role}</td><td>{decided_at}</td><td>{notes}</td><td>{checks}</td></tr>".format(
            key=_esc(key),
            status=_esc(appr.get("status", "")),
            actor=_esc(appr.get("actor", "")),
            actor_role=_esc(appr.get("actor_role", "")),
            decided_at=_esc(appr.get("decided_at", "")),
            notes=_esc(appr.get("notes", "")),
            checks=_esc("; ".join(str(item) for item in appr.get("checks", []) or [])),
        ))
    return '<div class="section"><h2>Approvals</h2><div class="body table-wrap"><table><tr><th>Gate</th><th>Status</th><th>Actor</th><th>Role</th><th>Decided</th><th>Notes</th><th>Checks</th></tr>{0}</table></div></div>'.format(
        "\n".join(rows)
    )


def _render_case_blockers_next(case: Dict[str, Any]) -> str:
    blockers = case.get("blockers", []) or []
    next_actions = case.get("next_actions", []) or []
    blocker_html = "".join("<li>{0}</li>".format(_esc(item)) for item in blockers)
    next_html = "".join("<li>{0}</li>".format(_esc(item)) for item in next_actions)
    return """<div class="grid-2">
<div class="section"><h2>Blockers</h2><div class="body"><ul class="plain">{0}</ul></div></div>
<div class="section"><h2>Next Actions</h2><div class="body"><ul class="plain">{1}</ul></div></div>
</div>""".format(
        blocker_html or '<li class="empty">none</li>',
        next_html or '<li class="empty">none</li>',
    )


def _render_case_action_history(case: Dict[str, Any]) -> str:
    history = case.get("action_history", []) or []
    if not history:
        return '<div class="section"><h2>Action History</h2><div class="body"><p class="empty">No actions recorded.</p></div></div>'
    rows = []
    for item in history[-30:]:
        authorization = item.get("authorization", {}) or {}
        authorization_text = "{allowed} / {principal} / policy {policy}".format(
            allowed="allowed" if authorization.get("allowed", True) else "denied",
            principal=item.get("principal_type", authorization.get("principal_type", "legacy")),
            policy=item.get("operations_policy_version", authorization.get("policy_version", "legacy")),
        )
        rows.append("<tr><td>{recorded_at}</td><td>{action}</td><td>{actor} ({role})</td><td>{task_id}</td><td>{assignee}</td><td>{authorization}</td><td>{notes}</td><td>{references}</td><td>{checks}</td></tr>".format(
            recorded_at=_esc(item.get("recorded_at", "")),
            action=_esc(item.get("action", "")),
            actor=_esc(item.get("actor", "")),
            role=_esc(item.get("actor_role", "")),
            task_id=_esc(item.get("task_id", "")),
            assignee=_esc(item.get("assignee", "")),
            authorization=_esc(authorization_text),
            notes=_esc(item.get("notes", "")),
            references=_esc("; ".join(str(ref) for ref in item.get("references", []) or [])),
            checks=_esc("; ".join(str(c) for c in item.get("checks", []) or [])),
        ))
    return '<div class="section"><h2>Action History</h2><div class="body table-wrap"><table><tr><th>At</th><th>Action</th><th>Actor</th><th>Task</th><th>Assignee</th><th>Authorization</th><th>Notes</th><th>References</th><th>Checks</th></tr>{0}</table></div></div>'.format(
        "\n".join(rows)
    )


def _render_runtime_forms(case: Dict[str, Any], config: Dict[str, Any], csrf_token: str, operations: Dict[str, Any] | None = None) -> str:
    case_id = str(case.get("case_id", ""))
    stage = str(case.get("stage") or "")
    parts = ['<div class="section forms"><h2>Controlled Actions</h2><div class="body">']
    parts.append('<p class="hint">Every action requires an explicit actor and actor role. Protected gates remain human-only.</p>')

    operations = operations or {}
    enforced = bool(operations_config(config).get("enabled") and operations_config(config).get("enforce_roster"))
    if enforced:
        eligible_by_role = operations.get("eligible_assignees_by_role", {}) or {}
        eligible_assigners = _unique(
            list(eligible_by_role.get("research_coordinator", []) or [])
            + list(eligible_by_role.get("research_supervisor", []) or [])
        )
        for action, label, role in [
            ("assign_lead", "Assign Lead", "lead_analyst"),
            ("assign_quality_reviewer", "Assign Quality Reviewer", "quality_reviewer"),
            ("assign_compliance_officer", "Assign Compliance Officer", "compliance_officer"),
            ("assign_approver", "Assign Approver", "publication_approver"),
        ]:
            eligible = eligible_by_role.get(role, []) or []
            parts.append(_runtime_form(
                case_id,
                csrf_token,
                role_hint="research_coordinator | research_supervisor",
                fields=_assignee_select(eligible, role),
                buttons=_action_buttons((action, label)) if eligible and eligible_assigners else '<span class="hint">No eligible assigner or assignee</span>',
                actor_options=eligible_assigners,
                actor_roles=["research_coordinator", "research_supervisor"],
            ))
        next_task_id = str(operations.get("next_task_id") or "")
        required_role = str(operations.get("required_role") or "")
        eligible = eligible_by_role.get(required_role, []) or []
        if next_task_id:
            parts.append(_runtime_form(
                case_id,
                csrf_token,
                role_hint="research_coordinator | research_supervisor",
                fields='<input type="hidden" name="task_id" value="{task_id}">{select}'.format(
                    task_id=_esc(next_task_id), select=_assignee_select(eligible, required_role)
                ),
                buttons=_action_buttons(("assign_workstream", "Assign Next Workstream")) if eligible and eligible_assigners else '<span class="hint">No eligible assigner or workstream assignee</span>',
                actor_options=eligible_assigners,
                actor_roles=["research_coordinator", "research_supervisor"],
            ))
    else:
        parts.append(_runtime_form(
            case_id,
            csrf_token,
            role_hint="research_coordinator | research_supervisor",
            fields='<label>Assignee <input name="assignee" autocomplete="off" required></label>',
            buttons=_action_buttons(
                ("assign_lead", "Assign Lead"),
                ("assign_quality_reviewer", "Assign Quality Reviewer"),
                ("assign_compliance_officer", "Assign Compliance Officer"),
                ("assign_approver", "Assign Approver"),
            ),
        ))

    parts.append(_runtime_form(
        case_id,
        csrf_token,
        role_hint="lead_analyst | information_analyst | ...",
        fields="".join([
            '<label>Task id <input name="task_id" autocomplete="off" placeholder="source_verification"></label>',
            '<label>Evidence request id <input name="request_id" autocomplete="off" placeholder="evidence_request:..."></label>',
            '<label>References <textarea name="references" placeholder="one reference per line"></textarea></label>',
            '<label>Notes <textarea name="notes"></textarea></label>',
        ]),
        buttons=_action_buttons(
            ("attach_workpaper", "Attach Workpaper"),
            ("complete_task", "Complete Task"),
            ("request_evidence", "Request Evidence"),
            ("resolve_evidence_request", "Resolve Evidence Request"),
        ),
    ))

    if stage == "quality_review":
        parts.append(_runtime_form(
            case_id,
            csrf_token,
            role_hint="quality_reviewer",
            fields='<label>Notes <textarea name="notes" placeholder="review notes (required to reject)"></textarea></label>',
            buttons=_action_buttons(("quality_approve", "Approve Quality"), ("quality_reject", "Reject Quality")),
        ))
    if stage == "compliance_review":
        check_html = "".join(
            '<label class="check"><input type="checkbox" name="check" value="{value}"> {value}</label>'.format(value=_esc(c))
            for c in _required_compliance_checks(config)
        )
        parts.append(_runtime_form(
            case_id,
            csrf_token,
            role_hint="compliance_officer",
            fields='<fieldset class="checks"><legend>Required compliance checks</legend>{0}</fieldset><label>Notes <textarea name="notes"></textarea></label>'.format(check_html),
            buttons=_action_buttons(("compliance_clear", "Clear Compliance"), ("compliance_block", "Block Compliance")),
        ))
    if stage == "human_approval":
        parts.append(_runtime_form(
            case_id,
            csrf_token,
            role_hint="research_supervisor",
            fields='<label>Notes <textarea name="notes"></textarea></label>',
            buttons=_action_buttons(
                ("publication_approve", "Approve Publication"),
                ("publication_reject", "Reject Publication"),
            ),
        ))
    elif stage == "published":
        parts.append(_runtime_form(
            case_id,
            csrf_token,
            role_hint="research_supervisor",
            fields='<label>Notes <textarea name="notes"></textarea></label>',
            buttons=_action_buttons(("start_monitoring", "Start Monitoring")),
        ))
    elif stage == "monitoring":
        parts.append(_runtime_form(
            case_id,
            csrf_token,
            role_hint="research_supervisor",
            fields='<label>Reason <textarea name="notes" placeholder="required reason"></textarea></label>',
            buttons=_action_buttons(("close_case", "Close Case")),
        ))
    elif stage == "closed":
        parts.append(_runtime_form(
            case_id,
            csrf_token,
            role_hint="research_supervisor",
            fields='<label>Reason <textarea name="notes" placeholder="required reason"></textarea></label>',
            buttons=_action_buttons(("reopen_case", "Reopen Case")),
        ))

    parts.append("</div></div>")
    return "\n".join(parts)


def _assignee_select(eligible: Sequence[str], role: str) -> str:
    options = '<option value="">Select eligible {0}</option>'.format(_esc(role))
    options += "".join('<option value="{0}">{0}</option>'.format(_esc(actor)) for actor in eligible)
    return '<label>Assignee <select name="assignee" required>{0}</select></label>'.format(options)


def _runtime_form(
    case_id: str,
    csrf_token: str,
    role_hint: str,
    fields: str,
    buttons: str,
    actor_options: Sequence[str] | None = None,
    actor_roles: Sequence[str] | None = None,
) -> str:
    if actor_options is None:
        actor_field = '<label>Actor <input name="actor" autocomplete="off" required></label>'
    else:
        options = '<option value="">Select authorized actor</option>'
        options += "".join('<option value="{0}">{0}</option>'.format(_esc(actor)) for actor in actor_options)
        actor_field = '<label>Actor <select name="actor" required>{0}</select></label>'.format(options)
    if actor_roles is None:
        role_field = '<label>Actor role <input name="actor_role" autocomplete="off" required placeholder="{0}"></label>'.format(_esc(role_hint))
    else:
        role_options = '<option value="">Select authorized role</option>'
        role_options += "".join('<option value="{0}">{0}</option>'.format(_esc(role)) for role in actor_roles)
        role_field = '<label>Actor role <select name="actor_role" required>{0}</select></label>'.format(role_options)
    return """<form method="post" action="/runtime-action">
{hidden}
<input type="hidden" name="case_id" value="{case_id}">
{actor_field}
{role_field}
{fields}
<div class="actions">{buttons}</div>
</form>""".format(
        hidden=_csrf_hidden(csrf_token),
        case_id=_esc(case_id),
        actor_field=actor_field,
        role_field=role_field,
        fields=fields,
        buttons=buttons,
    )


def _action_buttons(*pairs: Sequence[Any]) -> str:
    return "".join(
        '<button name="action" value="{value}">{label}</button>'.format(value=_esc(value), label=_esc(label))
        for value, label in pairs
    )


def _csrf_hidden(csrf_token: str) -> str:
    if csrf_token:
        return '<input type="hidden" name="csrf_token" value="{0}">'.format(_esc(csrf_token))
    return ""


def _required_compliance_checks(config: Dict[str, Any]) -> List[str]:
    raw = (config or {}).get("research_runtime", {}) if isinstance(config, dict) else {}
    checks = raw.get("required_compliance_checks", DEFAULT_COMPLIANCE_CHECKS) if isinstance(raw, dict) else DEFAULT_COMPLIANCE_CHECKS
    return [str(item).strip() for item in (checks or DEFAULT_COMPLIANCE_CHECKS) if str(item).strip()]


class _WorkbenchHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, _format: str, *args: Any) -> None:
        return

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path == "/health":
            self._send_json({
                "status": "ok",
                "generated_at": now_iso(),
                "instance_id": self.server.instance_id,
                "pid": os.getpid(),
            })
            return
        if parsed.path != "/":
            self._send_status_page(404, "Not Found", "The requested resource does not exist.")
            return
        query = parse_qs(parsed.query)
        message = _first_query(query, "message", "")
        view = _first_query(query, "view", "")
        if not view:
            # Backward compatibility: legacy remediation links/bookmarks used
            # ?status=... without an explicit view. Keep them landing on the
            # remediation queue; the new view=remediation semantics are unchanged.
            view = "remediation" if query.get("status") else "runtime"
        if view == "remediation":
            status = _first_query(query, "status", "open")
            risk = _first_query(query, "risk", "all")
            task_id = unquote_plus(_first_query(query, "task_id", ""))
            self._send_html(render_workbench_html(
                status=status,
                task_id=task_id,
                risk=risk,
                message=message,
                csrf_token=self.server.csrf_token,
            ))
            return
        if view == "review":
            status = _first_query(query, "review_status", "outstanding")
            priority = _first_query(query, "priority", "all")
            page = _first_query(query, "page", "1")
            signal_id = unquote_plus(_first_query(query, "signal_id", ""))
            self._send_html(render_review_queue_workbench_html(
                status=status,
                priority=priority,
                signal_id=signal_id,
                message=message,
                config=self.server.workbench_config,
                csrf_token=self.server.csrf_token,
                page=page,
            ))
            return
        if view == "pilot":
            self._send_html(render_production_pilot_workbench_html(
                message=message,
                config=self.server.workbench_config,
            ))
            return
        stage = _first_query(query, "stage", "all")
        priority = _first_query(query, "priority", "all")
        case_id = unquote_plus(_first_query(query, "case_id", ""))
        self._send_html(render_runtime_workbench_html(
            stage=stage,
            priority=priority,
            case_id=case_id,
            message=message,
            config=self.server.workbench_config,
            csrf_token=self.server.csrf_token,
        ))

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path == "/shutdown":
            try:
                length = int(self.headers.get("Content-Length", "0") or "0")
            except (TypeError, ValueError):
                self._send_plain(400, "Bad request: invalid content length")
                return
            if length != 0 or not self._valid_loopback_peer_headers():
                self._send_plain(400, "Bad request")
                return
            supplied = str(self.headers.get("X-Workbench-Instance", "") or "")
            expected = str(getattr(self.server, "instance_id", "") or "")
            if not supplied or not expected or not hmac.compare_digest(supplied, expected):
                self._send_plain(403, "Forbidden")
                return
            self._send_json({"status": "stopping"})
            threading.Thread(target=self.server.shutdown, daemon=True).start()
            return
        # Bounded request-body lifecycle. Validate Content-Length first so an
        # invalid or oversized declared body is rejected without blocking on
        # it, then for any declared body within the limit consume exactly that
        # body before sending any route/Host/Origin/content-type/CSRF error
        # response. Closing the connection with a declared body left unread
        # lets the OS reset the socket (Windows WinError 10054) before the
        # client reads the error response, so an unknown-route POST must still
        # drain its body before the 404 is written. A body that never
        # completes is answered with a sanitized 408 (stalled/timeout) or 400
        # (truncated) instead of letting the socket error escape do_POST.
        try:
            length = int(self.headers.get("Content-Length", "0") or "0")
        except (TypeError, ValueError):
            self._send_plain(400, "Bad request: invalid content length")
            return
        if length < 0 or length > MAX_FORM_BODY_BYTES:
            self._send_plain(400, "Bad request: form body too large")
            return
        try:
            raw_body = self._read_request_body(length)
        except OSError:
            # socket.timeout (== TimeoutError) and other socket failures while
            # reading a declared body must never surface as a traceback. The
            # body can no longer be completed, so close the connection and
            # answer with a sanitized 408 that leaks no exception details.
            self._send_plain(408, "Request Timeout")
            return
        if len(raw_body) != length:
            # The peer closed the stream before delivering the declared body;
            # a short read is a truncated body, not a complete one.
            self._send_plain(400, "Bad request: truncated request body")
            return
        raw = raw_body.decode("utf-8", errors="replace")
        # The route is only rejected once the declared body is fully consumed,
        # so a nonempty unknown-route POST drains cleanly before the 404.
        if parsed.path not in ("/runtime-action", "/review-action", "/action"):
            self._send_status_page(404, "Not Found", "The requested resource does not exist.")
            return
        if not self._valid_loopback_peer_headers():
            self._send_plain(400, "Bad request: invalid origin or host")
            return
        content_type = str(self.headers.get("Content-Type", "") or "").lower()
        if "application/x-www-form-urlencoded" not in content_type:
            self._send_plain(400, "Bad request: unsupported content type")
            return
        # Action fields are only parsed once the content type is confirmed to
        # be form-urlencoded.
        form = parse_qs(raw, keep_blank_values=True)
        if not self._valid_csrf(form):
            self._send_plain(400, "Bad request: invalid or missing CSRF token")
            return
        try:
            if parsed.path == "/runtime-action":
                result = submit_runtime_workbench_action(form, config=self.server.workbench_config)
                location = self._runtime_success_location(result)
            elif parsed.path == "/review-action":
                result = submit_review_queue_workbench_action(form, config=self.server.workbench_config)
                location = self._review_success_location(result)
            else:
                result = submit_workbench_action(form, config=self.server.workbench_config)
                location = self._remediation_success_location(result)
            self.send_response(303)
            self.send_header("Location", location)
            self.send_header("Content-Length", "0")
            for key, value in self._security_headers().items():
                self.send_header(key, value)
            self.end_headers()
        except ValueError as exc:
            self._send_status_page(400, "Bad Request", str(exc) or "Invalid request")
        except Exception:
            self._send_status_page(400, "Bad Request", "Invalid request")

    def _read_request_body(self, length: int) -> bytes:
        """Read up to ``length`` request body bytes, bounded by a short socket
        timeout.

        A declared body larger than what the client actually sends must not be
        able to block the handler forever. Normal UI posts are small and arrive
        promptly on loopback, so the timeout is only a safety net for truncated
        or stalled request bodies. An early EOF returns fewer than ``length``
        bytes (a truncated body); a stalled partial body raises socket.timeout
        (an OSError). The prior socket timeout is restored afterwards so
        keep-alive handling is unchanged. Callers must compare len() with the
        declared length and treat OSError as an unrecoverable request.
        """
        if length <= 0:
            return b""
        prior_timeout = self.connection.gettimeout()
        self.connection.settimeout(REQUEST_BODY_READ_TIMEOUT)
        try:
            return self.rfile.read(length)
        finally:
            self.connection.settimeout(prior_timeout)

    def _valid_loopback_peer_headers(self) -> bool:
        request_host = self.headers.get("Host", "")
        if not _is_loopback_host(request_host):
            return False
        origin = self.headers.get("Origin", "")
        if origin and not _is_loopback_origin(origin, request_host):
            return False
        return True

    def _valid_csrf(self, form: Dict[str, List[str]]) -> bool:
        values = form.get("csrf_token") or []
        token = values[-1] if values else ""
        expected = getattr(self.server, "csrf_token", "")
        if not token or not expected:
            return False
        try:
            return hmac.compare_digest(str(token), str(expected))
        except TypeError:
            return False

    def _runtime_success_location(self, result: Dict[str, Any]) -> str:
        record = result.get("record", {}) or {}
        return "/?view=runtime&case_id={0}&message={1}".format(
            quote_plus(str(record.get("case_id", ""))),
            quote_plus("recorded {0}".format(record.get("action", ""))),
        )

    def _remediation_success_location(self, result: Dict[str, Any]) -> str:
        record = result.get("record", {}) or {}
        return "/?status=all&risk=all&view=remediation&task_id={0}&message={1}".format(
            quote_plus(str(record.get("task_id", ""))),
            quote_plus("recorded {0} -> {1}".format(record.get("action", ""), record.get("status", ""))),
        )

    def _review_success_location(self, result: Dict[str, Any]) -> str:
        record = result.get("record", {}) or {}
        signal_id = str(record.get("signal_id") or "")
        status = str(record.get("to_status") or ("completed" if record.get("queue_action") == "complete_review" else "outstanding"))
        message = "recorded {0}".format(record.get("action") or record.get("queue_action") or "review action")
        if result.get("refresh_error"):
            message += "; " + str(result["refresh_error"])
        return "/?view=review&review_status={0}&signal_id={1}&message={2}".format(
            quote_plus(status), quote_plus(signal_id), quote_plus(message)
        )

    def _security_headers(self) -> Dict[str, str]:
        return {
            "Cache-Control": "no-store",
            "Pragma": "no-cache",
            "X-Content-Type-Options": "nosniff",
            "X-Frame-Options": "DENY",
            "Content-Security-Policy": CSP_HEADER_VALUE,
            "Referrer-Policy": "no-referrer",
        }

    def _send_bytes(self, status: int, content_type: str, payload: bytes) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(payload)))
        for key, value in self._security_headers().items():
            self.send_header(key, value)
        # On error responses the request body may be unread; close the connection
        # so any leftover bytes are not mistaken for a new request.
        if status >= 400:
            self.close_connection = True
            self.send_header("Connection", "close")
        self.end_headers()
        if payload:
            self.wfile.write(payload)

    def _send_html(self, text: str) -> None:
        self._send_bytes(200, "text/html; charset=utf-8", text.encode("utf-8"))

    def _send_json(self, data: Dict[str, Any]) -> None:
        payload = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self._send_bytes(200, "application/json; charset=utf-8", payload)

    def _send_plain(self, status: int, text: str) -> None:
        self._send_bytes(status, "text/plain; charset=utf-8", text.encode("utf-8"))

    def _send_status_page(self, status: int, title: str, message: str) -> None:
        body = "<!DOCTYPE html><html lang=\"zh-CN\"><head><meta charset=\"utf-8\"><title>{title}</title></head><body><h1>{status} {title}</h1><p>{message}</p></body></html>".format(
            status=status,
            title=_esc(title),
            message=_esc(message),
        )
        self._send_bytes(status, "text/html; charset=utf-8", body.encode("utf-8"))


def _render_status_tabs(state: Dict[str, Any]) -> str:
    counts = state.get("status_counts", {}) or {}
    items = []
    for status in ["open", "resolved", "rejected", "deferred", "all"]:
        count = len(state.get("all_tasks", [])) if status == "all" else counts.get(status, 0)
        cls = "active" if state.get("status") == status else ""
        items.append('<a class="{cls}" href="?status={status}&amp;risk={risk}&amp;view=remediation">{label} <span>{count}</span></a>'.format(
            cls=cls,
            status=status,
            risk=_esc(state.get("risk", "all")),
            label=status.title(),
            count=_esc(count),
        ))
    return '<nav class="tabs status-tabs">{0}</nav>'.format("\n".join(items))


def _render_risk_tabs(state: Dict[str, Any]) -> str:
    counts = state.get("risk_counts", {}) or {}
    items = []
    for risk in ["all", "high", "medium", "low"]:
        count = len(state.get("all_tasks", [])) if risk == "all" else counts.get(risk, 0)
        cls = "active" if state.get("risk") == risk else ""
        items.append('<a class="{cls}" href="?status={status}&amp;risk={risk}&amp;view=remediation">{label} <span>{count}</span></a>'.format(
            cls=cls,
            status=_esc(state.get("status", "open")),
            risk=risk,
            label="Risk {0}".format(risk.title()) if risk != "all" else "Risk All",
            count=_esc(count),
        ))
    return '<nav class="tabs risk-tabs">{0}</nav>'.format("\n".join(items))


def _render_priority_top10(state: Dict[str, Any]) -> str:
    top10 = list(state.get("tasks", []) or [])[:10]
    if not top10:
        return '<section class="panel"><h2>Today Priority Top 10</h2><p class="empty">No priority queue data.</p></section>'
    rows = []
    for task in top10[:10]:
        rows.append("<tr><td>{score}</td><td>{risk}</td><td><a href=\"?status=all&amp;risk=all&amp;view=remediation&amp;task_id={task_id}\">{label}</a></td><td>{gap}</td><td>{source}</td><td>{status}</td><td>{reasons}</td></tr>".format(
            score=_esc(task.get("review_priority_score", 0)),
            risk=_esc(task.get("evidence_risk_level", "")),
            task_id=quote_plus(str(task.get("task_id", ""))),
            label=_esc(task.get("task_id", "")),
            gap=_esc(task.get("gap_type", "")),
            source=_esc(task.get("source_id", "")),
            status=_esc(task.get("status", "")),
            reasons=_esc("; ".join(str(item) for item in task.get("priority_reasons", [])[:4])),
        ))
    return '<section class="panel"><h2>Today Priority Top 10</h2><table><tr><th>Score</th><th>Risk</th><th>Task</th><th>Gap</th><th>Source</th><th>Status</th><th>Reasons</th></tr>{0}</table></section>'.format("\n".join(rows))


def _render_task_list(state: Dict[str, Any]) -> str:
    rows = []
    for task in state.get("tasks", []) or []:
        task_id = str(task.get("task_id", ""))
        rows.append("<tr><td><a href=\"?status={status}&amp;risk={risk}&amp;view=remediation&amp;task_id={task_id}\">{label}</a></td><td>{task_type}</td><td>{source}</td><td>{gap}</td><td>{status_text}</td><td>{score}</td><td>{risk_level}</td></tr>".format(
            status=_esc(state.get("status", "open")),
            risk=_esc(state.get("risk", "all")),
            task_id=quote_plus(task_id),
            label=_esc(task_id),
            task_type=_esc(task.get("task_type", "")),
            source=_esc(task.get("source_id", "")),
            gap=_esc(task.get("gap", "")),
            status_text=_esc(task.get("status", "")),
            score=_esc(task.get("review_priority_score", 0)),
            risk_level=_esc(task.get("evidence_risk_level", "")),
        ))
    if not rows:
        return '<section class="panel"><h2>Tasks</h2><p class="empty">No tasks for this status.</p></section>'
    return '<section class="panel"><h2>Tasks</h2><table><tr><th>Task ID</th><th>Type</th><th>Source</th><th>Gap</th><th>Status</th><th>review_priority_score</th><th>Risk</th></tr>{0}</table></section>'.format("\n".join(rows))


def _render_task_detail(task: Dict[str, Any] | None, csrf_token: str = "") -> str:
    if not task:
        return '<section class="panel"><h2>Task Detail</h2><p class="empty">Select a task to review.</p></section>'
    task_json = _esc(json.dumps(task, ensure_ascii=False, indent=2, default=str))
    task_id = _esc(task.get("task_id", ""))
    return """<section class="panel">
<h2>Task Detail</h2>
<p><strong>{task_id}</strong></p>
<p>type={task_type} | source={source} | signal={signal} | gap={gap} | status={status} | review_priority_score={score} | risk={risk}</p>
<p>priority_reasons: {reasons}</p>
<form method="post" action="/action">
{csrf_hidden}
<input type="hidden" name="task_id" value="{task_id}">
<label>Actor <input name="actor" value="user"></label>
<label>Evidence URL <input name="evidence_url" placeholder="https://..."></label>
<label>Structured fields <textarea name="structured_fields" placeholder="key=value&#10;report_period=2026Q2"></textarea></label>
<label>Notes <textarea name="notes" placeholder="manual review note"></textarea></label>
<div class="actions">
<button name="action" value="attach_evidence_url">Attach Evidence URL</button>
<button name="action" value="attach_structured_fields">Attach Structured Fields</button>
<button name="action" value="resolve_remediation">Resolve</button>
<button name="action" value="reject_remediation">Reject</button>
<button name="action" value="defer_remediation">Defer</button>
</div>
</form>
<details><summary>Raw task</summary><pre>{task_json}</pre></details>
</section>""".format(
        task_id=task_id,
        task_type=_esc(task.get("task_type", "")),
        source=_esc(task.get("source_id", "")),
        signal=_esc(task.get("signal_id", "") or "-"),
        gap=_esc(task.get("gap", "")),
        status=_esc(task.get("status", "")),
        score=_esc(task.get("review_priority_score", 0)),
        risk=_esc(task.get("evidence_risk_level", "")),
        reasons=_esc("; ".join(str(item) for item in task.get("priority_reasons", []))),
        task_json=task_json,
        csrf_hidden=_csrf_hidden(csrf_token),
    )


def _render_recent_records(records: List[Dict[str, Any]]) -> str:
    if not records:
        return '<section class="panel"><h2>Recent Records</h2><p class="empty">No remediation records yet.</p></section>'
    items = []
    for record in records[-10:]:
        items.append("<li>{action} | {status} | {task} | {actor}</li>".format(
            action=_esc(record.get("action", "")),
            status=_esc(record.get("status", "")),
            task=_esc(record.get("task_id", "")),
            actor=_esc(record.get("actor", "")),
        ))
    return '<section class="panel"><h2>Recent Records</h2><ul>{0}</ul></section>'.format("\n".join(items))


def _parse_structured_fields(value: Any) -> Dict[str, str]:
    if isinstance(value, dict):
        return {str(k).strip(): str(v).strip() for k, v in value.items() if str(k).strip()}
    result: Dict[str, str] = {}
    for raw_line in str(value or "").replace("\r", "\n").split("\n"):
        line = raw_line.strip()
        if not line:
            continue
        if "=" not in line:
            raise ValueError("structured_fields line must be key=value: {0}".format(line))
        key, val = line.split("=", 1)
        key = key.strip()
        if not key:
            raise ValueError("structured_fields key cannot be empty")
        result[key] = val.strip()
    return result


def _first_query(query: Dict[str, List[str]], key: str, default: str) -> str:
    values = query.get(key)
    return values[-1] if values else default


def _normalize_status(status: str) -> str:
    value = str(status or "open").lower()
    return value if value in {"open", "resolved", "rejected", "deferred", "all"} else "open"


def _normalize_risk(risk: str) -> str:
    value = str(risk or "all").lower()
    return value if value in {"high", "medium", "low", "all"} else "all"


def _contains_trading_term(value: str) -> bool:
    text = str(value or "").lower()
    return any(term in text for term in TRADING_TERMS)


def _esc(value: Any) -> str:
    return html.escape(str(value), quote=True)


def _css() -> str:
    return """
* { box-sizing: border-box; }
body { margin: 0; font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Arial, sans-serif; background: #fff; color: #1f2328; font-size: 13px; line-height: 1.45; }
main { max-width: 1280px; margin: 0 auto; padding: 12px; }
h1 { font-size: 18px; margin: 0 0 12px; }
h2 { font-size: 13px; margin: 0 0 8px; }
a { color: #0969da; text-decoration: none; }
.tabs { display: flex; flex-wrap: wrap; gap: 0; border-bottom: 1px solid #d0d7de; margin-bottom: 10px; }
.tabs a { color: #57606a; padding: 6px 12px; border: 1px solid transparent; border-bottom: none; border-radius: 6px 6px 0 0; }
.tabs a.active { color: #1f2328; background: #fff; border-color: #d0d7de; }
.tabs.status-tabs a { font-size: 12px; padding: 4px 8px; }
.kpi-band { display: flex; flex-wrap: wrap; align-items: baseline; gap: 6px 18px; padding: 8px 0; border-bottom: 1px solid #eaeef2; margin-bottom: 8px; }
.kpi { display: inline-flex; align-items: baseline; gap: 6px; }
.kpi-label { color: #57606a; font-size: 12px; }
.kpi b { font-size: 15px; }
.stage-pill { color: #57606a; font-size: 12px; background: #f6f8fa; border: 1px solid #eaeef2; border-radius: 10px; padding: 1px 7px; }
.notice { background: #ddf4ff; border: 1px solid #54aeff; border-radius: 6px; padding: 8px 10px; margin-bottom: 10px; font-size: 13px; }
.notice.warning { background: #fff8c5; border-color: #d4a72c; }
.filters { display: flex; flex-wrap: wrap; gap: 10px; align-items: flex-end; margin-bottom: 10px; }
.filters label { font-weight: 600; font-size: 12px; }
.filters select, .filters button { font: inherit; padding: 4px 6px; }
.section { border: 1px solid #d0d7de; border-radius: 6px; margin: 0 0 10px; }
.section > h2 { background: #f6f8fa; border-bottom: 1px solid #d0d7de; padding: 6px 10px; margin: 0; }
.section > .body { padding: 8px 10px; }
.grid-2 { display: grid; grid-template-columns: 1fr 1fr; gap: 10px; }
.kvs { display: flex; flex-wrap: wrap; gap: 4px 22px; }
.kv { display: inline-flex; gap: 6px; }
.kv span { color: #57606a; }
.kv b { font-weight: 600; }
table { width: 100%; border-collapse: collapse; }
th, td { border-bottom: 1px solid #eaeef2; padding: 3px 6px; text-align: left; font-size: 12px; vertical-align: top; }
th { color: #57606a; font-weight: 600; white-space: nowrap; }
.pilot-gates { table-layout: fixed; }
.pilot-gates th:nth-child(1) { width: 18%; }
.pilot-gates th:nth-child(2) { width: 10%; }
.pilot-gates td { overflow-wrap: anywhere; word-break: break-word; }
.table-wrap { overflow-x: auto; }
.pagination { display: flex; align-items: center; justify-content: flex-end; gap: 12px; padding: 7px 10px; border-top: 1px solid #eaeef2; font-size: 12px; }
.pagination span[aria-disabled="true"] { color: #8c959f; }
tr.preserved { background: #fff8c5; }
.gate-pass { color: #1a7f37; font-weight: 600; }
.gate-blocked { color: #cf222e; font-weight: 600; }
.gate-pending { color: #9a6700; font-weight: 600; }
.empty { color: #6e7781; }
ul.plain { margin: 0; padding-left: 16px; }
.actions { margin-top: 6px; }
.actions button { margin: 4px 6px 4px 0; border: 1px solid #0969da; border-radius: 6px; background: #0969da; color: #fff; padding: 5px 9px; cursor: pointer; font: inherit; }
form { border: 1px solid #eaeef2; border-radius: 6px; padding: 8px 10px; margin-bottom: 10px; }
form label { display: block; margin: 6px 0; font-weight: 600; font-size: 12px; }
form input, form textarea, form select { display: block; width: 100%; margin-top: 3px; border: 1px solid #d0d7de; border-radius: 6px; padding: 5px 7px; font: inherit; }
form textarea { min-height: 56px; }
fieldset.checks { border: 1px solid #d0d7de; border-radius: 6px; padding: 6px 8px; margin: 6px 0; }
fieldset.checks label.check { font-weight: 400; display: block; }
fieldset.checks input { display: inline; width: auto; margin: 0 6px 0 0; }
.hint { color: #57606a; font-size: 12px; }
.panel { background: #fff; border: 1px solid #d0d7de; border-radius: 6px; padding: 14px; margin-bottom: 14px; }
footer { margin-top: 16px; font-size: 12px; color: #6e7781; }
pre { white-space: pre-wrap; background: #f6f8fa; padding: 10px; border-radius: 6px; }
@media (max-width: 720px) {
  .grid-2 { grid-template-columns: 1fr; }
  .kvs { gap: 2px 14px; }
  main { padding: 8px; }
  th, td { padding: 3px 4px; font-size: 11px; }
  .pilot-gates th:nth-child(1) { width: 36%; }
  .pilot-gates th:nth-child(2) { width: 18%; }
}
"""
