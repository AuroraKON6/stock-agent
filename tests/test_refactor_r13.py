"""R13: approved fact bundles — host-policy egress, re-verified use, payload
hash binding.

Acceptance from GLM_REFACTOR_TASKBOOK_20261020 (A60–A63):
- A60: a fact whose source's external-send right is denied by the HOST
  registry is excluded from the bundle with a recorded reason — and never
  reaches the model context.
- A61: a payload that self-reports ``classification=public`` gains nothing:
  the host registry decision stands; the self-report is recorded as ignored
  data, never as the basis.
- A62: a bundle bound to evidence that left the verified vault projection is
  INVALID on re-verification — stale permissions never keep working.
- A63: the worker's fact bundle records the EXACT outbound payload hash; the
  model_call delivery shows the same hash for the context actually sent.
"""
import json
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from stock_agent.config import project_path, project_path_override
from stock_agent.evidence_context import verified_evidence_documents
from stock_agent.evidence_excerpts import excerpt_selector
from stock_agent.fact_bundle import (
    FactBundleError,
    bind_outbound_payload,
    build_research_fact_bundle,
    verify_research_fact_bundle,
)
from stock_agent.task_runtime import TaskStore
from stock_agent.task_supply import build_task_spec

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _vault_fixture import import_document, vault_config

NOW = datetime.now(timezone.utc)


def registry_entry(source_id, *, external_send=True, state="approved"):
    return {
        "source_id": source_id, "kind": "price", "upstream": source_id,
        "purpose": "test", "purposes": ["research"],
        "approval": {"state": state,
                     "approved_by": "真实负责人" if state == "approved" else "",
                     "approved_at": "2026-10-01T00:00:00" if state == "approved" else ""},
        "body_retention_allowed": True,
        "llm_external_send_allowed": external_send,
        "timeliness": {"expected_max_delay_minutes": 1440, "staleness_policy": "flag"},
        "rate_limit": {"max_requests_per_minute": 60, "max_requests_per_day": 10},
        "expires_at": "2099-12-31",
    }


class R13TestCase(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.enterContext(project_path_override(lambda value: self.root / value))
        self.config = vault_config(self.root, {})
        self.body = "\ufeff标题\n营业收入为 150.0 亿元。\n研发费用为（300.00）万元。"
        self.doc_id, self.sha = import_document(
            self.config, self.root, content=self.body, title="甲公司 2024 年年度报告")
        from stock_agent.evidence_excerpts import create_excerpt

        document = verified_evidence_documents(self.config)[self.sha]
        text = project_path(document["object_path"]).read_bytes().decode("utf-8-sig")
        self.excerpt = create_excerpt(self.config, self.doc_id,
                                      text.index("营业收入"),
                                      text.index("。", text.index("营业收入")) + 1)
        self.selector = excerpt_selector(self.excerpt)
        self.task = {"task_id": "task_r13", "input_version": 3}

    def _write_registry(self, *entries):
        path = self.root / "config" / "source_registry.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"schema_version": "1.0",
                                    "entries": list(entries)}, ensure_ascii=False),
                        encoding="utf-8")
        self.config = dict(self.config, source_registry={"path": str(path)})
        return self.config


class BundleBuildTests(R13TestCase):
    def test_excerpt_facts_deliver_with_locator_and_binding(self):
        bundle = build_research_fact_bundle(
            self.config, self.task, fact_selectors=[self.selector])
        self.assertEqual(len(bundle["facts"]), 1)
        fact = bundle["facts"][0]
        self.assertIn("150.0", fact["content"])
        self.assertEqual(fact["document_sha256"], self.sha)
        self.assertIn("extractor_version", fact["locator"])
        self.assertEqual(bundle["input_version"], 3)
        self.assertEqual(bundle["evidence_documents"][0]["sha256"], self.sha)
        self.assertTrue(bundle["bundle_sha256"])

    def test_a60_registry_denied_source_excluded_with_reason(self):
        self._write_registry(registry_entry("kimi_x", external_send=False))
        bundle = build_research_fact_bundle(
            self.config, self.task,
            extra_facts=[{"fact_id": "f1", "source_id": "kimi_x",
                          "statement": "不该外发的事实"}])
        self.assertEqual(bundle["facts"], [])
        self.assertEqual(len(bundle["excluded"]), 1)
        self.assertEqual(bundle["excluded"][0]["reason"], "external_send_not_allowed")

    def test_a61_self_reported_public_grants_nothing(self):
        self._write_registry(registry_entry("kimi_x", external_send=False))
        bundle = build_research_fact_bundle(
            self.config, self.task,
            extra_facts=[{"fact_id": "f1", "source_id": "kimi_x",
                          "classification": "public",  # self-report
                          "statement": "自称 public 的事实"}])
        self.assertEqual(bundle["facts"], [],
                         "the host registry decision stands; the self-report is not a basis")
        self.assertEqual(bundle["excluded"][0]["reason"], "external_send_not_allowed")
        self.assertTrue(bundle["policy"]["self_reported_classification_ignored"])

    def test_registry_allowed_fact_delivers_and_records_ignored_self_report(self):
        self._write_registry(registry_entry("kimi_ok", external_send=True))
        bundle = build_research_fact_bundle(
            self.config, self.task,
            extra_facts=[{"fact_id": "f2", "source_id": "kimi_ok",
                          "classification": "internal",
                          "statement": "已获外发权的事实"}])
        self.assertEqual(len(bundle["facts"]), 1)
        self.assertEqual(bundle["facts"][0]["egress_basis"], "registry_external_send_allowed")
        self.assertEqual(bundle["facts"][0].get("self_reported_class_ignored"), "internal")

    def test_unregistered_fact_record_has_no_host_basis(self):
        bundle = build_research_fact_bundle(
            self.config, self.task,
            extra_facts=[{"fact_id": "f3", "statement": "无来源登记"}])
        self.assertEqual(bundle["facts"], [])
        self.assertEqual(bundle["excluded"][0]["reason"], "no_host_policy_basis")

    def test_empty_bundle_rejected(self):
        with self.assertRaises(FactBundleError):
            build_research_fact_bundle(self.config, self.task)


