"""R20: research library — rebuildable index, revocation-aware retrieval.

No new authority: the index is a pure projection of terminal tasks + vault
projections; dropping it loses nothing. Search-time revocation annotation
guarantees a revoked document never keeps feeding answers through an old
index row, and the whole thing works without any embedding store.
"""
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from stock_agent.config import project_path, project_path_override
from stock_agent.evidence_context import verified_evidence_documents
from stock_agent.research_library import (
    LibraryError,
    annotate_revocation,
    build_library_index,
    search_library,
)
from stock_agent.task_runtime import TaskStore

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _vault_fixture import import_document, vault_config


def _replay(summary, findings=(), questions=True):
    return {
        "schema_version": "1.0", "task_id": "placeholder",
        "summary": summary, "key_findings": list(findings),
        "unknowns": [], "evidence_refs": [],
        "human_questions": ([{"question": "确认？", "why_needed": "w", "options": []}]
                            if questions else []),
        "model_self_assessed_confidence": 0.4}


class R20TestCase(unittest.TestCase):
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
        self.store = TaskStore(self.root / "tasks.sqlite3")
        self.addCleanup(self.store.close)
        self.counter = [0]

    def _research(self, question, symbols, summary, findings=(), evidence_sha=None,
                  complete=True):
        from stock_agent.task_supply import build_task_spec

        self.counter[0] += 1
        spec = build_task_spec(research_question=question, symbols=symbols,
                               execution_mode="replay", config=self.config)
        spec["bound_signal_ids"] = []
        spec["public_context"]["evidence_summaries"] = (
            {"ev-1": {"summary": "s", "sha256": evidence_sha}}
            if evidence_sha else {})
        spec["replay_response"] = _replay(summary, findings, questions=not complete)
        task, _ = self.store.create_task(title="L{0}".format(self.counter[0]),
                                         input_payload=spec, execution_mode="replay")
        claimed = self.store.claim_next("w")
        from stock_agent.research_worker import run_research_task

        run_research_task(self.store, task["task_id"], worker_id="w", config=self.config)
        return self.store.get_task(task["task_id"])


class IndexBuildTests(R20TestCase):
    def test_index_covers_terminal_only_and_rebuilds_identically(self):
        self._research("完成研究一", ["600519"], "白酒营收结论。", complete=True)
        self._research("未完成研究", ["000001"], "排队中", complete=False)  # stays queued
        first = build_library_index(self.store, self.config)
        self.assertEqual(first["entry_count"], 1, "only terminal research enters the library")
        second = build_library_index(self.store, self.config)
        self.assertEqual(json.dumps(first, sort_keys=True), json.dumps(second, sort_keys=True),
                         "the index is a pure projection: rebuild reproduces it byte-for-byte")

    def test_entry_carries_research_facts_and_links(self):
        task = self._research("茅台风险研究", ["600519"], "估值偏高。",
                              findings=["营收增速放缓"], complete=True)
        index = build_library_index(self.store, self.config)
        entry = index["entries"][0]
        self.assertEqual(entry["task_id"], task["task_id"])
        self.assertEqual(entry["question"], "茅台风险研究")
        self.assertEqual(entry["symbols"], ["600519"])
        self.assertIn("估值", entry["summary"])
        self.assertEqual(entry["quality"], "ok")


class SearchTests(R20TestCase):
    def test_fulltext_and_structured_filters(self):
        self._research("白酒板块风险", ["600519"], "需求走弱。", complete=True)
        self._research("新能源车竞争", ["300750", "600519"], "价格战。", complete=True)
        self._research("银行分红研究", ["601398"], "分红率提升。", complete=True)
        index = annotate_revocation(build_library_index(self.store, self.config), self.config)

        hit = search_library(index, "白酒")
        self.assertEqual(hit["result_count"], 1)
        self.assertIn("白酒", hit["results"][0]["question"])

        by_symbol = search_library(index, symbols=["600519"])
        self.assertEqual(by_symbol["result_count"], 2)

        both = search_library(index, "竞争", symbols=["300750"])
        self.assertEqual(both["result_count"], 1)
        self.assertEqual(both["results"][0]["symbols"], ["300750", "600519"])

        by_task = search_library(index, task_id=index["entries"][0]["task_id"])
        self.assertEqual(by_task["result_count"], 1)

        none = search_library(index, "半导体")
        self.assertEqual(none["result_count"], 0)

    def test_date_window_filter(self):
        self._research("窗口内", ["600519"], "s", complete=True)
        index = annotate_revocation(build_library_index(self.store, self.config), self.config)
        completed = index["entries"][0]["completed_at"]
        inside = search_library(index, since="2000-01-01", until="2999-01-01")
        self.assertEqual(inside["result_count"], 1)
        before = search_library(index, until="2000-01-01")
        self.assertEqual(before["result_count"], 0)
        after = search_library(index, since="2999-01-01")
        self.assertEqual(after["result_count"], 0)

    def test_malformed_index_rejected(self):
        with self.assertRaises(LibraryError):
            search_library({"schema_version": "junk"})


