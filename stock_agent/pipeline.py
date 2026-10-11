from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from .analysis import analyze_signals
from .audit_log import atomic_write_text
from .brokerage_architecture import sync_brokerage_architecture_from_latest_report
from .config import ensure_directories, project_path, project_path_override
from .decisions import apply_decision_context, compute_decision_summary, load_decision_context, write_decision_summary
from .evidence_chain import attach_evidence_chains
from .evidence_vault import attach_evidence_vault_status
from .llm_agent import enrich_signals
from .llm_governance import write_llm_governance_summary
from .market_data_governance import assess_market_signals, write_market_data_governance_summary
from .market_resilience import collect_market_quotes_with_resilience, diagnose_network_error, network_repair_guide
from .models import PipelineResult, RawSignal, now_iso
from .reports import write_outputs
from .review_queue import build_review_queue, write_review_queue_snapshot
from .research_framework import apply_research_framework
from .research_cases import update_research_cases
from .research_git import stage_llm_research_drafts
from .model_operations import write_model_operations_summary
from .research_runtime import sync_research_runtime
from .research_workpapers import sync_research_workpapers
from .research_team import run_research_team
from .role_analyst import enrich_signals_with_role_analysts
from .run_ops import RunOpsContext, write_run_summary
from .run_coordination import (
    begin_coordinated_run,
    checkpoint_output_refs,
    checkpoint_stage,
    write_run_coordination_status,
)
from .run_manifest import run_manifest_config, write_run_manifest
from .sources import collect_akshare, collect_alltick, collect_eastmoney, collect_eastmoney_filings, collect_filing_jsonl, collect_free_quote, collect_local_jsonl, collect_pysnowball, collect_rss
from .source_health import attach_source_quality, build_source_health_summary, public_source_health_summary, write_source_health_summary
from .source_remediation import build_remediation_summary, split_source_degraded_signals, write_remediation_summary
from .storage import SignalStore
from .team_ops import build_team_ops_snapshot, write_team_ops_artifacts
from .workspace_inbox import sync_workspace_inbox

_LAST_SOURCE_HEALTH: Optional[Dict[str, Any]] = None
_LAST_MARKET_RESILIENCE: Optional[Dict[str, Any]] = None
_LAST_MARKET_DATA_GOVERNANCE: Optional[Dict[str, Any]] = None


def run_pipeline(config: Dict[str, Any]) -> PipelineResult:
    """Run the pipeline with every stage and writer routed through the caller's
    active project-path resolver.

    The override is installed only for the body of this call and always
    restored, so an error can never leak a temporary resolver into unrelated
    code. When no resolver is active the override is a no-op and production
    behavior is unchanged.
    """
    with project_path_override(project_path):
        return _run_pipeline_body(config)


def _run_with_coordination(
    config: Dict[str, Any],
    mode: str,
    body: Callable[[Dict[str, Any]], PipelineResult],
) -> PipelineResult:
    """Run one pipeline body under an optional durable lease and ledger."""

    coordinator = begin_coordinated_run(config, mode)
    if coordinator is None:
        return body(config)

    runtime_config = dict(config)
    runtime_config["_run_id"] = coordinator.run_id
    runtime_config["_run_coordination"] = coordinator
    try:
        result = body(runtime_config)
        # Finalize the coordination fragment inside the JSON artifact before
        # hashing it, so the completed ledger points at the final bytes rather
        # than the provisional RUNNING snapshot written by the body.
        final_snapshot = coordinator.summary()
        final_snapshot["status"] = "COMPLETED"
        _finalize_coordination_json(result, final_snapshot)
        output_refs = checkpoint_output_refs(result)
        coordinator.complete(output_refs)
        # Release before producing the read-only status projection so a
        # completed run does not appear to hold an active lease.
        coordinator.release()
        try:
            status_output = write_run_coordination_status(config, mode=mode)
        except Exception as exc:
            status_output = {"latest_path": "", "latest_report_path": "", "error": str(exc)}
        result.stats["run_coordination"] = coordinator.summary()
        result.stats["run_coordination_status_path"] = status_output.get("latest_path", "")
        result.stats["run_coordination_report_path"] = status_output.get("latest_report_path", "")
        if status_output.get("error"):
            result.stats["run_coordination_status_error"] = status_output["error"]
        # Record the exact per-run manifest only after the ledger finalized the
        # run: build_run_manifest requires a durably COMPLETED coordination
        # state. Failures are surfaced, never swallowed, and never flip the
        # ledger status already recorded above.
        manifest_stats = _record_run_manifest(runtime_config, result, mode=mode)
        if manifest_stats:
            result.stats["run_manifest"] = manifest_stats
        return result
    except BaseException as exc:
        try:
            coordinator.fail(exc)
        except Exception:
            # Preserve the original pipeline exception.  The status command
            # will expose a broken ledger if failure recording was impossible.
            pass
        try:
            coordinator.release()
        except Exception:
            pass
        try:
            write_run_coordination_status(config, mode=mode)
        except Exception:
            pass
        raise


def _coordination_checkpoint(config: Dict[str, Any], stage: str, metadata: Dict[str, Any]) -> None:
    """Record scalar stage metadata; the coordination layer stores no content."""

    checkpoint_stage(config, stage, metadata)


def _coordination_stats(config: Dict[str, Any]) -> Dict[str, Any]:
    coordinator = config.get("_run_coordination") if isinstance(config, dict) else None
    if coordinator is None:
        return {}
    return coordinator.summary()


