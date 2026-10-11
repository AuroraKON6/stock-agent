from __future__ import annotations

import json
from collections import Counter
from pathlib import Path
from typing import Any, Dict, List, Optional

from .brokerage_architecture import load_latest_architecture_state
from .config import project_path, project_path_override
from .models import now_iso
from .market_data_governance import load_latest_market_data_governance
from .review_queue import OPEN_STATUSES, load_latest_review_queue
from .review_priority import build_priority_queue
from .production_pilot import load_latest_production_pilot
from .pi_skills import pi_skill_supply_chain_status
from .research_workpapers import load_research_workpapers
from .evidence_vault import load_evidence_vault, verify_evidence_vault
from .business_onboarding import load_latest_business_onboarding
from .value_validation import load_latest_value_validation
from .backup_restore import load_latest_backup_restore
from .run_coordination import load_latest_run_coordination


def generate_dashboard(config: Optional[Dict[str, Any]] = None) -> str:
    ctx = _load_dashboard_context(config)
    html = _render_dashboard_html(ctx)
    stamp = now_iso().replace(":", "").replace("-", "").replace("+", "_").split(".")[0]
    dashboard_dir = project_path("reports/dashboard")
    dashboard_dir.mkdir(parents=True, exist_ok=True)
    latest_path = dashboard_dir / "latest.html"
    ts_path = dashboard_dir / "dashboard_{0}.html".format(stamp)
    latest_path.write_text(html, encoding="utf-8-sig")
    ts_path.write_text(html, encoding="utf-8-sig")
    return str(latest_path)