class RevocationTests(R20TestCase):
    def test_revoked_evidence_excluded_and_flagged(self):
        ref, sha = import_document(self.config, self.root,
                                   content="营收公告正文", title="营收公告")
        task = self._research("有证据研究", ["600519"], "基于公告。",
                              evidence_sha=sha, complete=True)
        index = build_library_index(self.store, self.config)
        self.assertEqual(index["entries"][0]["evidence_shas"], [sha])
        annotated = annotate_revocation(index, self.config)
        self.assertEqual(annotated["entries"][0]["live_evidence_shas"], [sha])
        self.assertEqual(search_library(annotated, "公告")["result_count"], 1)

        # The document leaves the verified projection (revocation): the SAME
        # index row must stop answering — no deleted rows, just propagation.
        import stock_agent.research_library as library
        original = library.verified_evidence_documents
        library.verified_evidence_documents = lambda config: {}
        try:
            stale = annotate_revocation(index, self.config)
        finally:
            library.verified_evidence_documents = original
        entry = stale["entries"][0]
        self.assertEqual(entry["stale_reason"], "evidence_revoked")
        self.assertEqual(search_library(stale, "公告")["result_count"], 0,
                         "revoked evidence never keeps feeding answers")
        # Explicit request can still see the stale row (audit path).
        self.assertEqual(search_library(stale, "公告", include_stale=True)["result_count"], 1)

    def test_no_evidence_entry_never_marked_stale(self):
        self._research("无证据研究", ["600519"], "公开上下文结论。", complete=True)
        annotated = annotate_revocation(build_library_index(self.store, self.config),
                                        self.config)
        self.assertIsNone(annotated["entries"][0].get("stale_reason"))


class RerunLinkageTests(R20TestCase):
    def test_prior_link_and_version_visible_in_library(self):
        # The FIRST research must be created through the question intake so
        # the rerun's prior lookup (question key) can find it (known trap:
        # raw create_task stores a content key instead).
        from stock_agent.task_intake import ensure_question_task
        from stock_agent.task_supply import build_task_spec

        first_seed = ensure_question_task(
            self.store, research_question="迭代研究", symbols=["600519"],
            config=self.config, execution_mode="replay")
        first_id = first_seed["task"]["task_id"]
        claimed = self.store.claim_next("w")
        from stock_agent.research_worker import run_research_task

        run_research_task(self.store, first_id, worker_id="w", config=self.config)
        awaiting = self.store.get_task(first_id)
        done = self.store.record_human_response(
            first_id, actor="lead", decision="acknowledge",
            expected_version=awaiting["version"], command_id="cmd-r20a")
        self.assertEqual(done["status"], "succeeded")

        second = ensure_question_task(
            self.store, research_question="迭代研究", symbols=["600519"],
            config=self.config, execution_mode="replay", new_research=True)
        claimed = self.store.claim_next("w")
        run_research_task(self.store, second["task"]["task_id"], worker_id="w",
                          config=self.config)
        awaiting = self.store.get_task(second["task"]["task_id"])
        done = self.store.record_human_response(
            awaiting["task_id"], actor="lead", decision="acknowledge",
            expected_version=awaiting["version"], command_id="cmd-r20b")
        index = annotate_revocation(build_library_index(self.store, self.config), self.config)
        by_prior = [e for e in index["entries"] if e.get("prior_task_id") == first_id]
        self.assertEqual(len(by_prior), 1, "the company timeline links versions")
        self.assertEqual(by_prior[0]["namespace"], "personal")


if __name__ == "__main__":
    unittest.main()
