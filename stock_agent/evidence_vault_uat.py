from __future__ import annotations

import json
import tempfile
from copy import deepcopy
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Dict, Optional, Sequence

from .audit_log import atomic_write_text, file_hash, object_hash
from .config import project_path, project_path_override
from .evidence_vault import evidence_vault_config, fetch_official_document, verify_evidence_vault
from .models import now_iso


class EvidenceVaultUATError(ValueError):
    pass


def run_evidence_vault_uat(
    config: Dict[str, Any],
    *,
    source_url: str,
    title: str,
    published_at: str,
    actor: str,
    actor_role: str,
    signal_ids: Optional[Sequence[str]] = None,
    issuer: str = "",
    symbol: str = "",
    source_kind: str = "company_disclosure",
    live: bool = False,
    fetcher: Callable[..., Dict[str, Any]] = fetch_official_document,
) -> Dict[str, Any]:
    """Run one explicit live capture in an isolated temporary vault.

    The command validates the real transport and object/event controls without
    adding test documents or events to the production evidence inventory.
    Only non-sensitive capture metadata is retained as the UAT report.
    """
    if not live:
        raise EvidenceVaultUATError(
            "refusing live official-source UAT without --live; this performs one exact network fetch"
        )
    production_before = _production_inventory_fingerprint(config)
    isolated = deepcopy(config)
    vault_cfg = isolated.setdefault("official_evidence_vault", {})
    vault_cfg["enabled"] = True
    vault_cfg["allow_network_fetch"] = True
    vault_cfg["auto_capture_official_signals"] = False
    _pin_operations_inputs_to_current_project(isolated)

    with tempfile.TemporaryDirectory(prefix="stock-agent-evidence-vault-uat-") as temp_dir:
        root = Path(temp_dir)
        with project_path_override(lambda value: root / str(value)):
            result = fetcher(
                isolated,
                source_url=source_url,
                title=title,
                published_at=published_at,
                actor=actor,
                actor_role=actor_role,
                signal_ids=list(signal_ids or []),
                issuer=issuer,
                symbol=symbol,
                source_kind=source_kind,
            )
            verification = verify_evidence_vault(isolated)
            document = result.get("document", {}) or {}
            isolated_event_count = verification.get("event_count", 0)
            isolated_document_count = verification.get("document_count", 0)

    production_after = _production_inventory_fingerprint(config)
    production_unchanged = production_before == production_after
    captured = document.get("status") == "captured"
    technical_ready = (
        verification.get("technical_status") == "READY"
        and verification.get("integrity_status") == "OK"
    )
    status = "PASS" if captured and technical_ready and production_unchanged else "FAIL"
    summary = {
        "version": "28.0",
        "generated_at": now_iso(),
        "status": status,
        "live_network_fetch": True,
        "isolated_temporary_vault": True,
        "production_inventory_unchanged": production_unchanged,
        "production_inventory_fingerprint_before": production_before,
        "production_inventory_fingerprint_after": production_after,
        "source_url": document.get("source_url", source_url),
        "source_domain": document.get("source_domain", ""),
        "title": document.get("title", title),
        "published_at": document.get("published_at", published_at),
        "document_id": document.get("document_id", ""),
        "sha256": document.get("sha256", ""),
        "content_type": document.get("content_type", ""),
        "byte_size": document.get("byte_size", 0),
        "capture_mode": document.get("capture_mode", ""),
        "source_binding_status": document.get("source_binding_status", ""),
        "document_status": document.get("status", ""),
        "temporary_technical_status": verification.get("technical_status", "UNKNOWN"),
        "temporary_business_status": verification.get("business_status", "BLOCKED"),
        "temporary_integrity_status": verification.get("integrity_status", "UNKNOWN"),
        "temporary_document_count": isolated_document_count,
        "temporary_event_count": isolated_event_count,
        "temporary_errors": list(verification.get("errors") or []),
        "temporary_business_blockers": list(verification.get("business_blockers") or []),
        "raw_document_retained_after_uat": False,
        "raw_document_sent_to_external_llm": False,
        "automatic_approval_executed": False,
        "automatic_publication_executed": False,
        "automatic_trading_executed": False,
        "safety_boundary": (
            "This UAT proves one isolated official-source transport and integrity check only. "
            "It does not add production evidence, approve an archive, publish research, or authorize trading."
        ),
    }
    paths = write_evidence_vault_uat_report(summary)
    summary.update(paths)
    return summary


