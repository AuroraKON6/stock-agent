"""R02: outbound attempt chain, unknown-outcome reconciliation, lease heartbeat.

Acceptance scenarios from GLM_REFACTOR_TASKBOOK_20261020 (A06–A10) plus the
supporting invariants: failure classification (not-sent / known / unknown),
in-call lease heartbeat on its own connection, no-new-outbound after a renewal
failure (F04), crash recovery that refuses to blindly requeue a task whose
request may have been sent, and budget retention for unknown outcomes (A08).
All data lives in a TemporaryDirectory via project_path_override; zero network
(pi runtime is patched), zero real model calls.
"""
import json
import sys
import tempfile
import time
import unittest
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from stock_agent.config import project_path_override
from stock_agent.invocation_runtime import (
    HeartbeatRunner,
    ModelCallNotSent,
    ModelKnownFailure,
    ModelOutcomeUnknown,
    classify_exception,
    classify_to_exception,
    heartbeat_interval_seconds,
    lease_heartbeat,
)
from stock_agent.model_operations import LLMResponse
from stock_agent.pi_runtime import PiRuntimeError
from stock_agent.task_runtime import TaskStore
from stock_agent.task_runtime.store import (
    ATTEMPT_FAILED_KNOWN,
    ATTEMPT_OUTCOME_UNKNOWN,
    ATTEMPT_RECONCILED,
    STATUS_BLOCKED,
    STATUS_QUEUED,
    TaskIdempotencyConflictError,
    TaskRuntimeError,
    TaskTransitionError,
)
from stock_agent.task_supply import build_task_spec

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _vault_fixture import vault_config

NOW = datetime.now(timezone.utc)


def _pi_error(message: str, *, stage: str = "", sent_hint: str = "") -> PiRuntimeError:
    error = PiRuntimeError(message)
    error.failure_stage = stage
    error.sent_hint = sent_hint
    return error


def _spec(question="R02 外发验收", symbols=("600519",), **extra):
    spec = build_task_spec(
        research_question=question, symbols=list(symbols),
        execution_mode="replay", config=None)
    spec["bound_signal_ids"] = []
    spec["public_context"]["evidence_summaries"] = {}
    spec["public_context"]["quote_snapshots"] = {
        "600519": {"price": 1500.0, "pct_change": 1.0, "provider": "eastmoney",
                   "source_time": (NOW - timedelta(minutes=2)).isoformat(),
                   "source_time_status": "verified",
                   "observed_at": (NOW - timedelta(minutes=1)).isoformat()},
    }
    spec.update(extra)
    return spec


class R02WorkerTestCase(unittest.TestCase):
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

    def create_live_task(self, **spec_extra):
        task, _ = self.store.create_task(
            title="R02 任务", input_payload=_spec(**spec_extra), execution_mode="live")
        claimed = self.store.claim_next("w-r02")
        self.assertEqual(claimed["task_id"], task["task_id"])
        return task

    def run_worker(self, task, config=None):
        from stock_agent.research_worker import run_research_task

        return run_research_task(self.store, task["task_id"], worker_id="w-r02",
                                 config=config if config is not None else self.config,
                                 ttl_seconds=120)

    def response(self, payload, model="deepseek-flash"):
        meta = {"model": model, "request_id": "req-r02", "usage": {"prompt_tokens": 3}}
        return LLMResponse(json.dumps(payload, ensure_ascii=False), meta)

    def final_output(self, task_id):
        return {"schema_version": "1.0", "task_id": task_id, "summary": "最终结论",
                "key_findings": [], "unknowns": [], "evidence_refs": [],
                "human_questions": [], "model_self_assessed_confidence": 0.5}


