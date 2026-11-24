"""R20 rework (KONM-75): per-query revocation re-check, caller-scoped
retrieval and the report version contract.

Three defects came out of the independent verification:

- a saved index kept answering after its evidence was revoked, because the
  only re-check was an annotation the caller had to remember to redo;
- nothing bound a query to a principal: one projection covered every task;
- entries carried no report version, so a case and its continuation could
  only be told apart by task id.

The normal entry is ``query_library`` (library level) and
``GET /api/v1/library`` / ``GET /api/v1/library/timeline`` (board, session
mode). Synthetic data only; every report is a replay; no model call.
"""
import http.client
import json
import os
import sys
import tempfile
import threading
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch
from urllib.parse import quote

from stock_agent.config import project_path_override
from stock_agent.identity_providers import PersonalSessionStore
from stock_agent.personal_evidence import (
    PERSONAL_ORIGIN_VIA,
    import_personal_document,
    personal_vault_view,
)
from stock_agent.evidence_context import verified_evidence_documents
from stock_agent.research_library import (
    AUDIT_ROLES,
    LIBRARY_SCHEMA,
    LibraryError,
    LibraryTimeWindowError,
    annotate_revocation,
    build_library_index,
    query_library,
    report_identity,
    search_library,
)
from stock_agent.research_worker import run_research_task
from stock_agent.task_board import create_task_board_server
from stock_agent.task_detail import task_detail
from stock_agent.task_supply import build_task_spec
from stock_agent.task_runtime import TaskStore

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _vault_fixture import import_document, vault_config

ALICE, BOB, CAROL = "scope-alice", "scope-bob", "scope-carol"
AUDITOR = "scope-quality-reviewer"


def _replay(summary, findings=(), questions=False):
    return {
        "schema_version": "1.0", "task_id": "placeholder",
        "summary": summary, "key_findings": list(findings),
        "unknowns": [], "evidence_refs": [],
        "human_questions": ([{"question": "确认？", "why_needed": "w", "options": []}]
                            if questions else []),
        "model_self_assessed_confidence": 0.4}


class ScopeTestCase(unittest.TestCase):
    def setUp(self):
        real = Path(os.path.realpath(tempfile.mkdtemp(prefix="r20-scope.")))
        self.addCleanup(lambda: __import__("shutil").rmtree(real, ignore_errors=True))
        self.root = real
        self.enterContext(project_path_override(lambda value: self.root / value))
        import stock_agent.config as config_module

        self.enterContext(patch.object(config_module, "PROJECT_ROOT", self.root))
        self.config = vault_config(self.root, {})
        self.config["personal_evidence"] = {"root": str(self.root / "personal_evidence")}
        mdg = self.config.setdefault("market_data_governance", {})
        mdg["approved_internal_research_providers"] = ["eastmoney"]
        mdg["use_production_pilot_provider_registry"] = False
        mdg["require_consensus_for_llm"] = False
        mdg["allow_degraded_offhours_llm"] = True
        self.store = TaskStore(self.root / "tasks.sqlite3")
        self.addCleanup(self.store.close)
        self.counter = [0]

    # ---- drivers (production entry points only) ------------------------

    def _finish(self, task_id, actor):
        from stock_agent import personal_workspace as pw

        self.assertEqual(self.store.claim_next("w")["task_id"], task_id)
        run_research_task(self.store, task_id, worker_id="w", config=self.config)
        task = self.store.get_task(task_id)
        if task["status"] == "awaiting_human":
            self.counter[0] += 1
            self.store.record_human_response(
                task_id, actor=actor, decision="acknowledge", expected_version=task["version"],
                command_id="scope-fin-{0}".format(self.counter[0]))
            task = self.store.get_task(task_id)
        return task

    def _shared(self, question, summary="共享研究结论。", evidence=None):
        """Shared (institutional/pipeline) research with a controlled report."""
        spec = build_task_spec(research_question=question, symbols=["600519"],
                               execution_mode="replay", config=self.config)
        spec["bound_signal_ids"] = []
        spec["public_context"]["evidence_summaries"] = dict(evidence or {})
        spec["replay_response"] = _replay(summary)
        task, _ = self.store.create_task(title="S{0}".format(len(self.store.list_tasks())),
                                         input_payload=spec, execution_mode="replay")
        self.store.claim_next("w")
        run_research_task(self.store, task["task_id"], worker_id="w", config=self.config)
        return self.store.get_task(task["task_id"])

    def _personal(self, actor, question, document_ids=None, rerun_of=None, note=None):
        from stock_agent import personal_workspace as pw

        if rerun_of:
            created = pw.rerun_completed_research(self.store, actor=actor, task_id=rerun_of,
                                                  config=self.config)
        elif note:
            created = pw.follow_up_completed_research(self.store, actor=actor, task_id=note[0],
                                                      note=note[1], config=self.config)
        else:
            created = pw.create_personal_research(
                self.store, actor=actor, question=question, symbols=["600519"],
                execution_mode="replay", config=self.config, document_ids=document_ids)
        self.assertIn(created["status"], {"RECORDED", "DUPLICATE"})
        if created["status"] == "DUPLICATE":
            return created["task"]
        return self._finish(created["task"]["task_id"], actor)

    def _personal_document(self, owner, content, title):
        source = self.root / "incoming-{0}.txt".format(abs(hash(title)) % 100000)
        source.write_text(content, encoding="utf-8")
        result = import_personal_document(
            self.config, owner, str(source), title=title,
            source_url="https://www.cninfo.com.cn/new/disclosure/detail?plate=sse&orgId=gssh0000001",
            published_at="2026-09-20T18:00:00+08:00")
        return (str(result["document"]["document_id"]),
                str(result["document"]["sha256"]), content)

    def _withdraw(self, content, config=None):
        """Real withdrawal: the same bytes re-ingested under a policy that no
        longer approves their content type land in quarantine (the production
        ingest entry appends ``evidence_quarantined``; nothing is deleted)."""
        from stock_agent.evidence_vault import ingest_official_document_bytes

        strict = json.loads(json.dumps(config or self.config))
        strict["official_evidence_vault"]["allowed_content_types"] = ["application/pdf"]
        result = ingest_official_document_bytes(
            strict, content.encode("utf-8"),
            source_url="https://www.cninfo.com.cn/new/disclosure/detail?plate=szse&orgId=gssz0000001",
            title="withdrawn", published_at="2026-09-20T18:00:00+08:00",
            actor="test-evidence-collector", actor_role="information_analyst")
        self.assertEqual(result["document"]["status"], "quarantined")

    def _roster(self, principals):
        Path(self.config["research_operations"]["roster_path"]).write_text(
            json.dumps({"principals": principals}), encoding="utf-8")

    def _questions(self, **kwargs):
        return sorted(r["question"] for r in query_library(
            build_library_index(self.store), self.config, kwargs.pop("actor"), **kwargs)["results"])


