from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from stock_agent.config import project_path_override
from stock_agent.dashboard import _section_official_evidence_vault
from stock_agent.doctor import _check_evidence_vault
from stock_agent.evidence_vault import ingest_official_document_bytes
from stock_agent.evidence_vault_uat import EvidenceVaultUATError, run_evidence_vault_uat
from stock_agent.models import AnalyzedSignal, RawSignal, now_iso
from stock_agent.pipeline import run_agent_pipeline
from stock_agent.production_pilot import (
    _build_gates,
    _critical_input_hashes,
    _scheduled_sequence_status,
    production_pilot_config,
)
from stock_agent.reports import write_outputs


def _vault_config() -> dict:
    return {
        "enabled": True,
        "enforce": True,
        "events_path": "data/evidence_vault/events.jsonl",
        "index_path": "data/evidence_vault/index.json",
        "objects_dir": "data/evidence_vault/objects/sha256",
        "quarantine_dir": "data/evidence_vault/quarantine/sha256",
        "report_dir": "reports/evidence_vault",
        "allowed_official_domains": [
            "bse.cn", "cninfo.com.cn", "csrc.gov.cn", "sac.net.cn", "sse.com.cn", "szse.cn",
        ],
        "allowed_content_types": ["application/pdf", "text/html", "text/plain"],
        "require_https": True,
        "resolve_public_ip_on_fetch": True,
        "allow_network_fetch": False,
        "auto_capture_official_signals": False,
        "require_published_at_for_llm": True,
        "require_retained_before_external_llm_for": ["announcement", "filing", "financial"],
        "retention_years": 5,
        "automatic_approval": False,
        "automatic_publication": False,
        "automatic_trading": False,
        "production_archive": {
            "backend": "local_content_addressed_shadow",
            "evidence_ref": "",
            "external_immutability_verified": False,
            "required_review_roles": ["compliance_officer", "information_security_officer"],
            "activation_roles": ["data_steward", "research_coordinator"],
        },
    }


def _signal() -> AnalyzedSignal:
    return AnalyzedSignal(
        signal_id="sig-v28-integration",
        source="cninfo",
        source_type="filing",
        title="测试公司重大合同公告",
        summary="公司披露重大合同。",
        url="https://static.cninfo.com.cn/finalpage/test.pdf",
        published_at="2026-08-20T08:00:00+08:00",
        collected_at="2026-08-20T08:01:00+08:00",
        symbols=["688888"],
        priority="P1",
        priority_score=70.0,
        needs_review=True,
        payload={},
    )


class PipelineEvidenceOrderTests(unittest.TestCase):
    def test_evidence_annotation_is_present_before_llm_enrichment(self):
        events = []

        def attach(signals, _config, allow_auto_capture=True):
            events.append("official_evidence_vault")
            for signal in signals:
                signal.payload = dict(signal.payload or {})
                signal.payload["official_evidence_vault"] = {
                    "status": "RETAINED_VERIFIED",
                    "llm_eligible": True,
                    "raw_document_sent_to_external_llm": False,
                }
            return list(signals), {
                "official_evidence_vault_enabled": True,
                "official_evidence_vault": {"document_count": 1},
                "official_evidence_vault_items": [],
                "official_evidence_vault_verification": {
                    "technical_status": "READY", "business_status": "BLOCKED", "integrity_status": "OK",
                },
            }

        def enrich(signals, _config, ops=None):
            events.append("llm")
            self.assertEqual(
                signals[0].payload["official_evidence_vault"]["status"],
                "RETAINED_VERIFIED",
            )
            return list(signals), {
                "llm_enabled": True, "llm_candidates": 1, "llm_called": 0,
                "llm_success": 0, "llm_failed": 0, "llm_skipped_no_key": 1,
                "llm_skipped_budget": 0,
            }

        raw = RawSignal(
            source="cninfo",
            source_type="filing",
            title="测试公司重大合同公告",
            summary="重大合同",
            url="https://static.cninfo.com.cn/finalpage/test.pdf",
            published_at=now_iso(),
        )
        config = {
            "run_profile": {"isolated": True},
            "storage": {"sqlite_path": "data/signals.sqlite3"},
            "topics": [{"name": "合同", "symbols": ["测试公司"], "keywords": ["重大合同"], "risk_keywords": []}],
            "priority_rules": {"p0_keywords": [], "p1_keywords": ["重大合同"], "positive_keywords": [], "negative_keywords": [], "review_keywords": []},
            "llm_agent": {"enabled": True, "min_priority": "P3"},
            "multi_role_agent": {"enabled": False},
            "research_cases": {"enabled": False},
            "research_framework": {"enabled": False},
            "research_team": {"enabled": False},
            "research_runtime": {"enabled": False, "auto_sync": False},
            "research_workpapers": {"enabled": False},
            "official_evidence_vault": _vault_config(),
            "report": {"include_low_priority": True},
        }
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            with project_path_override(lambda value: root / str(value)), patch(
                "stock_agent.pipeline.collect_sources", return_value=[raw]
            ), patch("stock_agent.pipeline.attach_evidence_vault_status", side_effect=attach), patch(
                "stock_agent.pipeline.enrich_signals", side_effect=enrich
            ):
                result = run_agent_pipeline(config)
        self.assertEqual(events, ["official_evidence_vault", "llm"])
        self.assertEqual(result.stats["official_evidence_vault"]["document_count"], 1)


