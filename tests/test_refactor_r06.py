"""R06: research-output quality contract — citations for specific claims.

Acceptance scenarios from GLM_REFACTOR_TASKBOOK_20261020 (A30–A33): an
uncited specific (numeric) claim can never complete a research task (A30);
uncited gap statements remain saveable (A31); cited numbers missing from the
delivered context are flagged for rule/human review (A32); perturbations —
deleting citations or mutating numbers/dates — drop the quality score or flip
the verdict (A33). Worker-level tests run offline in replay mode through the
real TaskStore path.
"""
import json
import sys
import tempfile
import unittest
from pathlib import Path

from stock_agent.config import project_path_override
from stock_agent.research_quality import (
    VERDICT_COMPLETE_WITH_FLAGS,
    VERDICT_INSUFFICIENT_EVIDENCE,
    VERDICT_OK,
    assess_draft,
    context_text_for_quality,
    is_inference,
    numeric_tokens,
)
from stock_agent.task_runtime import TaskStore
from stock_agent.task_supply import build_task_spec

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _vault_fixture import vault_config


def _draft(*, summary="研究草稿：基于已投递上下文。", findings=(), refs=(), unknowns=(),
           inferences=()):
    return {
        "schema_version": "1.0", "task_id": "placeholder", "summary": summary,
        "key_findings": list(findings), "unknowns": list(unknowns),
        "evidence_refs": list(refs), "human_questions": [],
        "model_self_assessed_confidence": 0.5,
        **({"inferences": list(inferences)} if inferences else {}),
    }


CONTEXT = context_text_for_quality({
    "evidence_summaries": {
        "ev-0001": {"summary": "2024 年度营业收入 150.0 亿元，同比上升 20%。"},
    },
    "quote_snapshots": {"600519": {"price": 1500.0, "pct_change": 1.2}},
})


class ClaimClassificationTests(unittest.TestCase):
    def test_numbers_make_a_claim_specific(self):
        self.assertTrue(numeric_tokens("营收 150.0 亿元"))
        self.assertTrue(numeric_tokens("2024-10-09 披露"))
        self.assertFalse(numeric_tokens("业绩显著改善"))

    def test_inference_markers_exempt_claims(self):
        self.assertTrue(is_inference("预计营收将增长 20%"))
        self.assertTrue(is_inference("Hypothesis: margin expands"))
        self.assertFalse(is_inference("营收 150.0 亿元"))


class A30A31Tests(unittest.TestCase):
    def test_a30_uncited_specific_claim_blocks_completion(self):
        report = assess_draft(
            _draft(findings=["营业收入为 150.0 亿元"], refs=[]),
            context_text=CONTEXT)
        self.assertEqual(report["verdict"], VERDICT_INSUFFICIENT_EVIDENCE)
        self.assertEqual(len(report["unsupported_specific_claims"]), 1)
        self.assertLess(report["score"], 100)

    def test_a31_uncited_gap_statement_is_saveable(self):
        report = assess_draft(
            _draft(findings=["业绩改善的驱动因素尚不明确"],
                   unknowns=["无官方原件确认收入口径"], refs=[]),
            context_text=CONTEXT)
        self.assertEqual(report["verdict"], VERDICT_OK,
                         "gap statements assert nothing; no citation demanded")

    def test_cited_specific_claim_with_matching_number_passes(self):
        report = assess_draft(
            _draft(findings=["营业收入为 150.0 亿元"], refs=["ev-0001"]),
            context_text=CONTEXT)
        self.assertEqual(report["verdict"], VERDICT_OK)
        self.assertEqual(report["score"], 100)

    def test_explicit_inference_list_is_exempt_but_marked(self):
        report = assess_draft(
            _draft(findings=["增长可能持续到下一季度"], refs=[],
                   inferences=["增长可能持续到下一季度"]),
            context_text=CONTEXT)
        self.assertEqual(report["verdict"], VERDICT_OK)
        self.assertEqual(len(report["inference_marked"]), 1,
                         "exemptions stay visible in the report")