class RevocationRecheckTests(ScopeTestCase):
    def _bound(self, question, evidence):
        task = self._shared(question, evidence=evidence)
        return task, build_library_index(self.store)

    def test_cached_index_stops_answering_after_full_withdrawal(self):
        from _vault_fixture import import_document

        _, sha = import_document(self.config, self.root, content="全撤销原件正文。", title="全撤销原件")
        task, cached = self._bound("全撤销研究", {"ev-1": {"summary": "s", "sha256": sha}})
        self.assertEqual(query_library(cached, self.config, ALICE, query="全撤销")["result_count"], 1)
        self._withdraw("全撤销原件正文。")
        # The SAME index object, saved before the withdrawal, is not rebuilt.
        result = query_library(cached, self.config, ALICE, query="全撤销")
        self.assertEqual(result["result_count"], 0, "a cached index must stop answering")
        self.assertTrue(result["revocation_checked"])
        self.assertFalse(result["audit_granted"])
        audit = query_library(cached, self.config, ALICE, query="全撤销", include_stale=True)
        self.assertEqual(audit["result_count"], 0, "a non-auditor cannot read the stale row")
        self.assertFalse(audit["audit_granted"])
        self._roster([{"actor_id": AUDITOR, "principal_type": "human", "active": True,
                       "roles": sorted(AUDIT_ROLES), "information_side": "public"}])
        audit = query_library(cached, self.config, AUDITOR, query="全撤销", include_stale=True)
        self.assertTrue(audit["audit_granted"])
        self.assertEqual([(r["stale_reason"], r["live_evidence_shas"]) for r in audit["results"]],
                         [("evidence_revoked", [])])

    def test_partial_withdrawal_hides_entry_and_reports_survivor(self):
        from _vault_fixture import import_document

        sha_a = import_document(self.config, self.root, content="存活原件正文。", title="存活原件")[1]
        sha_b = import_document(self.config, self.root, content="部分撤销原件正文。", title="部分撤销原件")[1]
        task, cached = self._bound("部分撤销研究", {"a": {"summary": "s", "sha256": sha_a},
                                                "b": {"summary": "s", "sha256": sha_b}})
        self.assertEqual(query_library(cached, self.config, ALICE, query="部分撤销")["result_count"], 1)
        self._withdraw("部分撤销原件正文。")
        result = query_library(cached, self.config, ALICE, query="部分撤销")
        self.assertEqual(result["result_count"], 0, "a partly stale report is withheld by default")
        self._roster([{"actor_id": AUDITOR, "principal_type": "human", "active": True,
                       "roles": ["quality_reviewer"], "information_side": "public"}])
        audit = query_library(cached, self.config, AUDITOR, query="部分撤销", include_stale=True)
        self.assertEqual([(r["stale_reason"], r["live_evidence_shas"]) for r in audit["results"]],
                         [("evidence_partially_revoked", [sha_a])])

    def test_recheck_follows_the_vault_on_every_query(self):
        from _vault_fixture import import_document

        sha = import_document(self.config, self.root, content="逐次复核原件正文。", title="逐次复核原件")[1]
        _, cached = self._bound("逐次复核研究", {"ev-1": {"summary": "s", "sha256": sha}})

        def ask():
            return query_library(cached, self.config, ALICE, query="逐次复核")["result_count"]

        seen = [ask()]
        self._withdraw("逐次复核原件正文。")
        seen += [ask(), ask()]
        self.assertEqual(seen, [1, 0, 0], "the same index object follows the vault on every query")

    def test_filter_step_refuses_unchecked_and_outdated_indexes(self):
        raw = build_library_index(self.store)
        with self.assertRaises(LibraryError):
            search_library(raw)
        outdated = {**raw, "schema_version": "1.0", "revocation_checked": True}
        with self.assertRaises(LibraryError):
            search_library(outdated)
        self.assertEqual(LIBRARY_SCHEMA, "1.1")

    def test_query_requires_a_principal(self):
        with self.assertRaises(LibraryError):
            query_library(build_library_index(self.store), self.config, " ")
        self.assertEqual(self.config["personal_evidence"]["root"], str(self.root / "personal_evidence"))


