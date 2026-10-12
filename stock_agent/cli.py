from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import time
from pathlib import Path

from .config import PROJECT_ROOT, ensure_directories, load_config, project_path
from .pipeline import run_pipeline, run_agent_pipeline
from .dashboard import generate_dashboard
from .brokerage_architecture import (
    compact_event_ledger,
    compute_event_health,
    load_events,
    project_brokerage_state,
    sync_brokerage_architecture_from_latest_report,
    write_event_audit,
    write_architecture_state,
)
from .review_workbench import serve_workbench, write_workbench_snapshot
from .review_priority import build_priority_queue, write_priority_queue
from .alerts import generate_notifications
from .doctor import write_doctor_report
from .release_audit import write_release_audit
from .release_controls import write_release_controls
from .python_environment import write_python_environment_audit
from .backup_restore import (
    create_backup,
    run_restore_drill,
    verify_backup,
    write_backup_restore_status,
)
from .run_coordination import (
    RunCoordinationError,
    build_run_coordination_status,
    recover_stale_lease,
    write_run_coordination_status,
)
from .value_validation import (
    exclude_value_case,
    freeze_value_cohort,
    freeze_value_treatment,
    open_value_study,
    prepare_blind_assignment,
    record_blind_evaluation,
    record_value_assisted,
    record_value_baseline,
    record_value_gate_decision,
    register_value_case,
    reveal_blind_assignment,
    write_value_validation_status,
)
from .llm_governance import (
    EXCEPTION_DISPOSITIONS,
    load_governance_exception_queue,
    load_latest_governance_summary,
    record_governance_exception_decision,
    validate_governance_policy,
)
from .model_operations import load_latest_model_operations, model_operations_config
from .decisions import list_pending_decisions, record_decision, compute_decision_summary, write_decision_summary
from .research_cases import list_research_cases, record_case_decision, compute_case_summary, write_case_summary
from .research_runtime import (
    VALID_ACTIONS as VALID_RUNTIME_ACTIONS,
    load_research_runtime,
    record_runtime_action,
    summarize_research_runtime,
    sync_research_runtime,
    write_research_runtime_report,
)
from .research_workpapers import (
    load_research_workpapers,
    sync_research_workpapers,
    verify_research_workpapers,
    write_research_workpaper_reports,
)
from .evidence_vault import (
    ARCHIVE_ACTIVATION_ROLES,
    ARCHIVE_REVOCATION_ROLES,
    ARCHIVE_REVIEW_ROLES,
    CAPTURE_ROLES,
    activate_production_archive,
    fetch_official_document,
    import_official_document,
    link_evidence_to_signal,
    load_evidence_vault,
    record_archive_review,
    revoke_production_archive,
    verify_evidence_vault,
    write_evidence_vault_reports,
)
from .evidence_vault_uat import EvidenceVaultUATError, run_evidence_vault_uat
from .research_operations import build_research_operations_snapshot
from .research_git import (
    approve_research_draft,
    commit_research_draft,
    defer_research_draft,
    project_research_git_state,
    reject_research_draft,
    stage_research_draft,
    stage_llm_research_drafts_from_report,
    write_research_git_summary,
)
from .review import export_review, compute_review_summary, write_review_summary
from .review_queue import ACTION_ROLES, build_review_queue, record_review_queue_action, write_review_queue_snapshot
from .skills import discover_skills, format_skill_table
from .source_remediation import (
    attach_evidence_url,
    attach_structured_fields,
    defer_remediation_task,
    reject_remediation_task,
    resolve_remediation_task,
)
from .workspace_inbox import sync_workspace_inbox
from .market_resilience import seed_quote_cache_from_reports
from .market_data_governance import load_latest_market_data_governance
from .openclaw_handoff import validate_openclaw_handoff, write_openclaw_handoff
from .agent_handoff import check_latest_agent_handoff, run_pi_public_handoff, validate_agent_handoff, write_agent_handoff
from .pi_runtime import run_pi_runtime_uat, run_pi_skills_uat, write_pi_runtime_status
from .pi_skills import write_pi_skill_lock, write_pi_skill_supply_chain_status
from .production_pilot import (
    INCIDENT_ACTIONS,
    INCIDENT_SEVERITIES,
    activate_production_candidate,
    load_latest_production_pilot,
    record_pilot_approval,
    record_pilot_incident,
    record_pilot_incident_action,
    revoke_production_activation,
    sync_production_pilot,
)
from .provider_registry import (
    activate_provider,
    initialize_provider_candidate,
    record_provider_review,
    submit_provider_due_diligence,
    suspend_provider,
)
from .control_evidence import (
    load_control_evidence_registry,
    register_control_evidence,
    review_control_evidence,
    revoke_control_evidence,
    write_control_evidence_snapshot,
)
from .business_onboarding import (
    generate_business_onboarding_pack,
    load_latest_business_onboarding,
    sync_business_onboarding,
)


def _configure_stdio() -> None:
    for stream in [sys.stdout, sys.stderr]:
        if hasattr(stream, "reconfigure"):
            try:
                stream.reconfigure(errors="replace")
            except Exception:
                pass


def _add_decision_shortcut_args(parser: argparse.ArgumentParser, default_outcome: str) -> None:
    parser.add_argument("--signal-id", required=True)
    parser.add_argument("--outcome", default=default_outcome)
    parser.add_argument("--followup-result", default="")
    parser.add_argument("--notes", default="")
    parser.add_argument("--actor", default="user")
    parser.add_argument("--evidence-ref", action="append", default=[])


def _add_value_actor_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--actor", required=True)
    parser.add_argument("--actor-role", required=True)