def _finalize_coordination_json(result: PipelineResult, snapshot: Dict[str, Any]) -> None:
    """Patch only the generated JSON artifact with the terminal run state."""

    raw_path = getattr(result, "json_path", "") or ""
    if not raw_path:
        return
    path = Path(str(raw_path))
    if not path.is_absolute():
        path = project_path(str(path))
    if not path.is_file():
        return
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return
    if not isinstance(payload, dict):
        return
    stats = payload.get("stats")
    if not isinstance(stats, dict):
        stats = {}
        payload["stats"] = stats
    stats["run_coordination"] = snapshot
    payload["run_coordination"] = snapshot
    atomic_write_text(path, json.dumps(payload, ensure_ascii=False, indent=2, default=str))


def _run_pipeline_body(config: Dict[str, Any]) -> PipelineResult:
    return _run_with_coordination(config, "run", _run_pipeline_body_uncoordinated)


def _run_pipeline_body_uncoordinated(config: Dict[str, Any]) -> PipelineResult:
    ensure_directories()
    isolated_run = _is_isolated_run(config)
    ops = RunOpsContext(config, mode="run", writer=config.get("_progress_writer"))
    with ops.stage("collect"):
        raw_signals, source_health = _collect_for_pipeline(config)
    _coordination_checkpoint(config, "collect", {
        "raw_count": len(raw_signals),
        "source_health_count": len(source_health or {}),
    })
    with ops.stage("rule_analysis"):
        attach_source_quality(raw_signals, source_health)
        analyzed = analyze_signals(raw_signals, config)
        analyzed = apply_decision_context(analyzed, {} if isolated_run else load_decision_context())
        analyzed, research_stats = apply_research_framework(analyzed, config)
        team_base_stats = {
            "raw": len(raw_signals),
            "analyzed": len(analyzed),
            "source_errors": len([signal for signal in analyzed if signal.source_type == "source_error"]),
        }
        analyzed, team_output, team_stats = run_research_team(analyzed, config, team_base_stats)
    _coordination_checkpoint(config, "rule_analysis", {
        "raw_count": len(raw_signals),
        "analyzed_count": len(analyzed),
        "source_error_count": len([signal for signal in analyzed if signal.source_type == "source_error"]),
    })
    with ops.stage("official_evidence_vault"):
        analyzed, official_evidence_stats = attach_evidence_vault_status(
            analyzed,
            config,
            allow_auto_capture=not isolated_run,
        )
    _coordination_checkpoint(config, "official_evidence_vault", {
        "signal_count": len(analyzed),
        "stats_keys": sorted(str(key) for key in official_evidence_stats.keys()),
    })
    with ops.stage("storage"):
        store = SignalStore(project_path(config.get("storage", {}).get("sqlite_path", "data/stock_agent.sqlite3")))
        try:
            inserted, duplicates = store.upsert_many(analyzed)
        finally:
            store.close()
    _coordination_checkpoint(config, "storage", {
        "inserted_count": len(inserted),
        "duplicate_count": int(duplicates or 0),
    })

    stats = {
        "raw": len(raw_signals),
        "analyzed": len(analyzed),
        "inserted": len(inserted),
        "duplicates": duplicates,
        "source_errors": len([signal for signal in analyzed if signal.source_type == "source_error"]),
        "example_data_signals": len(_example_data_signals(analyzed)),
        "disabled_sources": _disabled_source_names(config),
    }
    if stats["example_data_signals"]:
        stats["example_data_warning"] = (
            "Example/sample input contributed signals in this run; they are labeled "
            "payload.data_provenance=example and must not be counted as real production samples."
        )
    if _LAST_MARKET_RESILIENCE:
        stats["market_resilience"] = _LAST_MARKET_RESILIENCE
    if _LAST_MARKET_DATA_GOVERNANCE:
        stats["market_data_governance"] = _LAST_MARKET_DATA_GOVERNANCE
    stats.update(official_evidence_stats)
    with ops.stage("source_health"):
        source_health = _finalize_source_health(source_health, analyzed, inserted, config)
        if not isolated_run:
            stats.update(write_source_health_summary(source_health))
            if _LAST_MARKET_DATA_GOVERNANCE:
                stats.update(write_market_data_governance_summary(_LAST_MARKET_DATA_GOVERNANCE, config))
        stats["source_health"] = public_source_health_summary(source_health)
        if not isolated_run:
            stats.update(_write_source_remediation_stats(inserted, source_health, config))
    _coordination_checkpoint(config, "source_health", {
        "signal_count": len(inserted),
        "source_count": len(source_health or {}),
    })
    with ops.stage("team_ops"):
        stats.update(_filing_evidence_stats(inserted))
        inserted, team_output, team_stats = run_research_team(inserted, config, stats)
        stats.update(research_stats)
        stats.update(team_stats)
        stats["team"] = team_output
        if not isolated_run:
            team_ops_paths = write_team_ops_artifacts(inserted, team_output, stats, config)
            stats.update(team_ops_paths)
        stats["team_ops"] = build_team_ops_snapshot(inserted, team_output, stats, config)
    _coordination_checkpoint(config, "team_ops", {"signal_count": len(inserted)})
    with ops.stage("review_queue"):
        if isolated_run:
            queue = build_review_queue(config)
            stats["review_queue_summary"] = queue.get("summary", {})
        else:
            queue_paths = write_review_queue_snapshot(config)
            stats.update({key: value for key, value in queue_paths.items() if key != "review_queue"})
    _coordination_checkpoint(config, "review_queue", {"signal_count": len(inserted)})
    with ops.stage("research_cases"):
        research_cases, case_stats = update_research_cases(inserted, config, allow_llm=False, ops=ops)
        stats.update(case_stats)
        stats["research_cases"] = research_cases
    _coordination_checkpoint(config, "research_cases", {"case_count": len(research_cases or [])})
    with ops.stage("research_runtime"):
        if config.get("research_runtime", {}).get("auto_sync", True):
            stats.update(sync_research_runtime(config, cases=research_cases, signals=inserted, project_path_fn=project_path))
    _coordination_checkpoint(config, "research_runtime", {"case_count": len(research_cases or [])})
    with ops.stage("research_workpapers"):
        stats.update(sync_research_workpapers(
            config,
            cases=research_cases,
            signals=inserted,
            runtime_cases=stats.get("research_runtime_cases", []),
            project_path_fn=project_path,
        ))
    _coordination_checkpoint(config, "research_workpapers", {"case_count": len(research_cases or [])})
    with ops.stage("decisions"):
        decision_summary = _isolated_decision_summary() if isolated_run else compute_decision_summary()
        stats["decision_summary"] = decision_summary
        if not isolated_run:
            stats.update(write_decision_summary(decision_summary))
    _coordination_checkpoint(config, "decisions", {
        "decision_count": int(decision_summary.get("total", 0) or 0),
    })
    with ops.stage("run_summary"):
        pass
    stats["run_ops"] = ops.summary()
    coordination_snapshot = _coordination_stats(config)
    if coordination_snapshot:
        stats["run_coordination"] = coordination_snapshot
    if not isolated_run:
        stats.update(write_run_summary(ops))
    stats["run_ops"] = ops.summary()
    stats["llm_connectivity"] = _llm_connectivity_from_run_ops(stats["run_ops"], config)
    report_path, json_path, alert_path, alerts = write_outputs(inserted, config, stats)
    if not isolated_run:
        stats.update(_sync_workspace_inbox(json_path))
        stats.update(_sync_brokerage_architecture())
    return PipelineResult(
        generated_at=now_iso(),
        signals=inserted,
        alerts=alerts,
        report_path=report_path,
        json_path=json_path,
        alert_path=alert_path,
        stats=stats,
    )