def write_evidence_vault_uat_report(summary: Dict[str, Any]) -> Dict[str, str]:
    report_dir = project_path("reports/evidence_vault/uat")
    report_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().astimezone().strftime("%Y%m%dT%H%M%S%z")
    latest_json = report_dir / "latest.json"
    latest_md = report_dir / "latest.md"
    history_json = report_dir / "uat_{0}.json".format(stamp)
    history_md = report_dir / "uat_{0}.md".format(stamp)
    payload = json.dumps(summary, ensure_ascii=False, indent=2, default=str) + "\n"
    markdown = _render_markdown(summary)
    for path in (latest_json, history_json):
        atomic_write_text(path, payload)
    for path in (latest_md, history_md):
        atomic_write_text(path, markdown, encoding="utf-8-sig")
    return {
        "evidence_vault_uat_json_path": str(history_json),
        "evidence_vault_uat_path": str(history_md),
        "evidence_vault_uat_latest_json_path": str(latest_json),
        "evidence_vault_uat_latest_path": str(latest_md),
    }


def _production_inventory_fingerprint(config: Dict[str, Any]) -> str:
    cfg = evidence_vault_config(config)
    verification = verify_evidence_vault(config)
    events = project_path(cfg["events_path"])
    index = project_path(cfg["index_path"])
    return object_hash({
        "enabled": cfg["enabled"],
        "event_count": verification.get("event_count", 0),
        "document_count": verification.get("document_count", 0),
        "integrity_status": verification.get("integrity_status", "UNKNOWN"),
        "events_sha256": file_hash(events) if events.is_file() else "missing",
        "index_sha256": file_hash(index) if index.is_file() else "missing",
    })


def _pin_operations_inputs_to_current_project(config: Dict[str, Any]) -> None:
    operations = config.setdefault("research_operations", {})
    for key, default in (
        ("roster_path", "config/research_roster.local.json"),
        ("restrictions_path", "config/research_restrictions.local.json"),
    ):
        raw = str(operations.get(key) or default)
        operations[key] = str(project_path(raw).resolve())


def _render_markdown(summary: Dict[str, Any]) -> str:
    lines = [
        "# Official Evidence Vault Live UAT",
        "",
        "- generated_at: {0}".format(summary.get("generated_at", "")),
        "- status: {0}".format(summary.get("status", "")),
        "- source: {0}".format(summary.get("source_url", "")),
        "- domain / type / bytes: {0} / {1} / {2}".format(
            summary.get("source_domain", ""), summary.get("content_type", ""), summary.get("byte_size", 0)
        ),
        "- document_id: {0}".format(summary.get("document_id", "")),
        "- sha256: {0}".format(summary.get("sha256", "")),
        "- temporary technical / business / integrity: {0} / {1} / {2}".format(
            summary.get("temporary_technical_status", ""),
            summary.get("temporary_business_status", ""),
            summary.get("temporary_integrity_status", ""),
        ),
        "- production inventory unchanged: {0}".format(
            str(bool(summary.get("production_inventory_unchanged"))).lower()
        ),
        "- raw document retained after UAT: false",
        "- raw document sent to external LLM: false",
        "",
        "## Boundary",
        "",
        "- {0}".format(summary.get("safety_boundary", "")),
    ]
    if summary.get("temporary_errors"):
        lines.extend(["", "## Errors", ""])
        lines.extend("- {0}".format(item) for item in summary["temporary_errors"])
    return "\n".join(lines).rstrip() + "\n"