def main() -> None:
    _configure_stdio()
    parser = argparse.ArgumentParser(description="A股智能投研系统")
    subparsers = parser.add_subparsers(dest="command", required=True)

    run_parser = subparsers.add_parser("run", help="执行一次采集、分析和报告输出")
    run_parser.add_argument("--config", default="config/watchlist.example.json")

    agent_parser = subparsers.add_parser("agent-run", help="执行采集、分析、LLM研判和动作执行")
    agent_parser.add_argument("--config", default="config/watchlist.example.json")
    agent_parser.add_argument("--trigger-source", choices=["manual", "scheduled"], default="manual",
                              help="触发来源；scheduled 才会产生具备 pilot 资格的运行 manifest")

    watch_parser = subparsers.add_parser("watch", help="按固定间隔持续运行")
    watch_parser.add_argument("--config", default="config/watchlist.example.json")
    watch_parser.add_argument("--interval-minutes", type=float, default=30.0)
    watch_parser.add_argument("--max-runs", type=int, default=0, help="0 表示一直运行")

    init_parser = subparsers.add_parser("init-config", help="复制一份本地配置")
    init_parser.add_argument("--target", default="config/watchlist.local.json")
    init_parser.add_argument("--force", action="store_true")

    skill_parser = subparsers.add_parser("list-skills", help="扫描本地 skills")
    skill_parser.add_argument("--skills-path", default="skills")

    export_review_parser = subparsers.add_parser("export-review", help="导出待复核信号到 JSONL")
    export_review_parser.add_argument("--config", default="config/watchlist.example.json")

    review_summary_parser = subparsers.add_parser("review-summary", help="汇总已完成复盘数据并生成报告")

    review_queue_sync_parser = subparsers.add_parser("review-queue-sync", help="刷新去重且可审计的人工复核队列")
    review_queue_sync_parser.add_argument("--config", default="config/watchlist.local.json")

    review_queue_status_parser = subparsers.add_parser("review-queue-status", help="显示人工复核队列、SLA 和真实人员能力缺口")
    review_queue_status_parser.add_argument("--config", default="config/watchlist.local.json")

    review_queue_action_parser = subparsers.add_parser("review-queue-action", help="由名册中的真实人工记录复核状态动作")
    review_queue_action_parser.add_argument("--config", default="config/watchlist.local.json")
    review_queue_action_parser.add_argument("--signal-id", required=True)
    review_queue_action_parser.add_argument("--action", required=True, choices=sorted(ACTION_ROLES))
    review_queue_action_parser.add_argument("--actor", required=True)
    review_queue_action_parser.add_argument("--actor-role", required=True)
    review_queue_action_parser.add_argument("--notes", default="")
    review_queue_action_parser.add_argument("--decision-taken", default="")
    review_queue_action_parser.add_argument("--was-effective", choices=["true", "false", "unknown"], default="unknown")
    review_queue_action_parser.add_argument("--actual-price-move", default="")
    review_queue_action_parser.add_argument("--actual-news-followup", default="")
    review_queue_action_parser.add_argument("--false-positive-reason", default="")

    decision_list_parser = subparsers.add_parser("decision-list", help="列出等待上司处理的人工决策")

    decision_record_parser = subparsers.add_parser("decision-record", help="记录上司人工决策")
    decision_record_parser.add_argument("--signal-id", required=True)
    decision_record_parser.add_argument(
        "--decision",
        required=True,
        choices=[
            "approve_research_followup",
            "request_more_data",
            "mark_as_noise",
            "defer_until_more_evidence",
            "confirm_signal",
            "reject_signal",
            "defer_signal",
        ],
    )
    decision_record_parser.add_argument("--notes", default="")
    decision_record_parser.add_argument("--actor", default="user")
    decision_record_parser.add_argument("--outcome", default="")
    decision_record_parser.add_argument("--followup-result", default="")
    decision_record_parser.add_argument("--evidence-ref", action="append", default=[])

    decision_summary_parser = subparsers.add_parser("decision-summary", help="汇总人工决策记录")

    decision_confirm_parser = subparsers.add_parser("decision-confirm", help="Confirm a signal after evidence review")
    _add_decision_shortcut_args(decision_confirm_parser, default_outcome="effective")

    decision_reject_parser = subparsers.add_parser("decision-reject", help="Reject a signal after evidence review")
    _add_decision_shortcut_args(decision_reject_parser, default_outcome="false_positive")

    decision_defer_parser = subparsers.add_parser("decision-defer", help="Defer a signal until more evidence is available")
    _add_decision_shortcut_args(decision_defer_parser, default_outcome="pending")

    attach_url_parser = subparsers.add_parser("attach-evidence-url", help="Attach an evidence URL and resolve a remediation task")
    attach_url_parser.add_argument("--task-id", required=True)
    attach_url_parser.add_argument("--url", required=True)
    attach_url_parser.add_argument("--actor", default="user")
    attach_url_parser.add_argument("--notes", default="")

    attach_fields_parser = subparsers.add_parser("attach-structured-fields", help="Attach structured fields and resolve a remediation task")
    attach_fields_parser.add_argument("--task-id", required=True)
    attach_fields_parser.add_argument("--field", action="append", default=[], help="key=value; repeatable")
    attach_fields_parser.add_argument("--actor", default="user")
    attach_fields_parser.add_argument("--notes", default="")

    resolve_remediation_parser = subparsers.add_parser("resolve-remediation", help="Resolve a remediation task")
    resolve_remediation_parser.add_argument("--task-id", required=True)
    resolve_remediation_parser.add_argument("--notes", default="")
    resolve_remediation_parser.add_argument("--actor", default="user")

    reject_remediation_parser = subparsers.add_parser("reject-remediation", help="Reject a remediation task")
    reject_remediation_parser.add_argument("--task-id", required=True)
    reject_remediation_parser.add_argument("--reason", default="")
    reject_remediation_parser.add_argument("--actor", default="user")

    defer_remediation_parser = subparsers.add_parser("defer-remediation", help="Defer a remediation task")
    defer_remediation_parser.add_argument("--task-id", required=True)
    defer_remediation_parser.add_argument("--reason", default="")
    defer_remediation_parser.add_argument("--actor", default="user")

    case_list_parser = subparsers.add_parser("case-list", help="List research cases")
    case_list_parser.add_argument("--status", default="")

    case_record_parser = subparsers.add_parser("case-record", help="Record a manual research case decision")
    case_record_parser.add_argument("--case-id", required=True)
    case_record_parser.add_argument(
        "--decision",
        required=True,
        choices=[
            "continue_tracking",
            "needs_evidence",
            "mark_noise",
            "archive",
        ],
    )
    case_record_parser.add_argument("--notes", default="")
    case_record_parser.add_argument("--actor", default="user")

    case_summary_parser = subparsers.add_parser("case-summary", help="Write research case summary")

    runtime_sync_parser = subparsers.add_parser("research-runtime-sync", help="Sync institution-style research case runtime")
    runtime_sync_parser.add_argument("--config", default="config/watchlist.local.json")
    runtime_sync_parser.add_argument("--report", default="")

    runtime_status_parser = subparsers.add_parser("research-runtime-status", help="Show research runtime stages and blockers")
    runtime_status_parser.add_argument("--case-id", default="")
    runtime_status_parser.add_argument("--stage", default="")
    runtime_status_parser.add_argument("--write-summary", action="store_true")

    runtime_action_parser = subparsers.add_parser("research-runtime-action", help="Record a controlled research runtime action")
    runtime_action_parser.add_argument("--case-id", required=True)
    runtime_action_parser.add_argument("--action", required=True, choices=sorted(VALID_RUNTIME_ACTIONS))
    runtime_action_parser.add_argument("--actor", required=True)
    runtime_action_parser.add_argument("--actor-role", required=True)
    runtime_action_parser.add_argument("--notes", default="")
    runtime_action_parser.add_argument("--task-id", default="")
    runtime_action_parser.add_argument("--assignee", default="")
    runtime_action_parser.add_argument("--reference", action="append", default=[])
    runtime_action_parser.add_argument("--check", action="append", default=[])
    runtime_action_parser.add_argument("--config", default="config/watchlist.local.json")

    evidence_import_parser = subparsers.add_parser(
        "evidence-vault-import",
        help="Import one explicitly identified official public document into the evidence vault",
    )
    evidence_import_parser.add_argument("--config", default="config/watchlist.local.json")
    evidence_import_parser.add_argument("--file", required=True)
    evidence_import_parser.add_argument("--source-url", required=True)
    evidence_import_parser.add_argument("--title", required=True)
    evidence_import_parser.add_argument("--published-at", required=True)
    evidence_import_parser.add_argument("--actor", required=True)
    evidence_import_parser.add_argument("--actor-role", choices=sorted(CAPTURE_ROLES), required=True)
    evidence_import_parser.add_argument("--signal-id", action="append", default=[])
    evidence_import_parser.add_argument("--issuer", default="")
    evidence_import_parser.add_argument("--symbol", default="")
    evidence_import_parser.add_argument("--source-kind", default="company_disclosure")
    evidence_import_parser.add_argument("--content-type", default="")

    evidence_fetch_parser = subparsers.add_parser(
        "evidence-vault-fetch",
        help="Fetch one exact official HTTPS document under explicit network authorization",
    )
    evidence_fetch_parser.add_argument("--config", default="config/watchlist.local.json")
    evidence_fetch_parser.add_argument("--source-url", required=True)
    evidence_fetch_parser.add_argument("--title", required=True)
    evidence_fetch_parser.add_argument("--published-at", required=True)
    evidence_fetch_parser.add_argument("--actor", required=True)
    evidence_fetch_parser.add_argument("--actor-role", choices=sorted(CAPTURE_ROLES), required=True)
    evidence_fetch_parser.add_argument("--signal-id", action="append", default=[])
    evidence_fetch_parser.add_argument("--issuer", default="")
    evidence_fetch_parser.add_argument("--symbol", default="")
    evidence_fetch_parser.add_argument("--source-kind", default="company_disclosure")
    evidence_fetch_parser.add_argument("--live", action="store_true")

    evidence_uat_parser = subparsers.add_parser(
        "evidence-vault-uat",
        help="Run one explicit live official-source capture in a temporary isolated vault",
    )
    evidence_uat_parser.add_argument("--config", default="config/watchlist.local.json")
    evidence_uat_parser.add_argument("--source-url", required=True)
    evidence_uat_parser.add_argument("--title", required=True)
    evidence_uat_parser.add_argument("--published-at", required=True)
    evidence_uat_parser.add_argument("--actor", required=True)
    evidence_uat_parser.add_argument("--actor-role", choices=sorted(CAPTURE_ROLES), required=True)
    evidence_uat_parser.add_argument("--signal-id", action="append", default=[])
    evidence_uat_parser.add_argument("--issuer", default="")
    evidence_uat_parser.add_argument("--symbol", default="")
    evidence_uat_parser.add_argument("--source-kind", default="company_disclosure")
    evidence_uat_parser.add_argument("--live", action="store_true")

    excerpt_parser = subparsers.add_parser(
        "evidence-excerpt", help="Read an exact local-only UTF-8 excerpt without changing the vault")
    excerpt_parser.add_argument("--config", default="config/watchlist.local.json")
    excerpt_parser.add_argument("--document-id", required=True)
    excerpt_parser.add_argument("--start", type=int, required=True)
    excerpt_parser.add_argument("--end", type=int, required=True)
    excerpt_parser.add_argument("--summary", default="")
    excerpt_parser.add_argument("--summary-kind", choices=["human_judgment", "model_inference"], default="human_judgment")

    evidence_link_parser = subparsers.add_parser(
        "evidence-vault-link",
        help="Link an existing retained document to a signal without changing the original object",
    )
    evidence_link_parser.add_argument("--config", default="config/watchlist.local.json")
    evidence_link_parser.add_argument("--document-id", required=True)
    evidence_link_parser.add_argument("--signal-id", required=True)
    evidence_link_parser.add_argument("--actor", required=True)
    evidence_link_parser.add_argument("--actor-role", choices=sorted(CAPTURE_ROLES), required=True)
    evidence_link_parser.add_argument("--notes", default="")

    evidence_status_parser = subparsers.add_parser(
        "evidence-vault-status",
        help="Verify official evidence objects, hash-chain integrity, and archive-control readiness",
    )
    evidence_status_parser.add_argument("--config", default="config/watchlist.local.json")
    evidence_status_parser.add_argument("--require-ready", action="store_true")
    evidence_status_parser.add_argument("--require-business-ready", action="store_true")

    archive_review_parser = subparsers.add_parser(
        "evidence-vault-archive-review",
        help="Record an independent human compliance or security review of the production archive",
    )
    archive_review_parser.add_argument("--config", default="config/watchlist.local.json")
    archive_review_parser.add_argument("--decision", choices=["approve", "reject"], required=True)
    archive_review_parser.add_argument("--actor", required=True)
    archive_review_parser.add_argument("--actor-role", choices=sorted(ARCHIVE_REVIEW_ROLES), required=True)
    archive_review_parser.add_argument("--notes", required=True)
    archive_review_parser.add_argument("--evidence-ref", action="append", default=[])

    archive_activate_parser = subparsers.add_parser(
        "evidence-vault-archive-activate",
        help="Manually activate a fully reviewed external immutable archive",
    )
    archive_activate_parser.add_argument("--config", default="config/watchlist.local.json")
    archive_activate_parser.add_argument("--actor", required=True)
    archive_activate_parser.add_argument("--actor-role", choices=sorted(ARCHIVE_ACTIVATION_ROLES), required=True)
    archive_activate_parser.add_argument("--notes", required=True)

    archive_revoke_parser = subparsers.add_parser(
        "evidence-vault-archive-revoke",
        help="Fail closed by revoking production archive activation",
    )
    archive_revoke_parser.add_argument("--config", default="config/watchlist.local.json")
    archive_revoke_parser.add_argument("--actor", required=True)
    archive_revoke_parser.add_argument("--actor-role", choices=sorted(ARCHIVE_REVOCATION_ROLES), required=True)
    archive_revoke_parser.add_argument("--notes", required=True)

    workpaper_sync_parser = subparsers.add_parser("research-workpaper-sync", help="Build versioned, auditable research workpapers")
    workpaper_sync_parser.add_argument("--config", default="config/watchlist.local.json")
    workpaper_sync_parser.add_argument("--report", default="")

    workpaper_status_parser = subparsers.add_parser("research-workpaper-status", help="Show research workpaper integrity, stages, and blockers")
    workpaper_status_parser.add_argument("--config", default="config/watchlist.local.json")
    workpaper_status_parser.add_argument("--case-id", default="")
    workpaper_status_parser.add_argument("--write-summary", action="store_true")

    workpaper_verify_parser = subparsers.add_parser("research-workpaper-verify", help="Verify the workpaper hash chain and approval readiness")
    workpaper_verify_parser.add_argument("--config", default="config/watchlist.local.json")
    workpaper_verify_parser.add_argument("--require-ready", action="store_true")
    workpaper_verify_parser.add_argument("--require-business-ready", action="store_true")

    ops_check_parser = subparsers.add_parser("research-ops-check", help="Validate research roster and control readiness")
    ops_check_parser.add_argument("--config", default="config/watchlist.local.json")

    ops_plan_parser = subparsers.add_parser("research-ops-plan", help="Write SLA, escalation, and staffing recommendations")
    ops_plan_parser.add_argument("--config", default="config/watchlist.local.json")
    ops_plan_parser.add_argument("--case-id", default="")
    ops_plan_parser.add_argument("--limit", type=int, default=20)

    research_stage_parser = subparsers.add_parser("research-stage", help="Stage an auditable research draft")
    research_stage_parser.add_argument("--entity-type", required=True)
    research_stage_parser.add_argument("--entity-id", required=True)
    research_stage_parser.add_argument("--title", required=True)
    research_stage_parser.add_argument("--thesis", required=True)
    research_stage_parser.add_argument("--evidence-ref", action="append", default=[])
    research_stage_parser.add_argument("--review-question", action="append", default=[])
    research_stage_parser.add_argument("--actor", default="agent")

    research_commit_parser = subparsers.add_parser("research-commit", help="Commit a staged research draft for human review")
    research_commit_parser.add_argument("--draft-id", required=True)
    research_commit_parser.add_argument("--message", required=True)
    research_commit_parser.add_argument("--actor", default="user")

    research_approve_parser = subparsers.add_parser("research-approve", help="Approve a committed research draft")
    research_approve_parser.add_argument("--draft-id", required=True)
    research_approve_parser.add_argument("--notes", default="")
    research_approve_parser.add_argument("--actor", default="user")

    research_reject_parser = subparsers.add_parser("research-reject", help="Reject a committed research draft")
    research_reject_parser.add_argument("--draft-id", required=True)
    research_reject_parser.add_argument("--notes", default="")
    research_reject_parser.add_argument("--actor", default="user")

    research_defer_parser = subparsers.add_parser("research-defer", help="Defer a research draft until more evidence is available")
    research_defer_parser.add_argument("--draft-id", required=True)
    research_defer_parser.add_argument("--notes", default="")
    research_defer_parser.add_argument("--actor", default="user")

    research_status_parser = subparsers.add_parser("research-status", help="Show Research-as-Git draft status")
    research_status_parser.add_argument("--status", default="")
    research_status_parser.add_argument("--write-summary", action="store_true")

    research_auto_stage_parser = subparsers.add_parser("research-auto-stage", help="Stage LLM research drafts from a JSON report")
    research_auto_stage_parser.add_argument("--report", default="")
    research_auto_stage_parser.add_argument("--config", default="")

    workspace_sync_parser = subparsers.add_parser("workspace-sync", help="Build workspace inbox from the latest JSON report")
    workspace_sync_parser.add_argument("--report", default="")

    market_cache_seed_parser = subparsers.add_parser("market-cache-seed", help="Seed market quote cache from historical JSON reports")
    market_cache_seed_parser.add_argument("--config", default="config/watchlist.local.json")
    market_cache_seed_parser.add_argument("--symbol", action="append", default=[])

    market_governance_status_parser = subparsers.add_parser("market-data-governance-status", help="显示行情新鲜度、时段、授权和多源一致性门禁")
    market_governance_status_parser.add_argument("--config", default="config/watchlist.local.json")

    dashboard_parser = subparsers.add_parser("dashboard", help="生成静态 HTML 看板")
    dashboard_parser.add_argument("--config", default="config/watchlist.example.json")

    workbench_parser = subparsers.add_parser("review-workbench", help="Start local remediation review workbench")
    workbench_parser.add_argument("--config", default="config/watchlist.local.json")
    workbench_parser.add_argument("--host", default="127.0.0.1")
    workbench_parser.add_argument("--port", type=int, default=8767)
    workbench_parser.add_argument("--status", default="open")
    workbench_parser.add_argument("--risk", default="all")
    workbench_parser.add_argument("--task-id", default="")
    workbench_parser.add_argument("--once", action="store_true", help="write a static workbench snapshot and exit")

    review_priority_parser = subparsers.add_parser("review-priority", help="Generate remediation review priority queue")
    review_priority_parser.add_argument("--config", default="config/watchlist.local.json")

    architecture_parser = subparsers.add_parser("architecture-sync", help="Sync brokerage-style event ledger and state projection")
    architecture_parser.add_argument("--config", default="config/watchlist.local.json")

    event_sync_parser = subparsers.add_parser("event-sync", help="Sync event ledger from latest report")
    event_sync_parser.add_argument("--config", default="config/watchlist.local.json")

    event_state_parser = subparsers.add_parser("event-state", help="Project event ledger into object state")
    event_state_parser.add_argument("--config", default="config/watchlist.local.json")

    event_log_parser = subparsers.add_parser("event-log", help="Show recent event ledger entries")
    event_log_parser.add_argument("--limit", type=int, default=20)

    event_compact_parser = subparsers.add_parser("event-compact", help="Write compact de-duplicated event ledger")
    event_compact_parser.add_argument("--config", default="config/watchlist.local.json")

    event_health_parser = subparsers.add_parser("event-health", help="Show event ledger health metrics")
    event_health_parser.add_argument("--config", default="config/watchlist.local.json")

    event_audit_parser = subparsers.add_parser("event-audit", help="Write an event audit timeline")
    event_audit_parser.add_argument("--config", default="config/watchlist.local.json")
    event_audit_parser.add_argument("--entity-type", default="")
    event_audit_parser.add_argument("--entity-id", default="")
    event_audit_parser.add_argument("--limit", type=int, default=50)

    alerts_parser = subparsers.add_parser("alerts", help="生成通知摘要")
    alerts_parser.add_argument("--config", default="config/watchlist.example.json")

    doctor_parser = subparsers.add_parser("doctor", help="系统健康检查")
    doctor_parser.add_argument("--config", default="config/watchlist.example.json")

    release_audit_parser = subparsers.add_parser(
        "release-audit", help="Write a read-only canonical source/config/dependency release manifest"
    )
    release_audit_parser.add_argument("--config", default="config/watchlist.local.json")
    release_audit_parser.add_argument("--require-ready", action="store_true")

    release_controls_parser = subparsers.add_parser(
        "release-controls", help="Generate redacted secret scan, CycloneDX SBOM, and license inventory"
    )
    release_controls_parser.add_argument("--config", default="config/watchlist.local.json")
    release_controls_parser.add_argument(
        "--fetch-python-metadata",
        action="store_true",
        help="read exact-version license metadata from the official PyPI registry",
    )
    release_controls_parser.add_argument("--require-pass", action="store_true")

    python_environment_parser = subparsers.add_parser(
        "python-env-audit", help="Verify locked Python 3.11/3.12 environments and optional full tests"
    )
    python_environment_parser.add_argument("--python", action="append", default=[])
    python_environment_parser.add_argument("--run-tests", action="store_true")
    python_environment_parser.add_argument("--require-pass", action="store_true")

    backup_create_parser = subparsers.add_parser(
        "backup-create", help="Create and verify one credential-free local recovery bundle"
    )
    backup_create_parser.add_argument("--config", default="config/watchlist.local.json")
    backup_create_parser.add_argument("--require-pass", action="store_true")

    backup_verify_parser = subparsers.add_parser(
        "backup-verify", help="Recompute all hashes and validate SQLite/JSONL backup facts"
    )
    backup_verify_parser.add_argument("--config", default="config/watchlist.local.json")
    backup_verify_parser.add_argument("--backup", default="latest")
    backup_verify_parser.add_argument("--require-pass", action="store_true")

    restore_drill_parser = subparsers.add_parser(
        "restore-drill", help="Restore a verified bundle into an isolated drill directory"
    )
    restore_drill_parser.add_argument("--config", default="config/watchlist.local.json")
    restore_drill_parser.add_argument("--backup", default="latest")
    restore_drill_parser.add_argument("--require-pass", action="store_true")

    backup_status_parser = subparsers.add_parser(
        "backup-restore-status", help="Report local recovery evidence and production DR blockers separately"
    )
    backup_status_parser.add_argument("--config", default="config/watchlist.local.json")
    backup_status_parser.add_argument("--require-ready", action="store_true", help="require local technical PASS")
    backup_status_parser.add_argument(
        "--require-production-ready", action="store_true", help="require human-controlled production DR readiness"
    )

    run_coordination_status_parser = subparsers.add_parser(
        "run-coordination-status", help="显示运行租约、检查点账本和恢复状态（只读）"
    )
    run_coordination_status_parser.add_argument("--config", default="config/watchlist.local.json")
    run_coordination_status_parser.add_argument("--mode", default="", help="可选：只查看一个运行模式")
    run_coordination_status_parser.add_argument("--require-ready", action="store_true", help="要求技术协调状态为 PASS")

    run_recover_stale_parser = subparsers.add_parser(
        "run-recover-stale", help="在明确人工理由后归档一个已过期的运行租约"
    )
    run_recover_stale_parser.add_argument("--config", default="config/watchlist.local.json")
    run_recover_stale_parser.add_argument("--mode", required=True)
    run_recover_stale_parser.add_argument("--reason", required=True)

    value_status_parser = subparsers.add_parser(
        "value-validation-status", help="Build the paired-case G0.5 value gate report"
    )
    value_status_parser.add_argument("--config", default="config/watchlist.local.json")
    value_status_parser.add_argument("--require-ready", action="store_true")

    value_init_parser = subparsers.add_parser(
        "value-study-init", help="Open one human-owned value validation study"
    )
    value_init_parser.add_argument("--config", default="config/watchlist.local.json")
    value_init_parser.add_argument("--title", required=True)
    value_init_parser.add_argument("--scenario", required=True)
    _add_value_actor_args(value_init_parser)

    value_case_parser = subparsers.add_parser(
        "value-case-register", help="Register one real archived case without an outcome label"
    )
    value_case_parser.add_argument("--config", default="config/watchlist.local.json")
    value_case_parser.add_argument("--case-id", required=True)
    value_case_parser.add_argument("--case-ref", required=True)
    value_case_parser.add_argument("--case-sha256", required=True)
    value_case_parser.add_argument("--priority", required=True)
    value_case_parser.add_argument("--stratum", required=True)
    value_case_parser.add_argument("--evidence-cutoff", required=True)
    value_case_parser.add_argument("--data-classification", default="public")
    _add_value_actor_args(value_case_parser)

    value_exclude_parser = subparsers.add_parser(
        "value-case-exclude", help="Record a supervised case exclusion without deleting history"
    )
    value_exclude_parser.add_argument("--config", default="config/watchlist.local.json")
    value_exclude_parser.add_argument("--case-id", required=True)
    value_exclude_parser.add_argument("--reason", required=True)
    value_exclude_parser.add_argument("--category", required=True)
    _add_value_actor_args(value_exclude_parser)

    value_cohort_parser = subparsers.add_parser(
        "value-cohort-freeze", help="Freeze the reviewed 20-30 case cohort"
    )
    value_cohort_parser.add_argument("--config", default="config/watchlist.local.json")
    _add_value_actor_args(value_cohort_parser)

    value_baseline_parser = subparsers.add_parser(
        "value-baseline-record", help="Record one real manual baseline observation"
    )
    value_baseline_parser.add_argument("--config", default="config/watchlist.local.json")
    value_baseline_parser.add_argument("--case-id", required=True)
    value_baseline_parser.add_argument("--output-ref", required=True)
    value_baseline_parser.add_argument("--output-sha256", required=True)
    value_baseline_parser.add_argument("--metrics-file", required=True)
    _add_value_actor_args(value_baseline_parser)

    value_treatment_parser = subparsers.add_parser(
        "value-treatment-freeze", help="Bind the assisted treatment to one READY release manifest"
    )
    value_treatment_parser.add_argument("--config", default="config/watchlist.local.json")
    _add_value_actor_args(value_treatment_parser)

    value_assisted_parser = subparsers.add_parser(
        "value-assisted-record", help="Record one governed system-assisted observation"
    )
    value_assisted_parser.add_argument("--config", default="config/watchlist.local.json")
    value_assisted_parser.add_argument("--case-id", required=True)
    value_assisted_parser.add_argument("--output-ref", required=True)
    value_assisted_parser.add_argument("--output-sha256", required=True)
    value_assisted_parser.add_argument("--metrics-file", required=True)
    value_assisted_parser.add_argument("--observed-manifest-id", required=True)
    value_assisted_parser.add_argument("--attest-outcome-hidden", action="store_true")
    value_assisted_parser.add_argument("--attest-evidence-cutoff", action="store_true")
    value_assisted_parser.add_argument("--unauthorized-egress-count", type=int, default=0)
    _add_value_actor_args(value_assisted_parser)

    value_blind_prepare_parser = subparsers.add_parser(
        "value-blind-prepare", help="Create an A/B package and sealed commit/reveal mapping"
    )
    value_blind_prepare_parser.add_argument("--config", default="config/watchlist.local.json")
    value_blind_prepare_parser.add_argument("--case-id", required=True)
    _add_value_actor_args(value_blind_prepare_parser)

    value_blind_evaluate_parser = subparsers.add_parser(
        "value-blind-evaluate", help="Record independent A/B scores before reveal"
    )
    value_blind_evaluate_parser.add_argument("--config", default="config/watchlist.local.json")
    value_blind_evaluate_parser.add_argument("--assignment-id", required=True)
    value_blind_evaluate_parser.add_argument("--scores-a-file", required=True)
    value_blind_evaluate_parser.add_argument("--scores-b-file", required=True)
    value_blind_evaluate_parser.add_argument("--attest-blinding", action="store_true")
    value_blind_evaluate_parser.add_argument("--attest-no-ledger-access", action="store_true")
    value_blind_evaluate_parser.add_argument("--notes", default="")
    _add_value_actor_args(value_blind_evaluate_parser)

    value_blind_reveal_parser = subparsers.add_parser(
        "value-blind-reveal", help="Reveal the sealed A/B mapping after independent scoring"
    )
    value_blind_reveal_parser.add_argument("--config", default="config/watchlist.local.json")
    value_blind_reveal_parser.add_argument("--assignment-id", required=True)
    _add_value_actor_args(value_blind_reveal_parser)

    value_decision_parser = subparsers.add_parser(
        "value-gate-decision", help="Record a human CONTINUE, NARROW, STOP, or one-time extension"
    )
    value_decision_parser.add_argument("--config", default="config/watchlist.local.json")
    value_decision_parser.add_argument("--decision", required=True)
    value_decision_parser.add_argument("--notes", required=True)
    value_decision_parser.add_argument("--evidence-ref", required=True)
    value_decision_parser.add_argument("--decision-deadline", default="")
    _add_value_actor_args(value_decision_parser)

    llm_governance_parser = subparsers.add_parser("llm-governance-status", help="Show external LLM egress and grounding controls")
    llm_governance_parser.add_argument("--config", default="config/watchlist.local.json")

    model_ops_parser = subparsers.add_parser("model-ops-status", help="Show model quality, usage, cost estimate, and delivery SLO")
    model_ops_parser.add_argument("--config", default="config/watchlist.local.json")

    pi_status_parser = subparsers.add_parser("pi-runtime-status", help="Validate the pinned, isolated Pi Agent runtime")
    pi_status_parser.add_argument("--config", default="config/watchlist.local.json")
    pi_status_parser.add_argument("--require-ready", action="store_true")

    pi_uat_parser = subparsers.add_parser("pi-runtime-uat", help="Run one explicit synthetic live Pi/DeepSeek acceptance call")
    pi_uat_parser.add_argument("--config", default="config/watchlist.local.json")
    pi_uat_parser.add_argument("--live", action="store_true", help="required acknowledgement that this makes one billable model call")

    pi_skills_status_parser = subparsers.add_parser("pi-skills-status", help="Validate reviewed Pi method provenance, permissions, and integrity lock")
    pi_skills_status_parser.add_argument("--config", default="config/watchlist.local.json")
    pi_skills_status_parser.add_argument("--require-ready", action="store_true")

    pi_skills_lock_parser = subparsers.add_parser("pi-skills-lock", help="Regenerate the reviewed Pi method integrity lock after source review")
    pi_skills_lock_parser.add_argument("--config", default="config/watchlist.local.json")
    pi_skills_lock_parser.add_argument("--write", action="store_true", help="required acknowledgement that this changes the audited lock")

    pi_skills_uat_parser = subparsers.add_parser("pi-skills-uat", help="Run one explicit synthetic live UAT through the locked signal-analyst method bundle")
    pi_skills_uat_parser.add_argument("--config", default="config/watchlist.local.json")
    pi_skills_uat_parser.add_argument("--live", action="store_true", help="required acknowledgement that this makes one billable model call")

    agent_handoff_parser = subparsers.add_parser("agent-handoff", help="Write a runtime-neutral public-only governed handoff")
    agent_handoff_parser.add_argument("--config", default="config/watchlist.local.json")
    agent_handoff_parser.add_argument("--report", default="")
    agent_handoff_parser.add_argument("--check", action="store_true", help="validate the persisted report/handoff/result chain without writing or calling Pi")

    pi_handoff_parser = subparsers.add_parser("pi-handoff-run", help="Generate and conditionally execute the governed Pi public handoff")
    pi_handoff_parser.add_argument("--config", default="config/watchlist.local.json")
    pi_handoff_parser.add_argument("--report", default="")

    handoff_parser = subparsers.add_parser("openclaw-handoff", help="Write a public-only governed handoff for the isolated OpenClaw agent")
    handoff_parser.add_argument("--config", default="config/watchlist.local.json")
    handoff_parser.add_argument("--report", default="", help="Optional JSON report path; defaults to the latest report")

    pilot_sync_parser = subparsers.add_parser("production-pilot-sync", help="Reconcile the controlled shadow run and write Go/No-Go status")
    pilot_sync_parser.add_argument("--summary", default="", help="已绑定的 daily summary 路径；提供时对账该精确运行而非最新文件")
    pilot_sync_parser.add_argument("--config", default="config/watchlist.local.json")

    pilot_status_parser = subparsers.add_parser("production-pilot-status", help="Show technical, business, and activation readiness separately")
    pilot_status_parser.add_argument("--config", default="config/watchlist.local.json")
    pilot_status_parser.add_argument("--require-go", action="store_true", help="exit non-zero unless the current decision is GO")

    control_evidence_register_parser = subparsers.add_parser(
        "control-evidence-register",
        help="Hash one internal control file and register only metadata plus its external archive reference",
    )
    control_evidence_register_parser.add_argument("--config", default="config/watchlist.local.json")
    control_evidence_register_parser.add_argument("--file", required=True)
    control_evidence_register_parser.add_argument("--category", required=True)
    control_evidence_register_parser.add_argument("--title", required=True)
    control_evidence_register_parser.add_argument("--subject-id", required=True)
    control_evidence_register_parser.add_argument("--owner-role", required=True)
    control_evidence_register_parser.add_argument("--external-archive-ref", required=True)
    control_evidence_register_parser.add_argument("--actor", required=True)
    control_evidence_register_parser.add_argument("--actor-role", required=True)
    control_evidence_register_parser.add_argument("--valid-from", default="")
    control_evidence_register_parser.add_argument("--valid-to", default="")
    control_evidence_register_parser.add_argument("--notes", default="")

    control_evidence_review_parser = subparsers.add_parser(
        "control-evidence-review", help="Record an independent rostered-human control evidence review"
    )
    control_evidence_review_parser.add_argument("--config", default="config/watchlist.local.json")
    control_evidence_review_parser.add_argument("--evidence-id", required=True)
    control_evidence_review_parser.add_argument("--decision", choices=["approve", "reject"], required=True)
    control_evidence_review_parser.add_argument("--actor", required=True)
    control_evidence_review_parser.add_argument("--actor-role", required=True)
    control_evidence_review_parser.add_argument("--notes", required=True)
    control_evidence_review_parser.add_argument("--review-record-ref", required=True)

    control_evidence_revoke_parser = subparsers.add_parser(
        "control-evidence-revoke", help="Fail closed by revoking one control evidence record"
    )
    control_evidence_revoke_parser.add_argument("--config", default="config/watchlist.local.json")
    control_evidence_revoke_parser.add_argument("--evidence-id", required=True)
    control_evidence_revoke_parser.add_argument("--actor", required=True)
    control_evidence_revoke_parser.add_argument("--actor-role", required=True)
    control_evidence_revoke_parser.add_argument("--notes", required=True)

    control_evidence_status_parser = subparsers.add_parser(
        "control-evidence-status", help="Show internal control evidence integrity and review status"
    )
    control_evidence_status_parser.add_argument("--config", default="config/watchlist.local.json")
    control_evidence_status_parser.add_argument("--require-ready", action="store_true")

    onboarding_pack_parser = subparsers.add_parser(
        "business-onboarding-pack", help="Generate human-completion templates without changing production state"
    )
    onboarding_pack_parser.add_argument("--config", default="config/watchlist.local.json")

    onboarding_sync_parser = subparsers.add_parser(
        "business-onboarding-sync", help="Build the ordered business onboarding status and action queue"
    )
    onboarding_sync_parser.add_argument("--config", default="config/watchlist.local.json")

    onboarding_status_parser = subparsers.add_parser(
        "business-onboarding-status", help="Show technical and business onboarding readiness separately"
    )
    onboarding_status_parser.add_argument("--config", default="config/watchlist.local.json")
    onboarding_status_parser.add_argument("--require-ready", action="store_true")

    provider_init_parser = subparsers.add_parser("provider-init", help="Create an unapproved market-data provider candidate")
    provider_init_parser.add_argument("--config", default="config/watchlist.local.json")
    provider_init_parser.add_argument("--provider-id", required=True)
    provider_init_parser.add_argument("--legal-name", required=True)
    provider_init_parser.add_argument("--scope", action="append", required=True)
    provider_init_parser.add_argument("--actor", default="system:stock_agent")
    provider_init_parser.add_argument("--notes", default="")

    provider_due_parser = subparsers.add_parser("provider-due-diligence", help="Submit complete provider evidence for independent review")
    provider_due_parser.add_argument("--config", default="config/watchlist.local.json")
    provider_due_parser.add_argument("--provider-id", required=True)
    provider_due_parser.add_argument("--actor", required=True)
    provider_due_parser.add_argument("--actor-role", choices=["data_steward", "research_coordinator"], required=True)
    provider_due_parser.add_argument("--evidence-file", required=True, help="JSON object containing every required evidence reference")
    provider_due_parser.add_argument("--notes", required=True)

    provider_review_parser = subparsers.add_parser("provider-review", help="Record an independent compliance or information-security review")
    provider_review_parser.add_argument("--config", default="config/watchlist.local.json")
    provider_review_parser.add_argument("--provider-id", required=True)
    provider_review_parser.add_argument("--decision", choices=["approve", "reject"], required=True)
    provider_review_parser.add_argument("--actor", required=True)
    provider_review_parser.add_argument("--actor-role", choices=["compliance_officer", "information_security_officer"], required=True)
    provider_review_parser.add_argument("--notes", required=True)
    provider_review_parser.add_argument("--evidence-ref", action="append", required=True)

    provider_activate_parser = subparsers.add_parser("provider-activate", help="Manually activate a fully reviewed provider")
    provider_activate_parser.add_argument("--config", default="config/watchlist.local.json")
    provider_activate_parser.add_argument("--provider-id", required=True)
    provider_activate_parser.add_argument("--actor", required=True)
    provider_activate_parser.add_argument("--actor-role", choices=["data_steward", "research_coordinator"], required=True)
    provider_activate_parser.add_argument("--notes", required=True)

    provider_suspend_parser = subparsers.add_parser("provider-suspend", help="Immediately suspend an active provider under human control")
    provider_suspend_parser.add_argument("--config", default="config/watchlist.local.json")
    provider_suspend_parser.add_argument("--provider-id", required=True)
    provider_suspend_parser.add_argument("--actor", required=True)
    provider_suspend_parser.add_argument(
        "--actor-role",
        choices=["data_steward", "research_coordinator", "compliance_officer", "information_security_officer"],
        required=True,
    )
    provider_suspend_parser.add_argument("--notes", required=True)

    pilot_approval_parser = subparsers.add_parser("production-pilot-approval", help="Record one fingerprint-bound human Go/No-Go approval")
    pilot_approval_parser.add_argument("--config", default="config/watchlist.local.json")
    pilot_approval_parser.add_argument("--gate", required=True)
    pilot_approval_parser.add_argument("--decision", choices=["approve", "reject"], required=True)
    pilot_approval_parser.add_argument("--actor", required=True)
    pilot_approval_parser.add_argument("--actor-role", required=True)
    pilot_approval_parser.add_argument("--notes", required=True)
    pilot_approval_parser.add_argument("--evidence-ref", action="append", required=True)

    pilot_activate_parser = subparsers.add_parser("production-pilot-activate", help="Manually activate a fully approved internal-research candidate")
    pilot_activate_parser.add_argument("--config", default="config/watchlist.local.json")
    pilot_activate_parser.add_argument("--actor", required=True)
    pilot_activate_parser.add_argument("--actor-role", choices=["publication_approver"], required=True)
    pilot_activate_parser.add_argument("--notes", required=True)
    pilot_activate_parser.add_argument("--evidence-ref", action="append", required=True)

    pilot_revoke_parser = subparsers.add_parser("production-pilot-revoke", help="Fail closed by revoking current production activation")
    pilot_revoke_parser.add_argument("--config", default="config/watchlist.local.json")
    pilot_revoke_parser.add_argument("--actor", required=True)
    pilot_revoke_parser.add_argument(
        "--actor-role",
        choices=["publication_approver", "compliance_officer", "information_security_officer"],
        required=True,
    )
    pilot_revoke_parser.add_argument("--notes", required=True)

    pilot_incident_parser = subparsers.add_parser("production-pilot-incident", help="Append a production-pilot incident without changing research output")
    pilot_incident_parser.add_argument("--config", default="config/watchlist.local.json")
    pilot_incident_parser.add_argument("--title", required=True)
    pilot_incident_parser.add_argument("--severity", choices=sorted(INCIDENT_SEVERITIES), required=True)
    pilot_incident_parser.add_argument("--category", required=True)
    pilot_incident_parser.add_argument("--actor", default="system:stock_agent")
    pilot_incident_parser.add_argument("--actor-role", default="system_monitor")
    pilot_incident_parser.add_argument("--notes", required=True)
    pilot_incident_parser.add_argument("--evidence-ref", action="append", required=True)

    pilot_incident_action_parser = subparsers.add_parser("production-pilot-incident-action", help="Record a controlled human incident action")
    pilot_incident_action_parser.add_argument("--config", default="config/watchlist.local.json")
    pilot_incident_action_parser.add_argument("--incident-id", required=True)
    pilot_incident_action_parser.add_argument("--action", choices=sorted(INCIDENT_ACTIONS), required=True)
    pilot_incident_action_parser.add_argument("--actor", required=True)
    pilot_incident_action_parser.add_argument("--actor-role", required=True)
    pilot_incident_action_parser.add_argument("--notes", required=True)
    pilot_incident_action_parser.add_argument("--evidence-ref", action="append", required=True)

    llm_governance_review_parser = subparsers.add_parser(
        "llm-governance-review",
        help="Record a registered human decision for one LLM governance exception",
    )
    llm_governance_review_parser.add_argument("--config", default="config/watchlist.local.json")
    llm_governance_review_parser.add_argument("--exception-id", required=True)
    llm_governance_review_parser.add_argument("--disposition", choices=sorted(EXCEPTION_DISPOSITIONS), required=True)
    llm_governance_review_parser.add_argument("--actor", required=True)
    llm_governance_review_parser.add_argument(
        "--actor-role",
        choices=["compliance_officer", "information_security_officer"],
        required=True,
    )
    llm_governance_review_parser.add_argument("--notes", required=True)
    llm_governance_review_parser.add_argument("--evidence-ref", action="append", default=[])

    task_create_parser = subparsers.add_parser("task-create", help="创建一个持久研究任务（公开分类输入）")
    task_create_parser.add_argument("--config", default="config/watchlist.local.json")
    task_create_parser.add_argument("--title", required=True)
    task_create_parser.add_argument("--spec", default="", help="JSON 文件：research_question/public_context/replay_response 等；与 --from-symbols 二选一")
    task_create_parser.add_argument("--case-id", default="")
    task_create_parser.add_argument("--execution-mode", choices=["live", "replay"], default="replay")
    task_create_parser.add_argument("--from-symbols", default="", help="逗号分隔标的，自动从行情缓存/信号库构造公开上下文")
    task_create_parser.add_argument("--research-question", default="")
    task_create_parser.add_argument("--lookback-hours", type=int, default=48)

    task_rescan_parser = subparsers.add_parser(
        "task-rescan", help="从信号库重扫未消费事件并创建研究任务（消费集去重，可安全重跑）")
    task_rescan_parser.add_argument("--config", default="config/watchlist.local.json")
    task_rescan_parser.add_argument("--purpose", default="event_research_v1")
    task_rescan_parser.add_argument("--execution-mode", choices=["live", "replay"], default="replay")
    task_rescan_parser.add_argument("--actor", default="operator")
    task_rescan_parser.add_argument("--symbols", default="", help="可选：逗号分隔标的过滤")
    task_rescan_parser.add_argument("--lookback-hours", type=int, default=0,
                                    help="可选：回看小时数；0 表示不设截止")

    task_detail_parser = subparsers.add_parser(
        "task-detail", help="单个研究任务的只读详情（证据投递、研究轮次、AI 输出、待答问题、错误）")
    task_detail_parser.add_argument("--config", default="config/watchlist.local.json")
    task_detail_parser.add_argument("--task-id", required=True)

    daily_cycle_parser = subparsers.add_parser(
        "daily-task-cycle", help="任务引擎调度周期：重扫事件 → 恢复 → 清空队列（batch 模式拒绝）")
    daily_cycle_parser.add_argument("--config", default="config/watchlist.local.json")
    daily_cycle_parser.add_argument("--purpose", default="event_research_v1")
    daily_cycle_parser.add_argument("--execution-mode", choices=["live", "replay"], default="replay")
    daily_cycle_parser.add_argument("--actor", default="operator")
    daily_cycle_parser.add_argument("--worker-id", default="w-daily-cycle")
    daily_cycle_parser.add_argument("--max-tasks", type=int, default=20)

    overview_parser = subparsers.add_parser(
        "overview", help="工作台总览（引擎/行情/事件/任务/预算的只读聚合投影）")
    overview_parser.add_argument("--config", default="config/watchlist.local.json")

    events_view_parser = subparsers.add_parser(
        "events-view", help="去重事件视图（聚类/来源/修订/关联研究任务，只读投影）")
    events_view_parser.add_argument("--config", default="config/watchlist.local.json")
    events_view_parser.add_argument("--lookback-hours", type=int, default=72)
    events_view_parser.add_argument("--limit", type=int, default=50)

    summary_parser = subparsers.add_parser("task-summary", help="Local version-bound summary review; never publication approval")
    summary_parser.add_argument("--config", default="config/watchlist.local.json")
    summary_parser.add_argument("--task-id", required=True)
    summary_parser.add_argument("--action", choices=["show", "submit", "review"], default="show")
    summary_parser.add_argument("--draft-id", default="")
    summary_parser.add_argument("--summary-id", default="")
    summary_parser.add_argument("--spec", default="", help="Local JSON: text, content_kind, selectors")
    summary_parser.add_argument("--actor", default="")
    summary_parser.add_argument("--expected-version", type=int)
    summary_parser.add_argument("--command-id", default="")
    summary_parser.add_argument("--decision", choices=["supported", "reject", "request_changes", "revoke"])
    summary_parser.add_argument("--notes", default="")

    summary_open_parser = subparsers.add_parser("task-summary-open", help="Open a short-lived read-only view as a locally authenticated reviewer")
    summary_open_parser.add_argument("--config", default="config/watchlist.local.json")
    summary_open_parser.add_argument("--task-id", required=True)
    summary_open_parser.add_argument("--draft-id", required=True)
    summary_open_parser.add_argument("--expected-version", type=int, required=True)
    summary_open_parser.add_argument("--actor", required=True)

    task_list_parser = subparsers.add_parser("task-list", help="列出研究任务与 outbox 概览")
    task_list_parser.add_argument("--config", default="config/watchlist.local.json")
    task_list_parser.add_argument("--status", default="")
    task_list_parser.add_argument("--json", action="store_true")

    task_run_next_parser = subparsers.add_parser("task-run-next", help="认领并执行一个排队中的研究任务")
    task_run_next_parser.add_argument("--config", default="config/watchlist.local.json")
    task_run_next_parser.add_argument("--worker-id", default="")
    task_run_next_parser.add_argument("--ttl-seconds", type=int, default=120)

    task_cancel_parser = subparsers.add_parser("task-cancel", help="请求取消一个未终结的研究任务")
    task_cancel_parser.add_argument("--config", default="config/watchlist.local.json")
    task_cancel_parser.add_argument("--task-id", required=True)
    task_cancel_parser.add_argument("--actor", required=True)
    task_cancel_parser.add_argument("--reason", default="")
    task_cancel_parser.add_argument("--expected-version", type=int, required=True)

    human_response_parser = subparsers.add_parser("human-response", help="记录人工反馈（个人签收不等于机构批准）")
    human_response_parser.add_argument("--config", default="config/watchlist.local.json")
    human_response_parser.add_argument("--task-id", required=True)
    human_response_parser.add_argument("--actor", required=True)
    human_response_parser.add_argument("--decision", required=True, choices=["acknowledge", "reject", "provide_input"])
    human_response_parser.add_argument("--expected-version", type=int, required=True)
    human_response_parser.add_argument("--command-id", default="")
    human_response_parser.add_argument("--input-patch", default="")
    human_response_parser.add_argument("--notes", default="")

    task_recover_parser = subparsers.add_parser("task-recover", help="恢复租约已过期且未终结的任务（只重排或过期）")
    task_recover_parser.add_argument("--config", default="config/watchlist.local.json")

    task_board_parser = subparsers.add_parser("task-board", help="启动只读研究任务面板（127.0.0.1:8768）")
    task_board_parser.add_argument("--config", default="config/watchlist.local.json")
    task_board_parser.add_argument("--port", type=int, default=8768)

    manifest_bind_parser = subparsers.add_parser("run-manifest-bind", help="验证调度摘要绑定的精确运行 manifest（只读对账）")
    manifest_bind_parser.add_argument("--config", default="config/watchlist.local.json")
    manifest_bind_parser.add_argument("--summary", required=True, help="调度运行 summary JSON 路径")

    args = parser.parse_args()
    if args.command == "task-summary":
        _task_summary(args)
        return  # No credential loading or global directory creation for local review.
    if args.command == "task-summary-open":
        import sqlite3
        from .summary_viewer import serve_summary_viewer
        from .task_runtime import TaskRuntimeError
        try:
            serve_summary_viewer(args.config, args.task_id, args.actor, args.draft_id, args.expected_version)
        except (OSError, ValueError, TaskRuntimeError, sqlite3.Error) as exc:
            raise SystemExit("Local summary view rejected: " + str(exc)) from exc
        return  # No credentials, directories, approval writes or persistent service.
    from .config import load_local_secrets

    load_local_secrets()
    ensure_directories()

    if args.command == "run":
        _run_once(args.config)
    elif args.command == "agent-run":
        _agent_run_once(args.config, getattr(args, "trigger_source", "manual"))
    elif args.command == "watch":
        _watch(args.config, args.interval_minutes, args.max_runs)
    elif args.command == "init-config":
        _init_config(args.target, args.force)
    elif args.command == "list-skills":
        print(format_skill_table(discover_skills(args.skills_path)))
    elif args.command == "export-review":
        _export_review(args.config)
    elif args.command == "review-summary":
        _review_summary()
    elif args.command == "review-queue-sync":
        _review_queue_sync(args.config)
    elif args.command == "review-queue-status":
        _review_queue_status(args.config)
    elif args.command == "review-queue-action":
        _review_queue_action(
            args.config, args.signal_id, args.action, args.actor, args.actor_role, args.notes,
            args.decision_taken, args.was_effective, args.actual_price_move,
            args.actual_news_followup, args.false_positive_reason,
        )
    elif args.command == "decision-list":
        _decision_list()
    elif args.command == "decision-record":
        _decision_record(args.signal_id, args.decision, args.notes, args.actor, args.outcome, args.followup_result, args.evidence_ref)
    elif args.command == "decision-summary":
        _decision_summary()
    elif args.command == "decision-confirm":
        _decision_confirm(args.signal_id, args.outcome, args.followup_result, args.actor, args.evidence_ref, args.notes)
    elif args.command == "decision-reject":
        _decision_reject(args.signal_id, args.outcome, args.followup_result, args.actor, args.evidence_ref, args.notes)
    elif args.command == "decision-defer":
        _decision_defer(args.signal_id, args.outcome, args.followup_result, args.actor, args.evidence_ref, args.notes)
    elif args.command == "attach-evidence-url":
        _attach_evidence_url(args.task_id, args.url, args.actor, args.notes)
    elif args.command == "attach-structured-fields":
        _attach_structured_fields(args.task_id, args.field, args.actor, args.notes)
    elif args.command == "resolve-remediation":
        _resolve_remediation(args.task_id, args.notes, args.actor)
    elif args.command == "reject-remediation":
        _reject_remediation(args.task_id, args.reason, args.actor)
    elif args.command == "defer-remediation":
        _defer_remediation(args.task_id, args.reason, args.actor)
    elif args.command == "case-list":
        _case_list(args.status)
    elif args.command == "case-record":
        _case_record(args.case_id, args.decision, args.notes, args.actor)
    elif args.command == "case-summary":
        _case_summary()
    elif args.command == "research-runtime-sync":
        _research_runtime_sync(args.config, args.report)
    elif args.command == "research-runtime-status":
        _research_runtime_status(args.case_id, args.stage, args.write_summary)
    elif args.command == "research-runtime-action":
        _research_runtime_action(
            args.case_id,
            args.action,
            args.actor,
            args.actor_role,
            args.notes,
            args.task_id,
            args.assignee,
            args.reference,
            args.check,
            args.config,
        )
    elif args.command == "evidence-vault-import":
        _evidence_vault_import(
            args.config, args.file, args.source_url, args.title, args.published_at,
            args.actor, args.actor_role, args.signal_id, args.issuer, args.symbol,
            args.source_kind, args.content_type,
        )
    elif args.command == "evidence-vault-fetch":
        _evidence_vault_fetch(
            args.config, args.source_url, args.title, args.published_at, args.actor,
            args.actor_role, args.signal_id, args.issuer, args.symbol, args.source_kind, args.live,
        )
    elif args.command == "evidence-vault-uat":
        _evidence_vault_uat(
            args.config, args.source_url, args.title, args.published_at, args.actor,
            args.actor_role, args.signal_id, args.issuer, args.symbol, args.source_kind, args.live,
        )
    elif args.command == "evidence-excerpt":
        from .evidence_excerpts import EvidenceExcerptError, create_excerpt, excerpt_selector, bind_summary_draft

        config = load_config(args.config)
        try:
            excerpt = create_excerpt(config, args.document_id, args.start, args.end)
            output = {"excerpt": excerpt, "selector": excerpt_selector(excerpt)}
            if args.summary:
                output["summary_draft"] = bind_summary_draft(config, [output["selector"]], args.summary, args.summary_kind)
        except EvidenceExcerptError as exc:
            raise SystemExit(str(exc)) from exc
        print(json.dumps(output, ensure_ascii=False, indent=2))
    elif args.command == "evidence-vault-link":
        _evidence_vault_link(
            args.config, args.document_id, args.signal_id, args.actor, args.actor_role, args.notes,
        )
    elif args.command == "evidence-vault-status":
        _evidence_vault_status(args.config, args.require_ready, args.require_business_ready)
    elif args.command == "evidence-vault-archive-review":
        _evidence_vault_archive_review(
            args.config, args.decision, args.actor, args.actor_role, args.notes, args.evidence_ref,
        )
    elif args.command == "evidence-vault-archive-activate":
        _evidence_vault_archive_activate(args.config, args.actor, args.actor_role, args.notes)
    elif args.command == "evidence-vault-archive-revoke":
        _evidence_vault_archive_revoke(args.config, args.actor, args.actor_role, args.notes)
    elif args.command == "research-workpaper-sync":
        _research_workpaper_sync(args.config, args.report)
    elif args.command == "research-workpaper-status":
        _research_workpaper_status(args.config, args.case_id, args.write_summary)
    elif args.command == "research-workpaper-verify":
        _research_workpaper_verify(args.config, args.require_ready, args.require_business_ready)
    elif args.command == "research-ops-check":
        _research_ops_check(args.config)
    elif args.command == "research-ops-plan":
        _research_ops_plan(args.config, args.case_id, args.limit)
    elif args.command == "research-stage":
        _research_stage(
            args.entity_type,
            args.entity_id,
            args.title,
            args.thesis,
            args.evidence_ref,
            args.review_question,
            args.actor,
        )
    elif args.command == "research-commit":
        _research_commit(args.draft_id, args.message, args.actor)
    elif args.command == "research-approve":
        _research_approve(args.draft_id, args.notes, args.actor)
    elif args.command == "research-reject":
        _research_reject(args.draft_id, args.notes, args.actor)
    elif args.command == "research-defer":
        _research_defer(args.draft_id, args.notes, args.actor)
    elif args.command == "research-status":
        _research_status(args.status, args.write_summary)
    elif args.command == "research-auto-stage":
        _research_auto_stage(args.report, args.config)
    elif args.command == "workspace-sync":
        _workspace_sync(args.report)
    elif args.command == "market-cache-seed":
        _market_cache_seed(args.config, args.symbol)
    elif args.command == "market-data-governance-status":
        _market_data_governance_status(args.config)
    elif args.command == "dashboard":
        _dashboard(args.config)
    elif args.command == "review-workbench":
        _review_workbench(args.config, args.host, args.port, args.status, args.risk, args.task_id, args.once)
    elif args.command == "review-priority":
        _review_priority(args.config)
    elif args.command == "architecture-sync":
        _architecture_sync(args.config)
    elif args.command == "event-sync":
        _event_sync(args.config)
    elif args.command == "event-state":
        _event_state(args.config)
    elif args.command == "event-log":
        _event_log(args.limit)
    elif args.command == "event-compact":
        _event_compact(args.config)
    elif args.command == "event-health":
        _event_health(args.config)
    elif args.command == "event-audit":
        _event_audit(args.config, args.entity_type, args.entity_id, args.limit)
    elif args.command == "alerts":
        _alerts(args.config)
    elif args.command == "doctor":
        _doctor(args.config)
    elif args.command == "release-audit":
        _release_audit(args.config, args.require_ready)
    elif args.command == "release-controls":
        _release_controls(args.config, args.fetch_python_metadata, args.require_pass)
    elif args.command == "python-env-audit":
        _python_environment_audit(args.python, args.run_tests, args.require_pass)
    elif args.command == "backup-create":
        _backup_create(args.config, args.require_pass)
    elif args.command == "backup-verify":
        _backup_verify(args.config, args.backup, args.require_pass)
    elif args.command == "restore-drill":
        _restore_drill(args.config, args.backup, args.require_pass)
    elif args.command == "backup-restore-status":
        _backup_restore_status(args.config, args.require_ready, args.require_production_ready)
    elif args.command == "run-coordination-status":
        _run_coordination_status(args.config, args.mode, args.require_ready)
    elif args.command == "run-recover-stale":
        _run_recover_stale(args.config, args.mode, args.reason)
    elif args.command == "value-validation-status":
        _value_validation_status(args.config, args.require_ready)
    elif args.command == "value-study-init":
        _value_study_init(args.config, args.title, args.scenario, args.actor, args.actor_role)
    elif args.command == "value-case-register":
        _value_case_register(
            args.config, args.case_id, args.case_ref, args.case_sha256, args.priority,
            args.stratum, args.evidence_cutoff, args.data_classification, args.actor, args.actor_role,
        )
    elif args.command == "value-case-exclude":
        _value_case_exclude(
            args.config, args.case_id, args.reason, args.category, args.actor, args.actor_role
        )
    elif args.command == "value-cohort-freeze":
        _value_cohort_freeze(args.config, args.actor, args.actor_role)
    elif args.command == "value-baseline-record":
        _value_baseline_record(
            args.config, args.case_id, args.output_ref, args.output_sha256,
            args.metrics_file, args.actor, args.actor_role,
        )
    elif args.command == "value-treatment-freeze":
        _value_treatment_freeze(args.config, args.actor, args.actor_role)
    elif args.command == "value-assisted-record":
        _value_assisted_record(
            args.config, args.case_id, args.output_ref, args.output_sha256, args.metrics_file,
            args.observed_manifest_id, args.attest_outcome_hidden, args.attest_evidence_cutoff,
            args.unauthorized_egress_count, args.actor, args.actor_role,
        )
    elif args.command == "value-blind-prepare":
        _value_blind_prepare(args.config, args.case_id, args.actor, args.actor_role)
    elif args.command == "value-blind-evaluate":
        _value_blind_evaluate(
            args.config, args.assignment_id, args.scores_a_file, args.scores_b_file,
            args.attest_blinding, args.attest_no_ledger_access, args.notes,
            args.actor, args.actor_role,
        )
    elif args.command == "value-blind-reveal":
        _value_blind_reveal(args.config, args.assignment_id, args.actor, args.actor_role)
    elif args.command == "value-gate-decision":
        _value_gate_decision(
            args.config, args.decision, args.notes, args.evidence_ref,
            args.decision_deadline, args.actor, args.actor_role,
        )
    elif args.command == "llm-governance-status":
        _llm_governance_status(args.config)
    elif args.command == "model-ops-status":
        _model_ops_status(args.config)
    elif args.command == "pi-runtime-status":
        _pi_runtime_status(args.config, args.require_ready)
    elif args.command == "pi-runtime-uat":
        _pi_runtime_uat(args.config, args.live)
    elif args.command == "pi-skills-status":
        _pi_skills_status(args.config, args.require_ready)
    elif args.command == "pi-skills-lock":
        _pi_skills_lock(args.config, args.write)
    elif args.command == "pi-skills-uat":
        _pi_skills_uat(args.config, args.live)
    elif args.command == "agent-handoff":
        _agent_handoff(args.config, args.report, args.check)
    elif args.command == "pi-handoff-run":
        _pi_handoff_run(args.config, args.report)
    elif args.command == "openclaw-handoff":
        _openclaw_handoff(args.config, args.report)
    elif args.command == "production-pilot-sync":
        _production_pilot_sync(args.config, getattr(args, "summary", ""))
    elif args.command == "production-pilot-status":
        _production_pilot_status(args.config, args.require_go)
    elif args.command == "control-evidence-register":
        _control_evidence_register(
            args.config, args.file, args.category, args.title, args.subject_id, args.owner_role,
            args.external_archive_ref, args.actor, args.actor_role, args.valid_from, args.valid_to, args.notes,
        )
    elif args.command == "control-evidence-review":
        _control_evidence_review(
            args.config, args.evidence_id, args.decision, args.actor, args.actor_role,
            args.notes, args.review_record_ref,
        )
    elif args.command == "control-evidence-revoke":
        _control_evidence_revoke(args.config, args.evidence_id, args.actor, args.actor_role, args.notes)
    elif args.command == "control-evidence-status":
        _control_evidence_status(args.config, args.require_ready)
    elif args.command == "business-onboarding-pack":
        _business_onboarding_pack(args.config)
    elif args.command == "business-onboarding-sync":
        _business_onboarding_sync(args.config)
    elif args.command == "business-onboarding-status":
        _business_onboarding_status(args.config, args.require_ready)
    elif args.command == "provider-init":
        _provider_init(args.config, args.provider_id, args.legal_name, args.scope, args.actor, args.notes)
    elif args.command == "provider-due-diligence":
        _provider_due_diligence(
            args.config, args.provider_id, args.actor, args.actor_role, args.evidence_file, args.notes,
        )
    elif args.command == "provider-review":
        _provider_review(
            args.config, args.provider_id, args.decision, args.actor, args.actor_role, args.notes, args.evidence_ref,
        )
    elif args.command == "provider-activate":
        _provider_activate(args.config, args.provider_id, args.actor, args.actor_role, args.notes)
    elif args.command == "provider-suspend":
        _provider_suspend(args.config, args.provider_id, args.actor, args.actor_role, args.notes)
    elif args.command == "production-pilot-approval":
        _production_pilot_approval(
            args.config, args.gate, args.decision, args.actor, args.actor_role, args.notes, args.evidence_ref,
        )
    elif args.command == "production-pilot-activate":
        _production_pilot_activate(args.config, args.actor, args.actor_role, args.notes, args.evidence_ref)
    elif args.command == "production-pilot-revoke":
        _production_pilot_revoke(args.config, args.actor, args.actor_role, args.notes)
    elif args.command == "production-pilot-incident":
        _production_pilot_incident(
            args.config, args.title, args.severity, args.category, args.actor, args.actor_role, args.notes, args.evidence_ref,
        )
    elif args.command == "production-pilot-incident-action":
        _production_pilot_incident_action(
            args.config, args.incident_id, args.action, args.actor, args.actor_role, args.notes, args.evidence_ref,
        )
    elif args.command == "llm-governance-review":
        _llm_governance_review(
            args.config,
            args.exception_id,
            args.disposition,
            args.actor,
            args.actor_role,
            args.notes,
            args.evidence_ref,
        )
    elif args.command == "task-create":
        _task_create(args.config, args.title, args.spec, args.case_id, args.execution_mode,
                     args.from_symbols, args.research_question, args.lookback_hours)
    elif args.command == "task-rescan":
        _task_rescan(args.config, args.purpose, args.execution_mode, args.actor,
                     args.symbols, args.lookback_hours)
    elif args.command == "task-detail":
        _task_detail(args.config, args.task_id)
    elif args.command == "daily-task-cycle":
        _daily_task_cycle(args.config, args.purpose, args.execution_mode, args.actor,
                          args.worker_id, args.max_tasks)
    elif args.command == "overview":
        _overview(args.config)
    elif args.command == "events-view":
        _events_view(args.config, args.lookback_hours, args.limit)
    elif args.command == "task-list":
        _task_list(args.config, args.status, args.json)
    elif args.command == "task-run-next":
        _task_run_next(args.config, args.worker_id, args.ttl_seconds)
    elif args.command == "task-cancel":
        _task_cancel(args.config, args.task_id, args.actor, args.reason, args.expected_version)
    elif args.command == "human-response":
        _human_response(args.config, args.task_id, args.actor, args.decision, args.expected_version,
                        args.command_id, args.input_patch, args.notes)
    elif args.command == "task-recover":
        _task_recover(args.config)
    elif args.command == "task-board":
        _task_board(args.config, args.port)
    elif args.command == "run-manifest-bind":
        _run_manifest_bind(args.config, args.summary)


