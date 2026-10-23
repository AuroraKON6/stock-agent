"""R23: layered observability — technical health vs business gates, attempt
outcomes, queue age, reconciliation backlog. Read-only by construction."""
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from stock_agent.config import project_path_override
from stock_agent.observability import build_observability_status
from stock_agent.task_runtime import TaskStore
from stock_agent.task_runtime.store import (
    ATTEMPT_DISPATCHED,
    ATTEMPT_FAILED_KNOWN,
    ATTEMPT_OUTCOME_UNKNOWN,
    ATTEMPT_SUCCEEDED,
)

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _vault_fixture import vault_config

NOW = datetime.now(timezone.utc)


def _spec(config, question):
    from stock_agent.task_supply import build_task_spec

    spec = build_task_spec(research_question=question, symbols=["600519"],
                           execution_mode="replay", config=config)
    spec["bound_signal_ids"] = []
    spec["public_context"]["evidence_summaries"] = {}
    return spec


class ObservabilityTestCase(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.enterContext(project_path_override(lambda value: self.root / value))
        self.config = vault_config(self.root, {})
        self.store = TaskStore(self.root / "tasks.sqlite3")
        self.addCleanup(self.store.close)

    def _task(self, question):
        task, _ = self.store.create_task(title=question,
                                         input_payload=_spec(self.config, question),
                                         execution_mode="replay")
        return task

    def _blocked(self, question, error):
        task = self._task(question)
        claimed = self.store.claim_next("w")
        self.store.mark_running(task["task_id"], worker_id="w",
                                fencing_token=claimed["fencing_token"],
                                expected_version=claimed["version"])
        self.store.mark_blocked(task["task_id"], worker_id="w",
                                fencing_token=claimed["fencing_token"], error=error)
        return task


class LayeringTests(ObservabilityTestCase):
    def test_technical_green_business_blocked_reported_separately(self):
        self._blocked("预算阻断研究", "daily model-call budget exhausted: 2/2 attempts")
        status = build_observability_status(self.store)
        self.assertEqual(status["technical"]["queue_depth"], 0)
        self.assertEqual(status["technical"]["stale_leases"], 0)
        self.assertEqual(status["business"]["gate_counts"]["budget"], 1,
                         "the business gate is visible without inventing a technical fault")
        self.assertEqual(status["business"]["blocked_tasks"], 1)
        self.assertNotIn("error", status["technical"])

    def test_gate_categorization(self):
        self._blocked("质量阻断", "specific findings carry numbers without any citation; "
                                  "the draft is saved as an evidence gap")
        self._blocked("未知外发", "model invocation outcome unknown (attempt att1); "
                                 "manual reconciliation required")
        self._blocked("治理阻断", "task input classification is not public")
        self._blocked("其他", "some uncategorized condition")
        status = build_observability_status(self.store)
        counts = status["business"]["gate_counts"]
        self.assertEqual(counts["evidence_quality"], 1)
        self.assertEqual(counts["invocation_outcome_unknown"], 1)
        self.assertEqual(counts["governance"], 1)
        self.assertEqual(counts["other"], 1)
        self.assertTrue(status["business"]["blocked_sample"])

    def test_read_only(self):
        self._task("只读检查")
        before = self.store.events(limit=100000)
        build_observability_status(self.store)
        after = self.store.events(limit=100000)
        self.assertEqual(len(before), len(after),
                         "building the status never writes business events")


class QueueAgeTests(ObservabilityTestCase):
    def test_oldest_queued_age(self):
        old = self._task("老任务")
        self.store._conn.execute(
            "UPDATE research_tasks SET created_at = ? WHERE task_id = ?",
            ((datetime.now(timezone.utc) - timedelta(hours=3)).isoformat(),
             old["task_id"]))
        self.store._conn.commit()
        self._task("新任务")
        status = build_observability_status(self.store)
        self.assertEqual(status["technical"]["queue_depth"], 2)
        self.assertGreaterEqual(status["technical"]["oldest_queued_age_seconds"],
                                3 * 3600 - 60)


class AttemptOutcomeTests(ObservabilityTestCase):
    def test_attempt_chain_counts_and_reconciliation_backlog(self):
        task = self._task("外发账本")
        claimed = self.store.claim_next("w")
        token = claimed["fencing_token"]

        def _attempt(round_index, close=None, error=""):
            record = self.store.prepare_invocation_attempt(
                task["task_id"], worker_id="w", fencing_token=token,
                round_index=round_index, provider="p", request_model="m",
                prompt_sha256="a" * 64)
            self.store.mark_invocation_dispatched(record["attempt_id"],
                                                  worker_id="w", fencing_token=token)
            if close:
                self.store.complete_invocation_attempt(
                    record["attempt_id"], worker_id="w", fencing_token=token,
                    state=close, sent="unknown" if close == ATTEMPT_OUTCOME_UNKNOWN else "yes",
                    error=error)
            return record

        _attempt(1, close=ATTEMPT_SUCCEEDED)
        _attempt(2, close=ATTEMPT_FAILED_KNOWN, error="auth rejected")
        unknown = _attempt(3, close=ATTEMPT_OUTCOME_UNKNOWN, error="timeout")
        _attempt(4)  # still dispatched

        status = build_observability_status(self.store)
        attempts = status["technical"]["invocation_attempts"]
        self.assertEqual(attempts["succeeded"], 1)
        self.assertEqual(attempts["failed_known"], 1)
        self.assertEqual(attempts["outcome_unknown"], 1)
        self.assertEqual(attempts["outstanding"], 1)
        self.assertEqual(status["technical"]["reconciliations_waiting"], 1)

        # Reconciling the unknown clears the backlog (read reflects reality).
        self.store.reconcile_invocation_attempt(
            unknown["attempt_id"], actor="ops", resolution="confirmed_not_charged",
            reason="供应商回执无计费记录")
        refreshed = build_observability_status(self.store)
        self.assertEqual(refreshed["technical"]["reconciliations_waiting"], 0)
        self.assertEqual(refreshed["technical"]["invocation_attempts"]["outcome_unknown"], 0)
        self.assertEqual(refreshed["technical"]["invocation_attempts"]["reconciled"], 1,
                         "the reconciled terminal is its own visible outcome")


class StaleLeaseTests(ObservabilityTestCase):
    def test_expired_lease_counted(self):
        task = self._task("过期租约")
        self.store.claim_next("w")
        past = (datetime.now().astimezone() - timedelta(minutes=10)).isoformat(timespec="seconds")
        self.store._conn.execute(
            "UPDATE research_tasks SET status='running', lease_expires_at=? WHERE task_id=?",
            (past, task["task_id"]))
        self.store._conn.commit()
        status = build_observability_status(self.store)
        self.assertEqual(status["technical"]["stale_leases"], 1)


if __name__ == "__main__":
    unittest.main()