def _load_dashboard_context(config: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    with project_path_override(project_path):
        return _load_dashboard_context_scoped(config)


def _load_dashboard_context_scoped(config: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    report_data = _load_latest_json_report()
    current_research_cases = _load_current_research_cases()
    remediation = _load_latest_remediation_summary()
    alerts = _load_latest_alerts()
    review_queue = load_latest_review_queue(config or {})
    pending = [
        item for item in review_queue.get("items", [])
        if str(item.get("status") or "pending") in OPEN_STATUSES
    ] if review_queue else _load_latest_pending_review(config)
    review_summary = _load_latest_review_summary()
    decision_summary = _load_latest_decision_summary()
    workspace_inbox = _load_latest_workspace_inbox()
    research_runtime = _load_latest_research_runtime()
    model_operations = _load_latest_model_operations()
    market_data_governance = load_latest_market_data_governance(config or {})
    production_pilot = load_latest_production_pilot(config or {})
    business_onboarding = load_latest_business_onboarding(config or {})
    value_validation = load_latest_value_validation(config or {})
    backup_restore = load_latest_backup_restore(config or {})
    run_coordination = load_latest_run_coordination(config or {})
    pi_skill_supply_chain = pi_skill_supply_chain_status(config or {})
    workpapers_enabled = bool((config or {}).get("research_workpapers", {}).get("enabled", False))
    research_workpapers = load_research_workpapers(config or {}) if workpapers_enabled else {}
    evidence_vault_enabled = bool((config or {}).get("official_evidence_vault", {}).get("enabled", False))
    official_evidence_vault = load_evidence_vault(config or {}) if evidence_vault_enabled else {}
    if official_evidence_vault:
        official_evidence_vault["verification"] = verify_evidence_vault(config or {})
    notification_path = _load_notification_path()
    last_run = _load_last_scheduled_run()
    last_daily_run = _load_last_scheduled_run("daily")
    stats = dict(report_data.get("stats", {}) or {})
    stats.update(remediation)
    if workspace_inbox:
        stats["workspace_inbox"] = workspace_inbox
    if research_runtime:
        stats["research_runtime"] = research_runtime.get("summary", {})
        stats["research_runtime_cases"] = (research_runtime.get("runtime", {}) or {}).get("cases", [])
        stats["research_runtime_enabled"] = True
    if model_operations and not stats.get("model_operations"):
        stats["model_operations"] = model_operations
        stats["model_operations_enabled"] = True
    if review_queue:
        stats["review_queue_summary"] = review_queue.get("summary", {})
    if market_data_governance and not stats.get("market_data_governance"):
        stats["market_data_governance"] = market_data_governance
    stats["review_priority_queue"] = build_priority_queue()
    stats["brokerage_architecture"] = load_latest_architecture_state()
    if research_workpapers:
        stats["research_workpapers"] = research_workpapers.get("summary", {})
        stats["research_workpaper_items"] = research_workpapers.get("workpapers", [])
        stats["research_workpapers_enabled"] = workpapers_enabled
    if official_evidence_vault:
        stats["official_evidence_vault"] = official_evidence_vault.get("summary", {})
        stats["official_evidence_vault_enabled"] = evidence_vault_enabled
    return {
        "generated_at": now_iso(),
        "report_json_path": report_data.get("path", ""),
        "alert_path": alerts.get("path", ""),
        "signals": report_data.get("signals", []),
        "stats": stats,
        "team": report_data.get("team", {}),
        "team_ops": report_data.get("team_ops", {}),
        "research_cases": (
            current_research_cases
            if current_research_cases is not None
            else report_data.get("research_cases", [])
        ),
        "research_runtime": research_runtime.get("summary", {}) if research_runtime else report_data.get("research_runtime", {}),
        "research_runtime_cases": (research_runtime.get("runtime", {}) or {}).get("cases", []) if research_runtime else report_data.get("research_runtime_cases", []),
        "source_health": report_data.get("source_health", {}),
        "alerts_list": alerts.get("items", []),
        "pending_review": pending,
        "review_queue": review_queue,
        "review_summary": review_summary,
        "decision_summary": decision_summary,
        "notification_path": notification_path,
        "last_scheduled_run": last_run,
        "last_daily_run": last_daily_run,
        "production_pilot": production_pilot,
        "business_onboarding": business_onboarding,
        "value_validation": value_validation,
        "backup_restore": backup_restore,
        "run_coordination": run_coordination,
        "pi_skill_supply_chain": pi_skill_supply_chain,
        "research_workpapers": research_workpapers,
        "official_evidence_vault": official_evidence_vault,
    }


def _load_latest_json_report() -> Dict[str, Any]:
    json_dir = project_path("reports/json")
    if not json_dir.exists():
        return {"signals": [], "stats": {}, "team": {}, "team_ops": {}, "research_cases": [], "research_runtime": {}, "research_runtime_cases": [], "source_health": {}, "path": ""}
    files = sorted(json_dir.glob("*.json"))
    if not files:
        return {"signals": [], "stats": {}, "team": {}, "team_ops": {}, "research_cases": [], "research_runtime": {}, "research_runtime_cases": [], "source_health": {}, "path": ""}
    latest = files[-1]
    try:
        data = json.loads(latest.read_text(encoding="utf-8"))
        return {
            "signals": data.get("signals", []),
            "stats": data.get("stats", {}),
            "team": data.get("team", {}),
            "team_ops": data.get("team_ops", {}),
            "research_cases": data.get("research_cases", []),
            "research_runtime": data.get("research_runtime", {}),
            "research_runtime_cases": data.get("research_runtime_cases", []),
            "source_health": data.get("source_health", {}),
            "path": str(latest),
        }
    except (json.JSONDecodeError, OSError):
        return {"signals": [], "stats": {}, "team": {}, "team_ops": {}, "research_cases": [], "research_runtime": {}, "research_runtime_cases": [], "source_health": {}, "path": ""}


def _load_current_research_cases() -> Optional[List[Dict[str, Any]]]:
    path = project_path("data/research_cases/cases.json")
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except (json.JSONDecodeError, OSError):
        return None
    cases = data.get("cases") if isinstance(data, dict) else None
    return cases if isinstance(cases, list) else None


def _load_latest_alerts() -> Dict[str, Any]:
    alerts_dir = project_path("reports/alerts")
    if not alerts_dir.exists():
        return {"items": [], "path": ""}
    files = sorted(alerts_dir.glob("*.jsonl"))
    if not files:
        return {"items": [], "path": ""}
    latest = files[-1]
    items: List[Dict[str, Any]] = []
    try:
        for line in latest.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line:
                items.append(json.loads(line))
    except (json.JSONDecodeError, OSError):
        pass
    return {"items": items, "path": str(latest)}


def _load_latest_pending_review(config: Optional[Dict[str, Any]] = None) -> List[Dict[str, Any]]:
    with project_path_override(project_path):
        queue = load_latest_review_queue(config or {})
    if queue:
        return [
            item for item in queue.get("items", [])
            if str(item.get("status") or "pending") in OPEN_STATUSES
        ]
    review_dir = project_path("data/review")
    if not review_dir.exists():
        return []
    files = sorted(review_dir.glob("pending_review_*.jsonl"))
    if not files:
        return []
    latest = files[-1]
    items: List[Dict[str, Any]] = []
    try:
        for line in latest.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line:
                items.append(json.loads(line))
    except (json.JSONDecodeError, OSError):
        pass
    return items


def _load_latest_review_summary() -> Optional[Dict[str, Any]]:
    summary_dir = project_path("reports/review")
    if not summary_dir.exists():
        return None
    files = sorted(summary_dir.glob("review_summary_*.json"))
    if not files:
        return None
    latest = files[-1]
    try:
        return json.loads(latest.read_text(encoding="utf-8-sig"))
    except (json.JSONDecodeError, OSError):
        return None


def _load_latest_remediation_summary() -> Dict[str, Any]:
    remediation_dir = project_path("reports/remediation")
    latest = remediation_dir / "latest.json"
    if not latest.exists():
        return {}
    try:
        return json.loads(latest.read_text(encoding="utf-8-sig"))
    except (json.JSONDecodeError, OSError):
        return {}


def _load_latest_decision_summary() -> Optional[Dict[str, Any]]:
    summary_dir = project_path("reports/decisions")
    latest = summary_dir / "latest.json"
    if not latest.exists():
        return None
    try:
        return json.loads(latest.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None


def _load_latest_workspace_inbox() -> Optional[Dict[str, Any]]:
    workspace_dir = project_path("reports/workspace")
    latest = workspace_dir / "latest.json"
    if not latest.exists():
        return None
    try:
        data = json.loads(latest.read_text(encoding="utf-8-sig"))
        if isinstance(data, dict):
            data["workspace_latest_path"] = str(workspace_dir / "latest.md")
            data["workspace_latest_json_path"] = str(latest)
            return data
    except (json.JSONDecodeError, OSError):
        return None
    return None


def _load_latest_research_runtime() -> Optional[Dict[str, Any]]:
    path = project_path("reports/research_runtime/latest.json")
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
        return data if isinstance(data, dict) else None
    except (json.JSONDecodeError, OSError):
        return None


def _load_latest_model_operations() -> Optional[Dict[str, Any]]:
    path = project_path("reports/model_operations/latest.json")
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
        return data if isinstance(data, dict) else None
    except (json.JSONDecodeError, OSError):
        return None


def _load_notification_path() -> str:
    notify_dir = project_path("reports/notifications")
    if not notify_dir.exists():
        return ""
    latest = notify_dir / "latest.md"
    return str(latest) if latest.exists() else ""


def _load_last_scheduled_run(mode: str = "") -> Optional[Dict[str, Any]]:
    log_dir = project_path("logs/scheduled")
    if not log_dir.exists():
        return None
    files = sorted(log_dir.glob("run_*.summary.json"))
    if not files:
        return None
    for path in reversed(files):
        try:
            value = json.loads(path.read_text(encoding="utf-8-sig"))
        except (json.JSONDecodeError, OSError):
            continue
        if isinstance(value, dict) and (not mode or value.get("mode") == mode):
            return value
    return None


def _render_dashboard_html(ctx: Dict[str, Any]) -> str:
    signals = ctx.get("signals", [])
    stats = ctx.get("stats", {})
    team = ctx.get("team", {})
    team_ops = ctx.get("team_ops", {})
    research_cases = ctx.get("research_cases", [])
    research_runtime = ctx.get("research_runtime", {}) or stats.get("research_runtime", {})
    research_runtime_cases = ctx.get("research_runtime_cases", []) or stats.get("research_runtime_cases", [])
    source_health = ctx.get("source_health", {}) or stats.get("source_health", {})
    run_ops = stats.get("run_ops", {})
    external_dependencies = {
        "market_resilience": stats.get("market_resilience", {}),
        "market_data_governance": stats.get("market_data_governance", {}),
        "llm_connectivity": stats.get("llm_connectivity", {}),
    }
    alerts_list = ctx.get("alerts_list", [])
    pending = ctx.get("pending_review", [])
    review_summary = ctx.get("review_summary")
    decision_summary = ctx.get("decision_summary")

    priority_counts = Counter(s.get("priority", "P3") for s in signals)
    llm_count = sum(1 for s in signals if s.get("llm_analysis"))
    source_error_count = sum(1 for s in signals if s.get("source_type") == "source_error")
    price_signals = [s for s in signals if s.get("source_type") == "price"]
    filing_signals = [s for s in signals if isinstance((s.get("payload") or {}).get("filing_evidence"), dict)]
    high_priority = [s for s in signals if s.get("priority") in ("P0", "P1", "P2")]
    llm_signals = [s for s in signals if s.get("llm_analysis")]
    role_signals = [s for s in signals if s.get("role_analyses") or s.get("role_synthesis")]

    has_llm = llm_count > 0
    has_pending = len(pending) > 0
    has_review = review_summary is not None and review_summary.get("total", 0) > 0

    css = _dashboard_css()
    sections = []

    # A: Status bar
    sections.append(_section_status_bar(ctx, has_llm, has_pending, source_error_count))

    # B: KPI cards
    sections.append(_section_kpi_cards(
        len(signals), priority_counts, len(alerts_list), llm_count, len(pending), source_error_count,
    ))
    sections.append(_section_brokerage_architecture(stats.get("brokerage_architecture", {})))
    sections.append(_section_production_pilot(ctx.get("production_pilot", {})))
    sections.append(_section_value_validation(ctx.get("value_validation", {})))
    sections.append(_section_backup_restore(ctx.get("backup_restore", {})))
    sections.append(_section_run_coordination(ctx.get("run_coordination", {}) or stats.get("run_coordination", {})))
    sections.append(_section_business_onboarding(ctx.get("business_onboarding", {})))
    sections.append(_section_pi_skill_supply_chain(ctx.get("pi_skill_supply_chain", {})))

    # Team operations
    sections.append(_section_research_team(team, team_ops, stats, decision_summary))
    sections.append(_section_human_workbench(team_ops))

    # Source health
    sections.append(_section_source_health(source_health))
    sections.append(_section_source_remediation(stats, source_health))

    # Runtime operations
    sections.append(_section_runtime_ops(run_ops))
    sections.append(_section_model_operations(
        stats.get("model_operations", {}),
        ctx.get("last_daily_run") or ctx.get("last_scheduled_run") or {},
    ))

    # External dependencies
    sections.append(_section_external_dependencies(external_dependencies))

    # Research cases
    sections.append(_section_research_cases(research_cases, stats))
    sections.append(_section_research_runtime(research_runtime, research_runtime_cases, stats))
    sections.append(_section_official_evidence_vault(ctx.get("official_evidence_vault", {})))
    sections.append(_section_research_workpapers(ctx.get("research_workpapers", {})))
    sections.append(_section_research_git(stats))
    sections.append(_section_workspace_inbox(stats.get("workspace_inbox", {})))

    # C: Price snapshot
    sections.append(_section_price_table(price_signals))

    # C2: Filing/financial evidence
    sections.append(_section_filing_evidence(filing_signals, stats))

    # D: High priority signals
    sections.append(_section_high_priority(high_priority))

    # E: LLM analysis
    sections.append(_section_llm_analysis(llm_signals))
    sections.append(_section_multi_role_analysis(role_signals, stats))

    # F: Pending review queue
    sections.append(_section_review_queue_summary(ctx.get("review_queue", {}), stats.get("review_queue_summary", {})))
    sections.append(_section_pending_review(pending))

    # G: Review stats
    sections.append(_section_review_stats(review_summary))

    body = "\n".join(sections)
    return _html_template(css, body, ctx.get("generated_at", ""))


def _dashboard_css() -> str:
    return """
* { margin: 0; padding: 0; box-sizing: border-box; }
html { max-width: 100%; overflow-x: hidden; overflow-x: clip; }
body { max-width: 100%; overflow-x: hidden; overflow-x: clip; font-family: -apple-system, 'Segoe UI', Consolas, monospace; background: #f5f5f5; color: #333; font-size: 14px; line-height: 1.5; }
.container { max-width: 1200px; margin: 0 auto; padding: 16px; }
h1 { font-size: 20px; margin-bottom: 12px; color: #1a1a1a; }
h2 { font-size: 16px; margin: 16px 0 8px; color: #444; border-bottom: 1px solid #ddd; padding-bottom: 4px; }
.status-bar { background: #1a1a2e; color: #eee; padding: 12px 16px; border-radius: 6px; margin-bottom: 16px; font-size: 13px; }
.status-bar .flag { display: inline-block; margin-right: 12px; }
.status-bar .flag.ok { color: #4caf50; }
.status-bar .flag.warn { color: #ff9800; }
.kpi-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(140px, 1fr)); gap: 12px; margin-bottom: 16px; }
.kpi-card { background: #fff; border-radius: 6px; padding: 12px; text-align: center; box-shadow: 0 1px 3px rgba(0,0,0,0.1); }
.kpi-card .value { font-size: 28px; font-weight: bold; }
.kpi-card .label { font-size: 12px; color: #666; margin-top: 4px; }
.kpi-card.p0 .value { color: #d32f2f; }
.kpi-card.p1 .value { color: #e65100; }
.kpi-card.p2 .value { color: #1565c0; }
.kpi-card.p3 .value { color: #757575; }
table { width: 100%; border-collapse: collapse; background: #fff; border-radius: 6px; overflow: hidden; box-shadow: 0 1px 3px rgba(0,0,0,0.1); margin-bottom: 16px; }
th, td { padding: 8px 12px; text-align: left; border-bottom: 1px solid #eee; font-size: 13px; }
th { background: #f0f0f0; font-weight: 600; }
.prio-tag { display: inline-block; padding: 1px 6px; border-radius: 3px; font-size: 12px; font-weight: bold; }
.prio-P0 { background: #d32f2f; color: #fff; }
.prio-P1 { background: #e65100; color: #fff; }
.prio-P2 { background: #1565c0; color: #fff; }
.prio-P3 { background: #9e9e9e; color: #fff; }
.card { background: #fff; border-radius: 6px; padding: 16px; box-shadow: 0 1px 3px rgba(0,0,0,0.1); margin-bottom: 16px; }
.card p, .card li, .card code, .status-bar { overflow-wrap: anywhere; word-break: break-word; }
.empty-state { color: #999; font-style: italic; padding: 20px; text-align: center; }
.llm-card { border-left: 3px solid #1565c0; margin-bottom: 12px; }
.risk-item { color: #e65100; }
.review-item { color: #d32f2f; }
.direction-positive { color: #4caf50; }
.direction-negative { color: #d32f2f; }
.direction-unknown { color: #999; }
.pi-supply-hash code { overflow-wrap: anywhere; }
.pi-method-list { border: 1px solid #e5e5e5; border-radius: 4px; overflow: hidden; }
.pi-method-row { display: grid; grid-template-columns: minmax(120px, .8fr) 80px minmax(260px, 3fr) minmax(140px, 1fr); gap: 12px; padding: 9px 12px; border-bottom: 1px solid #eee; align-items: start; }
.pi-method-row:last-child { border-bottom: 0; }
.pi-method-field { min-width: 0; overflow-wrap: anywhere; }
.pi-method-label { display: block; color: #777; font-size: 11px; margin-bottom: 2px; }
.table-scroll { display: block; width: 100%; max-width: 100%; min-width: 0; overflow-x: auto; contain: inline-size; }
.onboarding-mobile { display: none; }
.onboarding-mobile-row { padding: 10px 0; border-bottom: 1px solid #e8e8e8; }
.onboarding-mobile-row:last-child { border-bottom: 0; }
.onboarding-mobile-head { display: flex; justify-content: space-between; gap: 10px; align-items: flex-start; font-weight: 600; }
.onboarding-mobile-head span:first-child { min-width: 0; overflow-wrap: anywhere; }
.onboarding-mobile-meta { display: grid; grid-template-columns: minmax(0, 1fr) auto; gap: 8px; margin-top: 4px; color: #666; font-size: 12px; }
.onboarding-mobile-detail { margin-top: 6px; font-size: 12px; overflow-wrap: anywhere; word-break: break-word; }
footer { text-align: center; color: #999; font-size: 12px; margin-top: 24px; padding: 12px; }
@media (max-width: 700px) {
  .container { padding: 10px; }
  table { display: block; width: 100%; max-width: 100%; min-width: 0; overflow-x: auto; -webkit-overflow-scrolling: touch; }
  .onboarding-desktop { display: none; }
  .onboarding-mobile { display: block; }
  .onboarding-mobile-meta { grid-template-columns: minmax(0, 1fr); }
  .pi-method-row { grid-template-columns: minmax(0, 1fr) 64px; gap: 8px 10px; }
  .pi-method-skills, .pi-method-hash { grid-column: 1 / -1; }
  .pi-method-hash code, .pi-supply-hash code { word-break: break-all; }
}
"""


def _html_template(css: str, body: str, generated_at: str) -> str:
    return """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<link rel="icon" href="data:,">
<title>A股智能投研看板</title>
<style>{css}</style>
</head>
<body>
<div class="container">
<h1>A股智能投研看板</h1>
{body}
<footer>生成时间: {generated_at} | A股智能投研系统 v1</footer>
</div>
</body>
</html>""".format(css=css, body=body, generated_at=generated_at)


def _section_status_bar(ctx: Dict[str, Any], has_llm: bool, has_pending: bool, source_errors: int) -> str:
    flags = []
    flags.append('<span class="flag {0}">LLM: {1}</span>'.format("ok" if has_llm else "warn", "已研判" if has_llm else "未启用"))
    flags.append('<span class="flag {0}">待复盘: {1}</span>'.format("warn" if has_pending else "ok", "有" if has_pending else "无"))
    flags.append('<span class="flag {0}">数据源异常: {1}</span>'.format("warn" if source_errors > 0 else "ok", source_errors))
    pilot = ctx.get("production_pilot", {}) or {}
    pilot_decision = str(pilot.get("go_live_decision") or "UNKNOWN")
    flags.append('<span class="flag {0}">生产门禁: {1}</span>'.format(
        "ok" if pilot_decision == "GO" else "warn", _esc(pilot_decision)
    ))
    onboarding = ctx.get("business_onboarding", {}) or {}
    onboarding_technical = str(onboarding.get("technical_status") or "UNKNOWN")
    onboarding_business = str(onboarding.get("business_status") or "BLOCKED")
    flags.append('<span class="flag {0}">组织接入: {1}</span>'.format(
        "ok" if onboarding_technical == "READY" and onboarding_business == "READY" else "warn",
        _esc("{0}/{1}".format(onboarding_technical, onboarding_business)),
    ))
    value_validation = ctx.get("value_validation", {}) or {}
    value_status = str(value_validation.get("operating_status") or "NOT_STARTED")
    value_ready = bool(value_validation.get("value_gate_ready", False))
    flags.append('<span class="flag {0}">价值验证: {1}</span>'.format(
        "ok" if value_ready else "warn", _esc(value_status)
    ))
    recovery = ctx.get("backup_restore", {}) or {}
    recovery_technical = str(recovery.get("technical_status") or "UNKNOWN")
    recovery_production = str(recovery.get("production_readiness") or "BLOCKED")
    flags.append('<span class="flag {0}">恢复演练: {1}</span>'.format(
        "ok" if recovery_technical == "PASS" else "warn",
        _esc("{0}/{1}".format(recovery_technical, recovery_production)),
    ))
    coordination = ctx.get("run_coordination", {}) or {}
    coordination_technical = str(coordination.get("technical_status") or "UNKNOWN")
    coordination_stale = len(coordination.get("stale_leases", []) or [])
    flags.append('<span class="flag {0}">运行协调: {1}</span>'.format(
        "ok" if coordination_technical == "PASS" and coordination_stale == 0 else "warn",
        _esc("{0}/stale={1}".format(coordination_technical, coordination_stale)),
    ))
    workpapers = ctx.get("research_workpapers", {}) or {}
    workpaper_summary = workpapers.get("summary", {}) or {}
    workpaper_integrity = str(workpapers.get("integrity_status") or "UNKNOWN")
    flags.append('<span class="flag {0}">底稿链: {1}</span>'.format(
        "ok" if workpaper_integrity == "OK" else "warn",
        _esc("{0}/{1}".format(workpaper_integrity, workpaper_summary.get("business_status", "BLOCKED"))),
    ))
    vault = ctx.get("official_evidence_vault", {}) or {}
    vault_verification = vault.get("verification", {}) or {}
    vault_technical = str(vault_verification.get("technical_status") or "UNKNOWN")
    vault_documents = int(vault_verification.get("document_count", 0) or 0)
    flags.append('<span class="flag {0}">官方原件库: {1}</span>'.format(
        "ok" if vault_technical == "READY" else "warn",
        _esc("{0}/{1}".format(vault_technical, vault_documents)),
    ))
    report_path = ctx.get("report_json_path", "")
    alert_path = ctx.get("alert_path", "")
    notification_path = ctx.get("notification_path", "")
    last_run = ctx.get("last_scheduled_run")
    handoff_run = ctx.get("last_daily_run") or last_run

    scheduled_html = ""
    if last_run:
        run_mode = last_run.get("mode", "?")
        run_started = last_run.get("started_at", "?")
        run_exit = last_run.get("exit_code", -1)
        run_ok = "ok" if run_exit == 0 else "warn"
        run_status = "成功" if run_exit == 0 else "失败(exit={0})".format(run_exit)
        # Truncate ISO timestamp for display
        if "T" in run_started:
            run_display = run_started.replace("T", " ").split(".")[0]
        else:
            run_display = run_started
        scheduled_html = '<br><small>上次定时运行: {time} | 模式: {mode} | 状态: <span class="flag {cls}">{status}</span></small>'.format(
            time=run_display, mode=run_mode, cls=run_ok, status=run_status,
        )
        if handoff_run and "agent_handoff_public_signal_count" in handoff_run:
            handoff_count = int(handoff_run.get("agent_handoff_public_signal_count", 0) or 0)
            model_calls = int(handoff_run.get("pi_model_call_count", 0) or 0)
            called = bool(handoff_run.get("used_pi"))
            skip_reason = str(handoff_run.get("pi_skip_reason") or "")
            scheduled_html += (
                '<br><small>公开交接: {count} 条 | Pi Agent: {called} | '
                '观测模型调用: {calls}{reason}</small>'
            ).format(
                count=handoff_count,
                called="已调用" if called else "已跳过",
                calls=model_calls,
                reason=" | 原因: {0}".format(_esc(skip_reason)) if skip_reason else "",
            )
        elif handoff_run and "openclaw_handoff_public_signal_count" in handoff_run:
            handoff_count = int(handoff_run.get("openclaw_handoff_public_signal_count", 0) or 0)
            scheduled_html += '<br><small>历史 OpenClaw 交接: {0} 条（只读留档）</small>'.format(handoff_count)
    else:
        scheduled_html = '<br><small>定时运行: 暂无记录</small>'

    return """<div class="status-bar">
<strong>状态</strong> &nbsp; {flags}
<br><small>JSON报告: {report} | 预警: {alert} | 通知: {notify}</small>
{scheduled}
</div>""".format(
        flags=" ".join(flags),
        report=report_path or "无",
        alert=alert_path or "无",
        notify=notification_path or "无",
        scheduled=scheduled_html,
    )


def _section_kpi_cards(
    total: int,
    priority_counts: Counter,
    alert_count: int,
    llm_count: int,
    pending_count: int,
    source_error_count: int,
) -> str:
    cards = []
    cards.append(_kpi_card("今日信号", total, ""))
    for p in ["P0", "P1", "P2", "P3"]:
        cards.append(_kpi_card(p, priority_counts.get(p, 0), p.lower()))
    cards.append(_kpi_card("预警", alert_count, ""))
    cards.append(_kpi_card("LLM研判", llm_count, ""))
    cards.append(_kpi_card("待复盘", pending_count, ""))
    cards.append(_kpi_card("源异常", source_error_count, "warn" if source_error_count else ""))
    return '<h2>KPI 概览</h2>\n<div class="kpi-grid">{0}</div>'.format("\n".join(cards))


def _kpi_card(label: str, value: int, cls: str) -> str:
    extra = ' {0}'.format(cls) if cls else ""
    return '<div class="kpi-card{0}"><div class="value">{1}</div><div class="label">{2}</div></div>'.format(
        extra, value, label
    )


def _section_production_pilot(pilot: Dict[str, Any]) -> str:
    if not pilot:
        return '<h2>受控生产试运行</h2>\n<div class="card"><div class="empty-state">尚无生产试运行对账。运行 production-pilot-sync。</div></div>'
    gates = pilot.get("gates", []) or []
    rows = []
    for gate in gates:
        status = str(gate.get("status") or "UNKNOWN")
        rows.append(
            '<tr><td>{gate}</td><td><span class="flag {cls}">{status}</span></td><td>{detail}</td></tr>'.format(
                gate=_esc(gate.get("gate", "")),
                cls="ok" if status == "PASS" else "warn",
                status=_esc(status),
                detail=_esc(gate.get("detail", "")),
            )
        )
    provider = pilot.get("provider_registry", {}) or {}
    shadow = pilot.get("shadow_evidence", {}) or {}
    capacity = pilot.get("review_capacity", {}) or {}
    blockers = pilot.get("go_live_blockers", []) or []
    blocker_html = "".join("<li>{0}</li>".format(_esc(value)) for value in blockers[:12]) or "<li>none</li>"
    decision = str(pilot.get("go_live_decision") or "NO_GO")
    return """<h2>受控生产试运行</h2>
<div class="card">
<p><strong>决策:</strong> <span class="flag {decision_cls}">{decision}</span> | <strong>技术:</strong> {technical} | <strong>候选:</strong> {candidate} | <strong>有效模式:</strong> {mode}</p>
<p><strong>候选指纹:</strong> {fingerprint}</p>
<p><strong>供应商:</strong> active={active}/{required} | registry integrity={integrity} | <strong>影子证据:</strong> runs={runs}/{min_runs}, days={days}/{min_days}</p>
<p><strong>人工复核:</strong> outstanding={outstanding} | overdue={overdue} | reviewers={reviewers} | suggested batches={batches} | 自动分派=false</p>
<table><tr><th>门禁</th><th>状态</th><th>细节</th></tr>{rows}</table>
<p><strong>阻断动作</strong></p><ul>{blockers}</ul>
<p><strong>边界:</strong> {boundary}</p>
</div>""".format(
        decision_cls="ok" if decision == "GO" else "warn",
        decision=_esc(decision),
        technical=_esc(pilot.get("technical_readiness", "UNKNOWN")),
        candidate=_esc(pilot.get("candidate_readiness", "UNKNOWN")),
        mode=_esc(pilot.get("effective_mode", "shadow")),
        fingerprint=_esc(pilot.get("candidate_fingerprint", "")),
        active=_esc(len(provider.get("active_provider_ids", []))),
        required=_esc((pilot.get("requirements") or {}).get("minimum_active_providers", 0)),
        integrity=_esc(provider.get("integrity_status", "UNKNOWN")),
        runs=_esc(shadow.get("qualifying_run_count", 0)),
        min_runs=_esc(shadow.get("minimum_runs", 0)),
        days=_esc(shadow.get("qualifying_trading_day_count", 0)),
        min_days=_esc(shadow.get("minimum_trading_days", 0)),
        outstanding=_esc(capacity.get("outstanding_count", 0)),
        overdue=_esc(capacity.get("overdue_count", 0)),
        reviewers=_esc(capacity.get("eligible_human_reviewer_count", 0)),
        batches=_esc(capacity.get("suggested_batch_count", 0)),
        rows="".join(rows),
        blockers=blocker_html,
        boundary=_esc(pilot.get("safety_boundary", "")),
    )


def _section_value_validation(snapshot: Dict[str, Any]) -> str:
    if not snapshot:
        return (
            '<h2>G0.5 价值验证</h2>\n<div class="card"><div class="empty-state">'
            '尚无价值验证投影。运行 value-validation-status；该命令不会创建研究或调用模型。'
            '</div></div>'
        )
    aggregate = snapshot.get("aggregate", {}) or {}
    cohort = snapshot.get("cohort", {}) or {}
    decision = snapshot.get("decision", {}) or {}
    gate_rows = []
    for gate in snapshot.get("gates", []) or []:
        status = str(gate.get("status") or "FAIL")
        gate_rows.append(
            '<tr><td>{gate}</td><td><span class="flag {cls}">{status}</span></td>'
            '<td>{detail}</td></tr>'.format(
                gate=_esc(gate.get("gate", "")),
                cls="ok" if status == "PASS" else "warn",
                status=_esc(status),
                detail=_esc(gate.get("detail", "")),
            )
        )
    next_actions = "".join(
        "<li>{0}</li>".format(_esc(item))
        for item in (snapshot.get("next_actions", []) or [])[:10]
    ) or "<li>none</li>"
    recommendation = str(snapshot.get("recommended_decision") or "INSUFFICIENT_EVIDENCE")
    human_decision = str(decision.get("decision") or "NOT_RECORDED")
    return """<h2>G0.5 价值验证</h2>
<div class="card">
<p><strong>运行状态:</strong> <span class="flag {ready_cls}">{operating}</span> | <strong>事件链:</strong> {integrity} | <strong>研究:</strong> {study}</p>
<p><strong>冻结队列:</strong> {cohort} | <strong>合格配对:</strong> {qualifying} | <strong>盲评完成:</strong> {blind}</p>
<p><strong>人工耗时改善:</strong> 中位数 {median:.1%}，加权 {weighted:.1%} | <strong>质量差:</strong> {quality_delta:.2f} | <strong>模型成本:</strong> CNY {cost:.4f}</p>
<p><strong>未授权外发:</strong> {egress} | <strong>协议偏差案件:</strong> {deviations}</p>
<p><strong>系统建议:</strong> {recommendation} | <strong>人工决定:</strong> {human_decision}</p>
<div class="table-scroll"><table><tr><th>门禁</th><th>状态</th><th>证据</th></tr>{gates}</table></div>
<p><strong>下一步</strong></p><ul>{actions}</ul>
<p><strong>边界:</strong> {boundary}</p>
</div>""".format(
        ready_cls="ok" if snapshot.get("value_gate_ready") else "warn",
        operating=_esc(snapshot.get("operating_status", "NOT_STARTED")),
        integrity=_esc(snapshot.get("integrity_status", "UNKNOWN")),
        study=_esc(snapshot.get("study_id", "")),
        cohort=_esc(cohort.get("case_count", 0)),
        qualifying=_esc(aggregate.get("qualifying_case_count", 0)),
        blind=_esc(aggregate.get("blind_complete_count", 0)),
        median=float(aggregate.get("median_efficiency_improvement", 0) or 0),
        weighted=float(aggregate.get("weighted_efficiency_improvement", 0) or 0),
        quality_delta=float(aggregate.get("quality_delta", 0) or 0),
        cost=float(aggregate.get("estimated_cost_cny", 0) or 0),
        egress=_esc(aggregate.get("unauthorized_egress_count", 0)),
        deviations=_esc(aggregate.get("protocol_deviation_case_count", 0)),
        recommendation=_esc(recommendation),
        human_decision=_esc(human_decision),
        gates="".join(gate_rows) or '<tr><td colspan="3">No gate projection.</td></tr>',
        actions=next_actions,
        boundary=_esc(snapshot.get("safety_boundary", "")),
    )


def _section_backup_restore(snapshot: Dict[str, Any]) -> str:
    if not snapshot:
        return (
            '<h2>备份与恢复</h2>\n<div class="card"><div class="empty-state">'
            '尚无恢复演练证据。先运行 backup-create，再运行 restore-drill。'
            '</div></div>'
        )
    backup = snapshot.get("backup", {}) or {}
    drill = snapshot.get("restore_drill", {}) or {}
    rpo = snapshot.get("rpo", {}) or {}
    rto = snapshot.get("rto", {}) or {}
    blockers = "".join(
        "<li>{0}</li>".format(_esc(item))
        for item in (snapshot.get("production_blockers", []) or [])[:10]
    ) or "<li>none</li>"
    technical = str(snapshot.get("technical_status") or "BLOCKED")
    production = str(snapshot.get("production_readiness") or "BLOCKED")
    return """<h2>备份与恢复</h2>
<div class="card">
<p><strong>本机技术演练:</strong> <span class="flag {technical_cls}">{technical}</span> |
<strong>生产灾备:</strong> <span class="flag {production_cls}">{production}</span></p>
<p><strong>备份:</strong> {backup_status} | <strong>Manifest:</strong> <code>{manifest}</code> |
<strong>隔离恢复:</strong> {drill_status}</p>
<p><strong>备份年龄:</strong> {age} 分钟 | <strong>RPO 周期证据:</strong> {rpo_evidence} |
<strong>本机恢复耗时:</strong> {rto_seconds} 秒</p>
<p><strong>生产缺口</strong></p><ul>{blockers}</ul>
<p><strong>边界:</strong> 本机 PASS 不代表加密、异地、全服务 RTO 或生产批准完成。</p>
</div>""".format(
        technical_cls="ok" if technical == "PASS" else "warn",
        technical=_esc(technical),
        production_cls="ok" if production == "READY" else "warn",
        production=_esc(production),
        backup_status=_esc(backup.get("status", "MISSING")),
        manifest=_esc(backup.get("manifest_id", "")),
        drill_status=_esc(drill.get("status", "MISSING")),
        age=_esc(rpo.get("latest_backup_age_minutes", "")),
        rpo_evidence=_esc(rpo.get("recurring_interval_evidence", "NOT_EVIDENCED")),
        rto_seconds=_esc(rto.get("latest_drill_elapsed_seconds", 0)),
        blockers=blockers,
    )


def _section_run_coordination(snapshot: Dict[str, Any]) -> str:
    if not snapshot:
        return (
            '<h2>运行协调与检查点</h2>\n<div class="card"><div class="empty-state">'
            '尚无运行协调投影。启用 run_coordination 后运行一次 pipeline。'
            '</div></div>'
        )
    technical = str(snapshot.get("technical_status") or "BLOCKED")
    production = str(snapshot.get("production_readiness") or "BLOCKED")
    ledger = snapshot.get("ledger", {}) or {}
    last_run = snapshot.get("last_run") or {}
    blockers = "".join(
        "<li>{0}</li>".format(_esc(item))
        for item in (snapshot.get("technical_blockers", []) or [])[:10]
    ) or "<li>none</li>"
    warnings = "".join(
        "<li>{0}</li>".format(_esc(item))
        for item in (snapshot.get("warnings", []) or [])[:10]
    ) or "<li>none</li>"
    return """<h2>运行协调与检查点</h2>
<div class="card">
<p><strong>技术状态:</strong> <span class="flag {technical_cls}">{technical}</span> |
<strong>生产就绪:</strong> <span class="flag warn">{production}</span></p>
<p><strong>账本:</strong> {ledger_path} | records={records} | integrity={integrity} |
<strong>活动租约:</strong> {active} | <strong>过期租约:</strong> {stale} | <strong>未完成运行:</strong> {incomplete}</p>
<p><strong>最近运行:</strong> {run_id} | mode={mode} | status={run_status} | checkpoints={checkpoints}</p>
<p><strong>技术阻断</strong></p><ul>{blockers}</ul>
<p><strong>提示</strong></p><ul>{warnings}</ul>
<p><strong>边界:</strong> 本地租约和检查点只证明技术协调，不构成业务批准或生产启用。</p>
</div>""".format(
        technical_cls="ok" if technical == "PASS" else "warn",
        technical=_esc(technical),
        production=_esc(production),
        ledger_path=_esc(ledger.get("path", "")),
        records=_esc(ledger.get("record_count", 0)),
        integrity=_esc(ledger.get("integrity_status", "FAIL")),
        active=_esc(len(snapshot.get("active_leases", []) or [])),
        stale=_esc(len(snapshot.get("stale_leases", []) or [])),
        incomplete=_esc(len(snapshot.get("incomplete_runs", []) or [])),
        run_id=_esc(last_run.get("run_id", "-")),
        mode=_esc(last_run.get("mode", "-")),
        run_status=_esc(last_run.get("status", "-")),
        checkpoints=_esc(last_run.get("checkpoint_count", 0)),
        blockers=blockers,
        warnings=warnings,
    )


def _section_business_onboarding(snapshot: Dict[str, Any]) -> str:
    if not snapshot:
        return '<h2>Business Onboarding</h2>\n<div class="card"><div class="empty-state">尚无组织接入状态。运行 business-onboarding-sync。</div></div>'
    stage_rows = []
    stage_mobile_rows = []
    for index, stage in enumerate(snapshot.get("stages", []) or [], 1):
        status = str(stage.get("status") or "BLOCKED")
        blockers = "; ".join((stage.get("blockers") or [])[:4]) or "none"
        stage_rows.append(
            "<tr><td>{index}</td><td>{phase}</td><td>{owner}</td><td><span class=\"flag {cls}\">{status}</span></td>"
            "<td>{ready}/{required}</td><td>{blockers}</td></tr>".format(
                index=index,
                phase=_esc(stage.get("phase", "")),
                owner=_esc(stage.get("owner_role", "")),
                cls="ok" if status == "PASS" else "warn",
                status=_esc(status),
                ready=_esc(stage.get("ready_count", 0)),
                required=_esc(stage.get("required_count", 0)),
                blockers=_esc(blockers),
            )
        )
        stage_mobile_rows.append(
            '<div class="onboarding-mobile-row"><div class="onboarding-mobile-head">'
            '<span>{index}. {phase}</span><span class="flag {cls}">{status}</span></div>'
            '<div class="onboarding-mobile-meta"><span>负责人: {owner}</span><span>进度: {ready}/{required}</span></div>'
            '<div class="onboarding-mobile-detail">阻断: {blockers}</div></div>'.format(
                index=index,
                phase=_esc(stage.get("phase", "")),
                cls="ok" if status == "PASS" else "warn",
                status=_esc(status),
                owner=_esc(stage.get("owner_role", "")),
                ready=_esc(stage.get("ready_count", 0)),
                required=_esc(stage.get("required_count", 0)),
                blockers=_esc(blockers),
            )
        )
    action_rows = []
    action_mobile_rows = []
    for action in (snapshot.get("action_queue") or [])[:8]:
        action_rows.append(
            "<tr><td>{sequence}</td><td>{phase}</td><td>{owner}</td><td>{action}</td><td>false</td></tr>".format(
                sequence=_esc(action.get("sequence", "")),
                phase=_esc(action.get("phase", "")),
                owner=_esc(action.get("owner_role", "")),
                action=_esc(action.get("action", "")),
            )
        )
        action_mobile_rows.append(
            '<div class="onboarding-mobile-row"><div class="onboarding-mobile-head">'
            '<span>{sequence}. {phase}</span><span>人工</span></div>'
            '<div class="onboarding-mobile-meta"><span>负责人: {owner}</span></div>'
            '<div class="onboarding-mobile-detail">{action}</div></div>'.format(
                sequence=_esc(action.get("sequence", "")),
                phase=_esc(action.get("phase", "")),
                owner=_esc(action.get("owner_role", "")),
                action=_esc(action.get("action", "")),
            )
        )
    control = snapshot.get("control_evidence", {}) or {}
    return """<h2>Business Onboarding</h2>
<div class="card">
<p><strong>technical:</strong> {technical} | <strong>business:</strong> {business} | <strong>current phase:</strong> {phase} | <strong>completed:</strong> {completed}/{total}</p>
<p><strong>Internal Control Evidence:</strong> integrity={integrity}, evidence={evidence}, verified={verified}, raw files stored=false.</p>
<div class="table-scroll onboarding-desktop"><table><tr><th>#</th><th>Phase</th><th>Human owner</th><th>Status</th><th>Ready</th><th>Blockers</th></tr>{stages}</table></div>
<div class="onboarding-mobile">{mobile_stages}</div>
<p><strong>Ordered human actions</strong></p>
<div class="table-scroll onboarding-desktop"><table><tr><th>#</th><th>Phase</th><th>Owner</th><th>Action</th><th>Automatic</th></tr>{actions}</table></div>
<div class="onboarding-mobile">{mobile_actions}</div>
<p><strong>边界:</strong> 只生成接入状态和人工行动清单；不自动新增人员、批准供应商、激活生产、发布或交易。</p>
</div>""".format(
        technical=_esc(snapshot.get("technical_status", "UNKNOWN")),
        business=_esc(snapshot.get("business_status", "BLOCKED")),
        phase=_esc(snapshot.get("current_phase", "")),
        completed=_esc(snapshot.get("completed_phase_count", 0)),
        total=_esc(snapshot.get("phase_count", 0)),
        integrity=_esc(control.get("integrity_status", "UNKNOWN")),
        evidence=_esc(control.get("evidence_count", 0)),
        verified=_esc(control.get("verified_count", 0)),
        stages="".join(stage_rows) or '<tr><td colspan="6">No stage projection.</td></tr>',
        mobile_stages="".join(stage_mobile_rows) or '<div class="empty-state">No stage projection.</div>',
        actions="".join(action_rows) or '<tr><td colspan="5">No pending action.</td></tr>',
        mobile_actions="".join(action_mobile_rows) or '<div class="empty-state">No pending action.</div>',
    )


def _section_pi_skill_supply_chain(status: Dict[str, Any]) -> str:
    if not status or not status.get("enabled"):
        return '<h2>Pi Research Methods</h2>\n<div class="card"><div class="empty-state">Reviewed method supply chain is disabled.</div></div>'
    state = str(status.get("status") or "BLOCKED")
    role_rows = []
    for role, bundle in sorted((status.get("role_bundles") or {}).items()):
        role_rows.append(
            """<div class="pi-method-row">
<div class="pi-method-field"><span class="pi-method-label">Role</span><code>{role}</code></div>
<div class="pi-method-field"><span class="pi-method-label">Methods</span>{count}</div>
<div class="pi-method-field pi-method-skills"><span class="pi-method-label">Skill IDs</span>{skills}</div>
<div class="pi-method-field pi-method-hash"><span class="pi-method-label">Role hash</span><code>{digest}</code></div>
</div>""".format(
                role=_esc(role),
                count=_esc(len(bundle.get("skill_ids", []))),
                skills=_esc(", ".join(bundle.get("skill_ids", []))),
                digest=_esc(str(bundle.get("sha256") or "")[:16]),
            )
        )
    if not role_rows:
        role_rows.append('<div class="pi-method-row"><div class="pi-method-field">No role bundles.</div></div>')
    errors = status.get("errors", []) or []
    error_html = ""
    if errors:
        error_html = "<p><strong>Blockers:</strong> {0}</p>".format(_esc(" | ".join(errors[:5])))
    return """<h2>Pi Research Methods</h2>
<div class="card">
<p><strong>Status:</strong> <span class="flag {cls}">{state}</span> | <strong>approved:</strong> {approved} | <strong>adapted:</strong> {adapted} | <strong>sources:</strong> {sources}</p>
<p><strong>Mode:</strong> compiled prompt only | dynamic skills disabled | production tools disabled</p>
<p class="pi-supply-hash"><strong>Supply-chain hash:</strong> <code>{bundle}</code></p>
<div class="pi-method-list">{rows}</div>
{errors}
</div>""".format(
        cls="ok" if state == "READY" else "warn",
        state=_esc(state),
        approved=_esc(status.get("approved_skill_count", 0)),
        adapted=_esc(status.get("adapted_skill_count", 0)),
        sources=_esc(status.get("source_repository_count", 0)),
        bundle=_esc(status.get("bundle_sha256", "")),
        rows="".join(role_rows),
        errors=error_html,
    )


def _section_brokerage_architecture(state: Dict[str, Any]) -> str:
    if not state:
        return '<h2>Brokerage Architecture</h2>\n<div class="card"><div class="empty-state">No brokerage architecture state yet. Run architecture-sync.</div></div>'
    domains = state.get("domains", {}) or {}
    objects = state.get("objects", {}) or {}
    object_rows = [
        ("Signal Objects", len(objects.get("signals", {}) or {})),
        ("Remediation Objects", len(objects.get("remediation_tasks", {}) or {})),
        ("Research Case Objects", len(objects.get("research_cases", {}) or {})),
        ("Source Objects", len(objects.get("sources", {}) or {})),
        ("Decision Objects", len(objects.get("decisions", {}) or {})),
        ("Review Objects", len(objects.get("reviews", {}) or {})),
    ]
    object_summary = " | ".join("{0}={1}".format(label, value) for label, value in object_rows)
    rows = []
    for name, values in domains.items():
        metrics = []
        for key, value in sorted((values or {}).items()):
            if key == "safety_boundary":
                continue
            metrics.append("{0}={1}".format(key, value))
        rows.append("<tr><td>{domain}</td><td>{metrics}</td></tr>".format(
            domain=_esc(name),
            metrics=_esc(" | ".join(metrics)),
        ))
    if not rows:
        rows.append("<tr><td>-</td><td>-</td></tr>")
    event_counts = state.get("event_type_counts", {}) or {}
    event_text = " | ".join("{0}={1}".format(k, v) for k, v in sorted(event_counts.items())) if event_counts else "-"
    recent_events = state.get("recent_events", []) or []
    recent_html = ""
    if recent_events:
        items = []
        for event in recent_events[-6:]:
            items.append("<li>{time} | {etype} | {entity}:{eid}</li>".format(
                time=_esc(event.get("occurred_at", "")),
                etype=_esc(event.get("event_type", "")),
                entity=_esc(event.get("entity_type", "")),
                eid=_esc(event.get("entity_id", "")),
            ))
        recent_html = "<p><strong>Recent Events</strong></p><ul>{0}</ul>".format("\n".join(items))
    audit_ref = state.get("latest_event_audit", {}) or {}
    audit_path = audit_ref.get("event_audit_latest_json_path", "")
    audit_html = "<p><strong>Latest Audit:</strong> {0}</p>".format(_esc(audit_path)) if audit_path else ""
    health = state.get("event_health", {}) or {}
    raw_total = health.get("raw_total_events", (state.get("domains", {}).get("audit_back_office", {}) or {}).get("event_count", 0))
    compact_total = health.get("compact_total_events", 0)
    duplicate_rows = health.get("raw_duplicate_rows", health.get("duplicate_rows", 0))
    projection_source = health.get("projection_source", "raw")
    freshness = "stale" if health.get("compact_stale") else "fresh"
    return """<h2>Brokerage Architecture</h2>
<div class="card">
<p><strong>Event-Driven State:</strong> {object_summary}</p>
<p><strong>Ledger Health:</strong> raw={raw_total} | compact={compact_total} | duplicates={duplicate_rows} | projection={projection_source} | freshness={freshness}</p>
<p><strong>version:</strong> {version} | <strong>events:</strong> {events}</p>
<p><strong>event types:</strong> {event_text}</p>
{recent_html}
{audit_html}
<table><tr><th>Domain</th><th>Metrics</th></tr>{rows}</table>
<p><strong>boundary:</strong> {boundary}</p>
</div>""".format(
        object_summary=_esc(object_summary),
        raw_total=_esc(raw_total),
        compact_total=_esc(compact_total),
        duplicate_rows=_esc(duplicate_rows),
        projection_source=_esc(projection_source),
        freshness=_esc(freshness),
        version=_esc(state.get("architecture_version", "")),
        events=_esc((state.get("domains", {}).get("audit_back_office", {}) or {}).get("event_count", 0)),
        event_text=_esc(event_text),
        recent_html=recent_html,
        audit_html=audit_html,
        rows="\n".join(rows),
        boundary=_esc(state.get("safety_boundary", "")),
    )


def _section_research_team(
    team: Dict[str, Any],
    team_ops: Dict[str, Any],
    stats: Dict[str, Any],
    decision_summary: Optional[Dict[str, Any]] = None,
) -> str:
    if not stats.get("team_enabled", False):
        return '<h2>投研团队</h2>\n<div class="card"><div class="empty-state">投研团队层未启用。</div></div>'
    brief = team.get("supervisor_brief", {}) if isinstance(team, dict) else {}
    messages = team.get("messages", []) if isinstance(team, dict) else []
    guardrails = team.get("guardrails", []) if isinstance(team, dict) else []

    parts = ["<div class='card'>"]
    parts.append("<p><strong>团队运行:</strong> 角色 {0} | 分工 {1} | 沟通 {2}</p>".format(
        stats.get("team_role_count", 0),
        stats.get("team_assignment_count", 0),
        stats.get("team_message_count", 0),
    ))
    if brief:
        parts.append("<p><strong>给上司简报:</strong> {0}</p>".format(_esc(brief.get("headline", ""))))
        focus = brief.get("today_focus", [])
        if focus:
            parts.append("<p><strong>今日重点:</strong> {0}</p>".format(_esc("；".join(focus))))
        actions = brief.get("next_actions", [])
        if actions:
            parts.append("<p><strong>下一步:</strong></p><ul>")
            for action in actions[:5]:
                parts.append("<li>{0}</li>".format(_esc(action)))
            parts.append("</ul>")
    if messages:
        parts.append("<p><strong>团队沟通:</strong></p><ul>")
        for msg in messages[:5]:
            parts.append("<li>{0} -> {1}: {2}</li>".format(
                _esc(msg.get("from", "")),
                _esc(msg.get("to", "")),
                _esc(msg.get("body", "")),
            ))
        parts.append("</ul>")
    if guardrails:
        parts.append("<p><strong>边界:</strong> {0}</p>".format(_esc(", ".join(guardrails))))
    if stats.get("team_brief_path"):
        parts.append("<p><strong>团队纪要:</strong> {0}</p>".format(_esc(stats.get("team_brief_path", ""))))
    decision_summary = decision_summary or stats.get("decision_summary", {})
    if decision_summary:
        parts.append("<p><strong>人工决策:</strong> 已记录 {0} | 待处理 {1}</p>".format(
            decision_summary.get("total", 0),
            len(decision_summary.get("pending", [])),
        ))
        if stats.get("decision_summary_path"):
            parts.append("<p><strong>决策汇总:</strong> {0}</p>".format(_esc(stats.get("decision_summary_path", ""))))
    strategy_backlog = team_ops.get("strategy_backlog", []) if isinstance(team_ops, dict) else []
    if strategy_backlog:
        parts.append("<p><strong>策略优化台账:</strong></p><ul>")
        for item in strategy_backlog[:5]:
            parts.append("<li>{0}</li>".format(_esc(item.get("suggestion", ""))))
        parts.append("</ul>")
    parts.append("</div>")
    return "<h2>投研团队</h2>\n{0}".format("\n".join(parts))


def _section_human_workbench(team_ops: Dict[str, Any]) -> str:
    workbench = team_ops.get("human_workbench", []) if isinstance(team_ops, dict) else []
    if not workbench:
        return '<h2>Human Workbench</h2>\n<div class="card"><div class="empty-state">No pending human workbench items.</div></div>'

    cards = []
    for item in workbench[:20]:
        conflict = item.get("role_conflict", {}) or {}
        quality = item.get("evidence_quality", {}) or {}
        gaps = item.get("evidence_gap_tasks", []) or []
        review_questions = item.get("review_questions", []) or []
        role_questions = item.get("role_review_questions", []) or []
        commands = item.get("allowed_decision_commands", []) or []
        forbidden = item.get("forbidden_decisions", []) or []

        gap_items = []
        for gap in gaps[:6]:
            gap_items.append("<li>{gap} <small>({status})</small></li>".format(
                gap=_esc(gap.get("gap", "")),
                status=_esc(gap.get("status", "open")),
            ))
        if not gap_items:
            gap_items.append("<li>-</li>")

        question_items = []
        for question in (review_questions + role_questions)[:8]:
            question_items.append("<li>{0}</li>".format(_esc(question)))
        if not question_items:
            question_items.append("<li>-</li>")

        command_items = []
        for command in commands[:5]:
            command_items.append("<li><code>{0}</code></li>".format(_esc(command)))
        if not command_items:
            command_items.append("<li>-</li>")

        forbidden_text = ", ".join(str(value) for value in forbidden) if forbidden else "-"
        cards.append("""<div class="card llm-card">
<strong>[{prio}] {title}</strong>
<div>Decision gate: {gate}</div>
<div>Role conflict: {level} | consensus: {consensus}</div>
<div>Disagreement: {disagreement}</div>
<div>Evidence Quality: {status} | score={score}</div>
<div>Evidence Gaps:<ul>{gaps}</ul></div>
<div>Review Questions:<ul>{questions}</ul></div>
<div>Allowed Human Commands:<ul>{commands}</ul></div>
<div>Forbidden automation: {forbidden}</div>
</div>""".format(
            prio=_esc(item.get("priority", "")),
            title=_esc(item.get("title", "")),
            gate=_esc(item.get("decision_gate", "")),
            level=_esc(conflict.get("conflict_level", "")),
            consensus=_esc(conflict.get("consensus_direction", "")),
            disagreement=_esc(conflict.get("key_disagreement", "")),
            status=_esc(quality.get("status", "unknown")),
            score=_esc(quality.get("score", 0)),
            gaps="\n".join(gap_items),
            questions="\n".join(question_items),
            commands="\n".join(command_items),
            forbidden=_esc(forbidden_text),
        ))
    return '<h2>Human Workbench</h2>\n{0}'.format("\n".join(cards))


def _section_research_cases(cases: List[Dict[str, Any]], stats: Dict[str, Any]) -> str:
    if not cases:
        return '<h2>Research Cases</h2>\n<div class="card"><div class="empty-state">No active research cases.</div></div>'
    status_counts = stats.get("research_case_status_counts", {})
    summary = "<p><strong>case_count:</strong> {0} | <strong>active:</strong> {1}</p>".format(
        stats.get("research_case_count", len(cases)),
        stats.get("research_case_active_count", 0),
    )
    if status_counts:
        summary += "<p><strong>status:</strong> {0}</p>".format(_esc(
            " | ".join("{0}={1}".format(k, v) for k, v in sorted(status_counts.items()))
        ))
    rows = []
    for case in cases[:20]:
        analysis = case.get("case_analysis", {}) or {}
        hypotheses = analysis.get("hypotheses", []) if isinstance(analysis, dict) else []
        lead = hypotheses[0].get("thesis", "") if hypotheses else ""
        gaps = analysis.get("evidence_gaps", []) if isinstance(analysis, dict) else []
        options = analysis.get("next_decision_options", []) if isinstance(analysis, dict) else []
        rows.append(
            "<tr><td>{case_id}</td><td>{status}</td><td>{title}</td><td>{signals}</td>"
            "<td>{top}</td><td>{lead}</td><td>{gaps}</td><td>{options}</td></tr>".format(
                case_id=_esc(str(case.get("case_id", ""))[:12]),
                status=_esc(case.get("status", "active")),
                title=_esc(case.get("title", "")),
                signals=_esc(case.get("signal_count", 0)),
                top=_esc(case.get("top_priority", "")),
                lead=_esc(lead or "-"),
                gaps=_esc("; ".join(str(item) for item in gaps[:3])) if gaps else "-",
                options=_esc(", ".join(str(item) for item in options[:4])) if options else "-",
            )
        )
    header = "<tr><th>Case</th><th>Status</th><th>Title</th><th>Signals</th><th>Top</th><th>Lead hypothesis</th><th>Evidence gaps</th><th>Decision options</th></tr>"
    return '<h2>Research Cases</h2>\n<div class="card">{0}<table>{1}{2}</table></div>'.format(
        summary,
        header,
        "\n".join(rows),
    )


def _section_research_runtime(
    summary: Dict[str, Any],
    cases: List[Dict[str, Any]],
    stats: Dict[str, Any],
) -> str:
    if not stats.get("research_runtime_enabled", False) and not summary:
        return '<h2>Institutional Research Runtime</h2>\n<div class="card"><div class="empty-state">Research runtime not enabled.</div></div>'
    stage_counts = summary.get("stage_counts", {}) or {}
    stage_text = " | ".join("{0}={1}".format(key, value) for key, value in stage_counts.items()) or "-"
    rows = []
    for case in cases[:30]:
        assignments = case.get("assignments", {}) or {}
        rows.append(
            "<tr><td>{priority}</td><td>{case_id}</td><td>{stage}</td><td>{title}</td>"
            "<td>{owner}</td><td>{blockers}</td><td>{next_actions}</td></tr>".format(
                priority=_esc(case.get("top_priority", "")),
                case_id=_esc(str(case.get("case_id", ""))[:12]),
                stage=_esc(case.get("stage", "")),
                title=_esc(case.get("title", "")),
                owner=_esc(assignments.get("lead_analyst", "unassigned")),
                blockers=_esc("; ".join(str(item) for item in case.get("blockers", [])[:4]) or "-"),
                next_actions=_esc("; ".join(str(item) for item in case.get("next_actions", [])[:3]) or "monitor"),
            )
        )
    header = "<tr><th>Priority</th><th>Case</th><th>Stage</th><th>Title</th><th>Lead</th><th>Blockers</th><th>Next actions</th></tr>"
    table = "<table>{0}{1}</table>".format(header, "\n".join(rows)) if rows else '<div class="empty-state">No runtime cases.</div>'
    return """<h2>Institutional Research Runtime</h2>
<div class="card">
<p><strong>cases:</strong> {cases} | <strong>blocked:</strong> {blocked} | <strong>pending human gates:</strong> {human} | <strong>monitoring:</strong> {monitoring}</p>
<p><strong>stages:</strong> {stages}</p>
<p><strong>boundary:</strong> internal research only; quality, compliance, and publication require separated human approval.</p>
{table}
</div>""".format(
        cases=_esc(summary.get("case_count", len(cases))),
        blocked=_esc(summary.get("blocked_count", 0)),
        human=_esc(summary.get("pending_human_count", 0)),
        monitoring=_esc(summary.get("monitoring_count", 0)),
        stages=_esc(stage_text),
        table=table,
    )


def _section_official_evidence_vault(vault: Dict[str, Any]) -> str:
    if not vault:
        return '<h2>Official Evidence Vault</h2>\n<div class="card"><div class="empty-state">官方原件证据库未启用。</div></div>'
    summary = vault.get("summary", {}) or {}
    verification = vault.get("verification", {}) or {}
    control = vault.get("archive_control", {}) or {}
    rows = []
    for document in list((vault.get("documents") or {}).values())[:40]:
        rows.append(
            "<tr><td>{status}</td><td>{document}</td><td>{domain}</td><td>{published}</td>"
            "<td>{content_type}</td><td>{size}</td><td>{signals}</td><td>{binding}</td></tr>".format(
                status=_esc(document.get("status", "")),
                document=_esc(str(document.get("document_id", ""))[:28]),
                domain=_esc(document.get("source_domain", "")),
                published=_esc(document.get("published_at", "")),
                content_type=_esc(document.get("content_type", "")),
                size=_esc(document.get("byte_size", 0)),
                signals=_esc(len(document.get("signal_ids") or [])),
                binding=_esc(document.get("source_binding_status", "")),
            )
        )
    if not rows:
        rows.append('<tr><td colspan="8">尚无已留存官方原件。使用 evidence-vault-import 导入明确来源的公开文件。</td></tr>')
    blockers = verification.get("business_blockers", []) or control.get("blockers", []) or []
    blocker_html = "".join("<li>{0}</li>".format(_esc(item)) for item in blockers[:8])
    return """<h2>Official Evidence Vault</h2>
<div class="card">
<p><strong>technical:</strong> {technical} | <strong>business:</strong> {business} | <strong>event chain:</strong> {integrity} | <strong>documents:</strong> {documents} | <strong>quarantined:</strong> {quarantined}</p>
<p><strong>production archive:</strong> {archive} | <strong>backend:</strong> {backend} | <strong>approved reviews:</strong> {approved}/{required}. 本地内容寻址存储只是审计影子库，原始文件字节不会发送给外部 LLM。</p>
<div class="table-scroll"><table><tr><th>Status</th><th>Document</th><th>Official domain</th><th>Published</th><th>Type</th><th>Bytes</th><th>Signals</th><th>Binding</th></tr>{rows}</table></div>
<p><strong>business blockers:</strong></p><ul>{blockers}</ul>
</div>""".format(
        technical=_esc(verification.get("technical_status", "UNKNOWN")),
        business=_esc(verification.get("business_status", "BLOCKED")),
        integrity=_esc(vault.get("integrity_status", "UNKNOWN")),
        documents=_esc(summary.get("document_count", 0)),
        quarantined=_esc(summary.get("quarantined_count", 0)),
        archive="ready" if control.get("ready") else "not approved",
        backend=_esc(control.get("backend", "")),
        approved=_esc(control.get("approved_review_count", 0)),
        required=_esc(control.get("required_review_count", 0)),
        rows="\n".join(rows),
        blockers=blocker_html or "<li>none</li>",
    )


def _section_research_workpapers(projection: Dict[str, Any]) -> str:
    if not projection or not projection.get("workpapers"):
        return '<h2>Research Workpapers</h2>\n<div class="card"><div class="empty-state">尚无结构化研究底稿。运行 research-workpaper-sync。</div></div>'
    summary = projection.get("summary", {}) or {}
    rows = []
    for item in projection.get("workpapers", [])[:40]:
        validation = item.get("validation", {}) or {}
        sources = item.get("source_records", []) or []
        official = any(source.get("primary_source_eligible") is True for source in sources if isinstance(source, dict))
        blockers = validation.get("errors", []) or []
        rows.append(
            "<tr><td>{priority}</td><td>{case_id}</td><td>{version}</td><td>{stage}</td>"
            "<td>{validation}</td><td>{sources}</td><td>{claims}</td><td>{official}</td><td>{blockers}</td></tr>".format(
                priority=_esc((item.get("case", {}) or {}).get("priority", "")),
                case_id=_esc(str(item.get("case_id", ""))[:16]),
                version=_esc(item.get("version", 0)),
                stage=_esc((item.get("workflow", {}) or {}).get("stage", "")),
                validation=_esc(validation.get("status", "UNKNOWN")),
                sources=_esc(len(sources)),
                claims=_esc(len(item.get("claims", []) or [])),
                official="yes" if official else "no",
                blockers=_esc("; ".join(str(value) for value in blockers[:3]) or "-"),
            )
        )
    header = "<tr><th>Priority</th><th>Case</th><th>Version</th><th>Stage</th><th>Validation</th><th>Sources</th><th>Claims</th><th>Official primary</th><th>Blockers</th></tr>"
    return """<h2>Research Workpapers</h2>
<div class="card">
<p><strong>integrity:</strong> {integrity} | <strong>events:</strong> {events} | <strong>workpapers:</strong> {count} | <strong>approval eligible:</strong> {eligible} | <strong>business:</strong> {business}</p>
<p><strong>archive:</strong> {archive}. 本地哈希链仅是审计影子账本；事实、来源陈述、行情观测、模型推断和人工批准分开记录。</p>
<div class="table-scroll"><table>{header}{rows}</table></div>
</div>""".format(
        integrity=_esc(projection.get("integrity_status", "UNKNOWN")),
        events=_esc(summary.get("event_count", 0)),
        count=_esc(summary.get("workpaper_count", 0)),
        eligible=_esc(summary.get("approval_eligible_count", 0)),
        business=_esc(summary.get("business_status", "BLOCKED")),
        archive="approved" if summary.get("production_archive_ready") else "not approved",
        header=header,
        rows="\n".join(rows),
    )


def _section_research_git(stats: Dict[str, Any]) -> str:
    if not stats.get("research_git_enabled", False):
        return '<h2>Research-as-Git</h2>\n<div class="card"><div class="empty-state">Research-as-Git not enabled.</div></div>'
    counts = stats.get("research_git_status_counts", {}) or {}
    status_text = " | ".join("{0}={1}".format(k, v) for k, v in sorted(counts.items())) if counts else "-"
    draft_ids = stats.get("research_git_draft_ids", []) or []
    draft_text = ", ".join(str(item) for item in draft_ids[:10]) if draft_ids else "-"
    errors = stats.get("research_git_errors", []) or []
    error_html = ""
    if errors:
        items = ["<li>{0}</li>".format(_esc(item)) for item in errors[:5]]
        error_html = "<p><strong>skipped_or_errors:</strong></p><ul>{0}</ul>".format("\n".join(items))
    return """<h2>Research-as-Git</h2>
<div class="card">
<p><strong>auto_staged:</strong> {auto} | <strong>draft_count:</strong> {drafts} | <strong>pending_review:</strong> {pending}</p>
<p><strong>status_counts:</strong> {status}</p>
<p><strong>new_drafts:</strong> {draft_ids}</p>
<p><strong>latest:</strong> {latest}</p>
<p><strong>summary:</strong> {summary}</p>
{error_html}
</div>""".format(
        auto=_esc(stats.get("research_git_auto_staged_count", 0)),
        drafts=_esc(stats.get("research_git_draft_count", 0)),
        pending=_esc(stats.get("research_git_pending_review_count", 0)),
        status=_esc(status_text),
        draft_ids=_esc(draft_text),
        latest=_esc(stats.get("research_git_summary_latest_path", "")),
        summary=_esc(stats.get("research_git_summary_path", "")),
        error_html=error_html,
    )


def _section_workspace_inbox(workspace: Dict[str, Any]) -> str:
    if not workspace:
        return '<h2>Workspace Inbox</h2>\n<div class="card"><div class="empty-state">No workspace inbox yet. Run workspace-sync.</div></div>'
    counts = workspace.get("object_counts", {}) or {}
    latest = workspace.get("workspace_latest_path", "") or workspace.get("workspace_latest_json_path", "")
    report_path = workspace.get("report_path", "")
    signals = workspace.get("signals", {}) or {}
    rows = []
    for signal in list(signals.values())[:8]:
        rows.append("<tr><td>{prio}</td><td>{title}</td><td>{evidence}</td><td>{comments}</td><td>{drafts}</td></tr>".format(
            prio=_esc(signal.get("priority", "")),
            title=_esc(signal.get("title", "")),
            evidence=_esc(len(signal.get("evidence_item_ids", []) or [])),
            comments=_esc(len(signal.get("comment_ids", []) or [])),
            drafts=_esc(", ".join(signal.get("research_draft_ids", []) or []) or "-"),
        ))
    table_html = ""
    if rows:
        table_html = "<table><tr><th>Priority</th><th>Signal</th><th>Evidence</th><th>Comments</th><th>Drafts</th></tr>{0}</table>".format(
            "\n".join(rows)
        )
    return """<h2>Workspace Inbox</h2>
<div class="card">
<p><strong>objects:</strong> signals={signals} | evidence={evidence} | comments={comments} | gaps={gaps} | drafts={drafts}</p>
<p><strong>latest:</strong> {latest}</p>
<p><strong>source report:</strong> {report}</p>
{table}
</div>""".format(
        signals=_esc(counts.get("signals", 0)),
        evidence=_esc(counts.get("evidence_items", 0)),
        comments=_esc(counts.get("comments", 0)),
        gaps=_esc(counts.get("gap_tasks", 0)),
        drafts=_esc(counts.get("research_drafts", 0)),
        latest=_esc(latest),
        report=_esc(report_path),
        table=table_html,
    )


def _section_source_health(source_health: Dict[str, Any]) -> str:
    if not source_health:
        return '<h2>Data Source Health</h2>\n<div class="card"><div class="empty-state">No source health data.</div></div>'
    sources = source_health.get("sources", []) or []
    summary = "<p><strong>overall:</strong> {0} | <strong>score:</strong> {1} | <strong>coverage gaps:</strong> {2}</p>".format(
        _esc(source_health.get("overall_status", "unknown")),
        _esc(source_health.get("overall_score", 0)),
        _esc(source_health.get("coverage_gap_count", 0)),
    )
    rows = []
    for item in sources[:20]:
        rows.append(
            "<tr><td>{source}</td><td>{status}</td><td>{score}</td><td>{latency}</td>"
            "<td>{fields}</td><td>{dups}</td><td>{trust}</td><td>{gaps}</td><td>{reasons}</td></tr>".format(
                source=_esc(item.get("source_id", "")),
                status=_esc(item.get("status", "")),
                score=_esc(item.get("score", 0)),
                latency=_esc(item.get("latency_ms", 0)),
                fields=_esc(item.get("field_completeness", 0)),
                dups=_esc(item.get("duplicate_rate", 0)),
                trust=_esc(item.get("trust_score", 0)),
                gaps=_esc(", ".join(str(x) for x in item.get("coverage_gap_symbols", []) or [])) or "-",
                reasons=_esc(", ".join(str(x) for x in item.get("anomaly_reasons", []) or [])) or "-",
            )
        )
    header = "<tr><th>Source</th><th>Status</th><th>Score</th><th>Latency ms</th><th>Fields</th><th>Duplicate rate</th><th>Trust</th><th>Coverage gaps</th><th>Reasons</th></tr>"
    return '<h2>Data Source Health</h2>\n<div class="card">{0}<table>{1}{2}</table></div>'.format(
        summary,
        header,
        "\n".join(rows),
    )


def _section_source_remediation(stats: Dict[str, Any], source_health: Dict[str, Any]) -> str:
    queue = stats.get("remediation_queue", []) or []
    degraded = stats.get("source_degraded_events", []) or source_health.get("source_degraded_events", []) or []
    gap_counts = stats.get("remediation_gap_type_counts", {}) or {}
    recent_records = stats.get("remediation_decision_records", []) or []
    priority_queue = stats.get("review_priority_queue", {}) or {}
    if not queue and not degraded:
        return '<h2>Source Remediation Queue</h2>\n<div class="card"><div class="empty-state">No source remediation tasks.</div></div>'

    parts = ["<div class='card'>"]
    parts.append("<p><strong>source_degraded:</strong> {0} | <strong>remediation_tasks:</strong> {1}</p>".format(
        _esc(len(degraded)),
        _esc(len(queue)),
    ))
    parts.append(
        '<p><strong>review-workbench:</strong> <a href="http://127.0.0.1:8767/?status=open">http://127.0.0.1:8767</a> '
        '<code>python -m stock_agent review-workbench --config config/watchlist.local.json</code></p>'
    )
    status_counts = Counter(str(task.get("status", "open")) for task in queue)
    parts.append("<p><strong>Open:</strong> {open} | <strong>Resolved:</strong> {resolved} | <strong>Rejected:</strong> {rejected} | <strong>Deferred:</strong> {deferred}</p>".format(
        open=_esc(status_counts.get("open", 0)),
        resolved=_esc(status_counts.get("resolved", 0)),
        rejected=_esc(status_counts.get("rejected", 0)),
        deferred=_esc(status_counts.get("deferred", 0)),
    ))
    if gap_counts:
        parts.append("<p><strong>gap types:</strong> {0}</p>".format(_esc(
            " | ".join("{0}={1}".format(k, v) for k, v in sorted(gap_counts.items()))
        )))
    top10 = priority_queue.get("top10", []) or []
    if top10:
        parts.append("<p><strong>Today Priority Top 10</strong></p>")
        rows = []
        for task in top10[:10]:
            rows.append("<tr><td>{score}</td><td>{risk}</td><td>{task}</td><td>{gap}</td><td>{source}</td><td>{status}</td><td>{reasons}</td></tr>".format(
                score=_esc(task.get("review_priority_score", 0)),
                risk=_esc(task.get("evidence_risk_level", "")),
                task=_esc(task.get("task_id", "")),
                gap=_esc(task.get("gap_type", "")),
                source=_esc(task.get("source_id", "")),
                status=_esc(task.get("status", "")),
                reasons=_esc("; ".join(str(item) for item in task.get("priority_reasons", [])[:4])),
            ))
        header = "<tr><th>Score</th><th>Risk</th><th>Task</th><th>Gap</th><th>Source</th><th>Status</th><th>Reasons</th></tr>"
        parts.append("<table>{0}{1}</table>".format(header, "\n".join(rows)))
    if degraded:
        parts.append("<p><strong>Source degraded events</strong></p><ul>")
        for event in degraded[:8]:
            parts.append("<li>[{severity}] {source}: {detail}</li>".format(
                severity=_esc(event.get("severity", "")),
                source=_esc(event.get("source_id", "")),
                detail=_esc(event.get("detail", "")),
            ))
        parts.append("</ul>")
    if queue:
        rows = []
        for task in queue[:20]:
            rows.append("<tr><td>{task_type}</td><td>{source}</td><td>{signal}</td><td>{gap}</td><td>{status}</td><td>{actions}</td></tr>".format(
                task_type=_esc(task.get("task_type", "")),
                source=_esc(task.get("source_id", "")),
                signal=_esc(task.get("signal_id", "") or "-"),
                gap=_esc(task.get("gap", "")),
                status=_esc(task.get("status", "")),
                actions=_esc(", ".join(str(item) for item in task.get("allowed_actions", []) or [])) or "-",
            ))
        header = "<tr><th>Task</th><th>Source</th><th>Signal</th><th>Gap</th><th>Status</th><th>Allowed actions</th></tr>"
        parts.append("<table>{0}{1}</table>".format(header, "\n".join(rows)))
    if recent_records:
        parts.append("<p><strong>Recent Remediation Records</strong></p><ul>")
        for record in recent_records[-8:]:
            parts.append("<li>{action} | {status} | {task} | {actor}</li>".format(
                action=_esc(record.get("action", "")),
                status=_esc(record.get("status", "")),
                task=_esc(record.get("task_id", "")),
                actor=_esc(record.get("actor", "")),
            ))
        parts.append("</ul>")
    parts.append("</div>")
    return "<h2>Source Remediation Queue</h2>\n{0}".format("\n".join(parts))


def _section_runtime_ops(run_ops: Dict[str, Any]) -> str:
    if not run_ops:
        return '<h2>Runtime Operations</h2>\n<div class="card"><div class="empty-state">No runtime operations data.</div></div>'
    budget = run_ops.get("llm_budget", {}) or {}
    summary = "<p><strong>overall:</strong> {0} | <strong>elapsed:</strong> {1}ms | <strong>LLM calls:</strong> {2}/{3}</p>".format(
        _esc(run_ops.get("overall_status", "unknown")),
        _esc(run_ops.get("elapsed_ms", 0)),
        _esc(budget.get("reserved_calls", 0)),
        _esc(budget.get("max_calls", 0)),
    )
    stage_rows = []
    for stage in (run_ops.get("stages", []) or [])[:20]:
        stage_rows.append("<tr><td>{name}</td><td>{status}</td><td>{duration}</td><td>{error}</td></tr>".format(
            name=_esc(stage.get("name", "")),
            status=_esc(stage.get("status", "")),
            duration=_esc(stage.get("duration_ms", 0)),
            error=_esc(stage.get("error", "")) or "-",
        ))
    stage_table = ""
    if stage_rows:
        stage_table = "<table><tr><th>Stage</th><th>Status</th><th>Duration ms</th><th>Error</th></tr>{0}</table>".format("\n".join(stage_rows))
    degradations = run_ops.get("degradations", []) or []
    degradation_html = ""
    if degradations:
        items = []
        for item in degradations[:12]:
            items.append("<li>[{severity}] {component}: {reason} {detail}</li>".format(
                severity=_esc(item.get("severity", "")),
                component=_esc(item.get("component", "")),
                reason=_esc(item.get("reason", "")),
                detail=_esc(item.get("detail", "")),
            ))
        degradation_html = "<p><strong>Degradations</strong></p><ul>{0}</ul>".format("\n".join(items))
    return '<h2>Runtime Operations</h2>\n<div class="card">{0}{1}{2}</div>'.format(summary, stage_table, degradation_html)


def _section_model_operations(snapshot: Dict[str, Any], scheduled: Dict[str, Any]) -> str:
    if not snapshot:
        return '<h2>Model Operations And Quality</h2>\n<div class="card"><div class="empty-state">No model operations snapshot.</div></div>'
    quality = snapshot.get("quality", {}) or {}
    grounding = quality.get("grounding", {}) or {}
    usage = snapshot.get("usage", {}) or {}
    cost = snapshot.get("estimated_cost", {}) or {}
    rolling = snapshot.get("rolling", {}) or {}
    checks = []
    for item in snapshot.get("checks", []) or []:
        checks.append("<tr><td>{name}</td><td>{status}</td><td>{actual}</td><td>{target}</td></tr>".format(
            name=_esc(item.get("name", "")), status=_esc(item.get("status", "")),
            actual=_esc(item.get("actual", "")), target=_esc(item.get("target", "")),
        ))
    scheduled_html = ""
    if scheduled:
        scheduled_html = "<p><strong>scheduled delivery:</strong> {0}s / {1}s | {2}</p>".format(
            _esc(scheduled.get("duration_seconds", "")),
            _esc(scheduled.get("scheduled_delivery_target_seconds", "")),
            _esc(scheduled.get("scheduled_slo_status", "UNKNOWN")),
        )
    deadline_items = []
    for component, decision in (snapshot.get("deadline_decisions", {}) or {}).items():
        if isinstance(decision, dict) and decision.get("action") in {"reduce", "skip"}:
            deadline_items.append("<li>{0}: {1}, selected={2}, skipped={3}</li>".format(
                _esc(component), _esc(decision.get("action")), _esc(decision.get("selected_count", 0)),
                _esc(decision.get("skipped_count", 0)),
            ))
    deadline_html = "<p><strong>deadline degradations</strong></p><ul>{0}</ul>".format("".join(deadline_items)) if deadline_items else ""
    rolling_html = ""
    if rolling:
        statuses = ", ".join(
            "{0}={1}".format(_esc(key), _esc(value))
            for key, value in sorted((rolling.get("status_counts", {}) or {}).items())
        ) or "-"
        rolling_html = (
            "<p><strong>rolling window:</strong> runs={0} | statuses={1} | "
            "tokens={2} | estimated cost={3} {4}</p>"
        ).format(
            _esc(rolling.get("window_runs", 0)),
            statuses,
            _esc(rolling.get("total_tokens", 0)),
            _esc(rolling.get("estimated_cost", 0)),
            _esc(cost.get("currency", "")),
        )
    return """<h2>Model Operations And Quality</h2>
<div class="card">
<p><strong>status:</strong> {status} | <strong>main:</strong> {main_success}/{main_selected} | <strong>roles:</strong> {role_success}/{role_selected}</p>
<p><strong>grounding:</strong> grounded={grounded} | partial={partial} | blocked={blocked} | usable={usable}</p>
<p><strong>usage:</strong> prompt={prompt} | completion={completion} | reasoning={reasoning} | total={total}</p>
<p><strong>estimated cost:</strong> {amount} {currency} (configuration estimate, not provider billing)</p>
{rolling}
{scheduled}
{deadline}
<table><tr><th>SLO / Quality Check</th><th>Status</th><th>Actual</th><th>Target</th></tr>{checks}</table>
<p><strong>boundary:</strong> no automatic trading, approval, assignment, or publication</p>
</div>""".format(
        status=_esc(snapshot.get("status", "UNKNOWN")),
        main_success=_esc(quality.get("main_success", 0)), main_selected=_esc(quality.get("main_selected", 0)),
        role_success=_esc(quality.get("role_success", 0)), role_selected=_esc(quality.get("role_selected", 0)),
        grounded=_esc(grounding.get("grounded", 0)), partial=_esc(grounding.get("partial", 0)),
        blocked=_esc(grounding.get("blocked", 0)), usable=_esc("{0:.1%}".format(float(grounding.get("usable_rate", 1)))),
        prompt=_esc(usage.get("prompt_tokens", 0)), completion=_esc(usage.get("completion_tokens", 0)),
        reasoning=_esc(usage.get("reasoning_tokens", 0)), total=_esc(usage.get("total_tokens", 0)),
        amount=_esc(cost.get("amount", 0)), currency=_esc(cost.get("currency", "")),
        rolling=rolling_html, scheduled=scheduled_html, deadline=deadline_html, checks="".join(checks),
    )


def _section_external_dependencies(external: Dict[str, Any]) -> str:
    market = external.get("market_resilience", {}) or {}
    governance = external.get("market_data_governance", {}) or {}
    llm = external.get("llm_connectivity", {}) or {}
    if not market and not governance and not llm:
        return '<h2>External Dependencies</h2>\n<div class="card"><div class="empty-state">No external dependency diagnostics.</div></div>'
    parts = ["<div class='card'>"]
    if market:
        parts.append("<p><strong>market_resilience:</strong> status={0} | fallback={1} | cache={2}</p>".format(
            _esc(market.get("status", "unknown")),
            _esc(market.get("fallback_used", False)),
            _esc(market.get("cache_used", False)),
        ))
        missing = market.get("missing_symbols", []) or []
        if missing:
            parts.append("<p><strong>missing_symbols:</strong> {0}</p>".format(_esc(", ".join(str(item) for item in missing))))
        failures = market.get("failures", []) or []
        if failures:
            parts.append("<ul>")
            for failure in failures[:5]:
                parts.append("<li>{0}: {1}</li>".format(_esc(failure.get("provider", "")), _esc(failure.get("diagnosis", ""))))
            parts.append("</ul>")
    if governance:
        parts.append(
            "<p><strong>market_data_governance:</strong> status={0} | pass={1} | degraded={2} | blocked={3} | llm_eligible={4}</p>".format(
                _esc(governance.get("status", "unknown")),
                _esc(governance.get("pass_count", 0)),
                _esc(governance.get("degraded_count", 0)),
                _esc(governance.get("blocked_count", 0)),
                _esc(governance.get("llm_eligible_count", 0)),
            )
        )
        if not governance.get("approved_internal_research_providers"):
            parts.append("<p><strong>provider attestation:</strong> none; price signals cannot use the normal external-LLM path.</p>")
    if llm:
        parts.append("<p><strong>llm_connectivity:</strong> status={0} | diagnosis={1}</p>".format(
            _esc(llm.get("status", "unknown")),
            _esc(llm.get("diagnosis", "")),
        ))
        guide = llm.get("repair_guide", []) or []
        if guide:
            parts.append("<ul>")
            for item in guide[:5]:
                parts.append("<li>{0}</li>".format(_esc(item)))
            parts.append("</ul>")
    parts.append("</div>")
    return "<h2>External Dependencies</h2>\n{0}".format("\n".join(parts))


def _section_price_table(price_signals: List[Dict[str, Any]]) -> str:
    if not price_signals:
        return '<h2>行情快照</h2>\n<div class="card"><div class="empty-state">暂无行情数据。</div></div>'
    rows = []
    for s in price_signals:
        payload = s.get("payload", {})
        pct = payload.get("pct_change", "")
        latest_price = payload.get("latest", "")
        inflow = payload.get("main_net_inflow", "")
        prio = s.get("priority", "P3")
        anomaly_reasons = [r for r in s.get("reasons", []) if "异常" in r or "显著" in r]
        has_llm = "是" if s.get("llm_analysis") else "否"
        gate = payload.get("market_data_governance", {}) or {}
        rows.append("<tr><td>{title}</td><td>{price}</td><td>{pct}</td><td>{inflow}</td>"
                     "<td><span class='prio-tag prio-{prio}'>{prio}</span></td>"
                     "<td>{anomaly}</td><td>{gate}</td><td>{session}</td><td>{llm}</td></tr>".format(
            title=_esc(s.get("title", "")),
            price=_fmt_num(latest_price),
            pct=_fmt_pct(pct),
            inflow=_fmt_inflow(inflow),
            prio=prio,
            anomaly=_esc("；".join(anomaly_reasons)) if anomaly_reasons else "-",
            gate=_esc(gate.get("status", "unknown")),
            session=_esc(gate.get("session", "unknown")),
            llm=has_llm,
        ))
    header = "<tr><th>标的</th><th>最新价</th><th>涨跌幅</th><th>主力净流入</th><th>优先级</th><th>异常原因</th><th>行情门禁</th><th>时段</th><th>LLM</th></tr>"
    return '<h2>行情快照</h2>\n<table>{0}{1}</table>'.format(header, "\n".join(rows))


def _section_filing_evidence(filing_signals: List[Dict[str, Any]], stats: Dict[str, Any]) -> str:
    if not filing_signals:
        return '<h2>公告/财报证据</h2>\n<div class="card"><div class="empty-state">暂无公告/财报证据。</div></div>'
    rows = []
    for s in filing_signals[:20]:
        payload = s.get("payload", {}) or {}
        filing = payload.get("filing_evidence", {}) or {}
        financial = payload.get("financial_evidence", {}) or {}
        missing = filing.get("missing_fields", []) or []
        metrics = financial.get("metrics", {}) if isinstance(financial, dict) else {}
        importance = filing.get("importance_level") or payload.get("filing_importance_level", "unknown")
        importance_score = filing.get("importance_score") or payload.get("filing_importance_score", 0)
        prio = s.get("priority", "P3")
        status = filing.get("status", "unknown")
        rows.append("<tr><td><span class='prio-tag prio-{prio}'>{prio}</span></td>"
                    "<td>{title}</td><td>{provider}</td><td>{status}</td>"
                    "<td>{importance}</td><td>{score}</td>"
                    "<td>{atype}</td><td>{period}</td><td>{metrics}</td><td>{missing}</td></tr>".format(
            prio=prio,
            title=_esc(s.get("title", "")),
            provider=_esc(filing.get("provider", s.get("source", ""))),
            status=_esc(status),
            importance=_esc(importance),
            score=_esc(importance_score),
            atype=_esc(filing.get("announcement_type", "")),
            period=_esc(filing.get("report_period", "") or "unknown"),
            metrics=len(metrics),
            missing=_esc("、".join(str(item) for item in missing)) if missing else "-",
        ))
    status_counts = stats.get("filing_evidence_status_counts", {})
    summary = ""
    if status_counts:
        summary = "<p><strong>状态分布:</strong> {0}</p>".format(_esc(
            "；".join("{0}={1}".format(k, v) for k, v in sorted(status_counts.items()))
        ))
    header = "<tr><th>优先级</th><th>标题</th><th>来源</th><th>状态</th><th>重要性</th><th>分数</th><th>类型</th><th>报告期</th><th>指标数</th><th>缺失字段</th></tr>"
    return '<h2>公告/财报证据</h2>\n<div class="card">{0}<table>{1}{2}</table></div>'.format(
        summary,
        header,
        "\n".join(rows),
    )


def _section_high_priority(high_priority: List[Dict[str, Any]]) -> str:
    if not high_priority:
        return '<h2>高优先级信号</h2>\n<div class="card"><div class="empty-state">暂无高优先级信号。</div></div>'
    rows = []
    for s in high_priority:
        llm = s.get("llm_analysis", {})
        thesis = llm.get("key_thesis", "") if llm else ""
        human_needed = "是" if llm and llm.get("human_decision_needed") else ""
        prio = s.get("priority", "P3")
        rows.append("<tr><td><span class='prio-tag prio-{prio}'>{prio}</span></td>"
                     "<td>{title}</td><td>{source}</td><td>{topics}</td><td>{symbols}</td>"
                     "<td>{reasons}</td><td>{thesis}</td><td>{human}</td></tr>".format(
            prio=prio,
            title=_esc(s.get("title", "")),
            source=_esc(s.get("source", "")),
            topics=_esc("、".join(s.get("topics", []))) or "-",
            symbols=_esc("、".join(s.get("symbols", []))) or "-",
            reasons=_esc("；".join(s.get("reasons", []))) or "-",
            thesis=_esc(thesis) or "-",
            human=human_needed or "-",
        ))
    header = "<tr><th>优先级</th><th>标题</th><th>来源</th><th>主题</th><th>标的</th><th>规则命中</th><th>核心判断</th><th>需人工</th></tr>"
    return '<h2>高优先级信号</h2>\n<table>{0}{1}</table>'.format(header, "\n".join(rows))


def _section_llm_analysis(llm_signals: List[Dict[str, Any]]) -> str:
    if not llm_signals:
        return '<h2>LLM Agent 研判</h2>\n<div class="card"><div class="empty-state">暂无 LLM 研判数据。</div></div>'
    cards = []
    for s in llm_signals:
        a = s.get("llm_analysis", {})
        direction = a.get("impact_direction", "unknown")
        dir_cls = "direction-{0}".format(direction)
        confidence = a.get("confidence", 0)
        horizon = a.get("horizon", "")
        thesis = a.get("key_thesis", "")
        risks = a.get("risk_points", [])
        questions = a.get("review_questions", [])
        risk_html = "".join('<li class="risk-item">{0}</li>'.format(_esc(r)) for r in risks) if risks else "<li>-</li>"
        q_html = "".join('<li class="review-item">{0}</li>'.format(_esc(q)) for q in questions) if questions else "<li>-</li>"
        cards.append("""<div class="card llm-card">
<strong>[{prio}] {title}</strong>
<div>影响方向: <span class="{dir_cls}">{direction}</span> | 置信度: {confidence:.0%} | 时间窗口: {horizon}</div>
<div>核心判断: {thesis}</div>
<div>风险点:<ul>{risks}</ul></div>
<div>人工复核:<ul>{questions}</ul></div>
</div>""".format(
            prio=s.get("priority", "P3"),
            title=_esc(s.get("title", "")),
            dir_cls=dir_cls,
            direction=direction,
            confidence=confidence,
            horizon=horizon,
            thesis=_esc(thesis),
            risks=risk_html,
            questions=q_html,
        ))
    return '<h2>LLM Agent 研判</h2>\n{0}'.format("\n".join(cards))


def _section_multi_role_analysis(role_signals: List[Dict[str, Any]], stats: Dict[str, Any]) -> str:
    if not role_signals:
        status = "disabled"
        if stats.get("role_llm_enabled"):
            status = "no result"
        return '<h2>Multi-Role LLM</h2>\n<div class="card"><div class="empty-state">{0}</div></div>'.format(_esc(status))
    cards = []
    for signal in role_signals:
        synthesis = signal.get("role_synthesis", {}) or {}
        role_rows = []
        for role in signal.get("role_analyses", []) or []:
            role_rows.append("<tr><td>{role}</td><td>{stance}</td><td>{confidence:.0%}</td><td>{evidence}</td><td>{missing}</td><td>{questions}</td></tr>".format(
                role=_esc(role.get("role_id", "")),
                stance=_esc(role.get("stance", "")),
                confidence=float(role.get("confidence", 0)),
                evidence=_esc(", ".join(role.get("evidence_refs", []) or [])) or "-",
                missing=_esc(", ".join(role.get("missing_info", []) or [])) or "-",
                questions=_esc("; ".join(role.get("review_questions", []) or [])) or "-",
            ))
        header = "<tr><th>Role</th><th>Stance</th><th>Confidence</th><th>Evidence</th><th>Missing</th><th>Review Questions</th></tr>"
        cards.append("""<div class="card llm-card">
<strong>[{prio}] {title}</strong>
<div>gate={gate} | conflict={conflict} | consensus={consensus}</div>
<div>disagreement: {disagreement}</div>
<table>{header}{rows}</table>
<div>guardrail: {guardrail}</div>
</div>""".format(
            prio=_esc(signal.get("priority", "")),
            title=_esc(signal.get("title", "")),
            gate=_esc(synthesis.get("decision_gate", "")),
            conflict=_esc(synthesis.get("conflict_level", "")),
            consensus=_esc(synthesis.get("consensus_direction", "")),
            disagreement=_esc(synthesis.get("key_disagreement", "")),
            header=header,
            rows="\n".join(role_rows),
            guardrail=_esc(synthesis.get("forbidden_actions_note", "")),
        ))
    return '<h2>Multi-Role LLM</h2>\n{0}'.format("\n".join(cards))


def _section_pending_review(pending: List[Dict[str, Any]]) -> str:
    if not pending:
        return '<h2>人工复核队列</h2>\n<div class="card"><div class="empty-state">暂无待复核项。</div></div>'
    rows = []
    for r in pending[:100]:
        questions = r.get("review_questions", []) or r.get("suggested_review_questions", [])
        q_text = "；".join(questions) if questions else "-"
        rows.append("<tr><td>{sid}</td><td>{title}</td>"
                     "<td><span class='prio-tag prio-{prio}'>{prio}</span></td>"
                     "<td>{sla}</td><td>{status}</td><td>{assigned}</td><td>{suggested}</td><td>{questions}</td></tr>".format(
            sid=_esc(r.get("signal_id", "")[:12]),
            title=_esc(r.get("title", "")),
            prio=r.get("priority", "P3"),
            sla=_esc(r.get("sla_status", "unknown")),
            questions=_esc(q_text),
            status=_esc(r.get("status", r.get("was_effective", "pending"))),
            assigned=_esc(r.get("assigned_to", "") or "unassigned"),
            suggested=_esc(r.get("assignment_recommendation", "") or "none"),
        ))
    header = "<tr><th>ID</th><th>标题</th><th>优先级</th><th>SLA</th><th>状态</th><th>已分派</th><th>建议人员</th><th>复核问题</th></tr>"
    return '<h2>人工复核队列</h2>\n<table>{0}{1}</table>'.format(header, "\n".join(rows))


def _section_review_queue_summary(queue: Dict[str, Any], fallback: Dict[str, Any]) -> str:
    summary = (queue.get("summary", {}) if isinstance(queue, dict) else {}) or fallback or {}
    if not summary:
        return '<h2>Review Queue Control</h2><div class="card"><div class="empty-state">No canonical queue snapshot.</div></div>'
    return (
        '<h2>Review Queue Control</h2><div class="card">'
        '<p><strong>outstanding:</strong> {outstanding} | <strong>overdue:</strong> {overdue} | '
        '<strong>at risk:</strong> {at_risk} | <strong>unassigned:</strong> {unassigned}</p>'
        '<p><strong>pending rows / unique / duplicates:</strong> {rows} / {unique} / {duplicates}</p>'
        '<p><strong>assignment mode:</strong> suggest_only | <strong>automatic assignment:</strong> false</p>'
        '</div>'
    ).format(
        outstanding=_esc(summary.get("outstanding_count", 0)),
        overdue=_esc(summary.get("overdue_count", 0)),
        at_risk=_esc(summary.get("at_risk_count", 0)),
        unassigned=_esc(summary.get("unassigned_count", 0)),
        rows=_esc(summary.get("pending_rows", 0)),
        unique=_esc(summary.get("pending_unique", 0)),
        duplicates=_esc(summary.get("pending_duplicate_rows", 0)),
    )


def _section_review_stats(review_summary: Optional[Dict[str, Any]]) -> str:
    if not review_summary or review_summary.get("total", 0) == 0:
        return '<h2>复盘统计</h2>\n<div class="card"><div class="empty-state">暂无已完成复盘数据。</div></div>'
    total = review_summary.get("total", 0)
    eff = review_summary.get("effective_count", 0)
    rate = review_summary.get("effective_rate", 0)
    lines = []
    lines.append("<div class='card'>")
    lines.append("<p>总复盘: {0} | 有效: {1} | 有效率: {2:.0%}</p>".format(total, eff, rate))
    by_prio = review_summary.get("effective_rate_by_priority", {})
    if by_prio:
        lines.append("<p>按优先级: ")
        parts = []
        for p, d in sorted(by_prio.items()):
            parts.append("{0}={1:.0%}({2}/{3})".format(p, d.get("rate", 0), d.get("effective", 0), d.get("count", 0)))
        lines.append(" | ".join(parts))
        lines.append("</p>")
    by_topic = review_summary.get("effective_rate_by_topic", {})
    if by_topic:
        lines.append("<p>按主题: ")
        parts = []
        for t, d in sorted(by_topic.items()):
            parts.append("{0}={1:.0%}({2}/{3})".format(t, d.get("rate", 0), d.get("effective", 0), d.get("count", 0)))
        lines.append(" | ".join(parts))
        lines.append("</p>")
    fp = review_summary.get("common_false_positive_reasons", [])
    if fp:
        lines.append("<p>常见无效原因: {0}</p>".format("、".join("{0}({1}次)".format(r, c) for r, c in fp)))
    suggestions = review_summary.get("suggestions", {})
    kw = suggestions.get("keyword_adjustments", [])
    th = suggestions.get("threshold_adjustments", [])
    if kw or th:
        lines.append("<p><strong>优化建议:</strong></p><ul>")
        for item in kw + th:
            lines.append("<li>{0}</li>".format(_esc(item)))
        lines.append("</ul>")
    lines.append("</div>")
    return '<h2>复盘统计</h2>\n{0}'.format("\n".join(lines))


def _esc(text: Any) -> str:
    s = str(text)
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _fmt_num(val: Any) -> str:
    if val is None or val == "":
        return "-"
    try:
        return "{:.2f}".format(float(val))
    except (ValueError, TypeError):
        return str(val)


def _fmt_pct(val: Any) -> str:
    if val is None or val == "":
        return "-"
    try:
        return "{0:+.2f}%".format(float(val))
    except (ValueError, TypeError):
        return str(val)


def _fmt_inflow(val: Any) -> str:
    if val is None or val == "":
        return "-"
    try:
        v = float(val)
        if abs(v) >= 100000000:
            return "{0:+.2f}亿".format(v / 100000000)
        elif abs(v) >= 10000:
            return "{0:+.0f}万".format(v / 10000)
        return "{0:+.0f}".format(v)
    except (ValueError, TypeError):
        return str(val)