def _task_summary(args) -> None:
    import sqlite3
    from .local_identity import authenticate_local_actor
    from .summary_review import review_draft, review_state, submit_draft
    from .task_runtime import TaskRuntimeError, TaskStore

    config = load_config(args.config)
    db = project_path(str((config.get("task_runtime") or {}).get("db_path") or "data/task_runtime/tasks.sqlite3"))
    if not db.is_file():
        raise SystemExit("Task database does not exist; no files were initialized")
    try:
        store = TaskStore(db, read_only=True)
        try:
            task = store.get_task(args.task_id)
            if args.action == "show":
                result = review_state(store, args.task_id, config=config, draft_id=args.draft_id, include_local_content=True)
                result["task_version"] = task["version"]
                print(json.dumps(result, ensure_ascii=False, indent=2))
                return
            authenticate_local_actor(config, args.actor, task["input_payload"].get("symbols"), review=args.action == "review")
        finally:
            store.close()
        spec = None
        if args.action == "submit":
            with project_path(args.spec).open("rb") as handle:
                data = handle.read(65537)
            if len(data) > 65536:
                raise ValueError("summary spec is too large")
            spec = json.loads(data.decode("utf-8-sig"))
            if not isinstance(spec, dict) or set(spec) != {"text", "content_kind", "selectors"}:
                raise ValueError("summary spec must contain exactly text, content_kind, selectors")
        store = TaskStore(db)
        try:
            common = dict(config=config, actor=args.actor, expected_version=args.expected_version, command_id=args.command_id)
            if args.action == "submit":
                result = submit_draft(store, args.task_id, selectors=spec["selectors"], text=spec["text"], kind=spec["content_kind"], **common)
            else:
                result = review_draft(store, args.task_id, draft_id=args.draft_id, summary_id=args.summary_id,
                                      decision=args.decision, notes=args.notes, **common)
            result["task_version"] = store.get_task(args.task_id)["version"]
        finally:
            store.close()
    except (OSError, ValueError, TaskRuntimeError, sqlite3.Error) as exc:
        raise SystemExit("Local summary review rejected: " + str(exc)) from exc
    print(json.dumps(result, ensure_ascii=False, indent=2))