class EvidenceOperationsSurfaceTests(unittest.TestCase):
    def test_live_uat_requires_explicit_acknowledgement(self):
        with self.assertRaisesRegex(EvidenceVaultUATError, "--live"):
            run_evidence_vault_uat(
                {"official_evidence_vault": _vault_config()},
                source_url="https://static.cninfo.com.cn/finalpage/test.pdf",
                title="测试公告",
                published_at="2026-08-20T08:00:00+08:00",
                actor="collector",
                actor_role="information_analyst",
                live=False,
            )

    def test_live_uat_uses_temporary_inventory_and_keeps_only_safe_report(self):
        config = {
            "official_evidence_vault": _vault_config(),
            "research_operations": {"enabled": True, "enforce_roster": False},
        }

        def fake_fetch(fetch_config, **kwargs):
            return ingest_official_document_bytes(
                fetch_config,
                b"%PDF-1.7\npublic filing\n%%EOF",
                content_type="application/pdf",
                original_filename="notice.pdf",
                capture_mode="network_fetch",
                source_binding_status="https_transport_verified",
                transport={"http_status": 200},
                **kwargs,
            )

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            with project_path_override(lambda value: root / str(value)):
                result = run_evidence_vault_uat(
                    config,
                    source_url="https://static.cninfo.com.cn/finalpage/test.pdf",
                    title="测试公告",
                    published_at="2026-08-20T08:00:00+08:00",
                    actor="collector",
                    actor_role="information_analyst",
                    signal_ids=["uat-signal"],
                    live=True,
                    fetcher=fake_fetch,
                )
            report = Path(result["evidence_vault_uat_latest_json_path"])
            payload = report.read_text(encoding="utf-8")
            self.assertFalse((root / "data/evidence_vault/events.jsonl").exists())
        self.assertEqual(result["status"], "PASS")
        self.assertTrue(result["production_inventory_unchanged"])
        self.assertFalse(result["raw_document_retained_after_uat"])
        self.assertNotIn("object_path", payload)
        self.assertNotIn(str(root), payload)

    def test_doctor_separates_technical_failure_from_business_block(self):
        config = {"official_evidence_vault": _vault_config()}
        with patch("stock_agent.doctor.verify_evidence_vault", return_value={
            "technical_status": "READY", "business_status": "BLOCKED", "integrity_status": "OK",
            "document_count": 1, "event_count": 2, "business_blockers": ["human archive review pending"],
        }):
            blocked = _check_evidence_vault(config)
        with patch("stock_agent.doctor.verify_evidence_vault", return_value={
            "technical_status": "FAIL", "business_status": "BLOCKED", "integrity_status": "FAIL",
            "document_count": 1, "event_count": 2, "errors": ["object hash mismatch"],
        }):
            failed = _check_evidence_vault(config)
        self.assertEqual(blocked[0], "WARN")
        self.assertIn("human archive review pending", blocked[2])
        self.assertEqual(failed[0], "FAIL")
        self.assertIn("object hash mismatch", failed[2])

    def test_dashboard_exposes_inventory_archive_state_and_no_raw_egress(self):
        html = _section_official_evidence_vault({
            "integrity_status": "OK",
            "summary": {"document_count": 1, "quarantined_count": 0},
            "verification": {
                "technical_status": "READY", "business_status": "BLOCKED",
                "business_blockers": ["production archive review pending"],
            },
            "archive_control": {
                "ready": False, "backend": "local_content_addressed_shadow",
                "approved_review_count": 0, "required_review_count": 2,
            },
            "documents": {
                "ev-1": {
                    "document_id": "ev-1", "status": "captured", "source_domain": "cninfo.com.cn",
                    "published_at": "2026-08-20", "content_type": "application/pdf", "byte_size": 42,
                    "signal_ids": ["sig-1"], "source_binding_status": "operator_attested",
                }
            },
        })
        self.assertIn("READY", html)
        self.assertIn("BLOCKED", html)
        self.assertIn("cninfo.com.cn", html)
        self.assertIn("production archive review pending", html)
        self.assertIn("原始文件字节不会发送给外部 LLM", html)

    def test_daily_and_json_outputs_include_vault_state_without_object_path(self):
        signal = _signal()
        signal.payload["official_evidence_vault"] = {
            "status": "RETAINED_VERIFIED", "llm_eligible": True,
            "document_ids": ["ev-1"], "raw_document_sent_to_external_llm": False,
        }
        stats = {
            "inserted": 1, "duplicates": 0,
            "official_evidence_vault_enabled": True,
            "official_evidence_vault": {"document_count": 1, "captured_count": 1, "quarantined_count": 0},
            "official_evidence_vault_items": [{
                "document_id": "ev-1", "status": "captured", "source_domain": "cninfo.com.cn",
                "published_at": "2026-08-20", "signal_ids": [signal.signal_id],
            }],
            "official_evidence_vault_verification": {
                "technical_status": "READY", "business_status": "BLOCKED", "integrity_status": "OK",
                "production_archive_ready": False, "business_blockers": ["human archive review pending"],
            },
        }
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            with project_path_override(lambda value: root / str(value)):
                report_path, json_path, _alert_path, _alerts = write_outputs(
                    [signal], {"project_name": "A股智能投研系统", "report": {"include_low_priority": True}}, stats
                )
            markdown = Path(report_path).read_text(encoding="utf-8-sig")
            structured = json.loads(Path(json_path).read_text(encoding="utf-8"))
        self.assertIn("Official Evidence Vault", markdown)
        self.assertIn("raw_document_sent_to_external_llm: false", markdown)
        self.assertEqual(structured["official_evidence_vault"]["document_count"], 1)
        self.assertNotIn("object_path", json.dumps(structured["official_evidence_vault_items"]))


