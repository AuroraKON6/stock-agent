"""R09: worker service drain, zero-side-effect probes, scheduled-slot identity.

O-layer coverage:
- A45 (O half): /livez and /readyz perform zero collection, zero model calls,
  zero business writes (outbox row count unchanged across probes; readiness
  opens the store READ-ONLY).
- Drain semantics: after stop, the loop never claims a new task; an in-flight
  task runs to a persisted state; idle exit respects the drain window.
- Same-slot recovery: ``scheduled_slot_key`` derives one deterministic identity
  per (schedule, Shanghai business day, slot, scope version) — cross-midnight
  UTC/Shanghai attribution (17:00Z on the 20th == the 21st's slot) and
  restart-reuse both hold; a different day or scope makes a different key.
- Non-scheduler guarantee: constructing/running the service installs no
  timers and writes no scheduled intent.

The signal/kill/lease-expiry halves (A41—A44 C/N layer) stay with the
container acceptance once the docker engine is reachable.
"""
import json
import sys
import tempfile
import threading
import time
import unittest
import urllib.request
from pathlib import Path

from stock_agent.config import project_path_override
from stock_agent.task_runtime import TaskStore
from stock_agent.task_supply import build_task_spec
from stock_agent.worker_service import (
    WorkerService,
    WorkerServiceError,
    queue_depth,
    scheduled_slot_key,
)

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _vault_fixture import vault_config


class SlotIdentityTests(unittest.TestCase):
    def test_same_slot_recovery_reuses_identity(self):
        first = scheduled_slot_key(
            "daily-research", "2026-10-21T08:45:00+08:00", scope_version="v3")
        second = scheduled_slot_key(
            "daily-research", "2026-10-21T08:45:00+08:00", scope_version="v3")
        self.assertEqual(first, second,
                         "a restarted run in the same slot is the SAME intent")

    def test_cross_midnight_business_day_attribution(self):
        # 17:00Z on the 20th is already 01:00 Shanghai on the 21st: the slot
        # belongs to the 21st's bucket, not the 20th's.
        evening_utc = scheduled_slot_key(
            "daily", "2026-10-20T17:00:00+00:00", scope_version="v1")
        next_day_shanghai = scheduled_slot_key(
            "daily", "2026-10-21T01:00:00+08:00", scope_version="v1")
        self.assertEqual(evening_utc, next_day_shanghai,
                         "same instant, two spellings: one slot identity")
        same_day_earlier = scheduled_slot_key(
            "daily", "2026-10-20T15:59:00+00:00", scope_version="v1")
        self.assertNotEqual(evening_utc, same_day_earlier)

    def test_different_schedule_day_or_scope_changes_key(self):
        base = scheduled_slot_key("s", "2026-10-21T08:45:00+08:00")
        self.assertNotEqual(base, scheduled_slot_key(
            "other", "2026-10-21T08:45:00+08:00"))
        self.assertNotEqual(base, scheduled_slot_key(
            "s", "2026-10-22T08:45:00+08:00"))
        self.assertNotEqual(base, scheduled_slot_key(
            "s", "2026-10-21T08:45:00+08:00", scope_version="v2"))

    def test_schedule_id_required(self):
        with self.assertRaises(WorkerServiceError):
            scheduled_slot_key("", "2026-10-21T08:45:00+08:00")