def _task_store(config_path: str):
    from .task_runtime import TaskStore

    config = load_config(config_path)
    db_path = project_path(str((config.get("task_runtime") or {}).get("db_path") or "data/task_runtime/tasks.sqlite3"))
    return TaskStore(db_path), config


def _validated_public_spec(spec: dict) -> dict:
    """Require an explicit public classification; never promote it silently."""

    if not isinstance(spec, dict) or str(spec.get("data_classification") or "").lower() != "public":
        raise SystemExit("任务 spec 必须显式声明 data_classification=public；系统不会自动提升分类")
    return spec


def _task_create(config_path: str, title: str, spec_path: str, case_id: str, execution_mode: str,
                 from_symbols: str = "", research_question: str = "", lookback_hours: int = 48) -> None:
    import json as _json
    from pathlib import Path as _Path

    if from_symbols.strip():
        # Question tasks go through the unified intake service: the same
        # question (identity = text + symbols + mode) returns the existing
        # task even when the rebuilt public context differs byte-wise.
        from .task_intake import ensure_question_task

        symbols = [item.strip() for item in from_symbols.split(",") if item.strip()]
        supply_config = load_config(config_path)
        store, _ = _task_store(config_path)
        try:
            result = ensure_question_task(
                store,
                research_question=research_question or title,
                symbols=symbols,
                config=supply_config,
                execution_mode=execution_mode,
                lookback_hours=lookback_hours,
            )
        finally:
            store.close()
        task, spec, status = result["task"], result["task"]["input_payload"], result["status"]
    else:
        if not spec_path:
            raise SystemExit("task-create 需要 --spec 或 --from-symbols 之一")
        spec = _json.loads(_Path(spec_path).read_text(encoding="utf-8"))
        spec = _validated_public_spec(spec)
        store, _ = _task_store(config_path)
        try:
            task, status = store.create_task(
                title=title,
                input_payload=spec,
                case_id=case_id,
                execution_mode=execution_mode,
            )
        finally:
            store.close()
    quotes = (spec.get("public_context") or {}).get("quote_snapshots") or {}
    print("task {0}: {1} (mode={2}, quotes={3}, missing={4})".format(
        task["task_id"], status, task["execution_mode"],
        sorted(quotes.keys()),
        (spec.get("public_context") or {}).get("context_notes", {}).get("quotes_missing", [])))


def _task_rescan(config_path: str, purpose: str, execution_mode: str, actor: str,
                 symbols: str, lookback_hours: int) -> None:
    from .task_intake import rescan_pending_events

    symbol_list = [item.strip() for item in symbols.split(",") if item.strip()] or None
    store, config = _task_store(config_path)
    try:
        report = rescan_pending_events(
            store, config=config, purpose=purpose, actor=actor,
            execution_mode=execution_mode, symbols=symbol_list,
            lookback_hours=lookback_hours if lookback_hours > 0 else None)
        summary = store.consumption_summary()
    finally:
        store.close()
    print(json.dumps({"rescan": report, "consumption_ledger": summary},
                     ensure_ascii=False, default=str, indent=2))


def _task_detail(config_path: str, task_id: str) -> None:
    from .task_detail import task_detail

    store, config = _task_store(config_path)
    try:
        try:
            detail = task_detail(store, task_id, config=config)
        except TaskRuntimeError as exc:
            raise SystemExit("task-detail: {0}".format(exc))
        summary = store.consumption_summary()
    finally:
        store.close()
    print(json.dumps(detail, ensure_ascii=False, default=str, indent=2))
    print("consumption_ledger: {0}".format(json.dumps(summary, ensure_ascii=False)))


def _daily_task_cycle(config_path: str, purpose: str, execution_mode: str, actor: str,
                      worker_id: str, max_tasks: int) -> None:
    from .task_intake import run_daily_task_cycle

    store, config = _task_store(config_path)
    try:
        try:
            report = run_daily_task_cycle(
                store, config=config, purpose=purpose, actor=actor,
                worker_id=worker_id, execution_mode=execution_mode,
                max_tasks=max_tasks)
        except ValueError as exc:
            raise SystemExit("daily-task-cycle: {0}".format(exc))
    finally:
        store.close()
    print(json.dumps(report, ensure_ascii=False, default=str, indent=2))


