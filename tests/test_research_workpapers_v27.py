from __future__ import annotations

import copy
import json
import tempfile
import unittest
from pathlib import Path

from stock_agent.audit_log import load_hash_chained_jsonl
from stock_agent.config import project_path_override
from stock_agent.research_workpapers import (
    ResearchWorkpaperError,
    build_research_workpaper,
    load_research_workpapers,
    refresh_workpaper_from_runtime_action,
    research_workpaper_config,
    sync_research_workpapers,
    validate_research_workpaper,
    validate_workpaper_gate,
    validate_workpaper_policy,
    verify_research_workpapers,
)


def _config(archive=False):
    return {
        "storage": {"sqlite_path": "data/signals.sqlite3"},
        "research_workpapers": {
            "enabled": True,
            "enforce": True,
            "events_path": "data/research_workpapers/events.jsonl",
            "projection_path": "data/research_workpapers/latest.json",
            "report_dir": "reports/research_workpapers",
            "retention_years": 5,
            "require_official_primary_for": ["P0", "P1"],
            "require_exact_evidence_refs": True,
            "require_market_governance_pass": True,
            "require_distinct_human_roles": True,
            "require_independent_challenge": True,
            "archive_backend": "approved_worm" if archive else "local_append_only_shadow",
            "production_archive_approved": archive,
            "production_archive_evidence_ref": "archive-control:2026" if archive else "",
            "automatic_approval": False,
            "automatic_assignment": False,
            "automatic_publication": False,
            "automatic_trading": False,
        },
        "pi_agent": {"compiled_skills_enabled": False},
    }


def _case(priority="P1", signal_ids=None):
    return {
        "case_id": "case-v27",
        "title": "测试公司重大合同",
        "entity": "688888",
        "topic": "AI服务器",
        "catalyst_path": "filing:major_contract",
        "top_priority": priority,
        "status": "active",
        "signal_ids": signal_ids or ["sig-v27"],
        "created_at": "2026-08-20T09:00:00+08:00",
        "case_analysis": {
            "hypotheses": [{"name": "base", "thesis": "订单可能支持收入", "evidence_for": ["filing"]}],
            "counter_questions": ["合同能否按期执行？"],
        },
    }


def _signal(url="https://www.cninfo.com.cn/new/disclosure/detail?plate=sse&id=1"):
    return {
        "signal_id": "sig-v27",
        "source": "cninfo",
        "source_type": "filing",
        "title": "测试公司重大合同公告",
        "summary": "公司披露重大合同，金额与执行期仍需核对原文。",
        "url": url,
        "published_at": "2026-08-20T08:00:00+08:00",
        "collected_at": "2026-08-20T08:10:00+08:00",
        "priority": "P1",
        "payload": {
            "filing_evidence": {
                "status": "ok",
                "published_at": "2026-08-20T08:00:00+08:00",
                "report_period": "2026H2",
                "key_points": ["公司披露重大合同"],
                "metrics": {"contract_amount": {"value": 3000000000, "unit": "CNY", "currency": "CNY"}},
            }
        },
        "llm_analysis": {
            "impact_direction": "positive",
            "confidence": 0.72,
            "horizon": "1-4w",
            "key_thesis": "合同可能改善收入预期，但执行与毛利率仍需验证。",
            "evidence_refs": ["summary", "evidence.filing_evidence"],
            "valid_evidence_refs": ["summary", "evidence.filing_evidence"],
            "unsupported_evidence_refs": [],
            "grounding_status": "grounded",
            "risk_points": ["执行延期", "毛利率不确定"],
            "review_questions": ["合同是否存在撤销条款？"],
            "human_decision_needed": True,
            "model_execution": {
                "runtime": "pi",
                "provider": "deepseek",
                "model": "deepseek-v4-pro",
                "request_id": "req-v27",
                "system_fingerprint": "pi:0.84.2:event-v27",
                "pi_audit_event_id": "event-v27",
            },
        },
        "role_analyses": [{
            "role_id": "risk_guard",
            "thesis": "执行不确定性可能削弱合同的业绩贡献。",
            "confidence": 0.6,
            "evidence_refs": ["summary"],
            "valid_evidence_refs": ["summary"],
            "unsupported_evidence_refs": [],
            "grounding_status": "grounded",
            "risk_points": ["执行延期"],
            "review_questions": ["回款节点是什么？"],
            "model_execution": {
                "runtime": "pi",
                "provider": "deepseek",
                "model": "deepseek-v4-pro",
                "system_fingerprint": "pi:0.84.2:role-event-v27",
                "pi_audit_event_id": "role-event-v27",
            },
        }],
        "role_synthesis": {"conflict_level": "medium", "key_disagreement": "收入贡献节奏"},
        "llm_governance": {
            "signal_llm": {
                "audit_id": "llmgov:v27",
                "classification": "public",
                "decision": "approved",
                "payload_sha256": "a" * 64,
                "postflight": {"status": "grounded"},
            }
        },
    }


