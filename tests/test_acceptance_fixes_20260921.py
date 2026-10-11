"""Acceptance-fix regression tests for ZCODE_ACCEPTANCE_REVIEW_20260921.

Written BEFORE the fixes (expected red), covering F01-F09. All offline; the
model path uses stubs; governance paths reuse the real llm_governance config.
"""

from __future__ import annotations

import json
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from stock_agent.config import load_config, project_path_override
from stock_agent.models import now_iso
from stock_agent.task_runtime import (
    STATUS_AWAITING_HUMAN,
    STATUS_BLOCKED,
    STATUS_CANCELLED,
    STATUS_QUEUED,
    STATUS_SUCCEEDED,
    TaskIdempotencyConflictError,
    TaskStore,
    TaskTransitionError,
    TaskVersionConflictError,
)

PROJECT_ROOT = Path(__file__).resolve().parent.parent
LOCAL_CONFIG = load_config(str(PROJECT_ROOT / "config" / "watchlist.local.json"))


def _spec(question: str = "q", extra: dict | None = None, replay: bool = True,
          doc_ref: str = "ev-0001", doc_sha: str | None = None) -> dict:
    sha = doc_sha or ("ab" * 32)
    payload = {
        "research_question": question,
        "data_classification": "public",
        "bound_signal_ids": ["sig-test-0001"],
        "public_context": {
            "evidence_summaries": {doc_ref: {"summary": "官方公告摘要", "sha256": sha}},
        },
    }
    if extra:
        payload.update(extra)
    if replay:
        payload["replay_response"] = {
            "schema_version": "1.0",
            "task_id": "placeholder",
            "summary": "REPLAY 草稿：需要人工确认。",
            "key_findings": ["发现一"],
            "unknowns": ["未知一"],
            "evidence_refs": [doc_ref],
            "human_questions": [
                {"question": "问题一？", "why_needed": "w", "options": []},
                {"question": "问题二？", "why_needed": "w", "options": []},
            ],
            "model_self_assessed_confidence": 0.4,
        }
    return payload