def _overview(config_path: str) -> None:
    from pathlib import Path as _Path

    from .overview import build_overview

    config = load_config(config_path)
    db_path = project_path(str((config.get("task_runtime") or {}).get("db_path")
                               or "data/task_runtime/tasks.sqlite3"))
    store = None
    if db_path.is_file():
        store, _ = _task_store(config_path)
    try:
        payload = build_overview(config, store=store)
    finally:
        if store is not None:
            store.close()
    print(json.dumps(payload, ensure_ascii=False, default=str, indent=2))


def _events_view(config_path: str, lookback_hours: int, limit: int) -> None:
    from .events_view import build_events_view

    config = load_config(config_path)
    db_path = project_path(str((config.get("task_runtime") or {}).get("db_path")
                               or "data/task_runtime/tasks.sqlite3"))
    store = None
    if db_path.is_file():
        store, _ = _task_store(config_path)
    try:
        payload = build_events_view(config, store=store,
                                    lookback_hours=max(1, lookback_hours),
                                    limit=max(1, limit))
    finally:
        if store is not None:
            store.close()
    print(json.dumps(payload, ensure_ascii=False, default=str, indent=2))


def _task_list(config_path: str, status: str, as_json: bool) -> None:
    store, _ = _task_store(config_path)
    try:
        tasks = store.list_tasks(status or "")
        summary = store.summary()
    finally:
        store.close()
    if as_json:
        print(_json.dumps({"summary": summary, "tasks": tasks}, ensure_ascii=False, default=str, indent=2))
        return
    print("summary: {0}".format(json.dumps(summary, ensure_ascii=False)))
    for task in tasks:
        print("{0} [{1}] {2} input_v={3} row_v={4} mode={5}".format(
            task["task_id"], task["status"], task["title"][:40], task["input_version"], task["version"], task["execution_mode"]))


def _task_run_next(config_path: str, worker_id: str, ttl_seconds: int) -> None:
    from .research_worker import run_research_task

    store, config = _task_store(config_path)
    worker = worker_id or "worker-{0}".format(os.getpid())
    try:
        task = store.claim_next(worker, ttl_seconds=ttl_seconds)
        if task is None:
            print("no queued task")
            return
        result = run_research_task(store, task["task_id"], worker_id=worker, config=config, ttl_seconds=ttl_seconds)
        print("task {0} -> {1} (model mode={2}, called={3})".format(
            task["task_id"], result.get("status"),
            (result.get("model_call") or {}).get("mode"),
            (result.get("model_call") or {}).get("called")))
        if result.get("status") == "awaiting_human":
            question = store.get_task(task["task_id"]).get("human_request") or {}
            print("human question: {0}".format(question.get("question") or ""))
    finally:
        store.close()


def _task_cancel(config_path: str, task_id: str, actor: str, reason: str, expected_version: int) -> None:
    store, _ = _task_store(config_path)
    try:
        task = store.request_cancel(task_id, actor=actor, reason=reason, expected_version=expected_version)
    finally:
        store.close()
    print("task {0} -> {1}".format(task_id, task["status"]))


def _human_response(config_path: str, task_id: str, actor: str, decision: str, expected_version: int,
                    command_id: str, input_patch_text: str, notes: str) -> None:
    store, _ = _task_store(config_path)
    input_patch = None
    if input_patch_text.strip():
        input_patch = json.loads(input_patch_text)
    try:
        task = store.record_human_response(
            task_id, actor=actor, decision=decision, expected_version=expected_version,
            command_id=command_id, input_patch=input_patch, notes=notes,
        )
    finally:
        store.close()
    print("task {0} -> {1}（个人反馈已记录，不构成机构批准）".format(task_id, task["status"]))


def _task_recover(config_path: str) -> None:
    store, _ = _task_store(config_path)
    try:
        recovered = store.recover_abandoned()
    finally:
        store.close()
    for task in recovered:
        print("recovered {0} -> {1}".format(task["task_id"], task["status"]))
    if not recovered:
        print("no abandoned tasks")


def _task_board(config_path: str, port: int) -> None:
    from .task_board import serve_task_board

    config = load_config(config_path)
    serve_task_board(config, port=port)


def _run_manifest_bind(config_path: str, summary_path: str) -> None:
    from .run_manifest import bind_scheduled_summary

    config = load_config(config_path)
    result = bind_scheduled_summary(config, summary_path)
    print("manifest binding: {0}".format(result.get("status")))
    for error in result.get("errors", []):
        print("  - {0}".format(error))
    manifest = result.get("manifest") or {}
    if manifest:
        print("run_id: {0} status: {1} eligible: {2}".format(
            manifest.get("run_id"), manifest.get("status"), manifest.get("eligible_for_pilot")))
    if result.get("status") != "PASS":
        raise SystemExit(1)


def _run_once(config_path: str) -> None:
    config = load_config(config_path)
    config["_progress_writer"] = lambda message: print(message, flush=True)
    result = run_pipeline(config)
    print("完成：新增 {0} 条，预警 {1} 条。".format(result.stats.get("inserted", 0), len(result.alerts)))
    print("日报: {0}".format(result.report_path))
    print("结构化结果: {0}".format(result.json_path))
    print("预警: {0}".format(result.alert_path))


def _agent_run_once(config_path: str, trigger_source: str = "manual") -> None:
    from .execution_mode import assert_agent_run_allowed

    config = load_config(config_path)
    # Single-engine rule: when the task engine owns the event stream, the
    # batch research run must not analyze the same signals again.
    assert_agent_run_allowed(config)
    config["_progress_writer"] = lambda message: print(message, flush=True)
    # The declared trigger source decides manifest pilot eligibility; it flows
    # into the pipeline config rather than being inferred from latest files.
    config["_trigger_source"] = str(trigger_source or "manual")
    result = run_agent_pipeline(config)
    print("完成：新增 {0} 条，预警 {1} 条。".format(result.stats.get("inserted", 0), len(result.alerts)))
    llm_called = result.stats.get("llm_called", 0)
    llm_success = result.stats.get("llm_success", 0)
    llm_failed = result.stats.get("llm_failed", 0)
    llm_model = result.stats.get("llm_model", "")
    if llm_called:
        model_info = " (model: {})".format(llm_model) if llm_model else ""
        print("LLM 研判: 调用 {0} 次，成功 {1} 条，失败 {2} 条。{3}".format(llm_called, llm_success, llm_failed, model_info))
    else:
        reason = "未启用" if not result.stats.get("llm_enabled") else "无符合条件的信号"
        if result.stats.get("llm_skipped_no_key"):
            reason = "缺少 LLM_API_KEY 环境变量"
        print("LLM 研判: {0}。".format(reason))
    print("日报: {0}".format(result.report_path))
    print("结构化结果: {0}".format(result.json_path))
    print("预警: {0}".format(result.alert_path))
    if result.stats.get("research_git_enabled"):
        print("Research-as-Git 草稿: 新增 {0} 条，待复核 {1} 条".format(
            result.stats.get("research_git_auto_staged_count", 0),
            result.stats.get("research_git_pending_review_count", 0),
        ))
        if result.stats.get("research_git_summary_latest_path"):
            print("Research-as-Git 摘要: {0}".format(result.stats.get("research_git_summary_latest_path")))
    if result.review_path:
        print("待复核: {0}".format(result.review_path))
    if result.actions_path:
        print("动作日志: {0}".format(result.actions_path))


def _watch(config_path: str, interval_minutes: float, max_runs: int) -> None:
    run_count = 0
    while True:
        run_count += 1
        print("第 {0} 轮开始。".format(run_count))
        _run_once(config_path)
        if max_runs and run_count >= max_runs:
            break
        sleep_seconds = max(1.0, interval_minutes * 60.0)
        print("等待 {0:.1f} 分钟。".format(interval_minutes))
        time.sleep(sleep_seconds)


def _init_config(target: str, force: bool) -> None:
    source = project_path("config/watchlist.example.json")
    destination = project_path(target)
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists() and not force:
        raise FileExistsError("目标配置已存在: {0}。如需覆盖请加 --force。".format(destination))
    shutil.copyfile(str(source), str(destination))
    print("已创建: {0}".format(destination))


def _export_review(config_path: str) -> None:
    config = load_config(config_path)
    path = export_review(config)
    if path:
        print("待复核文件: {0}".format(path))
    else:
        print("无符合条件的待复核信号。")


def _review_summary() -> None:
    summary = compute_review_summary()
    case_summary = summary.get("research_case_summary", {}) or {}
    has_case_status_changes = bool(case_summary.get("status_changes"))
    decision_summary = summary.get("decision_ledger_summary", {}) or {}
    has_decision_records = int(decision_summary.get("total", 0) or 0) > 0
    if summary["total"] == 0 and not has_case_status_changes and not has_decision_records:
        print("未找到已完成复盘数据（data/review/completed_review_*.jsonl）。")
        return
    md_path, json_path = write_review_summary(summary)
    print("复盘汇总: {0}".format(md_path))
    print("复盘数据: {0}".format(json_path))


def _review_queue_sync(config_path: str) -> None:
    config = load_config(config_path)
    result = write_review_queue_snapshot(config)
    summary = result["review_queue_summary"]
    print("review_queue: {0}".format(result["review_queue_latest_path"]))
    print("outstanding: {0}".format(summary.get("outstanding_count", 0)))
    print("overdue: {0}".format(summary.get("overdue_count", 0)))
    print("at_risk: {0}".format(summary.get("at_risk_count", 0)))
    print("unassigned: {0}".format(summary.get("unassigned_count", 0)))
    print("automatic_assignment_executed: false")


def _review_queue_status(config_path: str) -> None:
    config = load_config(config_path)
    queue = build_review_queue(config)
    summary = queue.get("summary", {})
    print("status_counts: {0}".format(summary.get("status_counts", {})))
    print("outstanding: {0}".format(summary.get("outstanding_count", 0)))
    print("overdue: {0}; at_risk: {1}; unassigned: {2}".format(
        summary.get("overdue_count", 0),
        summary.get("at_risk_count", 0),
        summary.get("unassigned_count", 0),
    ))
    missing = summary.get("missing_human_capabilities", []) or []
    print("missing_human_capabilities: {0}".format(", ".join(missing) if missing else "none"))
    print("assignment_mode: suggest_only; automatic_assignment_executed: false")


def _review_queue_action(
    config_path: str,
    signal_id: str,
    action: str,
    actor: str,
    actor_role: str,
    notes: str,
    decision_taken: str,
    was_effective: str,
    actual_price_move: str,
    actual_news_followup: str,
    false_positive_reason: str,
) -> None:
    config = load_config(config_path)
    result = record_review_queue_action(
        config, signal_id, action, actor, actor_role, notes=notes,
        fields={
            "decision_taken": decision_taken,
            "was_effective": was_effective,
            "actual_price_move": actual_price_move,
            "actual_news_followup": actual_news_followup,
            "false_positive_reason": false_positive_reason,
        },
    )
    record = result.get("record", {}) or {}
    print("review_action: {0}".format(record.get("action") or record.get("queue_action")))
    print("signal_id: {0}".format(signal_id))
    print("record_path: {0}".format(result.get("record_path", "")))
    print("automatic_assignment_or_approval_executed: false")


def _decision_list() -> None:
    pending = list_pending_decisions()
    if not pending:
        print("暂无待上司处理的人工决策。")
        return
    for item in pending:
        print("[{0}] {1} {2}".format(
            item.get("priority", ""),
            item.get("signal_id", ""),
            item.get("title", ""),
        ))
        allowed = item.get("allowed_decisions", [])
        if allowed:
            print("  allowed: {0}".format(", ".join(allowed)))


def _decision_record(
    signal_id: str,
    decision: str,
    notes: str,
    actor: str,
    outcome: str = "",
    followup_result: str = "",
    evidence_refs: list | None = None,
) -> None:
    record = record_decision(
        signal_id,
        decision,
        notes=notes,
        actor=actor,
        outcome=outcome,
        followup_result=followup_result,
        evidence_refs=evidence_refs or [],
    )
    print("已记录人工决策: {0} -> {1}".format(record["signal_id"], record["decision"]))
    if record.get("notes"):
        print("备注: {0}".format(record["notes"]))


def _decision_confirm(
    signal_id: str,
    outcome: str,
    followup_result: str,
    actor: str,
    evidence_refs: list | None = None,
    notes: str = "",
) -> None:
    _record_shortcut_decision(signal_id, "confirm_signal", outcome, followup_result, actor, evidence_refs or [], notes)


def _decision_reject(
    signal_id: str,
    outcome: str,
    followup_result: str,
    actor: str,
    evidence_refs: list | None = None,
    notes: str = "",
) -> None:
    _record_shortcut_decision(signal_id, "reject_signal", outcome, followup_result, actor, evidence_refs or [], notes)


def _decision_defer(
    signal_id: str,
    outcome: str,
    followup_result: str,
    actor: str,
    evidence_refs: list | None = None,
    notes: str = "",
) -> None:
    _record_shortcut_decision(signal_id, "defer_signal", outcome, followup_result, actor, evidence_refs or [], notes)


def _record_shortcut_decision(
    signal_id: str,
    decision: str,
    outcome: str,
    followup_result: str,
    actor: str,
    evidence_refs: list,
    notes: str,
) -> None:
    record = record_decision(
        signal_id,
        decision,
        notes=notes,
        actor=actor,
        outcome=outcome,
        followup_result=followup_result,
        evidence_refs=evidence_refs,
    )
    print("decision: {0} -> {1}".format(record["signal_id"], record["decision"]))
    if record.get("outcome"):
        print("outcome: {0}".format(record["outcome"]))


def _decision_summary() -> None:
    summary = compute_decision_summary()
    paths = write_decision_summary(summary)
    print("人工决策汇总: {0}".format(paths["decision_summary_path"]))
    print("人工决策数据: {0}".format(paths["decision_summary_json_path"]))


def _attach_evidence_url(task_id: str, url: str, actor: str, notes: str) -> None:
    record = attach_evidence_url(task_id, url, actor=actor, notes=notes)
    print("remediation: {0} -> {1}".format(record["task_id"], record["status"]))
    print("action: {0}".format(record["action"]))


def _attach_structured_fields(task_id: str, fields: list, actor: str, notes: str) -> None:
    record = attach_structured_fields(task_id, _parse_fields(fields), actor=actor, notes=notes)
    print("remediation: {0} -> {1}".format(record["task_id"], record["status"]))
    print("action: {0}".format(record["action"]))


def _resolve_remediation(task_id: str, notes: str, actor: str) -> None:
    record = resolve_remediation_task(task_id, notes=notes, actor=actor)
    print("remediation: {0} -> {1}".format(record["task_id"], record["status"]))
    print("action: {0}".format(record["action"]))


def _reject_remediation(task_id: str, reason: str, actor: str) -> None:
    record = reject_remediation_task(task_id, reason=reason, actor=actor)
    print("remediation: {0} -> {1}".format(record["task_id"], record["status"]))
    print("action: {0}".format(record["action"]))


def _defer_remediation(task_id: str, reason: str, actor: str) -> None:
    record = defer_remediation_task(task_id, reason=reason, actor=actor)
    print("remediation: {0} -> {1}".format(record["task_id"], record["status"]))
    print("action: {0}".format(record["action"]))


def _parse_fields(fields: list) -> dict:
    parsed = {}
    for item in fields:
        if "=" not in str(item):
            raise ValueError("--field must be key=value: {0}".format(item))
        key, value = str(item).split("=", 1)
        key = key.strip()
        if not key:
            raise ValueError("--field key cannot be empty")
        parsed[key] = value.strip()
    return parsed


def _case_list(status: str) -> None:
    cases = list_research_cases(status=status)
    if not cases:
        print("No research cases.")
        return
    for case in cases:
        print("[{0}] {1} {2} signals={3}".format(
            case.get("status", "active"),
            case.get("case_id", ""),
            case.get("title", ""),
            case.get("signal_count", 0),
        ))


def _case_record(case_id: str, decision: str, notes: str, actor: str) -> None:
    record = record_case_decision(case_id, decision, notes=notes, actor=actor)
    print("Recorded research case decision: {0} -> {1}".format(record["case_id"], record["decision"]))
    if record.get("notes"):
        print("notes: {0}".format(record["notes"]))


def _case_summary() -> None:
    summary = compute_case_summary()
    paths = write_case_summary(summary)
    print("Research case summary: {0}".format(paths["research_case_summary_path"]))
    print("Research case data: {0}".format(paths["research_case_summary_json_path"]))


def _research_runtime_sync(config_path: str, report_path: str = "") -> None:
    config = load_config(config_path)
    result = sync_research_runtime(config, report_path=report_path)
    print("research runtime cases: {0}".format(result.get("research_runtime_case_count", 0)))
    print("blocked: {0}".format(result.get("research_runtime_blocked_count", 0)))
    print("pending human gates: {0}".format(result.get("research_runtime_pending_human_count", 0)))
    if result.get("research_runtime_latest_path"):
        print("research runtime: {0}".format(result.get("research_runtime_latest_path")))


def _research_runtime_status(case_id: str = "", stage: str = "", write_summary: bool = False) -> None:
    runtime = load_research_runtime()
    cases = [item for item in runtime.get("cases", []) if isinstance(item, dict)]
    if case_id:
        cases = [item for item in cases if str(item.get("case_id", "")) == str(case_id)]
    if stage:
        cases = [item for item in cases if str(item.get("stage", "")) == str(stage)]
    if not cases:
        print("No research runtime cases.")
    for item in cases:
        print("[{0}/{1}] {2} {3}".format(
            item.get("top_priority", ""),
            item.get("stage", ""),
            item.get("case_id", ""),
            item.get("title", ""),
        ))
        if item.get("blockers"):
            print("  blockers: {0}".format("; ".join(str(value) for value in item.get("blockers", [])[:8])))
        if item.get("next_actions"):
            print("  next: {0}".format("; ".join(str(value) for value in item.get("next_actions", [])[:6])))
    summary = summarize_research_runtime(runtime)
    print("stage_counts: {0}".format(", ".join(
        "{0}={1}".format(key, value) for key, value in summary.get("stage_counts", {}).items()
    )))
    if write_summary:
        paths = write_research_runtime_report(runtime, summary)
        print("research runtime: {0}".format(paths.get("research_runtime_latest_path", "")))


def _research_runtime_action(
    case_id: str,
    action: str,
    actor: str,
    actor_role: str,
    notes: str,
    task_id: str,
    assignee: str,
    references: list,
    checks: list,
    config_path: str,
) -> None:
    config = load_config(config_path)
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
        config=config,
    )
    case = result.get("case", {})
    print("research runtime action: {0} -> {1}".format(case_id, action))
    print("stage: {0}".format(case.get("stage", "")))
    if case.get("blockers"):
        print("blockers: {0}".format("; ".join(str(value) for value in case.get("blockers", [])[:8])))
    print("runtime report: {0}".format(result.get("research_runtime_latest_path", "")))


def _evidence_vault_import(
    config_path: str,
    file_path: str,
    source_url: str,
    title: str,
    published_at: str,
    actor: str,
    actor_role: str,
    signal_ids: list,
    issuer: str,
    symbol: str,
    source_kind: str,
    content_type: str,
) -> None:
    config = load_config(config_path)
    result = _policy_call(lambda: import_official_document(
        config,
        file_path,
        source_url=source_url,
        title=title,
        published_at=published_at,
        actor=actor,
        actor_role=actor_role,
        signal_ids=signal_ids,
        issuer=issuer,
        symbol=symbol,
        source_kind=source_kind,
        content_type=content_type,
    ))
    _refresh_workpapers_after_evidence(config)
    _print_evidence_capture(result)