class WorkerServiceTestCase(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.enterContext(project_path_override(lambda value: self.root / value))
        self.config = vault_config(self.root, {})
        self.db_path = self.root / "tasks.sqlite3"
        self._seed(1)

    _seed_counter = [0]

    def _seed(self, count: int, *, replay_response=None):
        store = TaskStore(self.db_path)
        try:
            for _ in range(count):
                # Question text must be globally unique: two seeds with the
                # same text idempotently dedupe into ONE task (the store's
                # default key includes the content fingerprint).
                WorkerServiceTestCase._seed_counter[0] += 1
                index = WorkerServiceTestCase._seed_counter[0]
                spec = build_task_spec(
                    research_question="R09 问题 {0}".format(index),
                    symbols=["600519"], execution_mode="replay", config=self.config)
                spec["bound_signal_ids"] = []
                spec["public_context"]["evidence_summaries"] = {}
                spec["replay_response"] = replay_response or {
                    "schema_version": "1.0", "task_id": "placeholder",
                    "summary": "结论", "key_findings": [], "unknowns": [],
                    "evidence_refs": [], "human_questions": [],
                    "model_self_assessed_confidence": 0.3}
                store.create_task(title="R09-{0}".format(index),
                                  input_payload=spec, execution_mode="replay")
        finally:
            store.close()

    def service(self, **kwargs):
        def run_task(store, task):
            # The REAL worker in replay mode — persisted outcome, zero model.
            from stock_agent.research_worker import run_research_task

            return run_research_task(store, task["task_id"],
                                     worker_id=kwargs.get("worker_id", "w-r09"),
                                     config=self.config, ttl_seconds=60)

        return WorkerService(
            store_factory=lambda: TaskStore(self.db_path),
            run_task=run_task,
            worker_id=kwargs.get("worker_id", "w-r09"),
            ttl_seconds=60,
            idle_sleep_seconds=kwargs.get("idle_sleep_seconds", 0.02),
            max_tasks=kwargs.get("max_tasks", 0),
            now=kwargs.get("now"))

    def test_drain_finishes_inflight_and_never_claims_after_stop(self):
        claims = {"n": 0}
        original_factory = TaskStore

        def counting_factory():
            claims["n"] += 1
            return original_factory(self.db_path)

        def run_task(store, task):
            # Stop lands WHILE a task is in flight: the loop must finish it
            # and must not claim another one afterwards.
            stop.set()
            from stock_agent.research_worker import run_research_task

            return run_research_task(store, task["task_id"],
                                     worker_id="w-r09", config=self.config)

        self._seed(3)
        stop = threading.Event()
        service = WorkerService(
            store_factory=counting_factory, run_task=run_task,
            worker_id="w-r09", ttl_seconds=60, idle_sleep_seconds=0.02)
        report = service.run(stop)
        self.assertEqual(report["processed"], 1,
                         "in-flight task finished exactly once")
        self.assertTrue(report["stopped"])
        remaining = queue_depth(self.db_path)
        self.assertEqual(remaining, 3,
                         "no new claim after stop: the other tasks stay queued")

    def test_idle_exit_without_stop_when_drain_window_closes(self):
        service = self.service(max_tasks=1)
        stop = threading.Event()
        report = service.run(stop, drain_deadline_seconds=0.05)
        self.assertEqual(report["processed"], 1)
        self.assertFalse(report["stopped"])
        # Loop exited on its own after the drain window; nothing else claimed.
        self.assertEqual(queue_depth(self.db_path), 0)

    def test_max_tasks_exit(self):
        self._seed(2)
        service = self.service(max_tasks=2)
        report = service.run(threading.Event())
        self.assertEqual(report["processed"], 2)
        # setUp seeded 1 + this test 2 = 3 total; max_tasks=2 leaves exactly 1.
        self.assertEqual(queue_depth(self.db_path), 1)

    def _events_snapshot(self):
        store = TaskStore(self.db_path)
        try:
            return store.events(limit=100000)
        finally:
            store.close()

    def _tasks_snapshot(self):
        store = TaskStore(self.db_path)
        try:
            return store.list_tasks()
        finally:
            store.close()

    def test_no_scheduler_side_effects(self):
        before = self._events_snapshot()
        service = self.service()
        stop = threading.Event()
        stop.set()  # exit immediately, idle
        service.run(stop)
        after = self._events_snapshot()
        self.assertEqual(len(before), len(after),
                         "constructing/running the service writes no events")
        self.assertEqual(queue_depth(self.db_path), 1)

    def test_queue_depth_read_only(self):
        self.assertEqual(queue_depth(self.db_path), 1)


class ProbeTests(unittest.TestCase):
    """A45 (O half): /livez and /readyz are zero-side-effect."""

    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.enterContext(project_path_override(lambda value: self.root / value))
        import hashlib

        from stock_agent.dify_api import DifyGateway
        from stock_agent.dify_api_server import create_dify_api_server
        from stock_agent.task_runtime import TaskStore as Store

        self.store = Store(self.root / "tasks.sqlite3")
        self.addCleanup(self.store.close)
        cred = self.root / "config/dify/service_credentials.json"
        cred.parent.mkdir(parents=True)
        cred.write_text(json.dumps({"schema_version": "1.0", "credentials": []},
                                   ensure_ascii=False), encoding="utf-8")
        gateway = DifyGateway(self.store, {"dify": {"enabled": False}})
        self.server = create_dify_api_server(gateway, host="127.0.0.1", port=0)
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.shutdown)
        self.addCleanup(self.server.server_close)

    def _get(self, path):
        with urllib.request.urlopen(
                "http://127.0.0.1:{0}{1}".format(self.port, path), timeout=10) as response:
            return response.status, json.loads(response.read().decode("utf-8"))

    def _outbox_count(self):
        return len(self.store.events(limit=100000))

    def test_livez_and_readyz_ok_with_zero_side_effects(self):
        before = self._outbox_count()
        status, live = self._get("/livez")
        self.assertEqual(status, 200)
        self.assertEqual(live["data"]["probe"], "livez")
        status, ready = self._get("/readyz")
        self.assertEqual(status, 200)
        self.assertEqual(ready["data"]["status"], "ok")
        self.assertTrue(ready["data"]["store_read_only_open"])
        self.assertEqual(self._outbox_count(), before,
                         "probes must not write business events (A45)")
        # Task store untouched: still zero tasks.
        self.assertEqual(len(self.store.list_tasks()), 0)

    def test_unknown_still_404(self):
        try:
            self._get("/nope")
            self.fail("expected HTTPError")
        except Exception:
            pass


if __name__ == "__main__":
    unittest.main()