class Workspace(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.store = TaskStore(self.root / "t.sqlite3")
        # Real vault producer in the temp workspace (events chain + objects).
        import sys as _sys

        if str(Path(__file__).resolve().parent) not in _sys.path:
            _sys.path.insert(0, str(Path(__file__).resolve().parent))
        from _vault_fixture import import_document, vault_config

        self.vault_cfg = vault_config(self.root, {})
        _, self.doc_sha = import_document(self.vault_cfg, self.root, content="官方公告正文", title="官方公告摘要")
        self.doc_id = "document:sha256:{0}".format(self.doc_sha)

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def scoped(self):
        return project_path_override(lambda value, r=self.root: r / value)


class F01Tests(Workspace):
    """External egress must go through the real governance gate, fail closed."""

    def test_cli_rejects_non_public_spec_instead_of_forcing_public(self):
        from stock_agent.cli import _validated_public_spec

        spec = {"research_question": "q", "data_classification": "internal"}
        with self.assertRaises(SystemExit):
            _validated_public_spec(spec)
        ok = _validated_public_spec({"research_question": "q", "data_classification": "public"})
        self.assertEqual(ok["data_classification"], "public")

    def test_store_rejects_classification_patch_and_non_public_merge(self):
        task, _ = self.store.create_task(title="t", input_payload=_spec(), case_id="f01")
        with self.assertRaises(TaskTransitionError):
            self.store.record_human_response(
                task["task_id"], actor="u", decision="provide_input",
                expected_version=task["version"], command_id="p1",
                input_patch={"data_classification": "internal"},
            )
        with self.assertRaises(TaskTransitionError):
            self.store.record_human_response(
                task["task_id"], actor="u", decision="provide_input",
                expected_version=task["version"], command_id="p2",
                input_patch={"public_context": {"data_classification": "internal"}},
            )

    def test_secret_payload_blocked_before_model_with_governance(self):
        from stock_agent.research_worker import run_research_task

        task, _ = self.store.create_task(
            title="外发门禁", case_id="f01-secret",
            input_payload=_spec(question="内部备注 sk-abcdefgh12345678 需外发"),
            execution_mode="live",
        )
        claimed = self.store.claim_next("w1")
        self.assertEqual(claimed["task_id"], task["task_id"])
        with patch("stock_agent.pi_runtime.call_pi_agent") as stub, self.scoped():
            result = run_research_task(self.store, task["task_id"], worker_id="w1", config=LOCAL_CONFIG)
        self.assertEqual(stub.call_count, 0, "denied payload must never reach the model entry")
        self.assertEqual(result["status"], STATUS_BLOCKED)
        self.assertEqual(self.store.get_task(task["task_id"])["status"], STATUS_BLOCKED)


class F02F03Tests(Workspace):
    def _llm_response(self, content: str, usage: dict | None):
        from stock_agent.model_operations import LLMResponse

        meta = {"model": "deepseek-v4.1-flash", "request_id": "req-1", "usage": usage or {}}
        return LLMResponse(content, meta)

    def _worker_config(self):
        """Workspace vault paths + explicit policy relaxations for these tests."""

        cfg = json.loads(json.dumps(self.vault_cfg))
        mdg = cfg.setdefault("market_data_governance", {})
        mdg["approved_internal_research_providers"] = ["eastmoney"]
        mdg["use_production_pilot_provider_registry"] = False
        mdg["require_consensus_for_llm"] = False
        mdg["allow_degraded_offhours_llm"] = True
        return cfg

    def test_real_llm_response_success_records_usage(self):
        from stock_agent.research_worker import run_research_task

        task, _ = self.store.create_task(
            title="成功路径", case_id="f02",
            input_payload=_spec(question="评估", replay=False, doc_ref=self.doc_id, doc_sha=self.doc_sha),
            execution_mode="live",
        )
        claimed = self.store.claim_next("w1")
        output = {
            "schema_version": "1.0", "task_id": task["task_id"], "summary": "成功草稿",
            "key_findings": [], "unknowns": [], "evidence_refs": [self.doc_id],
            "human_questions": [], "model_self_assessed_confidence": 0.6,
        }
        response = self._llm_response(json.dumps(output, ensure_ascii=False), {"prompt_tokens": 10, "completion_tokens": 5})
        with patch("stock_agent.pi_runtime.call_pi_agent", return_value=response) as stub, self.scoped():
            result = run_research_task(self.store, task["task_id"], worker_id="w1", config=self._worker_config())
        self.assertEqual(stub.call_count, 1)
        self.assertEqual(result["status"], STATUS_SUCCEEDED, str(result.get("error") or result.get("model_call")))
        stored = self.store.get_task(task["task_id"])
        self.assertEqual(stored["model_call"]["usage"].get("prompt_tokens"), 10)
        self.assertEqual(stored["model_call"]["called"], True)
        self.assertIn("成功草稿", stored["output"]["summary"])

    def test_awaiting_human_persists_output_model_call_and_all_questions(self):
        from stock_agent.research_worker import run_research_task

        task, _ = self.store.create_task(
            title="持久化", case_id="f03",
            input_payload=_spec(doc_ref=self.doc_id, doc_sha=self.doc_sha), execution_mode="replay",
        )
        claimed = self.store.claim_next("w1")
        with self.scoped():
            result = run_research_task(self.store, task["task_id"], worker_id="w1", config=self._worker_config())
        self.assertEqual(result["status"], STATUS_AWAITING_HUMAN)
        stored = self.store.get_task(task["task_id"])
        self.assertNotEqual(stored["output"], {}, "research output must be persisted before awaiting human")
        self.assertNotEqual(stored["model_call"], {}, "model call audit must be persisted")
        questions = stored["human_request"].get("questions") or []
        self.assertEqual(len(questions), 2, "all human questions must be stored, not just the first")
        ack = self.store.record_human_response(
            task["task_id"], actor="u", decision="acknowledge",
            expected_version=stored["version"], command_id="a1",
        )
        self.assertIn("REPLAY", ack["output"]["summary"])


class F04F05Tests(Workspace):
    def test_version_increments_and_old_version_rejected(self):
        task, _ = self.store.create_task(title="v", input_payload=_spec(), case_id="f04")
        claimed = self.store.claim_next("w1")
        self.assertGreater(claimed["version"], task["version"], "claim must bump the row version")
        self.store.mark_running(task["task_id"], worker_id="w1", fencing_token=claimed["fencing_token"], expected_version=claimed["version"])
        awaiting = self.store.request_awaiting_human(
            task["task_id"], worker_id="w1", fencing_token=claimed["fencing_token"],
            human_request={"question": "Q"},
        )
        self.assertGreater(awaiting["version"], claimed["version"])
        with self.assertRaises(TaskVersionConflictError):
            self.store.record_human_response(
                task["task_id"], actor="stale-page", decision="acknowledge",
                expected_version=task["version"], command_id="old",
            )

    def test_cancelled_awaiting_human_cannot_be_acknowledged(self):
        task, _ = self.store.create_task(title="c", input_payload=_spec(), case_id="f05")
        claimed = self.store.claim_next("w1")
        self.store.mark_running(task["task_id"], worker_id="w1", fencing_token=claimed["fencing_token"], expected_version=claimed["version"])
        awaiting = self.store.request_awaiting_human(
            task["task_id"], worker_id="w1", fencing_token=claimed["fencing_token"],
            human_request={"question": "Q"},
        )
        cancelled = self.store.request_cancel(task["task_id"], actor="u", reason="r", expected_version=awaiting["version"])
        self.assertEqual(cancelled["status"], STATUS_CANCELLED)
        with self.assertRaises(TaskTransitionError):
            self.store.record_human_response(
                task["task_id"], actor="u", decision="acknowledge",
                expected_version=cancelled["version"], command_id="after-cancel",
            )


class F06Tests(Workspace):
    def test_distinct_requests_and_modes_are_not_merged(self):
        first, status1 = self.store.create_task(title="replay 研究", input_payload=_spec(), case_id="", execution_mode="replay")
        second, status2 = self.store.create_task(
            title="live 研究", input_payload=_spec(question="另一个独立问题", replay=False),
            case_id="", execution_mode="live",
        )
        self.assertEqual(status1, "RECORDED")
        self.assertEqual(status2, "RECORDED", "an independent request must never be treated as a duplicate")
        self.assertNotEqual(first["task_id"], second["task_id"])
        self.assertEqual(first["execution_mode"], "replay")
        self.assertEqual(second["execution_mode"], "live")

    def test_explicit_same_key_with_different_content_conflicts(self):
        self.store.create_task(title="A", input_payload=_spec(), case_id="x", idempotency_key="K1")
        with self.assertRaises(TaskIdempotencyConflictError):
            self.store.create_task(
                title="B", input_payload=_spec(question="不同内容"), case_id="x", idempotency_key="K1",
            )


class F07Tests(Workspace):
    def test_unknown_and_foreign_evidence_refs_rejected(self):
        from stock_agent.agent_protocol import IntentValidationError, validate_model_output

        good = _spec()["replay_response"]
        allowed = ["ev-0001"]
        validate_model_output(good, allowed_evidence_refs=allowed)
        for bad_refs in (["does-not-exist"], ["case2-ev-09"], ["ev-0000"]):
            with self.subTest(refs=bad_refs):
                with self.assertRaises(IntentValidationError):
                    validate_model_output(dict(good, evidence_refs=bad_refs), allowed_evidence_refs=allowed)

    def test_worker_rejects_output_with_fabricated_refs(self):
        from stock_agent.research_worker import run_research_task

        spec = _spec()
        spec["replay_response"]["evidence_refs"] = ["does-not-exist"]
        spec["replay_response"]["human_questions"] = []
        task, _ = self.store.create_task(title="f07", input_payload=spec, case_id="f07", execution_mode="replay")
        claimed = self.store.claim_next("w1")
        with self.scoped():
            result = run_research_task(self.store, task["task_id"], worker_id="w1", config={})
        self.assertEqual(result["status"], "failed")
        self.assertIn("evidence", str(result.get("error") or self.store.get_task(task["task_id"])["last_error"]).lower())


class F08Tests(Workspace):
    def test_renewed_lease_is_not_recovered(self):
        task, _ = self.store.create_task(title="r", input_payload=_spec(), case_id="f08")
        store_b = TaskStore(self.root / "t.sqlite3")
        try:
            worker = self.store.claim_next("w1", ttl_seconds=600)
            self.assertIsNotNone(worker)
            renewed = store_b.renew_lease(task["task_id"], worker_id="w1", fencing_token=worker["fencing_token"], ttl_seconds=600)
            self.assertGreaterEqual(renewed["version"], worker["version"])
            recovered = store_b.recover_abandoned(ttl_grace_seconds=0)
            self.assertEqual(recovered, [], "an actively renewed lease must not be recovered")
            self.assertEqual(store_b.get_task(task["task_id"])["status"], "leased")
        finally:
            store_b.close()

    def test_renewal_landing_after_snapshot_is_not_recovered(self):
        """The exact race from the review: the scanner materializes an expired
        snapshot, the worker renews via another connection AFTER the snapshot
        but BEFORE the scanner's write transaction opens; recovery must
        re-verify the fresh lease inside that transaction."""

        from datetime import datetime, timedelta

        task, _ = self.store.create_task(title="race", input_payload=_spec(), case_id="f08-race")
        store_b = TaskStore(self.root / "t.sqlite3")
        try:
            worker = self.store.claim_next("w1", ttl_seconds=600)
            past = (datetime.now().astimezone() - timedelta(seconds=600)).isoformat(timespec="seconds")
            self.store._conn.execute(
                "UPDATE research_tasks SET lease_expires_at = ? WHERE task_id = ?", (past, task["task_id"])
            )
            fired = [False]
            original_begin = store_b._begin
            outer = self

            def racing_begin():
                # Snapshot rows are already materialized when recovery opens
                # its per-task transaction; renew through the other connection
                # exactly here.
                if not fired[0]:
                    fired[0] = True
                    outer.store.renew_lease(
                        task["task_id"], worker_id="w1",
                        fencing_token=worker["fencing_token"], ttl_seconds=600,
                    )
                original_begin()

            store_b._begin = racing_begin
            recovered = store_b.recover_abandoned(ttl_grace_seconds=0)
            self.assertTrue(fired[0], "race injection must have fired")
            self.assertEqual(recovered, [], "recovery must re-verify the lease inside its write transaction")
            final = TaskStore(self.root / "t.sqlite3")
            try:
                self.assertEqual(final.get_task(task["task_id"])["status"], "leased")
            finally:
                final.close()
        finally:
            store_b.close()


class F09Tests(unittest.TestCase):
    def test_concurrent_same_stamp_writes_do_not_overlap(self):
        import stock_agent.reports as reports_module
        from stock_agent.reports import write_outputs

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            fixed = "2026-09-21T09:00:00+08:00"
            stats = {"run_coordination": {"run_id": "r", "mode": "run", "status": "COMPLETED"}}
            real_write = reports_module.atomic_write_text
            barrier = threading.Barrier(2, timeout=10)
            written: list[str] = []
            lock = threading.Lock()
            errors = []

            def isolated_write():
                try:
                    with project_path_override(lambda value: root / value):
                        write_outputs([], {"report": {}}, stats)
                except Exception as exc:
                    errors.append(exc)

            def barrier_write(path, text, **kwargs):
                path = Path(path)
                with lock:
                    written.append(str(path))
                if path.suffix == ".json":
                    barrier.wait()
                real_write(path, text, **kwargs)

            with project_path_override(lambda value, r=root: r / value), \
                 patch("stock_agent.reports.now_iso", return_value=fixed), \
                 patch.object(reports_module, "atomic_write_text", side_effect=barrier_write):
                threads = [
                    threading.Thread(target=isolated_write)
                    for _ in range(2)
                ]
                for thread in threads:
                    thread.start()
                for thread in threads:
                    thread.join()
            self.assertEqual(errors, [], "worker exceptions must fail the test")
            json_paths = sorted(item for item in written if item.endswith(".json"))
            self.assertEqual(len(json_paths), 2, "both runs must own their own JSON artifact")
            self.assertNotEqual(json_paths[0], json_paths[1])
            for item in json_paths:
                self.assertTrue(Path(item).exists())


if __name__ == "__main__":
    unittest.main()