def run_agent_pipeline(config: Dict[str, Any]) -> PipelineResult:
    """Pipeline with LLM enrichment stage.

    Flow: collect -> rule-based analyze -> upsert (dedup) -> LLM enrich new signals -> write outputs + actions.
    LLM failures never break the pipeline.

    Like ``run_pipeline``, the body is scoped to the caller's active project-path
    resolver so every stage and writer stays inside the intended root.
    """
    with project_path_override(project_path):
        return _run_agent_pipeline_body(config)


def _run_agent_pipeline_body(config: Dict[str, Any]) -> PipelineResult:
    return _run_with_coordination(config, "agent-run", _run_agent_pipeline_body_uncoordinated)


def _run_agent_pipeline_body_uncoordinated(config: Dict[str, Any]) -> PipelineResult:
    ensure_directories()
    isolated_run = _is_isolated_run(config)
    ops = RunOpsContext(config, mode="agent-run", writer=config.get("_progress_writer"))
    with ops.stage("collect"):
        raw_signals, source_health = _collect_for_pipeline(config)
    _coordination_checkpoint(config, "collect", {
        "raw_count": len(raw_signals),
        "source_health_count": len(source_health or {}),
    })

    with ops.stage("rule_analysis"):
        attach_source_quality(raw_signals, source_health)
        analyzed = analyze_signals(raw_signals, config)
        analyzed = apply_decision_context(analyzed, {} if isolated_run else load_decision_context())
        analyzed, research_stats = apply_research_framework(analyzed, config)
        team_base_stats = {
            "raw": len(raw_signals),
            "analyzed": len(analyzed),
            "source_errors": len([signal for signal in analyzed if signal.source_type == "source_error"]),
        }
        analyzed, team_output, team_stats = run_research_team(analyzed, config, team_base_stats)
    _coordination_checkpoint(config, "rule_analysis", {
        "raw_count": len(raw_signals),
        "analyzed_count": len(analyzed),
        "source_error_count": len([signal for signal in analyzed if signal.source_type == "source_error"]),
    })

    with ops.stage("official_evidence_vault"):
        analyzed, official_evidence_stats = attach_evidence_vault_status(
            analyzed,
            config,
            allow_auto_capture=not isolated_run,
        )
    _coordination_checkpoint(config, "official_evidence_vault", {
        "signal_count": len(analyzed),
        "stats_keys": sorted(str(key) for key in official_evidence_stats.keys()),
    })

    with ops.stage("storage"):
        store = SignalStore(project_path(config.get("storage", {}).get("sqlite_path", "data/stock_agent.sqlite3")))
        try:
            inserted, duplicates = store.upsert_many(analyzed)
        finally:
            store.close()
    _coordination_checkpoint(config, "storage", {
        "inserted_count": len(inserted),
        "duplicate_count": int(duplicates or 0),
    })

    # LLM enrichment on newly inserted signals only
    llm_stats: Dict[str, Any] = {
        "llm_enabled": config.get("llm_agent", {}).get("enabled", False),
        "llm_candidates": 0,
        "llm_called": 0,
        "llm_success": 0,
        "llm_failed": 0,
        "llm_skipped_no_key": 0,
        "llm_skipped_budget": 0,
    }
    role_stats: Dict[str, Any] = {
        "role_llm_enabled": config.get("multi_role_agent", {}).get("enabled", False),
        "role_llm_candidates": 0,
        "role_llm_called": 0,
        "role_llm_success": 0,
        "role_llm_failed": 0,
        "role_llm_skipped_no_key": 0,
        "role_llm_skipped_budget": 0,
    }
    with ops.stage("llm_signals"):
        if inserted:
            inserted, llm_stats = enrich_signals(inserted, config, ops=ops)
            # Re-store to persist LLM results
            store = SignalStore(project_path(config.get("storage", {}).get("sqlite_path", "data/stock_agent.sqlite3")))
            try:
                for signal in inserted:
                    if signal.llm_analysis or signal.llm_error:
                        _update_signal_llm(store, signal)
            finally:
                store.close()
    _coordination_checkpoint(config, "llm_signals", {
        "inserted_count": len(inserted),
        "llm_called": int(llm_stats.get("llm_called", 0) or 0),
        "llm_success": int(llm_stats.get("llm_success", 0) or 0),
        "llm_failed": int(llm_stats.get("llm_failed", 0) or 0),
    })

    with ops.stage("multi_role_llm"):
        if inserted:
            inserted, role_stats = enrich_signals_with_role_analysts(inserted, config, ops=ops)
            store = SignalStore(project_path(config.get("storage", {}).get("sqlite_path", "data/stock_agent.sqlite3")))
            try:
                for signal in inserted:
                    if signal.role_analyses or signal.role_synthesis or signal.role_analysis_error:
                        _update_signal_role_analysis(store, signal)
            finally:
                store.close()
    _coordination_checkpoint(config, "multi_role_llm", {
        "inserted_count": len(inserted),
        "role_llm_called": int(role_stats.get("role_llm_called", 0) or 0),
        "role_llm_success": int(role_stats.get("role_llm_success", 0) or 0),
        "role_llm_failed": int(role_stats.get("role_llm_failed", 0) or 0),
    })

    evidence_stats: Dict[str, Any] = {}
    with ops.stage("evidence_chain"):
        if inserted:
            inserted, evidence_stats = attach_evidence_chains(inserted)
            store = SignalStore(project_path(config.get("storage", {}).get("sqlite_path", "data/stock_agent.sqlite3")))
            try:
                for signal in inserted:
                    if signal.evidence_chain or signal.evidence_gap_tasks:
                        _update_signal_evidence_chain(store, signal)
            finally:
                store.close()
    _coordination_checkpoint(config, "evidence_chain", {
        "inserted_count": len(inserted),
        "evidence_chain_count": int(evidence_stats.get("evidence_chain_count", 0) or 0),
    })

    governance_report_stats: Dict[str, Any] = {}
    with ops.stage("llm_governance"):
        governance_report_stats = write_llm_governance_summary(
            inserted,
            dict(llm_stats, **role_stats),
            config,
        )
    _coordination_checkpoint(config, "llm_governance", {
        "llm_called": int(llm_stats.get("llm_called", 0) or 0),
        "role_llm_called": int(role_stats.get("role_llm_called", 0) or 0),
    })

    # Execute semi-automatic actions
    review_path = ""
    actions_path = ""
    llm_cfg = config.get("llm_agent", {})
    with ops.stage("actions"):
        if llm_cfg.get("auto_execute_actions", True) and not isolated_run:
            review_path = _execute_export_review(inserted, config)
            actions_path = _execute_write_action_log(inserted, config)
    _coordination_checkpoint(config, "actions", {
        "review_written": bool(review_path),
        "actions_written": bool(actions_path),
    })

    research_git_stats: Dict[str, Any] = {}
    with ops.stage("research_git"):
        research_git_stats = stage_llm_research_drafts(inserted, config)
    _coordination_checkpoint(config, "research_git", {
        "signal_count": len(inserted),
        "draft_count": int(research_git_stats.get("research_draft_count", 0) or 0),
    })

    stats = {
        "raw": len(raw_signals),
        "analyzed": len(analyzed),
        "inserted": len(inserted),
        "duplicates": duplicates,
        "source_errors": len([signal for signal in analyzed if signal.source_type == "source_error"]),
        "example_data_signals": len(_example_data_signals(analyzed)),
        "disabled_sources": _disabled_source_names(config),
    }
    if stats["example_data_signals"]:
        stats["example_data_warning"] = (
            "Example/sample input contributed signals in this run; they are labeled "
            "payload.data_provenance=example and must not be counted as real production samples."
        )
    if _LAST_MARKET_RESILIENCE:
        stats["market_resilience"] = _LAST_MARKET_RESILIENCE
    if _LAST_MARKET_DATA_GOVERNANCE:
        stats["market_data_governance"] = _LAST_MARKET_DATA_GOVERNANCE
    stats.update(official_evidence_stats)

    with ops.stage("source_health"):
        source_health = _finalize_source_health(source_health, analyzed, inserted, config)
        if not isolated_run:
            stats.update(write_source_health_summary(source_health))
            if _LAST_MARKET_DATA_GOVERNANCE:
                stats.update(write_market_data_governance_summary(_LAST_MARKET_DATA_GOVERNANCE, config))
        stats["source_health"] = public_source_health_summary(source_health)
        if not isolated_run:
            stats.update(_write_source_remediation_stats(inserted, source_health, config))
    _coordination_checkpoint(config, "source_health", {
        "signal_count": len(inserted),
        "source_count": len(source_health or {}),
    })

    with ops.stage("team_ops"):
        stats.update(_filing_evidence_stats(inserted))
        inserted, team_output, team_stats = run_research_team(inserted, config, stats)
        stats.update(research_stats)
        stats.update(team_stats)
        stats["team"] = team_output
        stats.update(llm_stats)
        stats.update(role_stats)
        stats.update(governance_report_stats)
        stats.update(evidence_stats)
        stats.update(research_git_stats)
        if not isolated_run:
            team_ops_paths = write_team_ops_artifacts(inserted, team_output, stats, config)
            stats.update(team_ops_paths)
        stats["team_ops"] = build_team_ops_snapshot(inserted, team_output, stats, config)
    _coordination_checkpoint(config, "team_ops", {"signal_count": len(inserted)})

    with ops.stage("review_queue"):
        if isolated_run:
            queue = build_review_queue(config)
            stats["review_queue_summary"] = queue.get("summary", {})
        else:
            queue_paths = write_review_queue_snapshot(config)
            stats.update({key: value for key, value in queue_paths.items() if key != "review_queue"})
    _coordination_checkpoint(config, "review_queue", {"signal_count": len(inserted)})

    with ops.stage("model_operations"):
        stats.update(write_model_operations_summary(inserted, stats, ops.summary(), config))
    _coordination_checkpoint(config, "model_operations", {"signal_count": len(inserted)})

    with ops.stage("research_cases"):
        research_cases, case_stats = update_research_cases(inserted, config, allow_llm=True, ops=ops)
        stats.update(case_stats)
        stats["research_cases"] = research_cases
    _coordination_checkpoint(config, "research_cases", {"case_count": len(research_cases or [])})
    with ops.stage("research_runtime"):
        if config.get("research_runtime", {}).get("auto_sync", True):
            stats.update(sync_research_runtime(config, cases=research_cases, signals=inserted, project_path_fn=project_path))
    _coordination_checkpoint(config, "research_runtime", {"case_count": len(research_cases or [])})
    with ops.stage("research_workpapers"):
        stats.update(sync_research_workpapers(
            config,
            cases=research_cases,
            signals=inserted,
            runtime_cases=stats.get("research_runtime_cases", []),
            project_path_fn=project_path,
        ))
    _coordination_checkpoint(config, "research_workpapers", {"case_count": len(research_cases or [])})

    with ops.stage("decisions"):
        decision_summary = _isolated_decision_summary() if isolated_run else compute_decision_summary()
        stats["decision_summary"] = decision_summary
        if not isolated_run:
            stats.update(write_decision_summary(decision_summary))
    _coordination_checkpoint(config, "decisions", {
        "decision_count": int(decision_summary.get("total", 0) or 0),
    })

    with ops.stage("run_summary"):
        pass
    stats["run_ops"] = ops.summary()
    coordination_snapshot = _coordination_stats(config)
    if coordination_snapshot:
        stats["run_coordination"] = coordination_snapshot
    if not isolated_run:
        stats.update(write_run_summary(ops))
    stats["run_ops"] = ops.summary()
    stats["llm_connectivity"] = _llm_connectivity_from_run_ops(stats["run_ops"], config)

    report_path, json_path, alert_path, alerts = write_outputs(inserted, config, stats)
    if not isolated_run:
        stats.update(_sync_workspace_inbox(json_path))
        stats.update(_sync_brokerage_architecture())
    return PipelineResult(
        generated_at=now_iso(),
        signals=inserted,
        alerts=alerts,
        report_path=report_path,
        json_path=json_path,
        alert_path=alert_path,
        stats=stats,
        review_path=review_path,
        actions_path=actions_path,
    )