class ScopeTests(ScopeTestCase):
    def test_each_session_sees_own_and_shared_never_foreign(self):
        shared = self._shared("共享流水线研究")
        alice_free = self._personal(ALICE, "Alice自有研究甲")
        bob_free = self._personal(BOB, "Bob自有研究乙")
        _, sha_b, _ = self._personal_document(BOB, "Bob的个人公告正文。", "Bob公告")
        bob_doc = self._personal(BOB, "Bob带附件研究", document_ids=[self._doc_id(BOB, sha_b)])
        for actor, own in ((ALICE, alice_free), (BOB, bob_free)):
            seen = query_library(build_library_index(self.store), self.config, actor)
            questions = [r["question"] for r in seen["results"]]
            self.assertIn("共享流水线研究", questions, "shared research is authorized for every session")
            self.assertIn(own["input_payload"]["research_question"], questions)
            foreign = [q for q in questions if q.startswith(("Alice" if actor == BOB else "Bob"))]
            self.assertEqual(foreign, [], "{0} must not receive the other session's research".format(actor))
            self.assertTrue(seen["scope"]["out_of_scope_hidden"])
        # The audit view lifts ONLY the stale exclusion, never the scope.
        self._roster([{"actor_id": AUDITOR, "principal_type": "human", "active": True,
                       "roles": ["compliance_officer"], "information_side": "public"}])
        audit = query_library(build_library_index(self.store), self.config, AUDITOR, include_stale=True)
        self.assertEqual(sorted(r["question"] for r in audit["results"]), ["共享流水线研究"])
        self.assertTrue(audit["audit_granted"])

    def _doc_id(self, owner, sha):
        return "document:sha256:{0}".format(sha)

    def test_namespace_and_owner_come_from_the_trusted_binding(self):
        plain = self._shared("流水线任务", summary="流水线。")
        personal = self._personal(CAROL, "Carol个人研究")
        rerun = self._personal(CAROL, "", rerun_of=personal["task_id"])
        index = build_library_index(self.store)
        by_id = {e["task_id"]: e for e in index["entries"]}
        self.assertEqual((by_id[plain["task_id"]]["namespace"], by_id[plain["task_id"]]["owner"]),
                         ("default", None))
        self.assertEqual((by_id[personal["task_id"]]["namespace"], by_id[personal["task_id"]]["owner"]),
                         ("personal", CAROL))
        self.assertEqual(by_id[rerun["task_id"]]["namespace"], "personal")
        self.assertEqual(by_id[rerun["task_id"]]["research_occasion"], "explicit_new_research")
        # A payload cannot claim ownership: a raw task with a personal-looking
        # origin but no server-side binding stays shared.
        spec = build_task_spec(research_question="伪造个人任务", symbols=["600519"],
                               execution_mode="replay", config=self.config)
        spec["bound_signal_ids"] = []
        spec["replay_response"] = _replay("伪造。")
        spec["origin"] = {"kind": "question", "research_occasion": "user_followup",
                          "created_via": PERSONAL_ORIGIN_VIA}
        forged, _ = self.store.create_task(title="forged", input_payload=spec, execution_mode="replay")
        self.store.claim_next("w")
        run_research_task(self.store, forged["task_id"], worker_id="w", config=self.config)
        entry = [e for e in build_library_index(self.store)["entries"] if e["task_id"] == forged["task_id"]][0]
        self.assertEqual((entry["namespace"], entry["owner"]), ("default", None))

    def test_personal_originals_are_live_in_their_own_namespace(self):
        document_id, sha_a, _ = self._personal_document(ALICE, "Alice的个人公告正文。", "Alice公告")
        self._personal(ALICE, "Alice带附件研究", document_ids=[document_id])
        raw = build_library_index(self.store)
        self.assertEqual(query_library(raw, self.config, ALICE, query="Alice带附件")["result_count"], 1)
        self.assertIsNone(annotate_revocation(raw, self.config, actor=ALICE)["entries"][0].get("stale_reason"))

    def test_every_session_gets_a_result_built_for_its_actor(self):
        self._shared("共享流水线研究")
        self._personal(ALICE, "Alice自有研究甲")
        self._personal(BOB, "Bob自有研究乙")
        seen = {actor: self._questions(actor=actor) for actor in (ALICE, BOB)}
        self.assertEqual(seen[ALICE], sorted(["共享流水线研究", "Alice自有研究甲"]))
        self.assertEqual(seen[BOB], sorted(["共享流水线研究", "Bob自有研究乙"]))

    def test_withdrawn_shared_evidence_is_withheld_for_every_caller(self):
        _, sha = import_document(self.config, self.root, content="共享原件正文。", title="共享原件")
        self._shared("共享证据研究", evidence={"ev-1": {"summary": "s", "sha256": sha}})
        raw = build_library_index(self.store)
        self.assertEqual(query_library(raw, self.config, BOB, query="共享证据")["result_count"], 1)
        self._withdraw("共享原件正文。")
        self.assertEqual(query_library(raw, self.config, BOB, query="共享证据")["result_count"], 0)


class ReportVersionTests(ScopeTestCase):
    def test_original_kept_continuation_distinguishable_prior_linked(self):
        document_id, _, _ = self._personal_document(ALICE, "时间线原件正文。", "时间线原件")
        first = self._personal(ALICE, "公司甲时间线研究", document_ids=[document_id])
        original = task_detail(self.store, first["task_id"], config=self.config)
        original_report = dict(original["output"])
        rerun = self._personal(ALICE, "", rerun_of=first["task_id"])
        followup = self._personal(ALICE, "", note=(first["task_id"], "公司甲续研追问"))
        index = build_library_index(self.store)
        by_id = {e["task_id"]: e for e in index["entries"]}
        original_entry, rerun_entry, follow_entry = (by_id[first["task_id"]],
                                                     by_id[rerun["task_id"]],
                                                     by_id[followup["task_id"]])
        # The digest is reproducible from the preserved report alone.
        for entry in (original_entry, rerun_entry, follow_entry):
            source = self.store.get_task(entry["task_id"])
            identity = report_identity(source["output"], source["task_id"], source["input_version"])
            self.assertEqual(identity["report_version"], entry["report_version"])
            self.assertEqual(identity["report_sha256"], entry["report_sha256"])
        after = task_detail(self.store, first["task_id"], config=self.config)
        timeline = query_library(index, self.config, ALICE, timeline_of=first["task_id"])
        versions = {original_entry["report_version"], rerun_entry["report_version"],
                    follow_entry["report_version"]}
        self.assertEqual(len(versions), 3, "each report has its own version")
        self.assertEqual(original_entry["input_version"], 1)
        self.assertEqual(rerun_entry["input_version"], 1, "a continuation starts its own input chain")
        self.assertEqual(follow_entry["input_version"], 1)
        self.assertEqual(rerun_entry["prior_task_id"], first["task_id"])
        self.assertEqual(follow_entry["prior_task_id"], first["task_id"])
        self.assertEqual(rerun_entry["research_occasion"], "explicit_new_research")
        self.assertEqual(follow_entry["research_occasion"], "user_followup")
        # The original keeps its report, version and identity after both
        # continuations were created from it.
        self.assertEqual(self.store.get_task(first["task_id"])["output"], original_report)
        self.assertEqual(after["report_version"], original["report_version"])
        self.assertEqual(after["input_version"], original["input_version"])
        self.assertEqual(original["report_version"]["report_schema_version"], "1.0")
        self.assertTrue(original["report_version"]["report_version"].startswith(
            "rv1:1.0:{0}:1:".format(first["task_id"])))
        self.assertEqual(sorted(r["task_id"] for r in timeline["results"]),
                         sorted([first["task_id"], rerun["task_id"], followup["task_id"]]))
        self.assertEqual(query_library(index, self.config, BOB, timeline_of=first["task_id"])["result_count"],
                         0, "another session's case has no visible timeline")

    def test_signoff_changes_the_row_not_the_report(self):
        from stock_agent import personal_workspace as pw

        created = pw.create_personal_research(self.store, actor=CAROL, question="签收后版本不变研究",
                                              symbols=["600519"], execution_mode="replay",
                                              config=self.config)
        task_id = created["task"]["task_id"]
        self.assertEqual(self.store.claim_next("w")["task_id"], task_id)
        run_research_task(self.store, task_id, worker_id="w", config=self.config)
        awaiting = task_detail(self.store, task_id, config=self.config)
        self.assertEqual(awaiting["status"], "awaiting_human")
        self.assertIsNone(awaiting["personal_acknowledgement"])
        self.store.record_human_response(task_id, actor=CAROL, decision="acknowledge",
                                         expected_version=awaiting["row_version"],
                                         command_id="scope-signoff")
        after = task_detail(self.store, task_id, config=self.config)
        self.assertEqual(after["status"], "succeeded")
        self.assertIsNotNone(after["personal_acknowledgement"])
        self.assertEqual(after["report_version"], awaiting["report_version"], "the report is unchanged")
        self.assertEqual(after["input_version"], awaiting["input_version"], "no new input was supplied")
        self.assertGreater(after["row_version"], awaiting["row_version"], "the row moved on")

    def test_report_without_body_has_no_version(self):
        self.assertEqual(report_identity({}), {"report_schema_version": "", "report_sha256": "",
                                          "input_version": 1, "report_version": ""})
        self.assertEqual(report_identity(None)["report_version"], "")