class A32NumberMatchTests(unittest.TestCase):
    def test_cited_number_missing_from_context_is_flagged_not_trusted(self):
        report = assess_draft(
            _draft(findings=["营业收入为 160.0 亿元"], refs=["ev-0001"]),
            context_text=CONTEXT)
        self.assertEqual(report["verdict"], VERDICT_COMPLETE_WITH_FLAGS)
        self.assertEqual(len(report["number_mismatch"]), 1)
        self.assertEqual(report["number_mismatch"][0]["unmatched_number"], "160.0")
        self.assertIn("人工复核", report["review_note"])

    def test_mutated_date_is_flagged(self):
        report = assess_draft(
            _draft(findings=["2024-10-08 披露年报"], refs=["ev-0001"]),
            context_text=CONTEXT)
        unmatched = {item["unmatched_number"] for item in report["number_mismatch"]}
        # The date splits into tokens 2024/10/08; "2024" alone matching does
        # not launder a mutated date — the absent tokens must be flagged.
        self.assertTrue(unmatched, "the mutated date tokens are not all in the delivered context")
        self.assertTrue(unmatched & {"10", "08"},
                        "expected the day/month tokens to be the flagged ones")
        self.assertLess(report["score"], 100)

    def test_quote_numbers_count_as_delivered_context(self):
        report = assess_draft(
            _draft(findings=["收盘价 1500.0，涨幅 1.2"], refs=["ev-0001"]),
            context_text=CONTEXT)
        self.assertEqual(report["verdict"], VERDICT_OK)


class A33PerturbationTests(unittest.TestCase):
    def test_deleting_citations_flips_verdict_and_drops_score(self):
        baseline = assess_draft(
            _draft(findings=["营业收入为 150.0 亿元"], refs=["ev-0001"]),
            context_text=CONTEXT)
        perturbed = assess_draft(
            _draft(findings=["营业收入为 150.0 亿元"], refs=[]),
            context_text=CONTEXT)
        self.assertEqual(baseline["verdict"], VERDICT_OK)
        self.assertEqual(perturbed["verdict"], VERDICT_INSUFFICIENT_EVIDENCE)
        self.assertLess(perturbed["score"], baseline["score"])

    def test_mutating_numbers_drops_score(self):
        baseline = assess_draft(
            _draft(findings=["营业收入为 150.0 亿元"], refs=["ev-0001"]),
            context_text=CONTEXT)
        perturbed = assess_draft(
            _draft(findings=["营业收入为 150.0 亿元，毛利率 65%"], refs=["ev-0001"]),
            context_text=CONTEXT)
        self.assertEqual(baseline["score"], 100)
        self.assertLess(perturbed["score"], baseline["score"])
        self.assertEqual(perturbed["verdict"], VERDICT_COMPLETE_WITH_FLAGS)