class BundleReverificationTests(R13TestCase):
    def test_a62_revoked_evidence_invalidates_bundle(self):
        bundle = build_research_fact_bundle(
            self.config, self.task, fact_selectors=[self.selector])
        self.assertTrue(verify_research_fact_bundle(self.config, bundle)["valid"])
        # The document leaves the verified projection (e.g. revocation): the
        # SAME bundle must stop working — stale permissions never persist.
        import stock_agent.fact_bundle as fb
        original = fb.verified_evidence_documents
        fb.verified_evidence_documents = lambda config: {}
        try:
            verdict = verify_research_fact_bundle(self.config, bundle)
        finally:
            fb.verified_evidence_documents = original
        self.assertFalse(verdict["valid"])
        self.assertEqual(verdict["reason"], "evidence_revoked_or_unverified")

    def test_a62_lapsed_registry_right_invalidates_bundle(self):
        self._write_registry(registry_entry("kimi_ok", external_send=True))
        bundle = build_research_fact_bundle(
            self.config, self.task,
            extra_facts=[{"fact_id": "f2", "source_id": "kimi_ok", "statement": "s"}])
        self.assertTrue(verify_research_fact_bundle(self.config, bundle)["valid"])
        # Right lapsed later: the previously valid bundle is now invalid.
        self._write_registry(registry_entry("kimi_ok", external_send=False))
        verdict = verify_research_fact_bundle(self.config, bundle)
        self.assertFalse(verdict["valid"])
        self.assertEqual(verdict["reason"], "registry_right_lapsed")

    def test_tampered_bundle_hash_rejected(self):
        bundle = build_research_fact_bundle(
            self.config, self.task, fact_selectors=[self.selector])
        tampered = dict(bundle, facts=bundle["facts"] + [{"kind": "smuggled"}])
        verdict = verify_research_fact_bundle(self.config, tampered)
        self.assertFalse(verdict["valid"])
        self.assertEqual(verdict["reason"], "bundle_hash_mismatch")

    def test_a63_payload_hash_binding(self):
        bundle = build_research_fact_bundle(
            self.config, self.task, fact_selectors=[self.selector])
        payload = {"quote_snapshots": {}, "evidence_summaries": {}}
        bound = bind_outbound_payload(bundle, payload)
        self.assertTrue(bound["outbound_payload_sha256"])
        other = bind_outbound_payload(bundle, {"different": True})
        self.assertNotEqual(bound["outbound_payload_sha256"],
                            other["outbound_payload_sha256"])


class WorkerBundleTests(R13TestCase):
    """A63 worker half: the model_call audit carries the bundle and the exact
    outbound payload hash for the context the model actually received."""

    def setUp(self):
        super().setUp()
        mdg = self.config.setdefault("market_data_governance", {})
        mdg["approved_internal_research_providers"] = ["eastmoney"]
        mdg["use_production_pilot_provider_registry"] = False
        mdg["require_consensus_for_llm"] = False
        mdg["allow_degraded_offhours_llm"] = True
        self.store = TaskStore(self.root / "tasks.sqlite3")
        self.addCleanup(self.store.close)

    def _run_replay(self, payload_extra):
        spec = build_task_spec(research_question="R13 事实包", symbols=["600519"],
                               execution_mode="replay", config=self.config)
        spec["bound_signal_ids"] = ["sig-test-0001"]
        spec["public_context"]["evidence_summaries"] = {
            self.doc_id: {"summary": "营业收入为 150.0 亿元。", "sha256": self.sha}}
        spec["public_context"]["quote_snapshots"] = {}
        spec["replay_response"] = {
            "schema_version": "1.0", "task_id": "placeholder",
            "summary": "基于正文事实的结论。", "key_findings": [], "unknowns": [],
            "evidence_refs": [], "human_questions": [],
            "model_self_assessed_confidence": 0.4}
        spec.update(payload_extra)
        task, _ = self.store.create_task(title="R13", input_payload=spec,
                                         execution_mode="replay")
        claimed = self.store.claim_next("w-r13")
        from stock_agent.research_worker import run_research_task

        result = run_research_task(self.store, task["task_id"], worker_id="w-r13",
                                   config=self.config)
        stored = self.store.get_task(task["task_id"])
        return result, stored

    def test_model_call_carries_bundle_and_matching_payload_hash(self):
        result, stored = self._run_replay({"approved_facts": [self.selector]})
        self.assertEqual(result["status"], "succeeded", result.get("error"))
        model_call = stored["model_call"]
        bundle = model_call.get("fact_bundle") or {}
        self.assertTrue(bundle.get("bundle_sha256"))
        # A63: the bundle's payload hash equals the delivered context hash.
        self.assertEqual(bundle.get("outbound_payload_sha256"),
                         model_call["context_delivery"].get("outbound_sha256"))
        # The delivered facts ride the minimized context with content bounded.
        outbound = model_call.get("outbound_context") or {}
        facts = outbound.get("verified_facts") or []
        self.assertEqual(len(facts), 1)
        self.assertIn("150.0", facts[0]["content"])

    def test_no_facts_no_bundle(self):
        result, stored = self._run_replay({})
        self.assertEqual(result["status"], "succeeded")
        self.assertNotIn("fact_bundle", stored["model_call"])
        self.assertNotIn("verified_facts", stored["model_call"].get("outbound_context") or {})


if __name__ == "__main__":
    unittest.main()