def _evidence_vault_fetch(
    config_path: str,
    source_url: str,
    title: str,
    published_at: str,
    actor: str,
    actor_role: str,
    signal_ids: list,
    issuer: str,
    symbol: str,
    source_kind: str,
    live: bool,
) -> None:
    if not live:
        raise SystemExit(
            "Refusing network evidence capture without --live; the command fetches one exact reviewed official URL."
        )
    config = load_config(config_path)
    result = _policy_call(lambda: fetch_official_document(
        config,
        source_url=source_url,
        title=title,
        published_at=published_at,
        actor=actor,
        actor_role=actor_role,
        signal_ids=signal_ids,
        issuer=issuer,
        symbol=symbol,
        source_kind=source_kind,
    ))
    _refresh_workpapers_after_evidence(config)
    _print_evidence_capture(result)


def _evidence_vault_uat(
    config_path: str,
    source_url: str,
    title: str,
    published_at: str,
    actor: str,
    actor_role: str,
    signal_ids: list,
    issuer: str,
    symbol: str,
    source_kind: str,
    live: bool,
) -> None:
    try:
        result = run_evidence_vault_uat(
            load_config(config_path),
            source_url=source_url,
            title=title,
            published_at=published_at,
            actor=actor,
            actor_role=actor_role,
            signal_ids=signal_ids,
            issuer=issuer,
            symbol=symbol,
            source_kind=source_kind,
            live=live,
        )
    except EvidenceVaultUATError as exc:
        raise SystemExit(str(exc))
    print("official evidence live UAT: {0}".format(result.get("status", "FAIL")))
    print("source/domain: {0} / {1}".format(result.get("source_url", ""), result.get("source_domain", "")))
    print("type/bytes: {0} / {1}".format(result.get("content_type", ""), result.get("byte_size", 0)))
    print("sha256: {0}".format(result.get("sha256", "")))
    print("temporary technical/integrity: {0}/{1}".format(
        result.get("temporary_technical_status", "UNKNOWN"), result.get("temporary_integrity_status", "UNKNOWN")
    ))
    print("production inventory unchanged: {0}".format(
        str(bool(result.get("production_inventory_unchanged"))).lower()
    ))
    print("raw document retained after UAT: false")
    print("report: {0}".format(result.get("evidence_vault_uat_latest_path", "")))
    if result.get("status") != "PASS":
        raise SystemExit(5)


def _evidence_vault_link(
    config_path: str,
    document_id: str,
    signal_id: str,
    actor: str,
    actor_role: str,
    notes: str,
) -> None:
    config = load_config(config_path)
    result = _policy_call(lambda: link_evidence_to_signal(
        config,
        document_id=document_id,
        signal_id=signal_id,
        actor=actor,
        actor_role=actor_role,
        notes=notes,
    ))
    _refresh_workpapers_after_evidence(config)
    print("evidence link: {0} -> {1}".format(document_id, signal_id))
    print("duplicate: {0}".format(str(bool(result.get("duplicate"))).lower()))
    print("report: {0}".format(result.get("evidence_vault_latest_path", project_path("reports/evidence_vault/latest.md"))))


def _evidence_vault_status(
    config_path: str,
    require_ready: bool = False,
    require_business_ready: bool = False,
) -> None:
    config = load_config(config_path)
    vault = load_evidence_vault(config)
    paths = write_evidence_vault_reports(vault, config)
    verification = verify_evidence_vault(config)
    print("official evidence vault technical: {0}".format(verification.get("technical_status", "UNKNOWN")))
    print("official evidence vault business: {0}".format(verification.get("business_status", "UNKNOWN")))
    print("event-chain integrity: {0}".format(verification.get("integrity_status", "UNKNOWN")))
    print("documents/events: {0}/{1}".format(
        verification.get("document_count", 0), verification.get("event_count", 0)
    ))
    print("verified/quarantined/tampered: {0}/{1}/{2}".format(
        verification.get("verified_object_count", 0), verification.get("quarantined_count", 0),
        verification.get("tampered_object_count", 0),
    ))
    print("production archive ready: {0}".format(
        "yes" if verification.get("production_archive_ready") else "no"
    ))
    for item in (verification.get("errors") or [])[:8]:
        print("ERROR: {0}".format(item))
    for item in (verification.get("business_blockers") or [])[:8]:
        print("BLOCKED: {0}".format(item))
    print("report: {0}".format(paths.get("evidence_vault_latest_path", "")))
    if require_ready and verification.get("technical_status") != "READY":
        raise SystemExit(3)
    if require_business_ready and verification.get("business_status") != "READY":
        raise SystemExit(4)


def _evidence_vault_archive_review(
    config_path: str,
    decision: str,
    actor: str,
    actor_role: str,
    notes: str,
    evidence_refs: list,
) -> None:
    result = _policy_call(lambda: record_archive_review(
        load_config(config_path),
        decision=decision,
        actor=actor,
        actor_role=actor_role,
        notes=notes,
        evidence_refs=evidence_refs,
    ))
    control = result["vault"].get("archive_control", {})
    print("archive review: {0} -> {1}".format(actor_role, decision))
    print("approved reviews: {0}/{1}".format(
        control.get("approved_review_count", 0), control.get("required_review_count", 0)
    ))
    print("production archive ready: {0}".format("yes" if control.get("ready") else "no"))


def _evidence_vault_archive_activate(config_path: str, actor: str, actor_role: str, notes: str) -> None:
    result = _policy_call(lambda: activate_production_archive(
        load_config(config_path), actor=actor, actor_role=actor_role, notes=notes
    ))
    print("production archive activation: {0}".format(
        "active" if result["vault"]["archive_control"].get("ready") else "blocked"
    ))
    print("policy fingerprint: {0}".format(result["vault"]["archive_control"].get("policy_fingerprint", "")))


def _evidence_vault_archive_revoke(config_path: str, actor: str, actor_role: str, notes: str) -> None:
    result = _policy_call(lambda: revoke_production_archive(
        load_config(config_path), actor=actor, actor_role=actor_role, notes=notes
    ))
    print("production archive activation: revoked")
    print("policy fingerprint: {0}".format(result["vault"]["archive_control"].get("policy_fingerprint", "")))


def _refresh_workpapers_after_evidence(config: dict) -> None:
    if (config.get("research_workpapers") or {}).get("enabled"):
        _policy_call(lambda: sync_research_workpapers(config))


def _print_evidence_capture(result: dict) -> None:
    document = result.get("document", {}) or {}
    print("official evidence: {0}".format(document.get("document_id", "")))
    print("status: {0}; duplicate: {1}".format(
        document.get("status", ""), str(bool(result.get("duplicate"))).lower()
    ))
    print("sha256: {0}".format(document.get("sha256", "")))
    print("source binding: {0}".format(document.get("source_binding_status", "")))
    print("report: {0}".format(result.get("evidence_vault_latest_path", project_path("reports/evidence_vault/latest.md"))))


def _research_workpaper_sync(config_path: str, report_path: str = "") -> None:
    config = load_config(config_path)
    result = sync_research_workpapers(config, report_path=report_path)
    print("research workpapers: {0}".format(result.get("research_workpaper_count", 0)))
    print("created revisions: {0}".format(result.get("research_workpaper_created_count", 0)))
    print("hash-chain integrity: {0}".format(result.get("research_workpaper_integrity_status", "UNKNOWN")))
    print("business status: {0}".format(result.get("research_workpaper_business_status", "BLOCKED")))
    if result.get("research_workpaper_latest_path"):
        print("workpaper report: {0}".format(result.get("research_workpaper_latest_path")))


def _research_workpaper_status(config_path: str, case_id: str = "", write_summary: bool = False) -> None:
    config = load_config(config_path)
    projection = load_research_workpapers(config)
    workpapers = [item for item in projection.get("workpapers", []) if isinstance(item, dict)]
    if case_id:
        workpapers = [item for item in workpapers if str(item.get("case_id") or "") == str(case_id)]
    print("hash-chain integrity: {0}; events={1}".format(
        projection.get("integrity_status", "UNKNOWN"), projection.get("event_count", 0)
    ))
    for item in workpapers:
        validation = item.get("validation", {}) or {}
        print("[{0}/{1}] {2} v{3} validation={4}".format(
            (item.get("case", {}) or {}).get("priority", ""),
            (item.get("workflow", {}) or {}).get("stage", ""),
            item.get("case_id", ""),
            item.get("version", 0),
            validation.get("status", "UNKNOWN"),
        ))
        if validation.get("errors"):
            print("  blockers: {0}".format("; ".join(validation.get("errors", [])[:8])))
    if not workpapers:
        print("No research workpapers.")
    if write_summary:
        paths = write_research_workpaper_reports(projection, config)
        print("workpaper report: {0}".format(paths.get("research_workpaper_latest_path", "")))


def _research_workpaper_verify(
    config_path: str,
    require_ready: bool = False,
    require_business_ready: bool = False,
) -> None:
    config = load_config(config_path)
    result = verify_research_workpapers(config)
    print("technical status: {0}".format(result.get("technical_status", "UNKNOWN")))
    print("business status: {0}".format(result.get("business_status", "BLOCKED")))
    print("hash-chain integrity: {0}".format(result.get("integrity_status", "UNKNOWN")))
    print("workpapers/events: {0}/{1}".format(result.get("workpaper_count", 0), result.get("event_count", 0)))
    for item in result.get("errors", [])[:20]:
        print("ERROR: {0}".format(item))
    for item in result.get("warnings", [])[:20]:
        print("WARN: {0}".format(item))
    if require_ready and result.get("technical_status") != "READY":
        raise SystemExit(2)
    if require_business_ready and result.get("business_status") != "READY":
        raise SystemExit(3)


def _research_ops_check(config_path: str) -> None:
    config = load_config(config_path)
    snapshot = build_research_operations_snapshot(config, write=False)
    roster = snapshot.get("roster", {}) or {}
    summary = snapshot.get("summary", {}) or {}
    print("research operations policy: {0}".format(snapshot.get("policy_version", "")))
    print("roster: principals={0}, human={1}, service={2}, valid={3}".format(
        roster.get("principal_count", 0),
        roster.get("human_count", 0),
        roster.get("service_count", 0),
        roster.get("valid", False),
    ))
    missing = roster.get("missing_mandatory_human_capabilities", []) or []
    print("missing mandatory human capabilities: {0}".format(", ".join(missing) or "none"))
    print("cases: {0}; unstaffed high priority: {1}; overdue: {2}".format(
        summary.get("case_count", 0),
        summary.get("unstaffed_high_priority_count", 0),
        summary.get("overdue_count", 0),
    ))
    if roster.get("errors"):
        print("roster errors: {0}".format("; ".join(roster.get("errors", [])[:10])))
    ready = bool(roster.get("valid")) and not missing
    print("operational readiness: {0}".format("READY" if ready else "BLOCKED"))
    if not ready:
        raise SystemExit(2)


def _research_ops_plan(config_path: str, case_id: str = "", limit: int = 20) -> None:
    config = load_config(config_path)
    snapshot = build_research_operations_snapshot(config, write=True)
    rows = snapshot.get("cases", []) or []
    if case_id:
        rows = [item for item in rows if str(item.get("case_id") or "") == str(case_id)]
    for item in rows[:max(1, int(limit or 20))]:
        print("[{0}/{1}/{2}] {3} stage={4} sla={5} role={6} recommend={7}".format(
            item.get("escalation_level", ""),
            item.get("priority", ""),
            item.get("access_classification", ""),
            item.get("case_id", ""),
            item.get("stage", ""),
            item.get("sla_status", ""),
            item.get("required_role", ""),
            item.get("assignment_recommendation", "") or "none",
        ))
    summary = snapshot.get("summary", {}) or {}
    print("queue: cases={0}, at_risk={1}, overdue={2}, unstaffed={3}".format(
        summary.get("case_count", 0),
        summary.get("at_risk_count", 0),
        summary.get("overdue_count", 0),
        summary.get("unstaffed_count", 0),
    ))
    print("operations report: {0}".format(snapshot.get("research_operations_report_path", "")))
    print("assignment mode: suggest_only; automatic assignments: 0")


def _research_stage(
    entity_type: str,
    entity_id: str,
    title: str,
    thesis: str,
    evidence_refs: list | None = None,
    review_questions: list | None = None,
    actor: str = "agent",
) -> None:
    draft = stage_research_draft(
        entity_type=entity_type,
        entity_id=entity_id,
        title=title,
        thesis=thesis,
        evidence_refs=evidence_refs or [],
        review_questions=review_questions or [],
        actor=actor,
    )
    print("research draft: {0}".format(draft["draft_id"]))
    print("status: {0}".format(draft["status"]))


def _research_commit(draft_id: str, message: str, actor: str) -> None:
    record = commit_research_draft(draft_id, message, actor=actor)
    print("research commit: {0} -> {1}".format(record["draft_id"], record.get("commit_hash", "")))


def _research_approve(draft_id: str, notes: str, actor: str) -> None:
    _research_gate(approve_research_draft(draft_id, notes=notes, actor=actor))


def _research_reject(draft_id: str, notes: str, actor: str) -> None:
    _research_gate(reject_research_draft(draft_id, notes=notes, actor=actor))


def _research_defer(draft_id: str, notes: str, actor: str) -> None:
    _research_gate(defer_research_draft(draft_id, notes=notes, actor=actor))


def _research_gate(record: dict) -> None:
    print("research gate: {0} -> {1}".format(record.get("draft_id", ""), record.get("status", "")))
    print("boundary: {0}".format(record.get("forbidden_actions_note", "")))


def _research_status(status: str = "", write_summary: bool = False) -> None:
    state = project_research_git_state()
    drafts = list((state.get("drafts", {}) or {}).values())
    status = str(status or "").strip()
    if status:
        drafts = [item for item in drafts if str(item.get("status", "")) == status]
    if not drafts:
        print("No research drafts.")
    for item in sorted(drafts, key=lambda value: str(value.get("updated_at", "")), reverse=True):
        print("[{0}] {1} {2}".format(
            item.get("status", ""),
            item.get("draft_id", ""),
            item.get("title", ""),
        ))
    counts = state.get("status_counts", {}) or {}
    if counts:
        print("status_counts: {0}".format(", ".join("{0}={1}".format(k, v) for k, v in sorted(counts.items()))))
    if write_summary:
        paths = write_research_git_summary(state)
        print("research summary: {0}".format(paths["research_git_summary_path"]))
        print("research summary latest: {0}".format(paths["research_git_summary_latest_path"]))


def _research_auto_stage(report_path: str = "", config_path: str = "") -> None:
    config = load_config(config_path) if config_path else None
    stats = stage_llm_research_drafts_from_report(report_path, config)
    print("Research-as-Git auto-stage: {0}".format(stats.get("research_git_auto_staged_count", 0)))
    print("existing skipped: {0}".format(stats.get("research_git_skipped_existing_count", 0)))
    print("filtered skipped: {0}".format(stats.get("research_git_skipped_filtered_count", 0)))
    print("unsafe skipped: {0}".format(stats.get("research_git_skipped_unsafe_count", 0)))
    if stats.get("research_git_source_report_path"):
        print("source report: {0}".format(stats.get("research_git_source_report_path")))
    if stats.get("research_git_summary_latest_path"):
        print("Research-as-Git summary: {0}".format(stats.get("research_git_summary_latest_path")))


def _workspace_sync(report_path: str = "") -> None:
    result = sync_workspace_inbox(report_path)
    counts = result.get("object_counts", {}) or {}
    print("workspace inbox: {0}".format(result.get("workspace_inbox_path", "")))
    print("workspace latest: {0}".format(result.get("workspace_latest_path", "")))
    print("objects: signals={0}, evidence={1}, comments={2}, drafts={3}".format(
        counts.get("signals", 0),
        counts.get("evidence_items", 0),
        counts.get("comments", 0),
        counts.get("research_drafts", 0),
    ))


def _market_cache_seed(config_path: str, symbols: list | None = None) -> None:
    config = load_config(config_path)
    result = seed_quote_cache_from_reports(config, symbols=symbols or None)
    print("seeded_count: {0}".format(result.get("seeded_count", 0)))
    print("seeded_symbols: {0}".format(",".join(result.get("seeded_symbols", []) or [])))
    print("missing_symbols: {0}".format(",".join(result.get("missing_symbols", []) or [])))
    print("cache_path: {0}".format(result.get("cache_path", "")))


def _market_data_governance_status(config_path: str) -> None:
    config = load_config(config_path)
    snapshot = load_latest_market_data_governance(config)
    if not snapshot:
        print("market data governance: configured; no snapshot")
        return
    print("market data governance: {0}".format(snapshot.get("status", "UNKNOWN")))
    print("price/pass/degraded/blocked: {0}/{1}/{2}/{3}".format(
        snapshot.get("price_signal_count", 0), snapshot.get("pass_count", 0),
        snapshot.get("degraded_count", 0), snapshot.get("blocked_count", 0),
    ))
    print("llm_eligible: {0}".format(snapshot.get("llm_eligible_count", 0)))
    approved = snapshot.get("approved_internal_research_providers", []) or []
    print("internally_attested_providers: {0}".format(", ".join(approved) if approved else "none"))
    print("policy_type: internal research operating control; no automatic trade or approval")


def _dashboard(config_path: str) -> None:
    config = load_config(config_path)
    path = generate_dashboard(config)
    print("看板: {0}".format(path))


def _review_workbench(config_path: str, host: str, port: int, status: str, risk: str, task_id: str, once: bool) -> None:
    config = load_config(config_path)
    if once:
        path = write_workbench_snapshot(status=status, task_id=task_id, risk=risk)
        print("review workbench snapshot: {0}".format(path))
        return
    url = "http://{0}:{1}/?status={2}&risk={3}".format(host, port, status, risk)
    print("review workbench: {0}".format(url), flush=True)
    serve_workbench(host=host, port=port, config=config)


def _review_priority(config_path: str) -> None:
    load_config(config_path)
    paths = write_priority_queue(build_priority_queue())
    print("priority queue: {0}".format(paths["priority_queue_path"]))
    print("priority queue latest: {0}".format(paths["priority_queue_latest_path"]))


def _architecture_sync(config_path: str) -> None:
    load_config(config_path)
    result = sync_brokerage_architecture_from_latest_report()
    print("architecture: {0}".format(result["architecture_path"]))
    print("architecture latest: {0}".format(result["architecture_latest_json_path"]))
    print("events written: {0}".format(result["events_written"]))


def _event_sync(config_path: str) -> None:
    load_config(config_path)
    result = sync_brokerage_architecture_from_latest_report()
    print("event sync: {0}".format(result["events_written"]))
    print("duplicate skipped: {0}".format(result.get("duplicate_skipped", 0)))
    print("state: {0}".format(result["architecture_latest_json_path"]))


def _event_state(config_path: str) -> None:
    load_config(config_path)
    state = project_brokerage_state()
    paths = write_architecture_state(state)
    domains = state.get("domains", {}) or {}
    audit = domains.get("audit_back_office", {}) or {}
    print("event state: {0}".format(paths["architecture_latest_json_path"]))
    print("events projected: {0}".format(audit.get("event_count", 0)))
    print("signals: {0}".format((domains.get("research_front_office", {}) or {}).get("signal_count", 0)))
    print("remediation: {0}".format((domains.get("operations_middle_office", {}) or {}).get("remediation_count", 0)))