class WorkerQualityGateTests(unittest.TestCase):
    """Replay-mode runs through the REAL worker: the gate refuses completion,
    preserves the gap draft, and lets cited drafts complete. The evidence is a
    genuinely imported vault document bound to the task's signal, so the
    delivered-context path (vault verification + signal binding) is the real
    one, not a hand-written summary dictionary."""

    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.enterContext(project_path_override(lambda value: self.root / value))
        self.config = vault_config(self.root, {})
        mdg = self.config.setdefault("market_data_governance", {})
        mdg["approved_internal_research_providers"] = ["eastmoney"]
        mdg["use_production_pilot_provider_registry"] = False
        mdg["require_consensus_for_llm"] = False
        mdg["allow_degraded_offhours_llm"] = True
        from _vault_fixture import import_document

        self.doc_id, self.doc_sha = import_document(
            self.config, self.root,
            content="\ufeff甲公司 2024 年年度报告\n2024 年度营业收入 150.0 亿元。",
            title="甲公司 2024 年年度报告")
        self.store = TaskStore(self.root / "tasks.sqlite3")
        self.addCleanup(self.store.close)

    def _spec(self, replay_response):
        spec = build_task_spec(research_question="R06 质量门", symbols=["600519"],
                               execution_mode="replay", config=self.config)
        spec["bound_signal_ids"] = ["sig-test-0001"]
        spec["public_context"]["evidence_summaries"] = {
            self.doc_id: {"summary": "2024 年度营业收入 150.0 亿元。",
                          "sha256": self.doc_sha}}
        spec["public_context"]["quote_snapshots"] = {}
        spec["replay_response"] = replay_response
        return spec

    def _run(self, spec):
        from stock_agent.research_worker import run_research_task

        task, _ = self.store.create_task(title="R06", input_payload=spec,
                                         execution_mode="replay")
        claimed = self.store.claim_next("w-r06")
        result = run_research_task(self.store, task["task_id"], worker_id="w-r06",
                                   config=self.config)
        return task["task_id"], result

    def test_uncited_specific_draft_blocks_and_preserves_gap(self):
        task_id, result = self._run(self._spec(_draft(
            summary="研究草稿：收入大幅增长。",
            findings=["营业收入为 150.0 亿元"], refs=[])))
        self.assertEqual(result["status"], "blocked",
                         "an uncited specific claim can never complete the research")
        stored = self.store.get_task(task_id)
        self.assertEqual(stored["status"], "blocked")
        self.assertIn("evidence gap", stored["last_error"])
        gap_draft = stored["model_call"]["evidence_gap_draft"]
        self.assertEqual(gap_draft["key_findings"], ["营业收入为 150.0 亿元"],
                         "A31: the draft is preserved as a gap, not discarded")
        self.assertEqual(stored["model_call"]["quality"]["verdict"],
                         VERDICT_INSUFFICIENT_EVIDENCE)
        # The detail projection surfaces both.
        from stock_agent.task_detail import task_detail
        detail = task_detail(self.store, task_id, config=self.config)
        self.assertEqual(detail["model_call"]["quality"]["verdict"],
                         VERDICT_INSUFFICIENT_EVIDENCE)
        self.assertEqual(detail["model_call"]["evidence_gap_draft"]["key_findings"],
                         ["营业收入为 150.0 亿元"])

    def test_cited_matching_draft_completes_with_quality_report(self):
        task_id, result = self._run(self._spec(_draft(
            findings=["营业收入为 150.0 亿元"], refs=[self.doc_id])))
        self.assertEqual(result["status"], "succeeded")
        stored = self.store.get_task(task_id)
        self.assertEqual(stored["status"], "succeeded")
        self.assertEqual(stored["output"]["quality"]["verdict"], VERDICT_OK)
        self.assertEqual(stored["output"]["quality"]["score"], 100)
        from stock_agent.task_detail import task_detail
        detail = task_detail(self.store, task_id, config=self.config)
        self.assertEqual(detail["output_quality"]["score"], 100)

    def test_cited_mismatched_number_completes_but_flagged(self):
        task_id, result = self._run(self._spec(_draft(
            findings=["营业收入为 160.0 亿元"], refs=[self.doc_id])))
        self.assertEqual(result["status"], "succeeded",
                         "A32: the rule points the mismatch out; it does not reject")
        stored = self.store.get_task(task_id)
        quality = stored["output"]["quality"]
        self.assertEqual(quality["verdict"], VERDICT_COMPLETE_WITH_FLAGS)
        self.assertEqual(quality["number_mismatch"][0]["unmatched_number"], "160.0")

    def test_plain_gap_unknowns_without_refs_still_complete(self):
        task_id, result = self._run(self._spec(_draft(
            findings=["业务结构改善"], unknowns=["未获官方原件确认"], refs=[])))
        self.assertEqual(result["status"], "succeeded",
                         "A31: gap statements save without citations")


if __name__ == "__main__":
    unittest.main()