def _update_signal_llm(store: "SignalStore", signal: Any) -> None:
    """Update LLM fields for an existing signal in the database."""
    store.conn.execute(
        "UPDATE signals SET llm_governance_json = ?, llm_analysis_json = ?, llm_actions_json = ?, llm_error = ? WHERE signal_id = ?",
        (
            json.dumps(signal.llm_governance, ensure_ascii=False) if signal.llm_governance else None,
            json.dumps(signal.llm_analysis, ensure_ascii=False) if signal.llm_analysis else None,
            json.dumps(signal.llm_actions, ensure_ascii=False) if signal.llm_actions else None,
            signal.llm_error or None,
            signal.signal_id,
        ),
    )
    store.conn.commit()


def _sync_brokerage_architecture() -> Dict[str, Any]:
    try:
        result = sync_brokerage_architecture_from_latest_report()
        return {
            "architecture_path": result.get("architecture_path", ""),
            "architecture_json_path": result.get("architecture_json_path", ""),
            "architecture_latest_path": result.get("architecture_latest_path", ""),
            "architecture_latest_json_path": result.get("architecture_latest_json_path", ""),
            "architecture_events_written": result.get("events_written", 0),
        }
    except Exception as exc:
        return {"architecture_error": str(exc)}


def _is_isolated_run(config: Dict[str, Any]) -> bool:
    profile = config.get("run_profile", {}) if isinstance(config, dict) else {}
    return bool(profile.get("isolated", False)) if isinstance(profile, dict) else False