def _event_log(limit: int = 20) -> None:
    events = load_events()
    limit = max(1, int(limit or 20))
    shown = events[-limit:]
    for event in shown:
        print("{time} | {event_type} | {entity_type}:{entity_id}".format(
            time=event.get("occurred_at", ""),
            event_type=event.get("event_type", ""),
            entity_type=event.get("entity_type", ""),
            entity_id=event.get("entity_id", ""),
        ))
    print("events shown: {0}/{1}".format(len(shown), len(events)))


def _event_compact(config_path: str) -> None:
    load_config(config_path)
    result = compact_event_ledger()
    print("raw_total_events: {0}".format(result["raw_total_events"]))
    print("compact_total_events: {0}".format(result["compact_total_events"]))
    print("duplicate_rows: {0}".format(result["duplicate_rows"]))
    print("compact latest: {0}".format(result["compact_latest_path"]))
    print("report: {0}".format(result["compaction_latest_json_path"]))


def _event_health(config_path: str) -> None:
    load_config(config_path)
    health = compute_event_health()
    print("total_events: {0}".format(health["total_events"]))
    print("unique_events: {0}".format(health["unique_events"]))
    print("duplicate_event_id_count: {0}".format(health["duplicate_event_id_count"]))
    print("duplicate_rows: {0}".format(health["duplicate_rows"]))
    print("duplicate_skipped: {0}".format(health["duplicate_skipped"]))
    print("raw_total_events: {0}".format(health["raw_total_events"]))
    print("raw_duplicate_rows: {0}".format(health["raw_duplicate_rows"]))
    print("compact_total_events: {0}".format(health["compact_total_events"]))
    print("compact_source_raw_total_events: {0}".format(health["compact_source_raw_total_events"]))
    print("compact_stale: {0}".format(health["compact_stale"]))
    print("needs_compaction: {0}".format(health["needs_compaction"]))
    print("projection_source: {0}".format(health["projection_source"]))


def _event_audit(config_path: str, entity_type: str, entity_id: str, limit: int = 50) -> None:
    load_config(config_path)
    result = write_event_audit(entity_type, entity_id, limit)
    entity = "{0}:{1}".format(entity_type or "*", entity_id or "*")
    print("event audit: {0}".format(result["event_audit_latest_json_path"]))
    print("entity: {0}".format(entity))
    print("events: {0}".format(result["event_count"]))
    print("projection_source: {0}".format(result.get("projection_source", "")))


def _alerts(config_path: str) -> None:
    config = load_config(config_path)
    path = generate_notifications(config)
    if path:
        print("通知: {0}".format(path))
    else:
        print("无触发条件，未生成通知。")


def _doctor(config_path: str) -> None:
    path, result = write_doctor_report(config_path)
    print("健康检查: {0}".format(path))
    print("整体状态: {0}".format(result["overall_status"]))
    counts = {"OK": 0, "WARN": 0, "FAIL": 0}
    for c in result["checks"]:
        counts[c["status"]] = counts.get(c["status"], 0) + 1
    print("OK: {0} | WARN: {1} | FAIL: {2}".format(counts["OK"], counts["WARN"], counts["FAIL"]))


def _release_audit(config_path: str, require_ready: bool = False) -> None:
    result = write_release_audit(config_path)
    audit = result["audit"]
    print("Release audit: {0}".format(result["latest_path"]))
    print("Manifest ID: {0}".format(audit.get("manifest_id", "")))
    print("Technical release status: {0}".format(audit.get("status", "BLOCKED")))
    print("Business enablement: {0}".format(
        (audit.get("business_enablement") or {}).get("status", "NOT_ASSERTED")
    ))
    print("Blockers: {0} | Warnings: {1}".format(
        len(audit.get("blockers", []) or []), len(audit.get("warnings", []) or [])
    ))
    if require_ready and not audit.get("technical_release_ready"):
        raise SystemExit(2)


def _release_controls(
    config_path: str,
    fetch_python_metadata: bool = False,
    require_pass: bool = False,
) -> None:
    output = write_release_controls(
        config_path,
        fetch_python_metadata=fetch_python_metadata,
    )
    result = output["result"]
    statuses = result.get("statuses", {}) or {}
    print("Release controls: {0}".format(output["summary_path"]))
    print("Aggregate status: {0}".format(result.get("status", "BLOCKED")))
    print("Secret scan: {0}".format(statuses.get("secret_scan", "INVALID")))
    print("SBOM: {0}".format(statuses.get("sbom", "INVALID")))
    print("License report: {0}".format(statuses.get("license_report", "INVALID")))
    print("Network metadata refresh: {0}".format("yes" if fetch_python_metadata else "no"))
    if require_pass and result.get("status") != "PASS":
        raise SystemExit(2)


def _python_environment_audit(
    python_paths: List[str],
    run_tests: bool = False,
    require_pass: bool = False,
) -> None:
    output = write_python_environment_audit(python_paths or None, run_tests=run_tests)
    audit = output["audit"]
    print("Python environment audit: {0}".format(output["python_environment_path"]))
    print("Status: {0}".format(audit.get("status", "BLOCKED")))
    print("Lock: {0}; packages: {1}".format(
        audit.get("lock_sha256", ""), audit.get("locked_package_count", 0)
    ))
    for item in audit.get("environments", []) or []:
        tests = item.get("test_suite", {}) or {}
        print("Python {0}: {1}; lock={2}; tests={3}; skipped={4}".format(
            item.get("version", "unknown"),
            item.get("status", "BLOCKED"),
            "match" if item.get("lock_match") else "mismatch",
            tests.get("test_count", 0),
            tests.get("skipped_count", 0),
        ))
    if require_pass and audit.get("status") != "PASS":
        raise SystemExit(2)


def _backup_create(config_path: str, require_pass: bool = False) -> None:
    config = load_config(config_path)
    result = create_backup(config, config_path=config_path)
    print("Backup create: {0}".format(result.get("status", "FAIL")))
    print("Bundle: {0}".format(result.get("bundle_path", "not created")))
    print("Manifest ID: {0}".format(result.get("manifest_id", "")))
    print("Files/bytes: {0}/{1}".format(result.get("file_count", 0), result.get("total_bytes", 0)))
    print("Backup/restore status: {0}".format(result.get("status_snapshot_path", "")))
    if result.get("errors"):
        print("Errors: {0}".format("; ".join(str(item) for item in result["errors"][:8])))
    if require_pass and result.get("status") != "PASS":
        raise SystemExit(2)


def _backup_verify(config_path: str, backup_path: str, require_pass: bool = False) -> None:
    config = load_config(config_path)
    result = verify_backup(config, backup_path)
    print("Backup verify: {0}".format(result.get("status", "FAIL")))
    print("Bundle: {0}".format(result.get("bundle_path", "")))
    print("Manifest ID: {0}".format(result.get("manifest_id", "")))
    print("Verified files: {0}/{1}".format(result.get("verified_file_count", 0), result.get("file_count", 0)))
    if result.get("errors"):
        print("Errors: {0}".format("; ".join(str(item) for item in result["errors"][:8])))
    if require_pass and result.get("status") != "PASS":
        raise SystemExit(2)


def _restore_drill(config_path: str, backup_path: str, require_pass: bool = False) -> None:
    config = load_config(config_path)
    result = run_restore_drill(config, backup_path)
    print("Restore drill: {0}".format(result.get("status", "FAIL")))
    print("Drill: {0}".format(result.get("drill_path", "not created")))
    print("Restored root: {0}".format(result.get("restored_root", "")))
    print("RTO seconds: {0}".format((result.get("rto") or {}).get("elapsed_seconds", 0)))
    print("Live source overwrite: {0}".format("yes" if result.get("source_overwrite_performed") else "no"))
    print("Backup/restore status: {0}".format(result.get("status_snapshot_path", "")))
    if result.get("errors"):
        print("Errors: {0}".format("; ".join(str(item) for item in result["errors"][:8])))
    if require_pass and result.get("status") != "PASS":
        raise SystemExit(2)


def _backup_restore_status(
    config_path: str,
    require_ready: bool = False,
    require_production_ready: bool = False,
) -> None:
    config = load_config(config_path)
    output = write_backup_restore_status(config)
    snapshot = output["snapshot"]
    print("Backup/restore report: {0}".format(output["latest_path"]))
    print("Local technical status: {0}".format(snapshot.get("technical_status", "BLOCKED")))
    print("Production DR readiness: {0}".format(snapshot.get("production_readiness", "BLOCKED")))
    print("Technical blockers: {0} | Production blockers: {1}".format(
        len(snapshot.get("technical_blockers", []) or []),
        len(snapshot.get("production_blockers", []) or []),
    ))
    if require_ready and snapshot.get("technical_status") != "PASS":
        raise SystemExit(2)
    if require_production_ready and snapshot.get("production_readiness") != "READY":
        raise SystemExit(2)


def _run_coordination_status(config_path: str, mode: str = "", require_ready: bool = False) -> None:
    config = load_config(config_path)
    output = write_run_coordination_status(config, mode=mode)
    snapshot = output["snapshot"]
    print("Run coordination report: {0}".format(output["latest_path"]))
    print("Technical status: {0}".format(snapshot.get("technical_status", "BLOCKED")))
    print("Production readiness: {0}".format(snapshot.get("production_readiness", "BLOCKED")))
    print("Active leases: {0} | Stale leases: {1} | Incomplete runs: {2}".format(
        len(snapshot.get("active_leases", []) or []),
        len(snapshot.get("stale_leases", []) or []),
        len(snapshot.get("incomplete_runs", []) or []),
    ))
    if snapshot.get("technical_blockers"):
        print("Technical blockers: {0}".format("; ".join(str(item) for item in snapshot["technical_blockers"][:8])))
    if require_ready and snapshot.get("technical_status") != "PASS":
        raise SystemExit(2)


def _run_recover_stale(config_path: str, mode: str, reason: str) -> None:
    config = load_config(config_path)
    try:
        result = recover_stale_lease(config, mode, reason)
    except RunCoordinationError as exc:
        print("Run stale recovery: BLOCKED - {0}".format(exc))
        raise SystemExit(2)
    print("Run stale recovery: {0}".format(result.get("status", "FAIL")))
    print("Mode: {0}".format(result.get("mode", mode)))
    print("Archived lease: {0}".format(result.get("archived_path", "not archived")))
    print("Checkpoint event: {0}".format(result.get("event_id", "")))
    if result.get("status") not in {"RECOVERED", "NOT_FOUND"}:
        raise SystemExit(2)


def _value_validation_status(config_path: str, require_ready: bool = False) -> None:
    config = load_config(config_path)
    output = write_value_validation_status(config)
    _print_value_validation(output)
    if require_ready and not output["snapshot"].get("value_gate_ready"):
        raise SystemExit(2)


def _value_study_init(
    config_path: str, title: str, scenario: str, actor: str, actor_role: str
) -> None:
    config = load_config(config_path)
    _policy_call(lambda: open_value_study(
        config, title=title, scenario=scenario, actor=actor, actor_role=actor_role
    ))
    _print_value_validation(write_value_validation_status(config))


def _value_case_register(
    config_path: str,
    case_id: str,
    case_ref: str,
    case_sha256: str,
    priority: str,
    stratum: str,
    evidence_cutoff: str,
    data_classification: str,
    actor: str,
    actor_role: str,
) -> None:
    config = load_config(config_path)
    _policy_call(lambda: register_value_case(
        config,
        case_id=case_id,
        case_ref=case_ref,
        case_sha256=case_sha256,
        priority=priority,
        stratum=stratum,
        evidence_cutoff=evidence_cutoff,
        data_classification=data_classification,
        actor=actor,
        actor_role=actor_role,
    ))
    _print_value_validation(write_value_validation_status(config))


def _value_case_exclude(
    config_path: str,
    case_id: str,
    reason: str,
    category: str,
    actor: str,
    actor_role: str,
) -> None:
    config = load_config(config_path)
    _policy_call(lambda: exclude_value_case(
        config,
        case_id=case_id,
        reason=reason,
        category=category,
        actor=actor,
        actor_role=actor_role,
    ))
    _print_value_validation(write_value_validation_status(config))


def _value_cohort_freeze(config_path: str, actor: str, actor_role: str) -> None:
    config = load_config(config_path)
    _policy_call(lambda: freeze_value_cohort(config, actor=actor, actor_role=actor_role))
    _print_value_validation(write_value_validation_status(config))


def _value_baseline_record(
    config_path: str,
    case_id: str,
    output_ref: str,
    output_sha256: str,
    metrics_file: str,
    actor: str,
    actor_role: str,
) -> None:
    config = load_config(config_path)
    metrics = _load_evidence_json(metrics_file)
    _policy_call(lambda: record_value_baseline(
        config,
        case_id=case_id,
        output_ref=output_ref,
        output_sha256=output_sha256,
        metrics=metrics,
        actor=actor,
        actor_role=actor_role,
    ))
    _print_value_validation(write_value_validation_status(config))


def _value_treatment_freeze(config_path: str, actor: str, actor_role: str) -> None:
    config = load_config(config_path)
    _policy_call(lambda: freeze_value_treatment(config, actor=actor, actor_role=actor_role))
    _print_value_validation(write_value_validation_status(config))


def _value_assisted_record(
    config_path: str,
    case_id: str,
    output_ref: str,
    output_sha256: str,
    metrics_file: str,
    observed_manifest_id: str,
    outcome_hidden: bool,
    evidence_cutoff_respected: bool,
    unauthorized_egress_count: int,
    actor: str,
    actor_role: str,
) -> None:
    config = load_config(config_path)
    metrics = _load_evidence_json(metrics_file)
    _policy_call(lambda: record_value_assisted(
        config,
        case_id=case_id,
        output_ref=output_ref,
        output_sha256=output_sha256,
        metrics=metrics,
        observed_manifest_id=observed_manifest_id,
        outcome_hidden=outcome_hidden,
        evidence_cutoff_respected=evidence_cutoff_respected,
        unauthorized_egress_count=unauthorized_egress_count,
        actor=actor,
        actor_role=actor_role,
    ))
    _print_value_validation(write_value_validation_status(config))


def _value_blind_prepare(
    config_path: str, case_id: str, actor: str, actor_role: str
) -> None:
    config = load_config(config_path)
    result = _policy_call(lambda: prepare_blind_assignment(
        config, case_id=case_id, actor=actor, actor_role=actor_role
    ))
    print("Blind assignment: {0}".format(result["assignment_id"]))
    print("Reviewer package: {0}".format(result["package_path"]))
    print("Sealed mapping (do not give to reviewer): {0}".format(result["sealed_mapping_path"]))
    _print_value_validation(write_value_validation_status(config))


def _value_blind_evaluate(
    config_path: str,
    assignment_id: str,
    scores_a_file: str,
    scores_b_file: str,
    blinding_attested: bool,
    no_ledger_access_attested: bool,
    notes: str,
    actor: str,
    actor_role: str,
) -> None:
    config = load_config(config_path)
    scores_a = _load_evidence_json(scores_a_file)
    scores_b = _load_evidence_json(scores_b_file)
    _policy_call(lambda: record_blind_evaluation(
        config,
        assignment_id=assignment_id,
        scores_a=scores_a,
        scores_b=scores_b,
        blinding_attested=blinding_attested,
        no_ledger_access_attested=no_ledger_access_attested,
        notes=notes,
        actor=actor,
        actor_role=actor_role,
    ))
    _print_value_validation(write_value_validation_status(config))


def _value_blind_reveal(
    config_path: str, assignment_id: str, actor: str, actor_role: str
) -> None:
    config = load_config(config_path)
    _policy_call(lambda: reveal_blind_assignment(
        config, assignment_id=assignment_id, actor=actor, actor_role=actor_role
    ))
    _print_value_validation(write_value_validation_status(config))


def _value_gate_decision(
    config_path: str,
    decision: str,
    notes: str,
    evidence_ref: str,
    decision_deadline: str,
    actor: str,
    actor_role: str,
) -> None:
    config = load_config(config_path)
    _policy_call(lambda: record_value_gate_decision(
        config,
        decision=decision,
        notes=notes,
        evidence_ref=evidence_ref,
        decision_deadline=decision_deadline,
        actor=actor,
        actor_role=actor_role,
    ))
    _print_value_validation(write_value_validation_status(config))


def _print_value_validation(output: dict) -> None:
    snapshot = output["snapshot"]
    aggregate = snapshot.get("aggregate", {}) or {}
    print("Value validation: {0}".format(output["latest_path"]))
    print("Operating status: {0}".format(snapshot.get("operating_status", "UNKNOWN")))
    print("Recommendation: {0}; human decision: {1}".format(
        snapshot.get("recommended_decision", "INSUFFICIENT_EVIDENCE"),
        (snapshot.get("decision") or {}).get("decision", "NOT_RECORDED"),
    ))
    print("Cases: cohort={0}; qualifying={1}; integrity={2}".format(
        aggregate.get("cohort_case_count", 0),
        aggregate.get("qualifying_case_count", 0),
        snapshot.get("integrity_status", "FAIL"),
    ))


def _model_ops_status(config_path: str) -> None:
    config = load_config(config_path)
    cfg = model_operations_config(config)
    if not cfg["enabled"]:
        print("model operations: disabled")
        return
    snapshot = load_latest_model_operations(config)
    if not snapshot:
        print("model operations: configured; no runtime snapshot")
        return
    quality = snapshot.get("quality", {}) or {}
    grounding = quality.get("grounding", {}) or {}
    usage = snapshot.get("usage", {}) or {}
    cost = snapshot.get("estimated_cost", {}) or {}
    print("model operations: {0}".format(snapshot.get("status", "UNKNOWN")))
    print("run_id: {0}; model_ops_version: {1}".format(snapshot.get("run_id", ""), snapshot.get("version", "")))
    print("main: {0}/{1}; roles: {2}/{3}; usable_grounding: {4:.1%}".format(
        quality.get("main_success", 0), quality.get("main_selected", 0),
        quality.get("role_success", 0), quality.get("role_selected", 0),
        float(grounding.get("usable_rate", 1)),
    ))
    print("tokens: prompt={0}; completion={1}; reasoning={2}; total={3}".format(
        usage.get("prompt_tokens", 0), usage.get("completion_tokens", 0),
        usage.get("reasoning_tokens", 0), usage.get("total_tokens", 0),
    ))
    print("estimated_cost: {0} {1}; source_effective_date={2}".format(
        cost.get("amount", 0), cost.get("currency", ""), cost.get("effective_date", ""),
    ))
    print("report: {0}".format(project_path(str(Path(cfg["report_dir"]) / "latest.md"))))


def _production_pilot_sync(config_path: str, summary_path: str = "") -> None:
    config = load_config(config_path)
    snapshot = _policy_call(lambda: sync_production_pilot(
        config, config_path=config_path, scheduled_summary_path=str(summary_path or "").strip()))
    _print_production_pilot(snapshot)


def _openclaw_handoff(config_path: str, report_path: str = "") -> None:
    config = load_config(config_path)
    payload = _policy_call(lambda: write_openclaw_handoff(config, report_path))
    errors = validate_openclaw_handoff(payload)
    if errors:
        raise SystemExit("OpenClaw handoff validation failed: {0}".format("; ".join(errors)))
    print("OpenClaw public handoff: {0}".format(project_path("reports/handoff/latest.json")))
    print("public eligible signals: {0}".format(payload.get("public_signal_count", 0)))
    print("external LLM call required: {0}".format(
        "yes" if payload.get("external_llm_call_required") else "no"
    ))
    print("decision reason: {0}".format(payload.get("call_decision_reason", "")))