class ProductionEvidenceGateTests(unittest.TestCase):
    @staticmethod
    def _scheduled_summary(include_evidence=True):
        names = ["market-cache-seed", "agent-run"]
        if include_evidence:
            names.append("evidence-vault-status")
        names.extend([
            "pi-handoff-run", "review-queue-sync", "research-ops-plan", "workspace-sync",
            "production-pilot-sync", "dashboard", "alerts",
        ])
        return {
            "agent_runtime": "pi", "used_pi": False, "agent_handoff_call_required": False,
            "agent_handoff_public_signal_count": 0, "pi_model_call_count": 0,
            "commands": [{"name": name} for name in names],
        }

    def test_scheduled_sequence_requires_evidence_verification_before_handoff(self):
        self.assertTrue(_scheduled_sequence_status(self._scheduled_summary(True))["valid"])
        missing = _scheduled_sequence_status(self._scheduled_summary(False))
        self.assertFalse(missing["valid"])
        self.assertIn("evidence-vault-status", missing["missing"])

    def test_offline_handoff_is_valid_without_reusing_a_stale_pi_result(self):
        summary = self._scheduled_summary(True)
        names = [item["name"] for item in summary["commands"]]
        names[names.index("pi-handoff-run")] = "agent-handoff"
        summary["commands"] = [{"name": name} for name in names]
        status = _scheduled_sequence_status(summary)
        self.assertTrue(status["valid"])
        self.assertEqual(status["handoff_mode"], "offline_agent_handoff")

    def test_vault_policy_changes_candidate_fingerprint_input(self):
        config = {"official_evidence_vault": _vault_config()}
        registry = {"providers": {}, "active_provider_ids": []}
        operations = {"roster_path": "", "restrictions_path": ""}
        with tempfile.TemporaryDirectory() as tmpdir, project_path_override(lambda value: Path(tmpdir) / str(value)):
            before = _critical_input_hashes(config, "", registry, operations)
            changed = json.loads(json.dumps(config))
            changed["official_evidence_vault"]["max_document_bytes"] = 8 * 1024 * 1024
            after = _critical_input_hashes(changed, "", registry, operations)
        self.assertNotEqual(before["critical_config"], after["critical_config"])
        self.assertIn("official_evidence_vault", before)

    def test_p0_p1_workpaper_without_retained_primary_is_blocked(self):
        config = {
            "official_evidence_vault": _vault_config(),
            "production_pilot": {
                "enabled": True, "enforce": True, "mode": "shadow",
                "minimum_active_providers": 1, "minimum_shadow_runs": 1,
                "minimum_shadow_trading_days": 1, "maximum_unreconciled_runs": 0,
                "automatic_assignment": False, "automatic_approval": False,
                "automatic_activation": False, "automatic_publication": False,
                "automatic_trading": False,
            },
        }
        cfg = production_pilot_config(config)
        base = {
            "provider_registry": {"integrity_status": "OK", "active_provider_ids": ["provider-1"]},
            "roster": {"ready": True, "ready_role_count": 1, "required_role_count": 1, "missing_roles": []},
            "market": {"status": "OK", "pass_count": 1, "blocked_count": 0, "llm_eligible_count": 1},
            "llm": {"status": "OK", "summary": {"pending_exception_count": 0}},
            "evidence_vault": {"technical_status": "READY", "integrity_status": "OK", "document_count": 1, "event_count": 1},
            "workpaper_projection": {"workpapers": [{
                "case": {"priority": "P1"},
                "source_records": [{"primary_source_eligible": True, "original_document_retained": False}],
            }]},
            "workpapers": {"technical_status": "READY", "integrity_status": "OK", "workpaper_count": 1, "production_archive_ready": True},
            "review_capacity": {"eligible_human_reviewer_count": 1, "outstanding_count": 0, "overdue_count": 0, "critical_unassigned_count": 0, "critical_overdue_count": 0},
            "scheduled": dict(self._scheduled_summary(True), exit_code=0),
            "scheduled_path": "logs/scheduled/run.json",
            "model": {"status": "PASS", "breach_count": 0, "rolling": {"total_tokens": 1}, "calls": [], "checks": []},
            "incidents": {"integrity_status": "OK", "open_critical_count": 0, "open_high_count": 0, "open_count": 0},
        }
        reconciliation = {
            "reconciled": True, "missing_artifacts": [], "hash_errors": [], "stale_artifacts": [],
            "scheduled_report_match": True, "scheduled_handoff_match": True,
        }
        control = {"integrity_status": "OK", "approvals": {}, "activation": {}}
        shadow = {"qualifying_run_count": 1, "qualifying_trading_day_count": 1, "unreconciled_run_count": 0}
        run_log = {"integrity_status": "OK"}
        blocked = _build_gates(config, cfg, base, reconciliation, control, shadow, run_log)
        gate = next(item for item in blocked if item["gate"] == "official_evidence_coverage")
        self.assertEqual(gate["status"], "BLOCKED")
        base["workpaper_projection"]["workpapers"][0]["source_records"][0]["original_document_retained"] = True
        passed = _build_gates(config, cfg, base, reconciliation, control, shadow, run_log)
        gate = next(item for item in passed if item["gate"] == "official_evidence_coverage")
        self.assertEqual(gate["status"], "PASS")


if __name__ == "__main__":
    unittest.main()