class LibraryHttpTests(ScopeTestCase):
    def setUp(self):
        super().setUp()
        self.authenticated = []

        def authenticator(config, actor, symbols):
            if actor not in (ALICE, BOB, AUDITOR):
                raise ValueError("unknown principal")
            self.authenticated.append(actor)
            return {"actor": actor, "provider": "windows_local",
                    "subject": "S-1-{0}".format(actor), "policy_sha256": "a" * 64}

        self.server = create_task_board_server(
            self.store, host="127.0.0.1", port=0, config=self.config,
            session_store=PersonalSessionStore(ttl_seconds=1800),
            login_authenticator=authenticator)
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.shutdown)
        self.addCleanup(self.server.server_close)

    def _request(self, path, cookie=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        try:
            conn.request("GET", path, None, {"Cookie": cookie} if cookie else {})
            response = conn.getresponse()
            return response.status, json.loads(response.read().decode("utf-8"))
        finally:
            conn.close()

    def _login(self, actor):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        try:
            conn.request("POST", "/api/v1/personal/login",
                         json.dumps({"actor": actor, "symbols": ["600519"]}),
                         {"Content-Type": "application/json"})
            response = conn.getresponse()
            self.assertEqual(response.status, 200, response.read())
            return (response.getheader("Set-Cookie") or "").split(";")[0]
        finally:
            conn.close()

    def test_two_sessions_each_get_their_authorized_results(self):
        shared = self._shared("共享流水线研究")
        alice = self._personal(ALICE, "Alice自有研究甲")
        bob = self._personal(BOB, "Bob自有研究乙")
        status, body = self._request("/api/v1/library")
        self.assertEqual((status, body["error"]), (403, "session_required"))
        self._roster([{"actor_id": AUDITOR, "principal_type": "human", "active": True,
                       "roles": ["quality_reviewer"], "information_side": "public"}])
        for actor, own, cookie in ((ALICE, alice, self._login(ALICE)), (BOB, bob, self._login(BOB))):
            status, body = self._request("/api/v1/library", cookie)
            self.assertEqual(status, 200)
            self.assertEqual(body["actor"], actor)
            questions = [r["question"] for r in body["results"]]
            self.assertEqual(sorted(questions), sorted(["共享流水线研究", own["input_payload"]["research_question"]]))
            self.assertIn("result_count", body)
            self.assertFalse(body["audit_granted"])
            self.assertEqual(self._request("/api/v1/library?actor=" + BOB, cookie)[0], 403)
        self.assertEqual(self._request("/api/v1/library?query=Bob", self._login(BOB))[1]["result_count"], 1)
        status, audit = self._request("/api/v1/library?include_stale=1", self._login(AUDITOR))
        self.assertEqual((status, audit["audit_granted"]), (200, True))
        # A session cannot read another session's case through the timeline.
        status, body = self._request("/api/v1/library/timeline?task_id=" + alice["task_id"], self._login(BOB))
        self.assertEqual((status, body["result_count"]), (200, 0))
        status, body = self._request("/api/v1/library/timeline", self._login(ALICE))
        self.assertEqual(status, 400)

    def test_route_answers_withdrawal_on_the_next_request(self):
        document_id, _, content = self._personal_document(ALICE, "逐次复核原件正文。", "逐次复核原件")
        self._personal(ALICE, "Alice带附件研究", document_ids=[document_id])
        cookie = self._login(ALICE)
        path = "/api/v1/library?query=" + quote("Alice带附件")
        self.assertEqual(self._request(path, cookie)[1]["result_count"], 1)
        self._withdraw(content, personal_vault_view(self.config, ALICE))
        status, body = self._request(path, cookie)
        self.assertEqual((status, body["result_count"]), (200, 0))
        self._roster([{"actor_id": ALICE, "principal_type": "human", "active": True,
                       "roles": ["quality_reviewer"], "information_side": "public"}])
        status, body = self._request(path + "&include_stale=1", cookie)
        self.assertEqual((status, body["result_count"], body["audit_granted"]), (200, 1, True))

    def test_timeline_route_lists_case_and_continuations_with_report_versions(self):
        first = self._personal(ALICE, "公司乙时间线研究")
        rerun = self._personal(ALICE, "", rerun_of=first["task_id"])
        follow = self._personal(ALICE, "", note=(first["task_id"], "公司乙续研追问"))
        cookie = self._login(ALICE)
        status, body = self._request("/api/v1/library/timeline?task_id=" + first["task_id"], cookie)
        self.assertEqual(status, 200)
        self.assertEqual(sorted(r["task_id"] for r in body["results"]),
                         sorted([first["task_id"], rerun["task_id"], follow["task_id"]]))
        self.assertEqual(len({r["report_version"] for r in body["results"]}), 3,
                         "one report version per case and input version")
        self.assertEqual([r["prior_task_id"] for r in sorted(body["results"], key=lambda r: r["task_id"])
                          if r["task_id"] != first["task_id"]].count(first["task_id"]), 2)
        self.assertTrue(all(r["prior_task_id"] is None for r in body["results"]
                            if r["task_id"] == first["task_id"]))
        # Every timeline row's version reads back identically from the case
        # detail route, and its digest recomputes from the preserved report.
        identity_fields = ("report_version", "report_sha256", "report_schema_version")
        for row in body["results"]:
            status, detail = self._request("/api/v1/tasks/" + row["task_id"])
            self.assertEqual(status, 200)
            self.assertEqual({k: detail["report_version"][k] for k in identity_fields},
                             {k: row[k] for k in identity_fields})
            recomputed = report_identity(detail["output"], row["task_id"], detail["input_version"])
            self.assertEqual({k: recomputed[k] for k in identity_fields},
                             {k: row[k] for k in identity_fields})
        self.assertTrue(all(r["input_version"] == 1 for r in body["results"]))
        self.assertEqual(self._request("/api/v1/timeline?task_id=" + first["task_id"], cookie)[0], 404)


class AuthorizingVaultTests(ScopeTestCase):
    CONTENT = "双金库同字节原件正文。"

    def test_shared_withdrawal_is_not_masked_by_a_personal_copy(self):
        _, sha = import_document(self.config, self.root, content=self.CONTENT, title="共享原件")
        self._shared("共享原件研究", evidence={"ev-1": {"summary": "s", "sha256": sha}})
        bob_copy = self._personal_document(BOB, self.CONTENT, "Bob的同字节副本")
        self.assertEqual(bob_copy[1], sha, "same bytes, same digest, other namespace")
        cached = build_library_index(self.store)
        ask = lambda actor: query_library(cached, self.config, actor, query="共享原件")["result_count"]  # noqa: E731
        self.assertEqual((ask(ALICE), ask(BOB)), (1, 1))
        self._withdraw(self.CONTENT)   # the SHARED vault withdraws its original
        self.assertNotIn(sha, verified_evidence_documents(self.config), "shared projection dropped it")
        self.assertIn(sha, verified_evidence_documents(personal_vault_view(self.config, BOB)),
                      "the personal copy is untouched")
        retained = Path(personal_vault_view(self.config, BOB)["official_evidence_vault"]["objects_dir"])
        self.assertTrue(any(p.stat().st_size == len(self.CONTENT.encode("utf-8"))
                            for p in retained.rglob(sha)), "no byte was deleted")
        self.assertEqual((ask(ALICE), ask(BOB)), (0, 0),
                         "the report cites the shared original: its revocation decides for everybody")
        rebuilt = build_library_index(self.store)
        self.assertEqual(query_library(rebuilt, self.config, BOB, query="共享原件")["result_count"], 0)

    def test_personal_original_is_judged_in_its_own_namespace(self):
        document_id, sha, content = self._personal_document(BOB, "仅个人金库持有。", "个人原件")
        self.assertNotIn(sha, verified_evidence_documents(self.config))
        self._personal(BOB, "个人原件研究", document_ids=[document_id])
        cached = build_library_index(self.store)
        self.assertEqual(query_library(cached, self.config, BOB, query="个人原件")["result_count"], 1)
        self._withdraw(content, personal_vault_view(self.config, BOB))
        self.assertEqual(query_library(cached, self.config, BOB, query="个人原件")["result_count"], 0)


class ReportTimeSourceTests(ScopeTestCase):
    def _delivered_personal_source(self):
        """A personal report whose ORIGINAL the worker actually delivered:
        the shared copy is withdrawn first, the personal copy is linked to
        the signal the task binds, and the delivery record names it."""
        from stock_agent.evidence_vault import link_evidence_to_signal
        from stock_agent.models import AnalyzedSignal, now_iso
        from stock_agent.storage import SignalStore

        signal_id = "sig-source-0001"
        database = SignalStore(Path(self.config["storage"]["sqlite_path"]))
        try:
            database.upsert_many([AnalyzedSignal(
                signal_id=signal_id, source="manual", source_type="filing", title="来源信号",
                summary="Synthetic fixture only", url="https://www.cninfo.com.cn/disclosure/fixture",
                published_at=now_iso(), collected_at=now_iso(), symbols=["600519"],
                payload={"data_classification": "public"})])
        finally:
            database.close()
        _, sha = import_document(self.config, self.root, content=self.CONTENT, title="共享原件",
                                 signal_ids=[signal_id])
        self._withdraw(self.CONTENT)  # the SHARED original is withdrawn first
        document_id, personal_sha, _ = self._personal_document(ALICE, self.CONTENT, "个人副本")
        self.assertEqual(personal_sha, sha)
        link_evidence_to_signal(personal_vault_view(self.config, ALICE), document_id=document_id,
                                signal_id=signal_id, actor="test-evidence-collector",
                                actor_role="information_analyst")
        task = self._personal(ALICE, "真实交付研究", document_ids=[document_id])
        return task, sha

    def test_live_personal_original_delivered_after_shared_withdrawal_stays_visible(self):
        task, sha = self._delivered_personal_source()
        delivery = task["model_call"]["context_delivery"]
        self.assertIn("document:sha256:{0}".format(sha), delivery["delivered_evidence"],
                      "the worker delivered the personal original: {0}".format(delivery["dropped_evidence"]))
        self.assertEqual(delivery["evidence_namespaces"]["document:sha256:{0}".format(sha)], "personal")
        cached = build_library_index(self.store)
        self.assertEqual(cached["entries"][0]["evidence_sources"], {sha: "personal"})
        self.assertEqual(query_library(cached, self.config, ALICE, query="真实交付")["result_count"], 1)
        # ... and it is that personal original's own withdrawal, not the
        # shared copy's, that later hides the report.
        self._withdraw(self.CONTENT, personal_vault_view(self.config, ALICE))
        self.assertEqual(query_library(cached, self.config, ALICE, query="真实交付")["result_count"], 0)

    """The authorizing namespace is fixed when the report is made."""

    CONTENT = "个人来源原件正文。"

    def _count(self, index, actor, query):
        return query_library(index, self.config, actor, query=query)["result_count"]

    def test_personal_original_withdrawal_not_masked_by_later_shared_copy(self):
        document_id, sha, content = self._personal_document(ALICE, self.CONTENT, "个人来源原件")
        self._personal(ALICE, "个人来源研究", document_ids=[document_id])
        shared_copy = import_document(self.config, self.root, content=content, title="后来的共享副本")
        self.assertEqual(shared_copy[1], sha)
        cached = build_library_index(self.store)
        entry = cached["entries"][0]
        self.assertEqual(entry["evidence_sources"], {sha: "unrecorded"},
                         "no delivery record names the source: resolved from the task's namespace")
        self.assertEqual(self._count(cached, ALICE, "个人来源"), 1)
        self._withdraw(content, personal_vault_view(self.config, ALICE))
        self.assertNotIn(sha, verified_evidence_documents(personal_vault_view(self.config, ALICE)),
                         "the personal original left the owner's deliverable projection")
        self.assertIn(sha, verified_evidence_documents(self.config), "the shared copy is still usable")
        self.assertEqual(self._count(cached, ALICE, "个人来源"), 0,
                         "the report cites the personal original; the later shared copy cannot stand in")
        self.assertEqual(self._count(build_library_index(self.store), ALICE, "个人来源"), 0)

    def test_recorded_source_decides_in_both_directions(self):
        sha_live = "a" * 64
        sha_gone = "b" * 64
        index = {"schema_version": LIBRARY_SCHEMA, "entry_count": 2, "entries": [
            {"task_id": "task-shared", "status": "succeeded", "question": "共享来源研究", "summary": "",
             "key_findings": [], "symbols": [], "completed_at": "2026-11-24T10:00:00+08:00",
             "namespace": "default", "owner": None, "evidence_shas": [sha_gone],
             "evidence_sources": {sha_gone: "shared"}, "live_evidence_shas": [], "stale_reason": "x"},
            {"task_id": "task-personal", "status": "succeeded", "question": "个人来源研究", "summary": "",
             "key_findings": [], "symbols": [], "completed_at": "2026-11-24T10:00:01+08:00",
             "namespace": "personal", "owner": BOB, "evidence_shas": [sha_live],
             "evidence_sources": {sha_live: "personal"}, "live_evidence_shas": [], "stale_reason": "x"},
        ]}
        # Each vault holds a copy of the OTHER report's bytes but not of its
        # own original (withdrawn). Judged by current holdings both reports
        # would look live; the recorded source makes both stale.
        self._roster([{"actor_id": BOB, "principal_type": "human", "active": True,
                       "roles": ["quality_reviewer"], "information_side": "public"}])
        from unittest.mock import patch as _patch

        import stock_agent.research_library as library

        def verified(view):
            events_path = str((view.get("official_evidence_vault") or {}).get("events_path") or "")
            return {sha_gone: {}} if events_path.startswith(str(self.root / "personal_evidence")) \
                else {sha_live: {}}

        with _patch.object(library, "verified_evidence_documents", verified):
            result = query_library(index, self.config, BOB, include_stale=True)
            default = query_library(index, self.config, BOB)
        by_id = {r["task_id"]: r for r in result["results"]}
        self.assertEqual(by_id["task-shared"]["stale_reason"], "evidence_revoked")
        self.assertEqual(by_id["task-personal"]["stale_reason"], "evidence_revoked")
        self.assertEqual([r["live_evidence_shas"] for r in result["results"]], [[], []])
        self.assertEqual(default["result_count"], 0)

    def test_old_record_without_source_field_needs_review_when_ledgers_disagree(self):
        """Cross-version: a personal task written by the previous worker
        (no ``evidence_namespaces``) while both vaults held the same bytes.
        The shared original is then withdrawn and only the personal copy
        survives. The record cannot say which copy the report used."""
        document_id, sha, content = self._personal_document(ALICE, "跨版本同字节原件。", "跨版本原件")
        import_document(self.config, self.root, content=content, title="共享侧同字节")
        task = self._personal(ALICE, "跨版本研究", document_ids=[document_id])
        model_call = dict(task["model_call"])
        model_call["context_delivery"] = {k: v for k, v in model_call["context_delivery"].items()
                                          if k != "evidence_namespaces"}
        self.store._conn.execute("UPDATE research_tasks SET model_call = ? WHERE task_id = ?",
                                 (json.dumps(model_call, ensure_ascii=False), task["task_id"]))
        self.store._conn.commit()
        cached = build_library_index(self.store)
        self.assertEqual(cached["entries"][0]["evidence_sources"], {sha: "unrecorded"})
        self.assertEqual(query_library(cached, self.config, ALICE, query="跨版本")["result_count"], 1,
                         "both copies verified: the report is backed either way")
        self._withdraw(content)   # the shared original; the personal copy stays
        result = query_library(cached, self.config, ALICE, query="跨版本")
        self.assertEqual(result["result_count"], 0, "the source cannot be proven: excluded by default")
        self._roster([{"actor_id": ALICE, "principal_type": "human", "active": True,
                       "roles": ["quality_reviewer"], "information_side": "public"}])
        audit = query_library(cached, self.config, ALICE, query="跨版本", include_stale=True)
        self.assertEqual([(r["stale_reason"], r["unverified_sources"]) for r in audit["results"]],
                         [("source_unverified", [sha])])
        # Had ONLY the personal namespace ever held the bytes, the source is
        # proven and the personal copy keeps the report live.
        only, only_sha, only_content = self._personal_document(BOB, "仅个人金库持有。", "仅个人原件")
        self._personal(BOB, "仅个人研究", document_ids=[only])
        index = build_library_index(self.store)
        self.assertEqual(query_library(index, self.config, BOB, query="仅个人")["result_count"], 1)

    def test_historical_record_without_source_fails_closed(self):
        sha = "c" * 64
        index = {"schema_version": LIBRARY_SCHEMA, "entry_count": 1, "entries": [
            {"task_id": "task-old", "status": "succeeded", "question": "旧记录研究", "summary": "",
             "key_findings": [], "symbols": [], "completed_at": "2026-11-24T10:00:00+08:00",
             "namespace": "default", "owner": None, "evidence_shas": [sha],
             "evidence_sources": {sha: "unrecorded"}}]}
        self.assertEqual(query_library(index, self.config, ALICE, query="旧记录")["result_count"], 0)
        stale = query_library(index, self.config, ALICE, query="旧记录", include_stale=True)
        self.assertEqual(stale["audit_granted"], False)
        annotated = annotate_revocation(index, self.config)
        self.assertEqual(annotated["entries"][0]["stale_reason"], "evidence_revoked")


class OrderingTests(unittest.TestCase):
    """Entries order by the real completion instant, not by the text."""

    @staticmethod
    def _store(rows):
        class Store:
            input_owner_binding = staticmethod(lambda task_id: None)

            @staticmethod
            def list_tasks():
                return [{"task_id": task_id, "status": "succeeded", "updated_at": updated,
                         "input_payload": {"research_question": task_id, "symbols": []}, "output": {}}
                        for task_id, updated in rows]

        return Store()

    def test_instant_order_with_task_id_tiebreak(self):
        early = build_library_index(self._store([("task-late", "2026-11-24T02:00:00+00:00"),
                                                 ("task-early", "2026-11-24T15:00:00+14:00")]))
        self.assertLess("2026-11-24T02:00:00+00:00", "2026-11-24T15:00:00+14:00")  # text says otherwise
        self.assertEqual([e["task_id"] for e in early["entries"]], ["task-early", "task-late"])
        same = build_library_index(self._store([("task-b", "2026-11-24T02:00:00Z"),
                                                ("task-a", "2026-11-24T10:00:00+08:00"),
                                                ("task-c", "2026-11-24T02:00:00+00:00")]))
        self.assertEqual([e["task_id"] for e in same["entries"]], ["task-a", "task-b", "task-c"])
        unparsable = build_library_index(self._store([("task-x", ""), ("task-y", "2026-11-24T02:00:00Z")]))
        self.assertEqual([e["task_id"] for e in unparsable["entries"]], ["task-y", "task-x"])

    def test_query_results_keep_index_order(self):
        class Store:
            input_owner_binding = staticmethod(lambda task_id: None)

            @staticmethod
            def list_tasks():
                return [{"task_id": task_id, "status": "succeeded", "updated_at": updated,
                         "input_payload": {"research_question": task_id, "symbols": []}, "output": {}}
                        for task_id, updated in (("task-late", "2026-11-24T02:00:00+00:00"),
                                                 ("task-early", "2026-11-24T15:00:00+14:00"))]

        config = {"official_evidence_vault": {"enabled": False}, "storage": {}}
        with project_path_override(lambda value: Path(value)):
            result = query_library(build_library_index(Store()), config, "anyone")
        self.assertEqual([r["task_id"] for r in result["results"]], ["task-early", "task-late"])


class TimeWindowTests(ScopeTestCase):
    def setUp(self):
        super().setUp()
        self.task = self._shared("时间窗研究")
        self.index = annotate_revocation(build_library_index(self.store), self.config)

    def _count(self, **bounds):
        return query_library(self.index, self.config, ALICE, **bounds)["result_count"]

    def test_same_instant_in_any_offset_bounds_identically(self):
        completed = datetime.fromisoformat(self.task["updated_at"])
        local = completed.isoformat(timespec="seconds")
        utc = completed.astimezone(timezone.utc).isoformat(timespec="seconds")
        z = utc.replace("+00:00", "Z")
        self.assertNotEqual(local, utc)
        for bound in (local, utc, z):
            self.assertEqual(self._count(since=bound, until=bound), 1, bound)
            self.assertEqual(self._count(until=bound), 1, bound)
            self.assertEqual(self._count(since=bound), 1, bound)
        minute = timedelta(minutes=1)
        self.assertEqual(self._count(since=(completed - minute).astimezone(timezone.utc).isoformat(),
                                     until=(completed + minute).isoformat()), 1)
        self.assertEqual(self._count(until=(completed - minute).isoformat()), 0)
        self.assertEqual(self._count(since=(completed + minute).astimezone(timezone.utc).isoformat()), 0)

    def test_mixed_offset_completions_order_by_instant(self):
        time.sleep(1.1)  # completions carry second-resolution timestamps
        self._shared("较晚完成研究")
        index = annotate_revocation(build_library_index(self.store), self.config)
        by_question = {e["question"]: e for e in index["entries"]}
        early, late = by_question["时间窗研究"], by_question["较晚完成研究"]
        # Same instants, different stored offsets: the earlier completion is
        # rewritten to UTC+14 and the later one to UTC. Their lexical order
        # flips; their instant order must not.
        early["completed_at"] = datetime.fromisoformat(early["completed_at"]).astimezone(
            timezone(timedelta(hours=14))).isoformat()
        late["completed_at"] = datetime.fromisoformat(late["completed_at"]).astimezone(timezone.utc).isoformat()
        self.assertGreater(early["completed_at"], late["completed_at"], "lexical order is now misleading")
        cut = (datetime.fromisoformat(late["completed_at"]) - timedelta(seconds=1)).isoformat()
        seen = [r["question"] for r in query_library(index, self.config, ALICE, until=cut)["results"]]
        self.assertEqual(seen, ["时间窗研究"])

    def test_bare_dates_cover_the_whole_day(self):
        day = self.task["updated_at"][:10]
        before = (datetime.fromisoformat(self.task["updated_at"]).date() - timedelta(days=1)).isoformat()
        after = (datetime.fromisoformat(self.task["updated_at"]).date() + timedelta(days=1)).isoformat()
        self.assertEqual(self._count(since=day, until=day), 1, "until on a date includes that day")
        self.assertEqual(self._count(until=day), 1)
        self.assertEqual(self._count(since=day), 1)
        self.assertEqual(self._count(until=before), 0)
        self.assertEqual(self._count(since=after), 0)

    def test_unparseable_bounds_are_refused(self):
        for bad in ("2026-11-24T17:08:52", "yesterday", "2026-13-40", "2026-11", "17:08"):
            with self.subTest(bound=bad), self.assertRaises(LibraryTimeWindowError):
                self._count(since=bad)
            with self.subTest(until=bad), self.assertRaises(LibraryTimeWindowError):
                self._count(until=bad)
        with self.assertRaises(LibraryTimeWindowError):
            self._count(since="2999-01-01", until="2000-01-01")
        # Mixed granularity is refused only when the window is empty in
        # every offset; any other mixed pair is a legal window.
        for since, until in (("2026-11-24", "2026-11-23T00:00:00+00:00"),
                             ("2026-11-24", "2026-11-22T09:59:59+00:00"),
                             ("2026-11-26T14:00:00+00:00", "2026-11-25"),
                             ("2026-11-24T00:00:00+08:00", "2026-11-21")):
            with self.subTest(since=since, until=until), self.assertRaises(LibraryTimeWindowError):
                self._count(since=since, until=until)
        completed = datetime.fromisoformat(self.task["updated_at"])
        day = self.task["updated_at"][:10]
        mixed_ok = {
            "date_then_same_instant": ((day, completed.isoformat()), 1),
            "date_then_instant_before_it": ((day, (completed - timedelta(seconds=1)).isoformat()), 0),
            "instant_then_date": (((completed - timedelta(seconds=1)).isoformat(), day), 1),
            "instant_after_then_date": (((completed + timedelta(seconds=1)).isoformat(), day), 0),
            "date_then_utc_next_day": ((day, (completed + timedelta(hours=8)).astimezone(timezone.utc).isoformat()), 1),
        }
        for name, ((since, until), expected) in mixed_ok.items():
            with self.subTest(window=name):
                self.assertEqual(self._count(since=since, until=until), expected)


class StandardStartTests(ScopeTestCase):
    """The supported start entry assembles sessions: login, scoped retrieval
    and the timeline work from ``serve_task_board`` (what ``cli task-board``
    calls), with a provider from the registry — here a synthetic one."""

    PROVIDER = "konm75-synthetic"

    def _authenticator(self, config, actor, symbols):
        if actor not in (ALICE, BOB):
            raise ValueError("unknown principal")
        return {"actor": actor, "provider": self.PROVIDER, "subject": "S-1-{0}".format(actor),
                "policy_sha256": "a" * 64}

    def _serve(self, sessions_enabled=True):
        from stock_agent.identity_providers import providers

        self.config["task_runtime"] = {"db_path": str(self.root / "tasks.sqlite3")}
        self.config["task_board"] = {"identity_provider": self.PROVIDER,
                                     "sessions_enabled": sessions_enabled}
        self.enterContext(patch.dict(providers._REGISTRY, {self.PROVIDER: self._authenticator}))
        from stock_agent import task_board

        held = {}
        thread = threading.Thread(
            target=task_board.serve_task_board,
            args=(self.config,), kwargs={"host": "127.0.0.1", "port": 0,
                                   "ready": lambda server: held.__setitem__("server", server)},
            daemon=True)
        thread.start()
        deadline = time.monotonic() + 10
        while "server" not in held and time.monotonic() < deadline:
            time.sleep(0.02)
        self.assertIn("server", held, "the standard entry never bound")
        self.addCleanup(held["server"].shutdown)
        self.addCleanup(held["server"].server_close)
        return held["server"]

    def _call(self, server, method, path, body=None, cookie=None):
        conn = http.client.HTTPConnection("127.0.0.1", server.server_address[1], timeout=10)
        try:
            headers = {"Content-Type": "application/json"} if body is not None else {}
            if cookie:
                headers["Cookie"] = cookie
            conn.request(method, path, json.dumps(body) if body is not None else None, headers)
            response = conn.getresponse()
            return response.status, json.loads(response.read().decode("utf-8")), response.getheader("Set-Cookie")
        finally:
            conn.close()

    def _login(self, server, actor):
        status, body, cookie = self._call(server, "POST", "/api/v1/personal/login",
                                          {"actor": actor, "symbols": ["600519"]})
        self.assertEqual(status, 200, body)
        return cookie.split(";")[0]

    def test_login_then_scoped_retrieval_and_timeline_from_the_standard_entry(self):
        shared = self._shared("共享流水线研究")
        alice = self._personal(ALICE, "Alice自有研究甲")
        self._personal(BOB, "Bob自有研究乙")
        rerun = self._personal(ALICE, "", rerun_of=alice["task_id"])
        server = self._serve()
        self.assertIsNotNone(server.session_store)
        self.assertEqual(self._call(server, "GET", "/health")[0], 200)
        self.assertEqual(self._call(server, "GET", "/api/v1/library")[0], 403)
        bob = self._login(server, BOB)
        seen = self._call(server, "GET", "/api/v1/library", cookie=bob)[1]
        self.assertEqual(sorted(r["question"] for r in seen["results"]),
                         sorted(["共享流水线研究", "Bob自有研究乙"]))
        alice_cookie = self._login(server, ALICE)
        status, timeline, _ = self._call(
            server, "GET", "/api/v1/library/timeline?task_id=" + alice["task_id"], cookie=alice_cookie)
        self.assertEqual(status, 200)
        self.assertEqual(sorted(r["task_id"] for r in timeline["results"]),
                         sorted([alice["task_id"], rerun["task_id"]]))
        self.assertEqual(len({r["report_version"] for r in timeline["results"]}), 2)
        self.assertEqual(self._call(
            server, "GET", "/api/v1/library?since=2026-11-24T17:08:52", cookie=alice_cookie)[1]["error"],
            "invalid_time_window")
        self.assertEqual(self._call(server, "GET", "/api/v1/library?actor=" + BOB, cookie=alice_cookie)[0], 403)
        # The response names the bounds that were actually applied.
        day = shared["updated_at"][:10]
        base = "/api/v1/library?since={0}".format(day)
        status, body, _ = self._call(server, "GET", base + "&until=" + day, cookie=alice_cookie)
        self.assertEqual(status, 200)
        self.assertEqual((body["filters"]["since"], body["filters"]["until"]), (day, day))
        self.assertEqual(body["result_count"], 3)  # shared, Alice's own, Alice's rerun
        # A calendar date plus a timestamp is a legal window and is echoed
        # as applied; the certainly inverted pair is 400.
        instant = shared["updated_at"]
        status, body, _ = self._call(
            server, "GET", base + "&until=" + quote(instant), cookie=alice_cookie)
        self.assertEqual((status, body["filters"]["until"], body["result_count"]), (200, instant, 3))
        status, body, _ = self._call(
            server, "GET", base + "&until=2026-11-23T00:00:00%2B00:00", cookie=alice_cookie)
        self.assertEqual((status, body["error"]), (400, "invalid_time_window"))

    def test_sessions_explicitly_disabled_fails_closed(self):
        server = self._serve(sessions_enabled=False)
        self.assertIsNone(server.session_store)
        self.assertEqual(self._call(server, "GET", "/health")[0], 200)
        self.assertEqual(self._call(server, "POST", "/api/v1/personal/login",
                                    {"actor": ALICE, "symbols": ["600519"]})[0], 503)
        self.assertEqual(self._call(server, "GET", "/api/v1/library")[0], 503)
        self.assertEqual(self._call(server, "GET", "/api/v1/library/timeline?task_id=x")[0], 503)


class EmbeddingFreeTests(ScopeTestCase):
    def test_library_stays_embedding_free(self):
        import stock_agent.research_library as library

        self.assertEqual([m for m in sys.modules if m in {"numpy", "faiss", "sentence_transformers", "torch"}], [])
        self.assertNotIn("import numpy", Path(library.__file__).read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