def _record_run_manifest(config: Dict[str, Any], result: PipelineResult, mode: str) -> Dict[str, Any]:
    """Write the immutable per-run manifest for a completed coordinated run.

    The manifest hashes the FINAL JSON artifact, so it must run after
    ``_finalize_coordination_json`` and after the ledger completed the run.
    An error is reported as an explicit ERROR state — a completed ledger entry
    with a missing manifest stays visible instead of being silently downgraded.
    """

    cfg = run_manifest_config(config)
    if not cfg.get("enabled"):
        return {"status": "DISABLED"}
    try:
        outcome = write_run_manifest(
            config,
            str(getattr(result, "json_path", "") or ""),
            mode=mode,
            trigger_source=str(config.get("_trigger_source") or "manual"),
            config_path=str(config.get("_config_path") or ""),
        )
    except Exception as exc:
        return {"status": "ERROR", "error": "{0}: {1}".format(type(exc).__name__, exc)}
    return {
        "status": outcome["status"],
        "manifest_path": outcome["manifest_path"],
        "manifest_relative_path": outcome["manifest_relative_path"],
        "manifest_sha256": outcome["manifest_sha256"],
        "pointer_path": outcome["pointer_path"],
        "eligible_for_pilot": bool(outcome["eligible_for_pilot"]),
    }