class ClassificationTests(unittest.TestCase):
    def test_structural_hints_take_precedence(self):
        sent, stage = classify_exception(_pi_error("Pi wall-clock timeout after 60s",
                                                   stage="process", sent_hint="unknown"))
        self.assertEqual((sent, stage), ("unknown", "process"))
        sent, stage = classify_exception(_pi_error("Pi Agent runtime is disabled",
                                                   stage="preflight", sent_hint="no"))
        self.assertEqual((sent, stage), ("no", "preflight"))
        sent, stage = classify_exception(_pi_error("Pi response contained no assistant text",
                                                   stage="parse", sent_hint="yes"))
        self.assertEqual((sent, stage), ("yes", "parse"))

    def test_untagged_falls_back_to_markers_then_conservative_unknown(self):
        self.assertEqual(classify_exception(RuntimeError("Connection refused (getaddrinfo failed)"))[0], "no")
        self.assertEqual(classify_exception(OSError("HTTP Error 429: Too Many Requests"))[0], "yes")
        # Default is unknown: an unclassifiable failure after dispatch never
        # earns an automatic retry.
        self.assertEqual(classify_exception(RuntimeError("something odd happened"))[0], "unknown")

    def test_typed_mapping(self):
        self.assertIsInstance(classify_to_exception(RuntimeError("Connection refused")), ModelCallNotSent)
        self.assertIsInstance(classify_to_exception(RuntimeError("weird")), ModelOutcomeUnknown)
        self.assertIsInstance(classify_to_exception(RuntimeError("HTTP Error 401 Unauthorized")), ModelKnownFailure)


class AttemptChainTests(R02WorkerTestCase):
    def test_a06_not_sent_failure_requeues_within_attempt_budget(self):
        task = self.create_live_task()
        with patch("stock_agent.pi_runtime.call_pi_agent",
                   side_effect=_pi_error("Pi Agent runtime is disabled",
                                         stage="preflight", sent_hint="no")) as stub:
            result = self.run_worker(task)
        self.assertEqual(stub.call_count, 1)
        self.assertEqual(result["status"], STATUS_QUEUED,
                         "a provably-not-sent failure is safely retryable")
        stored = self.store.get_task(task["task_id"])
        self.assertEqual(stored["status"], STATUS_QUEUED)
        self.assertEqual(stored["attempt_count"], 1)
        attempts = self.store.invocation_attempts(task["task_id"])
        self.assertEqual(len(attempts), 1)
        self.assertEqual(attempts[0]["state"], ATTEMPT_FAILED_KNOWN)
        self.assertEqual(attempts[0]["sent"], "no")
        # Bounded: with the last attempt spent, the same failure is terminal.
        second = self.store.claim_next("w-r02")
        self.assertEqual(second["task_id"], task["task_id"])
        with patch("stock_agent.pi_runtime.call_pi_agent",
                   side_effect=_pi_error("Pi Agent runtime is disabled",
                                         stage="preflight", sent_hint="no")):
            result2 = self.run_worker(task)
        self.assertEqual(result2["status"], "failed")
        self.assertEqual(self.store.get_task(task["task_id"])["status"], "failed")

    def test_a07_timeout_after_send_is_unknown_and_never_auto_requeued(self):
        task = self.create_live_task()
        with patch("stock_agent.pi_runtime.call_pi_agent",
                   side_effect=_pi_error("Pi wall-clock timeout after 60s",
                                         stage="process", sent_hint="unknown")) as stub:
            result = self.run_worker(task)
        self.assertEqual(stub.call_count, 1,
                         "an unknown outcome must not trigger a second dispatch")
        self.assertEqual(result["status"], STATUS_BLOCKED)
        stored = self.store.get_task(task["task_id"])
        self.assertEqual(stored["status"], STATUS_BLOCKED)
        self.assertIn("manual reconciliation", stored["last_error"])
        attempts = self.store.invocation_attempts(task["task_id"])
        self.assertEqual(len(attempts), 1)
        self.assertEqual(attempts[0]["state"], ATTEMPT_OUTCOME_UNKNOWN)
        self.assertEqual(attempts[0]["sent"], "unknown")
        self.assertTrue(attempts[0]["budget_event_id"],
                        "the attempt must link its budget reservation event")
        # Even though attempts remain, nothing is queued for pickup.
        self.assertIsNone(self.store.claim_next("w-other"))

    def test_known_provider_failure_is_not_auto_retried(self):
        task = self.create_live_task()
        with patch("stock_agent.pi_runtime.call_pi_agent",
                   side_effect=OSError("HTTP Error 401: Unauthorized")) as stub:
            result = self.run_worker(task)
        self.assertEqual(stub.call_count, 1)
        self.assertEqual(result["status"], "failed")
        attempts = self.store.invocation_attempts(task["task_id"])
        self.assertEqual(attempts[0]["state"], ATTEMPT_FAILED_KNOWN)
        self.assertEqual(attempts[0]["sent"], "yes")

    def test_a08_unknown_outcome_keeps_budget_held(self):
        from stock_agent.budget_guard import may_start_model_call

        self.config.setdefault("task_runtime", {})["budget"] = {"daily_model_call_limit": 2}
        task = self.create_live_task()
        with patch("stock_agent.pi_runtime.call_pi_agent",
                   side_effect=_pi_error("Pi wall-clock timeout after 60s",
                                         stage="process", sent_hint="unknown")):
            self.run_worker(task)
        attempt = self.store.invocation_attempts(task["task_id"])[0]
        # The reservation is still counted: unknown does not refund budget.
        view = may_start_model_call(self.store, self.config)
        self.assertEqual(view["calls_today"], 1)
        # Reconciliation does not erase the reservation history either.
        self.store.reconcile_invocation_attempt(
            attempt["attempt_id"], actor="ops-lead",
            resolution="confirmed_not_charged",
            reason="供应商回执查询：该 request_id 无计费记录（回执编号 r-1）")
        self.assertEqual(may_start_model_call(self.store, self.config)["calls_today"], 1)
        self.assertEqual(len([e for e in self.store.events(task["task_id"])
                              if e["event_type"] == "research_attempt_started"]), 1)

    def test_successful_call_records_succeeded_attempt(self):
        task = self.create_live_task()
        with patch("stock_agent.pi_runtime.call_pi_agent",
                   return_value=self.response(self.final_output(task["task_id"]))):
            result = self.run_worker(task)
        self.assertEqual(result["status"], "succeeded")
        attempts = self.store.invocation_attempts(task["task_id"])
        self.assertEqual(len(attempts), 1)
        self.assertEqual(attempts[0]["state"], "succeeded")
        self.assertEqual(attempts[0]["sent"], "yes")
        self.assertEqual(result["model_call"]["rounds"][0]["attempt_id"], attempts[0]["attempt_id"])