def _pi_runtime_status(config_path: str, require_ready: bool = False) -> None:
    status = write_pi_runtime_status(load_config(config_path))
    print("Pi runtime: {0}".format("READY" if status.get("runtime_ready") else "BLOCKED"))
    print("package: {0}@{1}".format(status.get("expected_package", ""), status.get("installed_version", "")))
    print("isolated settings: {0}".format("PASS" if status.get("settings_safe") else "FAIL"))
    print("API key present: {0}".format("yes" if status.get("api_key_present") else "no"))
    print("report: {0}".format(project_path("reports/pi_agent/runtime/latest.md")))
    if require_ready and not status.get("runtime_ready"):
        raise SystemExit(3)


def _pi_runtime_uat(config_path: str, live: bool = False) -> None:
    if not live:
        raise SystemExit("Refusing live Pi UAT without --live; the command makes one billable synthetic model call.")
    result = _policy_call(lambda: run_pi_runtime_uat(load_config(config_path)))
    print("Pi runtime UAT: {0}".format(result.get("status", "FAIL")))
    print("live model call: {0}".format("yes" if result.get("live_model_call") else "no"))
    print("runtime/model: {0}/{1}".format(result.get("runtime", ""), result.get("model", "")))
    print("tools/sessions/project resources: disabled/disabled/disabled")
    print("report: {0}".format(project_path("reports/pi_agent/uat/latest.json")))


def _pi_skills_status(config_path: str, require_ready: bool = False) -> None:
    status = write_pi_skill_supply_chain_status(load_config(config_path))
    print("Pi reviewed methods: {0}".format(status.get("status", "BLOCKED")))
    print("approved/adapted: {0}/{1}".format(
        status.get("approved_skill_count", 0), status.get("adapted_skill_count", 0)
    ))
    print("dynamic skills/tools: disabled/disabled")
    print("bundle SHA-256: {0}".format(status.get("bundle_sha256", "")))
    print("report: {0}".format(project_path("reports/pi_agent/skills/latest.md")))
    if require_ready and status.get("status") != "READY":
        raise SystemExit(3)


def _pi_skills_lock(config_path: str, write: bool = False) -> None:
    if not write:
        raise SystemExit("Refusing to change the Pi method lock without --write; review registry and skill diffs first.")
    config = load_config(config_path)
    lock = write_pi_skill_lock(config)
    status = write_pi_skill_supply_chain_status(config)
    if status.get("status") != "READY":
        raise SystemExit("Pi method lock was written but validation remains blocked: {0}".format(
            "; ".join(status.get("errors", []))
        ))
    print("Pi reviewed method lock written: {0}".format(project_path(
        (config.get("pi_agent", {}) or {}).get("skill_lock", "config/pi_skills.lock.json")
    )))
    print("methods: {0}".format(len(lock.get("skills", []))))
    print("bundle SHA-256: {0}".format(lock.get("bundle_sha256", "")))


def _pi_skills_uat(config_path: str, live: bool = False) -> None:
    if not live:
        raise SystemExit("Refusing live Pi method UAT without --live; the command makes one billable synthetic model call.")
    result = _policy_call(lambda: run_pi_skills_uat(load_config(config_path)))
    print("Pi reviewed method UAT: {0}".format(result.get("status", "FAIL")))
    print("live model call: {0}".format("yes" if result.get("live_model_call") else "no"))
    print("runtime/model: {0}/{1}".format(result.get("runtime", ""), result.get("model", "")))
    print("compiled methods: {0}".format(", ".join(result.get("compiled_skill_ids", []))))
    print("dynamic skills/tools/sessions: disabled/disabled/disabled")
    print("report: {0}".format(project_path("reports/pi_agent/skills_uat/latest.json")))


def _agent_handoff(config_path: str, report_path: str = "", check_only: bool = False) -> None:
    if check_only:
        status = check_latest_agent_handoff()
        print("Pi handoff chain: {0}".format(status.get("status", "BLOCKED")))
        print("public eligible signals: {0}".format(status.get("public_signal_count", 0)))
        print("Pi called/model calls: {0}/{1}".format(
            "yes" if status.get("called") else "no", status.get("model_call_count", 0),
        ))
        print("handoff: {0}".format(status.get("handoff_path", "")))
        if status.get("errors"):
            raise SystemExit("Pi handoff chain validation failed: {0}".format("; ".join(status["errors"])))
        return
    payload = _policy_call(lambda: write_agent_handoff(load_config(config_path), report_path))
    errors = validate_agent_handoff(payload)
    if errors:
        raise SystemExit("Pi handoff validation failed: {0}".format("; ".join(errors)))
    print("Pi public handoff: {0}".format(project_path("reports/agent_handoff/latest.json")))
    print("public eligible signals: {0}".format(payload.get("public_signal_count", 0)))
    print("Pi call required: {0}".format("yes" if payload.get("external_llm_call_required") else "no"))
    print("decision reason: {0}".format(payload.get("call_decision_reason", "")))


def _pi_handoff_run(config_path: str, report_path: str = "") -> None:
    result = _policy_call(lambda: run_pi_public_handoff(load_config(config_path), report_path))
    print("Pi handoff result: {0}".format(project_path("reports/pi_agent/public_handoff/latest.json")))
    print("public eligible signals: {0}".format(result.get("public_signal_count", 0)))
    print("Pi called: {0}".format("yes" if result.get("called") else "no"))
    print("model calls: {0}".format(result.get("model_call_count", 0)))
    print("skip reason: {0}".format(result.get("skip_reason", "")))


def _production_pilot_status(config_path: str, require_go: bool = False) -> None:
    config = load_config(config_path)
    snapshot = load_latest_production_pilot(config)
    if not snapshot:
        snapshot = _policy_call(lambda: sync_production_pilot(config, config_path=config_path, record_run=False))
    _print_production_pilot(snapshot)
    if require_go and snapshot.get("go_live_decision") != "GO":
        raise SystemExit(3)


def _print_production_pilot(snapshot: dict) -> None:
    print("production pilot: {0}".format(snapshot.get("go_live_decision", "UNKNOWN")))
    print("technical readiness: {0}".format(snapshot.get("technical_readiness", "UNKNOWN")))
    print("candidate readiness: {0}".format(snapshot.get("candidate_readiness", "UNKNOWN")))
    print("configured/effective mode: {0}/{1}".format(
        snapshot.get("configured_mode", ""), snapshot.get("effective_mode", "")
    ))
    print("candidate fingerprint: {0}".format(snapshot.get("candidate_fingerprint", "")))
    print("blockers: {0}".format(len(snapshot.get("go_live_blockers", []) or [])))
    for item in (snapshot.get("go_live_blockers", []) or [])[:12]:
        print("- {0}".format(item))
    print("report: {0}".format(snapshot.get("production_pilot_latest_path") or project_path("reports/production_pilot/latest.md")))


def _control_evidence_register(
    config_path: str,
    file_path: str,
    category: str,
    title: str,
    subject_id: str,
    owner_role: str,
    external_archive_ref: str,
    actor: str,
    actor_role: str,
    valid_from: str,
    valid_to: str,
    notes: str,
) -> None:
    config = load_config(config_path)
    source = project_path(file_path) if not Path(file_path).is_absolute() else Path(file_path)
    result = _policy_call(lambda: register_control_evidence(
        config,
        file_path=str(source),
        category=category,
        title=title,
        subject_id=subject_id,
        owner_role=owner_role,
        external_archive_ref=external_archive_ref,
        actor=actor,
        actor_role=actor_role,
        valid_from=valid_from,
        valid_to=valid_to,
        notes=notes,
    ))
    record = result["record"]
    print("control evidence registered: {0}".format(record.get("evidence_id", "")))
    print("sha256: {0}".format((record.get("payload") or {}).get("sha256", "")))
    print("raw file copied to project: false")
    print("approval granted: false")


def _control_evidence_review(
    config_path: str,
    evidence_id: str,
    decision: str,
    actor: str,
    actor_role: str,
    notes: str,
    review_record_ref: str,
) -> None:
    result = _policy_call(lambda: review_control_evidence(
        load_config(config_path),
        evidence_id=evidence_id,
        decision=decision,
        actor=actor,
        actor_role=actor_role,
        notes=notes,
        review_record_ref=review_record_ref,
    ))
    item = (result["registry"].get("evidence") or {}).get(evidence_id, {})
    print("control evidence review: {0} -> {1}".format(actor_role, decision))
    print("evidence status: {0}".format(item.get("status", "unknown")))
    print("automatic approval: false")


def _control_evidence_revoke(
    config_path: str, evidence_id: str, actor: str, actor_role: str, notes: str
) -> None:
    result = _policy_call(lambda: revoke_control_evidence(
        load_config(config_path), evidence_id=evidence_id, actor=actor, actor_role=actor_role, notes=notes
    ))
    item = (result["registry"].get("evidence") or {}).get(evidence_id, {})
    print("control evidence revoked: {0}".format(evidence_id))
    print("evidence status: {0}".format(item.get("status", "unknown")))


def _control_evidence_status(config_path: str, require_ready: bool = False) -> None:
    registry = load_control_evidence_registry(load_config(config_path))
    write_control_evidence_snapshot(registry)
    print("control evidence technical: {0}".format(registry.get("technical_status", "UNKNOWN")))
    print("integrity: {0}".format(registry.get("integrity_status", "UNKNOWN")))
    print("evidence/verified: {0}/{1}".format(
        registry.get("evidence_count", 0), len(registry.get("verified_evidence_ids", []) or [])
    ))
    print("raw files stored in project: false")
    print("report: {0}".format(project_path("reports/business_onboarding/control_evidence_latest.md")))
    if require_ready and registry.get("technical_status") != "READY":
        raise SystemExit(3)


def _business_onboarding_pack(config_path: str) -> None:
    result = generate_business_onboarding_pack(load_config(config_path))
    print("business onboarding pack: {0}".format(result.get("pack_dir", "")))
    print("files: {0}".format(len(result.get("files", []) or [])))
    print("production state changed: false")


def _business_onboarding_sync(config_path: str) -> None:
    snapshot = sync_business_onboarding(load_config(config_path))
    _print_business_onboarding(snapshot)


def _business_onboarding_status(config_path: str, require_ready: bool = False) -> None:
    config = load_config(config_path)
    snapshot = load_latest_business_onboarding(config)
    if not snapshot:
        snapshot = sync_business_onboarding(config)
    _print_business_onboarding(snapshot)
    if require_ready and (
        snapshot.get("technical_status") != "READY" or snapshot.get("business_status") != "READY"
    ):
        raise SystemExit(3)


def _print_business_onboarding(snapshot: dict) -> None:
    print("business onboarding technical: {0}".format(snapshot.get("technical_status", "UNKNOWN")))
    print("business onboarding business: {0}".format(snapshot.get("business_status", "BLOCKED")))
    print("current phase: {0}".format(snapshot.get("current_phase", "")))
    print("completed phases: {0}/{1}".format(
        snapshot.get("completed_phase_count", 0), snapshot.get("phase_count", 0)
    ))
    print("ordered actions: {0}".format(snapshot.get("action_count", 0)))
    for action in (snapshot.get("action_queue") or [])[:8]:
        print("- {0}: {1}".format(action.get("owner_role", ""), action.get("action", "")))
    print("automatic approval/activation/publication/trading: false/false/false/false")
    print("report: {0}".format(snapshot.get("business_onboarding_path") or project_path("reports/business_onboarding/latest.md")))


def _provider_init(
    config_path: str,
    provider_id: str,
    legal_name: str,
    scopes: list,
    actor: str,
    notes: str,
) -> None:
    config = load_config(config_path)
    result = _policy_call(lambda: initialize_provider_candidate(
        config, provider_id, legal_name, scopes, actor=actor, notes=notes,
    ))
    print("provider candidate created: {0}".format(result["record"].get("provider_id", "")))
    print("approval granted: false")
    print("registry: {0}".format(project_path("reports/production_pilot/provider_registry_latest.md")))


def _provider_due_diligence(
    config_path: str,
    provider_id: str,
    actor: str,
    actor_role: str,
    evidence_file: str,
    notes: str,
) -> None:
    config = load_config(config_path)
    evidence = _load_evidence_json(evidence_file)
    result = _policy_call(lambda: submit_provider_due_diligence(
        config, provider_id, actor, actor_role, evidence, notes=notes,
    ))
    print("provider due diligence submitted: {0}".format(result["record"].get("provider_id", "")))
    print("independent reviews still required")


def _provider_review(
    config_path: str,
    provider_id: str,
    decision: str,
    actor: str,
    actor_role: str,
    notes: str,
    evidence_refs: list,
) -> None:
    config = load_config(config_path)
    result = _policy_call(lambda: record_provider_review(
        config, provider_id, decision, actor, actor_role, notes=notes, evidence_refs=evidence_refs,
    ))
    print("provider review recorded: {0} {1} by {2}".format(provider_id, decision, actor_role))
    print("event: {0}".format(result["record"].get("event_id", "")))


def _provider_activate(config_path: str, provider_id: str, actor: str, actor_role: str, notes: str) -> None:
    config = load_config(config_path)
    result = _policy_call(lambda: activate_provider(config, provider_id, actor, actor_role, notes=notes))
    print("provider activated for approved scopes: {0}".format(provider_id))
    print("event: {0}".format(result["record"].get("event_id", "")))


def _provider_suspend(config_path: str, provider_id: str, actor: str, actor_role: str, notes: str) -> None:
    config = load_config(config_path)
    result = _policy_call(lambda: suspend_provider(config, provider_id, actor, actor_role, notes=notes))
    print("provider suspended: {0}".format(provider_id))
    print("event: {0}".format(result["record"].get("event_id", "")))


def _production_pilot_approval(
    config_path: str,
    gate: str,
    decision: str,
    actor: str,
    actor_role: str,
    notes: str,
    evidence_refs: list,
) -> None:
    config = load_config(config_path)
    result = _policy_call(lambda: record_pilot_approval(
        config, gate, decision, actor, actor_role, notes=notes,
        evidence_refs=evidence_refs, config_path=config_path,
    ))
    print("pilot approval recorded: {0} {1}".format(gate, decision))
    print("candidate fingerprint: {0}".format(result["record"].get("candidate_fingerprint", "")))


def _production_pilot_activate(
    config_path: str,
    actor: str,
    actor_role: str,
    notes: str,
    evidence_refs: list,
) -> None:
    config = load_config(config_path)
    result = _policy_call(lambda: activate_production_candidate(
        config, actor, actor_role, notes=notes, evidence_refs=evidence_refs, config_path=config_path,
    ))
    print("production activation recorded: {0}".format(result["pilot"].get("go_live_decision", "UNKNOWN")))
    print("scope: internal research operations only")


def _production_pilot_revoke(config_path: str, actor: str, actor_role: str, notes: str) -> None:
    config = load_config(config_path)
    result = _policy_call(lambda: revoke_production_activation(
        config, actor, actor_role, notes=notes, config_path=config_path,
    ))
    print("production activation revoked; effective mode: {0}".format(result["pilot"].get("effective_mode", "shadow")))


def _production_pilot_incident(
    config_path: str,
    title: str,
    severity: str,
    category: str,
    actor: str,
    actor_role: str,
    notes: str,
    evidence_refs: list,
) -> None:
    config = load_config(config_path)
    result = _policy_call(lambda: record_pilot_incident(
        config, title, severity, category, actor, actor_role, notes=notes, evidence_refs=evidence_refs,
    ))
    print("incident recorded: {0}".format(result["record"].get("incident_id", "")))
    print("severity: {0}; automatic operational action: false".format(severity))


def _production_pilot_incident_action(
    config_path: str,
    incident_id: str,
    action: str,
    actor: str,
    actor_role: str,
    notes: str,
    evidence_refs: list,
) -> None:
    config = load_config(config_path)
    result = _policy_call(lambda: record_pilot_incident_action(
        config, incident_id, action, actor, actor_role, notes=notes, evidence_refs=evidence_refs,
    ))
    status = (result["incidents"].get("incidents") or {}).get(incident_id, {}).get("status", "unknown")
    print("incident action recorded: {0}; status={1}".format(action, status))


def _load_evidence_json(path_value: str) -> dict:
    path = project_path(path_value)
    try:
        value = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError) as exc:
        print("Policy denied: evidence file is unreadable ({0})".format(type(exc).__name__), file=sys.stderr)
        raise SystemExit(2)
    if not isinstance(value, dict):
        print("Policy denied: evidence file must contain one JSON object", file=sys.stderr)
        raise SystemExit(2)
    return value


def _policy_call(call):
    try:
        return call()
    except (ValueError, TimeoutError) as exc:
        print("Policy denied: {0}".format(exc), file=sys.stderr)
        raise SystemExit(2)


def _llm_governance_status(config_path: str) -> None:
    config = load_config(config_path)
    errors = validate_governance_policy(config)
    if errors:
        print("LLM governance policy: FAIL")
        for item in errors:
            print("- {0}".format(item))
        raise SystemExit(2)
    snapshot = load_latest_governance_summary(config)
    if not snapshot:
        print("LLM governance policy: configured; no runtime snapshot")
        return
    summary = snapshot.get("summary", {}) or {}
    print("LLM governance policy: {0}".format(snapshot.get("policy_version", "")))
    print("status: {0}".format(snapshot.get("status", "")))
    print("enforced: {0}".format(snapshot.get("enforced", False)))
    print("external classifications: {0}".format(",".join(snapshot.get("allowed_external_classifications", []) or [])))
    print("approved: {0}; denied: {1}; output_blocked: {2}; pending_exceptions: {3}".format(
        summary.get("approved_count", 0),
        summary.get("denied_count", 0),
        summary.get("output_blocked_count", 0),
        summary.get("pending_exception_count", 0),
    ))
    queue = load_governance_exception_queue(config)
    print("historical exceptions: {0}; resolved: {1}".format(
        queue.get("exception_count", 0),
        queue.get("resolved_count", 0),
    ))
    for item in [row for row in queue.get("exceptions", []) if not row.get("decision")][:10]:
        print("- pending {0}: component={1}; classification={2}; reasons={3}".format(
            item.get("exception_id", ""),
            item.get("component", ""),
            item.get("classification", ""),
            ",".join(item.get("reasons", []) or []),
        ))
    print("automatic exception approval: disabled")


def _llm_governance_review(
    config_path: str,
    exception_id: str,
    disposition: str,
    actor: str,
    actor_role: str,
    notes: str,
    evidence_refs: List[str],
) -> None:
    config = load_config(config_path)
    try:
        record = record_governance_exception_decision(
            exception_id=exception_id,
            disposition=disposition,
            actor=actor,
            actor_role=actor_role,
            notes=notes,
            evidence_refs=evidence_refs,
            config=config,
        )
    except (PermissionError, ValueError) as exc:
        print("LLM governance review rejected: {0}".format(exc))
        raise SystemExit(2)
    queue = load_governance_exception_queue(config)
    print("LLM governance exception recorded: {0}".format(record.get("decision_id", "")))
    print("status: {0}; disposition: {1}".format(record.get("status", ""), record.get("disposition", "")))
    print("pending exceptions: {0}".format(queue.get("pending_count", 0)))
    print("automatic re-ingest: disabled; external egress authorization: disabled")


if __name__ == "__main__":
    main()
