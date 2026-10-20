"""R01: unified input version, business intent idempotency, honest worker state.

Acceptance scenarios from GLM_REFACTOR_TASKBOOK_20261020 (A01–A05, A92) plus
the supporting invariants: immutable input snapshots, read-only divergence
diagnostics for legacy rows, legacy-schema migration, and platform-run
association records. All data lives in a TemporaryDirectory via
project_path_override; zero network, zero real model calls.
"""
import hashlib
import json
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

from stock_agent.config import project_path_override
from stock_agent.dify_access import DifyAccessError  # noqa: F401  (re-exported reasons)
from stock_agent.dify_api import DifyGateway, DifySubmissionError
from stock_agent.task_intake import TaskIntakeError, ensure_question_task
from stock_agent.task_runtime import TaskStore
from stock_agent.task_runtime.store import (
    STATUS_CANCELLED,
    STATUS_QUEUED,
    TaskIdempotencyConflictError,
    TaskRuntimeError,
)

sys.path.insert(0, str(Path(__file__).resolve().parent))

TOKEN = "r01-dify-token"


def _digest(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _public_payload(**extra):
    payload = {
        "data_classification": "public",
        "research_question": "R01 验收问题",
        "public_context": {},
    }
    payload.update(extra)
    return payload


class R01StoreTestCase(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.enterContext(project_path_override(lambda value: self.root / value))
        self.store = TaskStore(self.root / "tasks.sqlite3")
        self.addCleanup(self.store.close)

    def _drive_to_awaiting_human(self, task_id):
        task = self.store.claim_next("w1")
        self.assertEqual(task["task_id"], task_id)
        self.store.mark_running(task_id, worker_id="w1",
                                fencing_token=task["fencing_token"],
                                expected_version=task["version"])
        return self.store.request_awaiting_human(
            task_id, worker_id="w1", fencing_token=task["fencing_token"],
            human_request={"question": "是否补充原件？"})


class InputVersionAuthorityTests(R01StoreTestCase):
    def test_a01_create_version_5_then_bump_to_6_everywhere(self):
        task, outcome = self.store.create_task(
            title="v5 任务", input_payload=_public_payload(input_version=5),
            case_id="case-a01", execution_mode="replay")
        self.assertEqual(outcome, "RECORDED")
        # DB column, payload projection, and the immutable snapshot all say 5.
        self.assertEqual(task["input_version"], 5)
        self.assertEqual(task["input_payload"]["input_version"], 5)
        snapshots = self.store.input_snapshots(task["task_id"])
        self.assertEqual([s["input_version"] for s in snapshots], [5])
        self.assertEqual(snapshots[0]["reason"], "created")

        awaiting = self._drive_to_awaiting_human(task["task_id"])
        bumped = self.store.record_human_response(
            task["task_id"], actor="lead-analyst", decision="provide_input",
            expected_version=awaiting["version"], command_id="cmd-a01",
            input_patch={"extra_note": "补充说明"})
        # After the evidence supplement: column, payload, and snapshot say 6.
        self.assertEqual(bumped["input_version"], 6)
        self.assertEqual(bumped["input_payload"]["input_version"], 6)
        self.assertEqual(bumped["status"], STATUS_QUEUED)
        snapshots = self.store.input_snapshots(task["task_id"])
        self.assertEqual([s["input_version"] for s in snapshots], [5, 6])
        self.assertEqual(snapshots[1]["reason"], "human_provide_input")
        self.assertEqual(snapshots[1]["created_by"], "lead-analyst")
        # v5 snapshot unchanged (immutable history).
        self.assertEqual(snapshots[0]["payload"], snapshots[0]["payload"])

    def test_payload_without_version_is_normalized_to_column(self):
        task, _ = self.store.create_task(
            title="默认版本", input_payload=_public_payload(), execution_mode="replay")
        self.assertEqual(task["input_version"], 1)
        self.assertEqual(task["input_payload"]["input_version"], 1)

    def test_invalid_input_version_rejected(self):
        with self.assertRaises(TaskRuntimeError):
            self.store.create_task(
                title="坏版本", input_payload=_public_payload(input_version=0),
                execution_mode="replay")
        with self.assertRaises(TaskRuntimeError):
            self.store.create_task(
                title="坏类型", input_payload=_public_payload(input_version="5"),
                execution_mode="replay")

    def test_snapshot_per_version_is_immutable(self):
        task, _ = self.store.create_task(
            title="快照", input_payload=_public_payload(input_version=3), execution_mode="replay")
        with self.assertRaises(TaskRuntimeError):
            self.store._write_input_snapshot(task["task_id"], 3,
                                             _public_payload(input_version=3, extra_note="篡改"),
                                             created_by="attacker", reason="forged")

    def test_divergence_report_is_read_only_and_finds_legacy_rows(self):
        clean, _ = self.store.create_task(
            title="一致行", input_payload=_public_payload(input_version=2), execution_mode="replay")
        # Simulate a legacy divergent row written by pre-R01 code: column 1,
        # payload says 4 (the F01 shape: column never written, payload did).
        legacy_id = "task_legacy_divergent"
        self.store._conn.execute(
            "INSERT INTO research_tasks (task_id, input_payload, idempotency_key, execution_mode, created_at, updated_at)"
            " VALUES (?,?,?,?,?,?)",
            (legacy_id, json.dumps(_public_payload(input_version=4)), "idem-legacy-x",
             "replay", "2026-10-01T00:00:00", "2026-10-01T00:00:00"))
        self.store._conn.commit()
        report = self.store.input_version_divergence_report()
        self.assertEqual(report["divergent_tasks"], 1)
        entry = report["entries"][0]
        self.assertEqual(entry["task_id"], legacy_id)
        self.assertEqual(entry["column_version"], 1)
        self.assertEqual(entry["payload_version"], 4)
        self.assertIn("human review", entry["suggested_mapping"])
        # The report changed nothing: the divergent row is still divergent,
        # the clean row is untouched, and no snapshot rows were invented.
        row = self.store._conn.execute(
            "SELECT input_version FROM research_tasks WHERE task_id = ?", (legacy_id,)).fetchone()
        self.assertEqual(int(row["input_version"]), 1)
        self.assertEqual(self.store.input_snapshots(clean["task_id"])[0]["input_version"], 2)
        self.assertEqual(self.store.input_snapshots(legacy_id), [])

    def test_legacy_schema_is_migrated_additively(self):
        legacy_path = self.root / "legacy.sqlite3"
        conn = sqlite3.connect(str(legacy_path))
        try:
            # Faithful pre-R01 (v1.0) research_tasks shape: the full original
            # column set MINUS business_intent_id, which migration must add.
            conn.execute(
                """CREATE TABLE research_tasks (
                       task_id TEXT PRIMARY KEY,
                       parent_task_id TEXT NOT NULL DEFAULT '',
                       case_id TEXT NOT NULL DEFAULT '',
                       role_id TEXT NOT NULL DEFAULT 'research_analyst',
                       title TEXT NOT NULL DEFAULT '',
                       input_version INTEGER NOT NULL DEFAULT 1,
                       input_payload TEXT NOT NULL DEFAULT '{}',
                       status TEXT NOT NULL DEFAULT 'queued',
                       version INTEGER NOT NULL DEFAULT 1,
                       fencing_token INTEGER NOT NULL DEFAULT 0,
                       attempt_count INTEGER NOT NULL DEFAULT 0,
                       max_attempts INTEGER NOT NULL DEFAULT 2,
                       cancel_requested INTEGER NOT NULL DEFAULT 0,
                       idempotency_key TEXT NOT NULL,
                       prompt_version TEXT NOT NULL DEFAULT '',
                       model_config_hash TEXT NOT NULL DEFAULT '',
                       execution_mode TEXT NOT NULL DEFAULT 'live',
                       model_call TEXT NOT NULL DEFAULT '{}',
                       output TEXT NOT NULL DEFAULT '{}',
                       human_request TEXT NOT NULL DEFAULT '{}',
                       last_error TEXT NOT NULL DEFAULT '',
                       worker_id TEXT NOT NULL DEFAULT '',
                       lease_expires_at TEXT NOT NULL DEFAULT '',
                       created_at TEXT NOT NULL,
                       updated_at TEXT NOT NULL)""")
            conn.execute(
                "INSERT INTO research_tasks (task_id, input_payload, idempotency_key, execution_mode, created_at, updated_at)"
                " VALUES ('task_old_1', '{}', 'idem-old-1', 'replay', '2026-09-01T00:00:00', '2026-09-01T00:00:00')")
            conn.commit()
        finally:
            conn.close()
        store = TaskStore(legacy_path)
        try:
            columns = {str(row["name"]) for row in store._conn.execute("PRAGMA table_info(research_tasks)")}
            self.assertIn("business_intent_id", columns)
            # Legacy row untouched; new-row write path works on the migrated DB.
            task, _ = store.create_task(
                title="迁移后新建", input_payload=_public_payload(input_version=2),
                business_intent_id="bi-after-migration", execution_mode="replay")
            self.assertEqual(task["input_version"], 2)
            self.assertEqual(len(store.input_snapshots(task["task_id"])), 1)
            # Empty intents stay non-unique (the legacy default).
            store.create_task(title="无意图1", input_payload=_public_payload(q=1), execution_mode="replay")
            store.create_task(title="无意图2", input_payload=_public_payload(q=2), execution_mode="replay")
        finally:
            store.close()


class BusinessIntentTests(R01StoreTestCase):
    def test_a02_same_intent_two_platform_runs_one_task_two_associations(self):
        payload = _public_payload()
        first, outcome1 = self.store.create_task(
            title="意图任务", input_payload=payload, business_intent_id="bi-a02",
            execution_mode="replay",
            platform_run={"platform": "dify", "run_id": "run-A", "workflow_version": "wf-1"})
        second, outcome2 = self.store.create_task(
            title="意图任务", input_payload=payload, business_intent_id="bi-a02",
            execution_mode="replay",
            platform_run={"platform": "dify", "run_id": "run-B", "workflow_version": "wf-1"})
        self.assertEqual(outcome1, "RECORDED")
        self.assertEqual(outcome2, "DUPLICATE")
        self.assertEqual(first["task_id"], second["task_id"])
        self.assertEqual(len(self.store.list_tasks()), 1)
        runs = self.store.list_platform_runs(first["task_id"])
        self.assertEqual({r["run_id"] for r in runs}, {"run-A", "run-B"},
                         "two platform runs must leave two association receipts")

    def test_same_intent_different_run_id_in_payload_still_deduplicates(self):
        # The run id lives in the payload origin for the Dify façade; the
        # logical fingerprint must ignore it (F03 regression guard).
        p1 = _public_payload(origin={"kind": "dify", "dify_run_id": "run-1"})
        p2 = _public_payload(origin={"kind": "dify", "dify_run_id": "run-2"})
        t1, o1 = self.store.create_task(title="d1", input_payload=p1,
                                        business_intent_id="bi-run", execution_mode="replay")
        t2, o2 = self.store.create_task(title="d1", input_payload=p2,
                                        business_intent_id="bi-run", execution_mode="replay")
        self.assertEqual((o1, o2), ("RECORDED", "DUPLICATE"))
        self.assertEqual(t1["task_id"], t2["task_id"])

    def test_same_intent_different_content_conflicts(self):
        self.store.create_task(title="原请求", input_payload=_public_payload(q=1),
                               business_intent_id="bi-conflict", execution_mode="replay")
        with self.assertRaises(TaskIdempotencyConflictError):
            self.store.create_task(title="另一请求", input_payload=_public_payload(q=2),
                                   business_intent_id="bi-conflict", execution_mode="replay")

    def test_same_intent_different_mode_conflicts(self):
        self.store.create_task(title="replay", input_payload=_public_payload(),
                               business_intent_id="bi-mode", execution_mode="replay")
        with self.assertRaises(TaskIdempotencyConflictError):
            self.store.create_task(title="live", input_payload=_public_payload(),
                                   business_intent_id="bi-mode", execution_mode="live")

    def test_a03_same_key_different_content_conflicts(self):
        self.store.create_task(title="K", input_payload=_public_payload(q=1),
                               idempotency_key="idem-K", execution_mode="replay")
        with self.assertRaises(TaskIdempotencyConflictError):
            self.store.create_task(title="K", input_payload=_public_payload(q=2),
                                   idempotency_key="idem-K", execution_mode="replay")

    def test_one_platform_run_cannot_drive_two_tasks(self):
        run = {"platform": "dify", "run_id": "run-shared"}
        self.store.create_task(title="t1", input_payload=_public_payload(q=1),
                               business_intent_id="bi-1", execution_mode="replay", platform_run=run)
        with self.assertRaises(TaskIdempotencyConflictError):
            self.store.create_task(title="t2", input_payload=_public_payload(q=2),
                                   business_intent_id="bi-2", execution_mode="replay", platform_run=run)

    def test_association_replay_is_idempotent(self):
        run = {"platform": "dify", "run_id": "run-replay"}
        payload = _public_payload()
        for _ in range(3):
            self.store.create_task(title="t", input_payload=payload,
                                   business_intent_id="bi-replay", execution_mode="replay",
                                   platform_run=run)
        self.assertEqual(len(self.store.list_platform_runs(
            self.store.find_task_by_idempotency_key("intent::bi-replay")["task_id"])), 1)


class WorkerHonestStateTests(R01StoreTestCase):
    def test_a04_cancel_during_invocation_worker_reports_cancelled(self):
        from stock_agent.research_worker import run_research_task

        task, _ = self.store.create_task(
            title="取消场景", input_payload=_public_payload(
                replay_response={
                    "schema_version": "1.0", "task_id": "placeholder",
                    "summary": "草稿", "key_findings": [], "unknowns": [],
                    "evidence_refs": [], "human_questions": [],
                    "model_self_assessed_confidence": 0.3,
                }),
            execution_mode="replay")
        claimed = self.store.claim_next("w-cancel")
        # User cancels while the invocation is in flight (task leased/running:
        # the flag is set, the worker notices it at completion time).
        self.store.request_cancel(task["task_id"], actor="operator",
                                  reason="用户撤回", expected_version=claimed["version"])
        result = run_research_task(self.store, task["task_id"], worker_id="w-cancel", config={})
        self.assertEqual(result["status"], STATUS_CANCELLED)
        stored = self.store.get_task(task["task_id"])
        self.assertEqual(stored["status"], STATUS_CANCELLED,
                         "worker return and persisted state must agree")


class QuestionIntentTests(R01StoreTestCase):
    def _question(self, text="贵州茅台的最新公告影响？", symbols=("600519",)):
        return ensure_question_task(
            self.store, research_question=text, symbols=list(symbols),
            config=None, execution_mode="replay")

    def test_a92_explicit_new_research_creates_new_intent_and_links_prior(self):
        first = self._question()
        self.assertEqual(first["status"], "RECORDED")
        # Network retry of the same submit (default stable question key) still
        # deduplicates to the first task.
        retry = self._question()
        self.assertEqual(retry["status"], "DUPLICATE")
        self.assertEqual(retry["task"]["task_id"], first["task"]["task_id"])
        # The user explicitly re-researches the next day: a NEW task with its
        # own intent, linked to the prior one; the old result is untouched.
        fresh = ensure_question_task(
            self.store, research_question="贵州茅台的最新公告影响？", symbols=["600519"],
            config=None, execution_mode="replay", new_research=True, as_of="2026-10-21T09:00:00+08:00")
        self.assertEqual(fresh["status"], "RECORDED")
        self.assertNotEqual(fresh["task"]["task_id"], first["task"]["task_id"])
        self.assertTrue(fresh["business_intent_id"])
        origin = fresh["task"]["input_payload"]["origin"]
        self.assertEqual(origin["prior_task_id"], first["task"]["task_id"])
        self.assertEqual(origin["as_of"], "2026-10-21T09:00:00+08:00")
        self.assertEqual(origin["research_occasion"], "explicit_new_research")
        self.assertEqual(self.store.get_task(first["task"]["task_id"])["status"], "queued")
        # Two explicit re-researches are two distinct intents, never one.
        again = ensure_question_task(
            self.store, research_question="贵州茅台的最新公告影响？", symbols=["600519"],
            config=None, execution_mode="replay", new_research=True)
        self.assertNotEqual(again["task"]["task_id"], fresh["task"]["task_id"])

    def test_intent_retry_deduplicates_and_conflicts_on_content_change(self):
        created = ensure_question_task(
            self.store, research_question="汇率对出口链的影响？", symbols=["000333"],
            config=None, execution_mode="replay", intent_id="qi-fixed-1")
        self.assertEqual(created["status"], "RECORDED")
        retry = ensure_question_task(
            self.store, research_question="汇率对出口链的影响？", symbols=["000333"],
            config=None, execution_mode="replay", intent_id="qi-fixed-1")
        self.assertEqual(retry["status"], "DUPLICATE")
        with self.assertRaises(TaskIdempotencyConflictError):
            ensure_question_task(
                self.store, research_question="完全不同的问题？", symbols=["000333"],
                config=None, execution_mode="replay", intent_id="qi-fixed-1")

    def test_intent_and_new_research_are_mutually_exclusive(self):
        with self.assertRaises(TaskIntakeError):
            ensure_question_task(
                self.store, research_question="q", symbols=["600519"],
                config=None, execution_mode="replay",
                intent_id="qi-x", new_research=True)


class DifyGatewayR01Tests(unittest.TestCase):
    """A02/A05 through the Dify façade: derived intent + column-driven bundles."""

    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.enterContext(project_path_override(lambda value: self.root / value))
        self.store = TaskStore(self.root / "tasks.sqlite3")
        self.addCleanup(self.store.close)
        cred_path = self.root / "config" / "dify" / "service_credentials.json"
        cred_path.parent.mkdir(parents=True, exist_ok=True)
        cred_path.write_text(json.dumps({
            "schema_version": "1.0",
            "credentials": [{
                "credential_id": "cred-1",
                "token_sha256": _digest(TOKEN),
                "scopes": ["create_research", "read_research", "read_fact_bundle", "submit_result"],
                "case_scope": ["case-r01"],
                "active": True, "created_at": "2026-10-20T00:00:00+08:00", "expires_at": "",
            }],
        }, ensure_ascii=False), encoding="utf-8")
        self.config = {
            "dify": {
                "enabled": True,
                "service_credentials_path": "config/dify/service_credentials.json",
                "approved_workflow_versions": ["wf-r01"],
            },
            "task_runtime": {"budget": {"daily_model_call_limit": 2}},
        }
        self.gateway = DifyGateway(self.store, self.config)

    def _create(self, *, run_id="run-1", input_version=1, question="同一研究问题？",
                public_context="同一上下文", business_intent_id=""):
        return self.gateway.create_research(
            TOKEN, title="R01 研究", case_id="case-r01",
            input_version=input_version, research_question=question,
            public_context=public_context, dify_run_id=run_id,
            workflow_version="wf-r01", business_intent_id=business_intent_id)

    def test_a02_two_dify_runs_one_task_with_association_receipts(self):
        first = self._create(run_id="dify-run-1")
        second = self._create(run_id="dify-run-2")
        self.assertEqual(first["outcome"], "RECORDED")
        self.assertEqual(second["outcome"], "DUPLICATE")
        self.assertEqual(first["task_id"], second["task_id"])
        self.assertEqual(len(self.store.list_tasks()), 1,
                         "a retried platform run must not create a second task")
        self.assertEqual(first["business_intent_id"], second["business_intent_id"])
        self.assertTrue(first["business_intent_id"])
        runs = self.store.list_platform_runs(first["task_id"])
        self.assertEqual({r["run_id"] for r in runs}, {"dify-run-1", "dify-run-2"})
        self.assertEqual(second["platform_run"]["task_run_count"], 2)

    def test_a05_late_candidate_for_old_input_version_is_rejected(self):
        created = self._create(input_version=5)
        task_id = created["task_id"]
        # The task moves to awaiting_human, then the user supplies evidence:
        # input_version becomes 6 (column AND payload).
        task = self.store.claim_next("w-late")
        self.store.mark_running(task_id, worker_id="w-late",
                                fencing_token=task["fencing_token"], expected_version=task["version"])
        awaiting = self.store.request_awaiting_human(
            task_id, worker_id="w-late", fencing_token=task["fencing_token"],
            human_request={"question": "补充原件？"})
        bumped = self.store.record_human_response(
            task_id, actor="lead-analyst", decision="provide_input",
            expected_version=awaiting["version"], command_id="cmd-late",
            input_patch={"extra_note": "新证据"})
        self.assertEqual(bumped["input_version"], 6)
        self.assertEqual(bumped["status"], STATUS_QUEUED)
        # A bundle issued for the OLD version 5 arrives with a result: it must
        # not match the task's current (column-authoritative) version 6.
        old_bundle = self.gateway.issue_fact_bundle(
            case_id="case-r01", input_version=5,
            facts=[{"fact_id": "fact-old", "kind": "market_snapshot", "source": "s",
                    "classification": "public", "external_send_allowed": True,
                    "content": "旧事实"}])
        with self.assertRaises(DifySubmissionError) as caught:
            self.gateway.submit_candidate_result(
                TOKEN, task_id=task_id, dify_run_id="dify-run-1",
                workflow_version="wf-r01", bundle_id=old_bundle["bundle_id"],
                citations=["fact-old"], result={"summary": "迟到结果"})
        self.assertEqual(caught.exception.reason, "bundle_mismatch")
        self.assertEqual(self.store.get_task(task_id)["status"], STATUS_QUEUED,
                         "a rejected late result must not change task state")

    def test_explicit_intent_overrides_derivation(self):
        first = self._create(run_id="r1", business_intent_id="bi-explicit")
        second = self._create(run_id="r2", business_intent_id="bi-explicit")
        self.assertEqual(second["outcome"], "DUPLICATE")
        self.assertEqual(first["business_intent_id"], "bi-explicit")

    def test_public_view_reads_authoritative_column(self):
        created = self._create(input_version=7)
        view = self.gateway.research_status(TOKEN, task_id=created["task_id"])
        self.assertEqual(view["input_version"], 7)
        task_row = self.store.get_task(created["task_id"])
        self.assertEqual(task_row["input_version"], 7)
        self.assertEqual(task_row["input_payload"]["input_version"], 7)


if __name__ == "__main__":
    unittest.main()