class CrashRecoveryTests(R02WorkerTestCase):
    def _expire_lease(self, task_id):
        past = (datetime.now().astimezone() - timedelta(minutes=10)).isoformat(timespec="seconds")
        self.store._conn.execute(
            "UPDATE research_tasks SET status='running', lease_expires_at = ? WHERE task_id = ?",
            (past, task_id))
        self.store._conn.commit()

    def test_a09_dispatched_attempt_blocks_recovery_requeue(self):
        task, _ = self.store.create_task(
            title="崩溃恢复", input_payload=_spec(), execution_mode="live")
        claimed = self.store.claim_next("w-dying")
        self.store.mark_running(task["task_id"], worker_id="w-dying",
                                fencing_token=claimed["fencing_token"],
                                expected_version=claimed["version"])
        invocation = self.store.prepare_invocation_attempt(
            task["task_id"], worker_id="w-dying", fencing_token=claimed["fencing_token"],
            round_index=1, provider="deepseek", request_model="m1",
            prompt_sha256="a" * 64, budget_event_id="evt-dying")
        self.store.mark_invocation_dispatched(
            invocation["attempt_id"], worker_id="w-dying", fencing_token=claimed["fencing_token"])
        self._expire_lease(task["task_id"])

        recovered = self.store.recover_abandoned(ttl_grace_seconds=0)
        self.assertEqual(len(recovered), 1)
        self.assertEqual(self.store.get_task(task["task_id"])["status"], STATUS_BLOCKED,
                         "a maybe-sent request must not be blindly requeued")
        attempt = self.store.invocation_attempts(task["task_id"])[0]
        self.assertEqual(attempt["state"], ATTEMPT_OUTCOME_UNKNOWN)
        self.assertEqual(attempt["sent"], "unknown")
        events = [e["event_type"] for e in self.store.events(task["task_id"])]
        self.assertIn("invocation_outcome_unknown", events)
        self.assertIsNone(self.store.claim_next("w-fresh"))

    def test_prepared_only_attempt_is_safe_to_requeue(self):
        task, _ = self.store.create_task(
            title="准备即崩", input_payload=_spec(), execution_mode="live")
        claimed = self.store.claim_next("w-dying2")
        self.store.prepare_invocation_attempt(
            task["task_id"], worker_id="w-dying2", fencing_token=claimed["fencing_token"],
            round_index=1, provider="deepseek", request_model="m1",
            prompt_sha256="b" * 64, budget_event_id="evt-prepared")
        self._expire_lease(task["task_id"])

        self.store.recover_abandoned(ttl_grace_seconds=0)
        self.assertEqual(self.store.get_task(task["task_id"])["status"], STATUS_QUEUED,
                         "nothing provably left the machine: requeue stays safe")
        attempt = self.store.invocation_attempts(task["task_id"])[0]
        self.assertEqual(attempt["state"], ATTEMPT_FAILED_KNOWN)
        self.assertEqual(attempt["sent"], "no")

    def test_recovery_without_attempts_still_reuses_old_semantics(self):
        task, _ = self.store.create_task(
            title="无 attempt 崩溃", input_payload=_spec(), execution_mode="live")
        self.store.claim_next("w-none")
        self._expire_lease(task["task_id"])
        self.store.recover_abandoned(ttl_grace_seconds=0)
        self.assertEqual(self.store.get_task(task["task_id"])["status"], STATUS_QUEUED)


