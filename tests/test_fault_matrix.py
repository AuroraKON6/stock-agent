"""GLM-11 fault matrix (offline slice): lock contention, concurrent claims,
cycle crash containment, cancel-vs-complete race, clock-drift robustness."""
import json
import sys
import tempfile
import threading
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from stock_agent.config import project_path_override
from stock_agent.task_intake import run_daily_task_cycle
from stock_agent.task_runtime import TaskStore
from stock_agent.task_supply import build_task_spec

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _task_fixture_helpers import final_replay_response

NOW = datetime.now(timezone.utc)


def _spec(question):
    spec = build_task_spec(research_question=question, symbols=["600519"],
                           execution_mode="replay", config=None)
    spec["bound_signal_ids"] = []
    spec["public_context"]["evidence_summaries"] = {}
    spec["public_context"]["quote_snapshots"] = {}
    spec["public_context"]["news_clusters"] = {}
    spec["replay_response"] = final_replay_response()
    return spec


class FaultMatrixTestCase(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.enterContext(project_path_override(lambda value: self.root / value))
        self.db_path = self.root / "tasks.sqlite3"
        self.store = TaskStore(self.db_path)
        self.addCleanup(self.store.close)

    def seed_task(self, name):
        task, _ = self.store.create_task(title=name, input_payload=_spec(name),
                                         execution_mode="replay")
        return task


class LockContentionTests(FaultMatrixTestCase):
    def test_write_blocks_then_succeeds_after_commit(self):
        holder = TaskStore(self.db_path)
        self.addCleanup(holder.close)
        holder._begin()  # hold the single write lock
        # Build the payload in the MAIN thread: the path override context is
        # not visible to worker threads, so the thread only performs the write.
        payload = _spec("contender")

        result = {}

        def contender():
            result["outcome"] = self.store.create_task(
                title="contender", input_payload=payload, execution_mode="replay")

        thread = threading.Thread(target=contender)
        thread.start()
        time.sleep(0.3)
        self.assertTrue(thread.is_alive(), "write must block while the lock is held")
        holder._commit()  # release
        thread.join(timeout=10)
        self.assertFalse(thread.is_alive())
        task, status = result["outcome"]
        self.assertEqual(status, "RECORDED")
        self.assertEqual(self.store.get_task(task["task_id"])["status"], "queued")

    def test_lock_timeout_raises_clean_without_partial_state(self):
        # Shorten this connection's busy wait so the test fails fast.
        self.store._conn.execute("PRAGMA busy_timeout=200")
        holder = TaskStore(self.db_path)
        self.addCleanup(holder.close)
        holder._begin()
        try:
            with self.assertRaises(Exception) as raised:
                self.seed_task("never-created")
            self.assertIn("locked", str(raised.exception).lower())
        finally:
            holder._commit()
        # No partial state: the failed creation left neither task nor event.
        self.assertEqual(self.store.list_tasks(), [])
        self.assertEqual(self.store.events(limit=1000), [])
        # And the store recovers for the next write.
        task = self.seed_task("after-lock")
        self.assertEqual(task["status"], "queued")


class ConcurrentClaimTests(FaultMatrixTestCase):
    def test_two_workers_never_claim_the_same_task(self):
        first = self.seed_task("task-one")
        second = self.seed_task("task-two")
        other = TaskStore(self.db_path)
        self.addCleanup(other.close)
        claimed_a = self.store.claim_next("worker-a", ttl_seconds=60)
        claimed_b = other.claim_next("worker-b", ttl_seconds=60)
        ids = {claimed_a["task_id"], claimed_b["task_id"]}
        self.assertEqual(ids, {first["task_id"], second["task_id"]})
        self.assertNotEqual(claimed_a["worker_id"], claimed_b["worker_id"])
        self.assertIsNone(self.store.claim_next("worker-c", ttl_seconds=60))


class CancelRaceTests(FaultMatrixTestCase):
    def test_cancel_flag_wins_over_late_completion(self):
        task = self.seed_task("race-task")
        claimed = self.store.claim_next("w1")
        self.store.mark_running(task["task_id"], worker_id="w1",
                                fencing_token=claimed["fencing_token"],
                                expected_version=claimed["version"])
        current = self.store.get_task(task["task_id"])
        self.store.request_cancel(task["task_id"], actor="operator", reason="late cancel",
                                  expected_version=current["version"])
        # The worker tries to complete with its still-valid token: cancellation
        # must win, not the late result.
        final = self.store.complete(task["task_id"], worker_id="w1",
                                    fencing_token=claimed["fencing_token"],
                                    output={"summary": "late"}, model_call={})
        self.assertEqual(final["status"], "cancelled")
        events = [e["event_type"] for e in self.store.events(task["task_id"])]
        self.assertIn("task_cancelled", events)


class ClockDriftTests(FaultMatrixTestCase):
    def test_unparseable_lease_timestamp_is_skipped_not_crashed(self):
        task = self.seed_task("drift-task")
        claimed = self.store.claim_next("w1")
        self.store._conn.execute(
            "UPDATE research_tasks SET status='running', lease_expires_at='not-a-time' WHERE task_id = ?",
            (task["task_id"],))
        self.store._conn.commit()
        recovered = self.store.recover_abandoned(ttl_grace_seconds=0)
        # The unparseable lease is skipped; nothing crashes or gets requeued.
        self.assertEqual(recovered, [])
        self.assertEqual(self.store.get_task(task["task_id"])["status"], "running")


class CycleCrashContainmentTests(FaultMatrixTestCase):
    def test_poisoned_worker_fails_one_task_and_cycle_continues(self):
        config = {"task_runtime": {"db_path": str(self.db_path),
                                   "event_intake_mode": "task"}}
        good = self.seed_task("good-task")
        doomed = self.seed_task("doomed-task")
        from unittest.mock import patch
        import stock_agent.research_worker as worker_module

        real_run = worker_module.run_research_task

        def poisoned(store, task_id, **kwargs):
            if task_id == doomed["task_id"]:
                raise RuntimeError("simulated worker crash")
            return real_run(store, task_id, **kwargs)

        with patch.object(worker_module, "run_research_task", side_effect=poisoned):
            report = run_daily_task_cycle(self.store, config=config)
        # The doomed task claims, crashes (attempt consumed, requeued),
        # claims again, crashes terminally - then the good task succeeds.
        self.assertEqual(report["processed"], 3)
        self.assertEqual(report["failed"], 1)
        self.assertEqual(report["retried"], 1)
        self.assertEqual(report["succeeded"], 1)
        self.assertEqual(report["remaining_queued"], 0)
        self.assertEqual(self.store.get_task(doomed["task_id"])["status"], "failed")
        self.assertIn("cycle worker crashed",
                      self.store.get_task(doomed["task_id"])["last_error"])
        self.assertEqual(self.store.get_task(good["task_id"])["status"], "succeeded")


if __name__ == "__main__":
    unittest.main()