def _example_data_signals(signals: List[Any]) -> List[Any]:
    return [s for s in signals if isinstance(getattr(s, "payload", None), dict) and s.payload.get("data_provenance") == "example"]


_LIST_FORM_SOURCES = ("local_jsonl", "filing_jsonl", "rss")
_DICT_FORM_SOURCES = ("eastmoney_filing", "eastmoney", "alltick", "free_quote", "pysnowball", "akshare")


def _disabled_source_names(config: Dict[str, Any]) -> List[str]:
    """List configured-but-disabled source ids so a run states them explicitly."""

    sources = config.get("sources", {}) if isinstance(config.get("sources"), dict) else {}
    disabled: List[str] = []
    for source_id in _LIST_FORM_SOURCES:
        entries = sources.get(source_id, [])
        if isinstance(entries, dict):
            entries = [entries]
        if not isinstance(entries, list):
            continue
        for entry in entries:
            if isinstance(entry, dict) and not entry.get("enabled", True):
                disabled.append("{0}:{1}".format(source_id, entry.get("name") or "unnamed"))
    for source_id in _DICT_FORM_SOURCES:
        entry = sources.get(source_id, {})
        if isinstance(entry, dict) and not entry.get("enabled", True):
            disabled.append(source_id)
    return sorted(disabled)


def _isolated_decision_summary() -> Dict[str, Any]:
    return {
        "generated_at": now_iso(),
        "total": 0,
        "decision_counts": {},
        "decision_family_counts": {},
        "outcome_counts": {},
        "latest_by_signal": {},
        "pending": [],
        "valid_decisions": [],
        "valid_outcomes": [],
        "scope": "isolated_run_no_production_history",
    }


def _sync_workspace_inbox(report_path: str = "") -> Dict[str, Any]:
    try:
        result = sync_workspace_inbox(report_path)
        counts = result.get("object_counts", {}) or {}
        return {
            "workspace_inbox_path": result.get("workspace_inbox_path", ""),
            "workspace_report_path": result.get("workspace_report_path", ""),
            "workspace_latest_path": result.get("workspace_latest_path", ""),
            "workspace_latest_json_path": result.get("workspace_latest_json_path", ""),
            "workspace_object_counts": counts,
            "workspace_signal_count": counts.get("signals", 0),
            "workspace_evidence_count": counts.get("evidence_items", 0),
            "workspace_comment_count": counts.get("comments", 0),
            "workspace_research_draft_count": counts.get("research_drafts", 0),
        }
    except Exception as exc:
        return {"workspace_inbox_error": str(exc)}


def _update_signal_role_analysis(store: "SignalStore", signal: Any) -> None:
    store.conn.execute(
        "UPDATE signals SET llm_governance_json = ?, role_analyses_json = ?, role_synthesis_json = ?, role_analysis_error = ? WHERE signal_id = ?",
        (
            json.dumps(signal.llm_governance, ensure_ascii=False) if signal.llm_governance else None,
            json.dumps(signal.role_analyses, ensure_ascii=False) if signal.role_analyses else None,
            json.dumps(signal.role_synthesis, ensure_ascii=False) if signal.role_synthesis else None,
            signal.role_analysis_error or None,
            signal.signal_id,
        ),
    )
    store.conn.commit()


def _update_signal_evidence_chain(store: "SignalStore", signal: Any) -> None:
    store.conn.execute(
        "UPDATE signals SET evidence_chain_json = ?, evidence_gap_tasks_json = ? WHERE signal_id = ?",
        (
            json.dumps(signal.evidence_chain, ensure_ascii=False) if signal.evidence_chain else None,
            json.dumps(signal.evidence_gap_tasks, ensure_ascii=False) if signal.evidence_gap_tasks else None,
            signal.signal_id,
        ),
    )
    store.conn.commit()


def _filing_evidence_stats(signals: List[Any]) -> Dict[str, Any]:
    evidence_items = []
    for signal in signals:
        payload = getattr(signal, "payload", {}) or {}
        filing = payload.get("filing_evidence")
        if isinstance(filing, dict):
            evidence_items.append(filing)
    statuses: Dict[str, int] = {}
    providers: Dict[str, int] = {}
    missing_fields: Dict[str, int] = {}
    importance_levels: Dict[str, int] = {}
    for item in evidence_items:
        status = str(item.get("status") or "unknown")
        provider = str(item.get("provider") or "unknown")
        level = str(item.get("importance_level") or "unknown")
        statuses[status] = statuses.get(status, 0) + 1
        providers[provider] = providers.get(provider, 0) + 1
        importance_levels[level] = importance_levels.get(level, 0) + 1
        for field in item.get("missing_fields", []) or []:
            field = str(field)
            missing_fields[field] = missing_fields.get(field, 0) + 1
    return {
        "filing_evidence_count": len(evidence_items),
        "filing_evidence_status_counts": statuses,
        "filing_evidence_provider_counts": providers,
        "filing_evidence_missing_field_counts": missing_fields,
        "filing_importance_level_counts": importance_levels,
    }