class ReconciliationTests(R02WorkerTestCase):
    def _unknown_attempt(self):
        task, _ = self.store.create_task(
            title="对账", input_payload=_spec(), execution_mode="live")
        claimed = self.store.claim_next("w-rec")
        self.store.mark_running(task["task_id"], worker_id="w-rec",
                                fencing_token=claimed["fencing_token"],
                                expected_version=claimed["version"])
        invocation = self.store.prepare_invocation_attempt(
            task["task_id"], worker_id="w-rec", fencing_token=claimed["fencing_token"],
            round_index=1, provider="deepseek", request_model="m",
            prompt_sha256="c" * 64, budget_event_id="evt-rec")
        self.store.mark_invocation_dispatched(
            invocation["attempt_id"], worker_id="w-rec", fencing_token=claimed["fencing_token"])
        self.store.complete_invocation_attempt(
            invocation["attempt_id"], worker_id="w-rec", fencing_token=claimed["fencing_token"],
            state=ATTEMPT_OUTCOME_UNKNOWN, sent="unknown", error="timeout")
        self.store.mark_blocked(task["task_id"], worker_id="w-rec",
                                fencing_token=claimed["fencing_token"],
                                error="model invocation outcome unknown; manual reconciliation required")
        return task, invocation["attempt_id"]

    def test_a10_reconciliation_records_evidence_and_can_unlock(self):
        task, attempt_id = self._unknown_attempt()
        with self.assertRaises(TaskRuntimeError):
            self.store.reconcile_invocation_attempt(
                attempt_id, actor="ops", resolution="maybe_charged", reason="x")
        with self.assertRaises(TaskRuntimeError):
            self.store.reconcile_invocation_attempt(
                attempt_id, actor="ops", resolution="confirmed_charged", reason="  ")
        result = self.store.reconcile_invocation_attempt(
            attempt_id, actor="ops-lead", resolution="confirmed_not_charged",
            reason="供应商回执：request_id 无计费记录（票据号 rcpt-7）",
            requeue_if_blocked=True)
        self.assertEqual(result["state"], ATTEMPT_RECONCILED)
        self.assertEqual(result["reconciled_by"], "ops-lead")
        self.assertEqual(result["reconciliation"], "confirmed_not_charged")
        self.assertTrue(result["requeued"])
        self.assertEqual(self.store.get_task(task["task_id"])["status"], STATUS_QUEUED)
        events = [e["event_type"] for e in self.store.events(task["task_id"])]
        self.assertIn("invocation_reconciled", events)
        self.assertIn("task_requeued", events)

    def test_reconciliation_replay_is_idempotent_and_conflicts_on_flip(self):
        task, attempt_id = self._unknown_attempt()
        first = self.store.reconcile_invocation_attempt(
            attempt_id, actor="ops", resolution="confirmed_charged", reason="回执显示已计费")
        self.assertFalse(first.get("replay"))
        replay = self.store.reconcile_invocation_attempt(
            attempt_id, actor="ops", resolution="confirmed_charged", reason="回执显示已计费")
        self.assertTrue(replay["replay"])
        with self.assertRaises(TaskIdempotencyConflictError):
            self.store.reconcile_invocation_attempt(
                attempt_id, actor="ops", resolution="confirmed_not_charged", reason="改口")

    def test_non_unknown_attempt_cannot_be_reconciled(self):
        task, _ = self.store.create_task(
            title="不可对账", input_payload=_spec(), execution_mode="live")
        claimed = self.store.claim_next("w-x")
        invocation = self.store.prepare_invocation_attempt(
            task["task_id"], worker_id="w-x", fencing_token=claimed["fencing_token"],
            round_index=1, provider="p", request_model="m", prompt_sha256="d" * 64)
        with self.assertRaises(TaskTransitionError):
            self.store.reconcile_invocation_attempt(
                invocation["attempt_id"], actor="ops",
                resolution="confirmed_charged", reason="still prepared")