def _runtime(stage="analysis", challenge=False):
    override = {}
    if challenge:
        override = {
            "independent_challenge": {
                "status": "completed",
                "actor": "risk.human",
                "actor_role": "risk_reviewer",
                "completed_at": "2026-08-20T12:00:00+08:00",
                "references": ["risk_note:case-v27:v1"],
                "notes": "已核对反证。",
            }
        }
    return {
        "case_id": "case-v27",
        "stage": stage,
        "status": "active",
        "assignments": {
            "lead_analyst": "lead.human",
            "quality_reviewer": "quality.human",
            "compliance_officer": "compliance.human",
            "publication_approver": "publisher.human",
        },
        "approvals": {
            "quality_review": {"status": "pending"},
            "compliance_review": {"status": "pending"},
            "publication": {"status": "pending"},
        },
        "task_overrides": override,
        "action_history": [],
    }


class ResearchWorkpapersV27Tests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.config = _config()
        self.override = project_path_override(lambda value: self.root / str(value))
        self.override.__enter__()

    def tearDown(self):
        self.override.__exit__(None, None, None)
        self.temp.cleanup()

    def _sync(self, signal=None, case=None, runtime=None):
        return sync_research_workpapers(
            self.config,
            cases=[case or _case()],
            signals=[signal or _signal()],
            runtime_cases=[runtime or _runtime()],
        )

    def test_sync_creates_hash_chained_projection_and_reports(self):
        result = self._sync()
        projection = load_research_workpapers(self.config)
        workpaper = projection["workpapers"][0]
        self.assertEqual(result["research_workpaper_created_count"], 1)
        self.assertEqual(projection["integrity_status"], "OK")
        self.assertEqual(projection["event_count"], 1)
        self.assertTrue(Path(result["research_workpaper_latest_path"]).exists())
        self.assertEqual(workpaper["schema_version"], "27.0")
        self.assertEqual(workpaper["case_id"], "case-v27")
        self.assertEqual(workpaper["author"]["principal_type"], "service")
        self.assertFalse(workpaper["automatic_trading_executed"])

    def test_repeat_sync_is_idempotent(self):
        first = self._sync()
        second = self._sync()
        self.assertEqual(first["research_workpaper_created_count"], 1)
        self.assertEqual(second["research_workpaper_created_count"], 0)
        self.assertEqual(second["research_workpaper_unchanged_count"], 1)
        self.assertEqual(load_research_workpapers(self.config)["event_count"], 1)

    def test_changed_source_creates_new_immutable_version(self):
        self._sync()
        changed = _signal()
        changed["summary"] = "公司披露重大合同，交付期调整。"
        result = self._sync(signal=changed)
        projection = load_research_workpapers(self.config)
        self.assertEqual(result["research_workpaper_created_count"], 1)
        self.assertEqual(projection["event_count"], 2)
        self.assertEqual(projection["workpapers"][0]["version"], 2)

    def test_tampered_event_chain_fails_closed(self):
        self._sync()
        path = self.root / "data" / "research_workpapers" / "events.jsonl"
        line = json.loads(path.read_text(encoding="utf-8"))
        line["workpaper"]["case"]["title"] = "tampered"
        path.write_text(json.dumps(line) + "\n", encoding="utf-8")
        result = verify_research_workpapers(self.config)
        self.assertEqual(result["integrity_status"], "FAIL")
        self.assertEqual(result["technical_status"], "FAIL")
        with self.assertRaisesRegex(ResearchWorkpaperError, "hash chain"):
            validate_workpaper_gate("case-v27", "quality_approve", self.config)

    def test_p1_secondary_aggregator_blocks_approval_but_not_technical_integrity(self):
        self._sync(signal=_signal("https://data.eastmoney.com/notices/detail/688888/abc.html"))
        result = verify_research_workpapers(self.config)
        workpaper = load_research_workpapers(self.config)["workpapers"][0]
        self.assertEqual(result["technical_status"], "READY")
        self.assertEqual(result["business_status"], "BLOCKED")
        self.assertIn("P1 workpaper requires an official primary source", workpaper["validation"]["approval_blockers"])
        self.assertEqual(workpaper["source_records"][0]["authority_tier"], "secondary_aggregator")

    def test_claims_separate_source_observations_and_model_inferences(self):
        self._sync()
        workpaper = load_research_workpapers(self.config)["workpapers"][0]
        types = {item["claim_type"] for item in workpaper["claims"]}
        self.assertIn("source_statement", types)
        self.assertIn("data_observation", types)
        self.assertIn("model_inference", types)
        self.assertTrue(workpaper["validation"]["facts_and_inferences_separated"])
        inference = next(item for item in workpaper["claims"] if item["claim_type"] == "model_inference")
        self.assertEqual(inference["support_status"], "grounded_in_input")
        self.assertTrue(inference["evidence_refs"])

    def test_source_record_has_exact_locator_hash_and_source_tier(self):
        self._sync()
        source = load_research_workpapers(self.config)["workpapers"][0]["source_records"][0]
        self.assertEqual(source["source_locator"], "sqlite:signals:sig-v27")
        self.assertEqual(len(source["content_sha256"]), 64)
        self.assertTrue(source["primary_source_eligible"])
        self.assertEqual(source["authority_tier"], "official_disclosure_platform")

    def test_market_claim_is_blocked_without_attestation_and_governance_pass(self):
        signal = _signal("")
        signal.update({"source": "eastmoney", "source_type": "price", "title": "行情快照"})
        signal["payload"] = {
            "latest": 18.2,
            "pct_change": 3.5,
            "quote_timestamp": "2026-08-20T10:00:00+08:00",
            "market_data_governance": {
                "status": "BLOCKED",
                "provider_internally_attested": False,
                "reason_codes": ["provider_not_internally_attested"],
            },
        }
        signal["llm_analysis"] = {}
        signal["role_analyses"] = []
        self._sync(signal=signal, case=_case(priority="P2"))
        workpaper = load_research_workpapers(self.config)["workpapers"][0]
        market_claims = [item for item in workpaper["claims"] if item["claim_type"] == "data_observation"]
        self.assertTrue(market_claims)
        self.assertTrue(all(item["support_status"] == "blocked" for item in market_claims))
        self.assertFalse(workpaper["validation"]["approval_eligible"])

    def test_passed_attested_market_snapshot_is_approval_eligible_for_p2(self):
        signal = _signal("")
        signal.update({"source": "licensed_vendor", "source_type": "price", "title": "行情快照"})
        signal["payload"] = {
            "latest": 18.2,
            "pct_change": 3.5,
            "quote_timestamp": "2026-08-20T10:00:00+08:00",
            "market_data_governance": {
                "status": "PASS",
                "provider_internally_attested": True,
                "reason_codes": [],
            },
        }
        signal["llm_analysis"] = {}
        signal["role_analyses"] = []
        self._sync(signal=signal, case=_case(priority="P2"))
        workpaper = load_research_workpapers(self.config)["workpapers"][0]
        market_claims = [item for item in workpaper["claims"] if item["claim_type"] == "data_observation"]
        self.assertTrue(all(item["support_status"] == "verified_for_internal_research" for item in market_claims))
        self.assertTrue(workpaper["validation"]["approval_eligible"])

    def test_model_execution_links_pi_audit_event(self):
        self._sync()
        methods = load_research_workpapers(self.config)["workpapers"][0]["methods"]
        event_ids = {item["pi_audit_event_id"] for item in methods["model_executions"]}
        self.assertIn("event-v27", event_ids)
        self.assertIn("role-event-v27", event_ids)
        self.assertFalse(methods["dynamic_skills_enabled"])
        self.assertFalse(methods["tools_enabled"])

    def test_unavailable_signal_preserves_prior_version(self):
        self._sync()
        result = sync_research_workpapers(
            self.config,
            cases=[_case(signal_ids=["sig-v27", "missing-signal"])],
            signals=[_signal()],
            runtime_cases=[_runtime()],
        )
        self.assertEqual(result["research_workpaper_preserved_count"], 1)
        self.assertEqual(load_research_workpapers(self.config)["event_count"], 1)

    def test_unsafe_automatic_flags_are_rejected(self):
        config = _config()
        config["research_workpapers"]["automatic_approval"] = True
        self.assertIn("research_workpapers.automatic_approval must be false", validate_workpaper_policy(config))
        self.assertFalse(research_workpaper_config(config)["automatic_approval"])

    def test_enforced_control_policy_cannot_be_silently_weakened(self):
        config = _config()
        config["research_workpapers"].update({
            "require_exact_evidence_refs": False,
            "require_market_governance_pass": False,
            "require_distinct_human_roles": False,
            "require_independent_challenge": False,
            "require_official_primary_for": ["P2"],
            "retention_years": 4,
        })
        errors = validate_workpaper_policy(config)
        self.assertTrue(any("retention" in item for item in errors))
        self.assertTrue(any("exact retained evidence" in item for item in errors))
        self.assertTrue(any("market-data governance PASS" in item for item in errors))
        self.assertTrue(any("distinct protected-gate" in item for item in errors))
        self.assertTrue(any("independent human challenge" in item for item in errors))
        self.assertTrue(any("P0, P1" in item for item in errors))

    def test_disabled_verification_is_explicit_and_does_not_readiness_fail(self):
        result = verify_research_workpapers({})
        self.assertEqual(result["technical_status"], "DISABLED")
        self.assertEqual(result["business_status"], "DISABLED")
        self.assertEqual(result["integrity_status"], "DISABLED")
        self.assertEqual(result["warnings"], [])

    def test_local_shadow_archive_blocks_business_readiness_only(self):
        self._sync()
        result = verify_research_workpapers(self.config)
        self.assertEqual(result["technical_status"], "READY")
        self.assertEqual(result["business_status"], "BLOCKED")
        self.assertFalse(result["production_archive_ready"])

    def test_approved_archive_requires_retained_evidence_reference(self):
        config = _config(archive=True)
        config["research_workpapers"]["production_archive_evidence_ref"] = ""
        self.assertIn("approved production archive requires a retained evidence reference", validate_workpaper_policy(config))

    def test_quality_gate_requires_existing_workpaper(self):
        with self.assertRaisesRegex(ResearchWorkpaperError, "does not exist"):
            validate_workpaper_gate("case-v27", "quality_approve", self.config)

    def test_quality_gate_requires_independent_human_challenge(self):
        self._sync(runtime=_runtime(stage="quality_review", challenge=False))
        with self.assertRaisesRegex(ResearchWorkpaperError, "independent human challenge"):
            validate_workpaper_gate("case-v27", "quality_approve", self.config)

    def test_quality_gate_allows_grounded_workpaper_after_human_challenge(self):
        self._sync(runtime=_runtime(stage="quality_review", challenge=True))
        result = validate_workpaper_gate("case-v27", "quality_approve", self.config)
        self.assertTrue(result["allowed"])

    def test_publication_gate_requires_real_approved_archive(self):
        runtime = _runtime(stage="human_approval", challenge=True)
        runtime["approvals"]["quality_review"] = {
            "status": "approved", "actor": "quality.human", "actor_role": "quality_reviewer"
        }
        runtime["approvals"]["compliance_review"] = {
            "status": "approved", "actor": "compliance.human", "actor_role": "compliance_officer"
        }
        self._sync(runtime=runtime)
        with self.assertRaisesRegex(ResearchWorkpaperError, "approved production archive"):
            validate_workpaper_gate("case-v27", "publication_approve", self.config)

    def test_automated_protected_approval_is_technical_failure(self):
        runtime = _runtime(stage="compliance_review", challenge=True)
        runtime["approvals"]["quality_review"] = {
            "status": "approved", "actor": "pi-agent", "actor_role": "quality_reviewer"
        }
        workpaper = build_research_workpaper(_case(), [_signal()], runtime, self.config)
        validation = validate_research_workpaper(workpaper, self.config)
        self.assertEqual(validation["status"], "FAIL")
        self.assertTrue(any("named human actor" in item for item in validation["technical_errors"]))

    def test_role_separation_is_validated(self):
        runtime = _runtime(stage="compliance_review", challenge=True)
        runtime["assignments"]["lead_analyst"] = "same.human"
        runtime["approvals"]["quality_review"] = {
            "status": "approved", "actor": "same.human", "actor_role": "quality_reviewer"
        }
        workpaper = build_research_workpaper(_case(), [_signal()], runtime, self.config)
        self.assertTrue(any("must be distinct" in item for item in workpaper["validation"]["technical_errors"]))

    def test_runtime_action_refresh_creates_linked_revision(self):
        self._sync()
        runtime = _runtime(stage="challenge", challenge=True)
        action = {
            "action_id": "runtime-action-v27",
            "action": "complete_task",
            "actor": "risk.human",
            "actor_role": "risk_reviewer",
            "principal_type": "human",
        }
        result = refresh_workpaper_from_runtime_action(runtime, action, self.config)
        projection = load_research_workpapers(self.config)
        chain = load_hash_chained_jsonl(self.root / "data" / "research_workpapers" / "events.jsonl")
        self.assertTrue(result["updated"])
        self.assertEqual(projection["workpapers"][0]["version"], 2)
        self.assertEqual(chain["records"][-1]["runtime_action_id"], "runtime-action-v27")

    def test_report_projection_does_not_replace_event_source_of_truth(self):
        self._sync()
        projection_path = self.root / "data" / "research_workpapers" / "latest.json"
        projection_path.write_text("{}", encoding="utf-8")
        reloaded = load_research_workpapers(self.config)
        self.assertEqual(len(reloaded["workpapers"]), 1)
        self.assertEqual(reloaded["integrity_status"], "OK")


if __name__ == "__main__":
    unittest.main()