def _execute_export_review(signals: List[Any], config: Dict[str, Any]) -> str:
    """Write signals needing human review to a JSONL file."""
    review_signals = [s for s in signals if s.llm_analysis and s.llm_analysis.get("human_decision_needed")]
    if not review_signals:
        return ""
    stamp = now_iso().replace(":", "").replace("-", "").replace("+", "_").split(".")[0]
    path = project_path("data/review/pending_review_{}.jsonl".format(stamp))
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = []
    for s in review_signals:
        lines.append(json.dumps({
            "signal_id": s.signal_id,
            "title": s.title,
            "priority": s.priority,
            "llm_analysis": s.llm_analysis,
            "role_analyses": s.role_analyses,
            "role_synthesis": s.role_synthesis,
            "evidence_chain": s.evidence_chain,
            "evidence_gap_tasks": s.evidence_gap_tasks,
            "exported_at": now_iso(),
        }, ensure_ascii=False))
    path.write_text("\n".join(lines), encoding="utf-8")
    return str(path)


def _execute_write_action_log(signals: List[Any], config: Dict[str, Any]) -> str:
    """Write LLM action suggestions to a JSONL file."""
    action_signals = [s for s in signals if s.llm_actions]
    if not action_signals:
        return ""
    stamp = now_iso().replace(":", "").replace("-", "").replace("+", "_").split(".")[0]
    path = project_path("reports/actions/actions_{}.jsonl".format(stamp))
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = []
    for s in action_signals:
        lines.append(json.dumps({
            "signal_id": s.signal_id,
            "title": s.title,
            "priority": s.priority,
            "llm_actions": s.llm_actions,
            "recorded_at": now_iso(),
            "forbidden_actions_note": "禁止自动交易、下单、调仓，需人工确认",
        }, ensure_ascii=False))
    path.write_text("\n".join(lines), encoding="utf-8")
    return str(path)


def collect_sources(config: Dict[str, Any]) -> List[RawSignal]:
    global _LAST_SOURCE_HEALTH
    raw_signals, source_health = collect_sources_with_health(config)
    _LAST_SOURCE_HEALTH = source_health
    return raw_signals


def _collect_for_pipeline(config: Dict[str, Any]) -> Tuple[List[RawSignal], Dict[str, Any]]:
    global _LAST_SOURCE_HEALTH, _LAST_MARKET_RESILIENCE, _LAST_MARKET_DATA_GOVERNANCE
    _LAST_SOURCE_HEALTH = None
    _LAST_MARKET_RESILIENCE = None
    _LAST_MARKET_DATA_GOVERNANCE = None
    raw_signals = collect_sources(config)
    raw_signals, _LAST_MARKET_DATA_GOVERNANCE = assess_market_signals(
        raw_signals, config, _LAST_MARKET_RESILIENCE
    )
    if _LAST_SOURCE_HEALTH:
        return raw_signals, _LAST_SOURCE_HEALTH
    return raw_signals, _fallback_source_health(raw_signals, config)


def _fallback_source_health(raw_signals: List[RawSignal], config: Dict[str, Any]) -> Dict[str, Any]:
    grouped: Dict[str, List[RawSignal]] = {}
    for signal in raw_signals:
        grouped.setdefault(signal.source, []).append(signal)
    events = []
    for source_id, signals in grouped.items():
        events.append({
            "source_id": source_id,
            "source_kind": source_id,
            "source_name": source_id,
            "enabled": True,
            "attempted": True,
            "latency_ms": 0,
            "signals": signals,
            "expected_symbols": [],
        })
    return build_source_health_summary(events, config)