class HeartbeatTests(unittest.TestCase):
    def test_interval_is_at_least_two_beats_per_ttl(self):
        self.assertEqual(heartbeat_interval_seconds(120), 40)
        self.assertEqual(heartbeat_interval_seconds(6), 5)
        self.assertEqual(heartbeat_interval_seconds(3), 5)

    def test_runner_beats_and_records_failure(self):
        beats = {"n": 0}

        def renew():
            beats["n"] += 1
            return True

        with HeartbeatRunner(renew_fn=renew, interval_seconds=0.05) as runner:
            time.sleep(0.25)
        self.assertGreaterEqual(runner.beats, 2)
        self.assertTrue(runner.healthy)

        def failing():
            return False

        with HeartbeatRunner(renew_fn=failing, interval_seconds=0.05) as runner2:
            time.sleep(0.2)
        self.assertFalse(runner2.healthy)
        self.assertTrue(runner2.last_error)

    def test_lease_heartbeat_renews_on_its_own_connection(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        self.enterContext(project_path_override(lambda value: root / value))
        store = TaskStore(root / "hb.sqlite3")
        self.addCleanup(store.close)
        task, _ = store.create_task(title="心跳", input_payload=_spec(), execution_mode="live")
        claimed = store.claim_next("w-hb", ttl_seconds=2)
        before = store.get_task(task["task_id"])["lease_expires_at"]
        with lease_heartbeat(store.db_path, task["task_id"], worker_id="w-hb",
                             fencing_token=claimed["fencing_token"],
                             ttl_seconds=6, interval_seconds=0.1) as runner:
            time.sleep(0.35)
        self.assertGreaterEqual(runner.beats, 2)
        self.assertTrue(runner.healthy)
        after = store.get_task(task["task_id"])["lease_expires_at"]
        self.assertGreater(after, before, "renewals during the call must extend the lease")


class HeartbeatGateTests(R02WorkerTestCase):
    def test_failed_heartbeat_forbids_next_round_dispatch(self):
        task = self.create_live_task()
        intents = [{"schema_version": "1.0", "task_id": task["task_id"],
                    "action": "read_public_quote_snapshot",
                    "parameters": {"symbol": "600519"}, "reason_summary": "需要更多数据"}]
        round1 = dict(self.final_output(task["task_id"]), summary="中间轮",
                      requested_intents=intents)

        class _FailingRunner:
            beats = 1
            healthy = False
            last_error = "renewal callback reported failure"

        @contextmanager
        def _failing_heartbeat(*args, **kwargs):
            yield _FailingRunner()

        with patch("stock_agent.research_worker.lease_heartbeat", _failing_heartbeat), \
             patch("stock_agent.pi_runtime.call_pi_agent",
                   return_value=self.response(round1)) as stub:
            result = self.run_worker(task)
        self.assertEqual(stub.call_count, 1,
                         "a failed heartbeat must forbid the next round's dispatch")
        self.assertEqual(result["status"], STATUS_QUEUED)
        stored = self.store.get_task(task["task_id"])
        self.assertEqual(stored["status"], STATUS_QUEUED)
        self.assertIn("heartbeat", str(result.get("error") or ""))
        # The one completed round was recorded as a succeeded attempt; the
        # unbudgeted second round never happened.
        attempts = self.store.invocation_attempts(task["task_id"])
        self.assertEqual(len(attempts), 1)
        self.assertEqual(attempts[0]["state"], "succeeded")


if __name__ == "__main__":
    unittest.main()