def collect_sources_with_health(config: Dict[str, Any]) -> Tuple[List[RawSignal], Dict[str, Any]]:
    global _LAST_MARKET_RESILIENCE
    sources_config = config.get("sources", {})
    raw_signals: List[RawSignal] = []
    events = []
    source_plan = [
        ("local_jsonl", "local_jsonl", sources_config.get("local_jsonl", []), collect_local_jsonl, []),
        ("filing_jsonl", "filing_jsonl", sources_config.get("filing_jsonl", []), collect_filing_jsonl, []),
        ("eastmoney_filing", "eastmoney_filing", sources_config.get("eastmoney_filing", {}), collect_eastmoney_filings, _symbols(sources_config.get("eastmoney_filing", {}))),
        ("rss", "rss", sources_config.get("rss", []), collect_rss, []),
    ]
    for source_id, source_kind, source_cfg, collector, expected_symbols in source_plan:
        signals, event = _collect_one_source(source_id, source_kind, source_cfg, collector, expected_symbols)
        raw_signals.extend(signals)
        events.append(event)
    if config.get("market_resilience", {}).get("enabled", True):
        market_signals, market_events, market_diag = collect_market_quotes_with_resilience(
            config,
            collectors={
                "eastmoney": collect_eastmoney,
                "alltick": collect_alltick,
                "pysnowball": collect_pysnowball,
                "akshare": collect_akshare,
                "free_quote": collect_free_quote,
            },
        )
        raw_signals.extend(market_signals)
        events.extend(market_events)
        _LAST_MARKET_RESILIENCE = market_diag
    else:
        price_plan = [
            ("eastmoney", "eastmoney", sources_config.get("eastmoney", {}), collect_eastmoney, _symbols(sources_config.get("eastmoney", {}))),
            ("alltick", "alltick", sources_config.get("alltick", {}), collect_alltick, _symbols(sources_config.get("alltick", {}))),
            ("pysnowball", "pysnowball", sources_config.get("pysnowball", {}), collect_pysnowball, _symbols(sources_config.get("pysnowball", {}))),
            ("akshare", "akshare", sources_config.get("akshare", {}), collect_akshare, _symbols(sources_config.get("akshare", {}))),
            ("free_quote", "free_quote", sources_config.get("free_quote", {}), collect_free_quote, _symbols(sources_config.get("free_quote", {}))),
        ]
        for source_id, source_kind, source_cfg, collector, expected_symbols in price_plan:
            signals, event = _collect_one_source(source_id, source_kind, source_cfg, collector, expected_symbols)
            raw_signals.extend(signals)
            events.append(event)
        _LAST_MARKET_RESILIENCE = {"enabled": False, "status": "DISABLED"}
    source_health = build_source_health_summary(events, config)
    return raw_signals, source_health


def _collect_one_source(source_id: str, source_kind: str, source_cfg: Any, collector: Any, expected_symbols: List[str]) -> Tuple[List[RawSignal], Dict[str, Any]]:
    enabled = _source_enabled(source_cfg)
    event = {
        "source_id": source_id,
        "source_kind": source_kind,
        "source_name": source_id,
        "enabled": enabled,
        "attempted": False,
        "latency_ms": 0,
        "signals": [],
        "expected_symbols": expected_symbols,
        "exception": "",
    }
    if not enabled:
        return [], event
    started = time.perf_counter()
    try:
        signals = collector(source_cfg)
    except Exception as exc:
        signals = [
            RawSignal(
                source=source_id,
                source_type="source_error",
                title="{0} collection failed".format(source_id),
                summary=str(exc),
                published_at=now_iso(),
            )
        ]
        event["exception"] = str(exc)
    event["attempted"] = True
    event["latency_ms"] = int((time.perf_counter() - started) * 1000)
    event["signals"] = signals
    normal_signals, degraded_events = split_source_degraded_signals(signals)
    event["source_degraded_events"] = degraded_events
    return normal_signals, event


def _source_enabled(source_cfg: Any) -> bool:
    if isinstance(source_cfg, list):
        return any(item.get("enabled", True) for item in source_cfg if isinstance(item, dict))
    if isinstance(source_cfg, dict):
        return bool(source_cfg.get("enabled", False))
    return False


def _symbols(source_cfg: Any) -> List[str]:
    if not isinstance(source_cfg, dict):
        return []
    return [str(item).strip() for item in source_cfg.get("symbols", []) if str(item).strip()]


def _finalize_source_health(
    source_health: Dict[str, Any],
    analyzed: List[Any],
    inserted: List[Any],
    config: Dict[str, Any],
) -> Dict[str, Any]:
    del inserted  # Historical database duplicates are not source-feed duplicates.
    seen_by_source: Dict[str, set] = {}
    duplicate_counts: Dict[str, int] = {}
    for signal in analyzed:
        quality = (signal.payload or {}).get("source_quality", {})
        source_id = str(quality.get("source_id") or signal.source)
        seen = seen_by_source.setdefault(source_id, set())
        if signal.signal_id in seen:
            duplicate_counts[source_id] = duplicate_counts.get(source_id, 0) + 1
        seen.add(signal.signal_id)
    if not duplicate_counts:
        return source_health
    events = [item.get("_event", {}) for item in source_health.get("sources", []) if item.get("_event")]
    updated = build_source_health_summary(events, config, duplicate_counts=duplicate_counts)
    return updated


def _write_source_remediation_stats(
    signals: List[Any],
    source_health: Dict[str, Any],
    config: Dict[str, Any],
) -> Dict[str, Any]:
    remediation = build_remediation_summary(signals, source_health, config)
    paths = write_remediation_summary(remediation)
    result = dict(remediation)
    result.update(paths)
    return result


def _llm_connectivity_from_run_ops(run_ops: Dict[str, Any], config: Dict[str, Any]) -> Dict[str, Any]:
    llm_cfg = config.get("llm_agent", {}) or {}
    if not llm_cfg.get("enabled", False):
        return {"status": "DISABLED", "diagnosis": "", "repair_guide": []}
    failures = [
        item for item in run_ops.get("degradations", []) or []
        if item.get("component") in ("signal_llm", "research_case_llm") and item.get("reason") == "llm_call_failed"
    ]
    if not failures:
        return {
            "status": "OK",
            "diagnosis": "",
            "model": llm_cfg.get("model", ""),
            "base_url": llm_cfg.get("base_url", ""),
            "repair_guide": [],
        }
    detail = str(failures[-1].get("detail", ""))
    diagnosis = diagnose_network_error(detail)
    return {
        "status": "WARN",
        "diagnosis": diagnosis,
        "last_error": detail,
        "model": llm_cfg.get("model", ""),
        "base_url": llm_cfg.get("base_url", ""),
        "repair_guide": network_repair_guide(diagnosis),
    }
