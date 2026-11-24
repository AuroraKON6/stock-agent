"""R25: unique-scheduler ownership registry, cutover gate, freeze/rollback.

O-layer coverage of the cutover discipline (GLM_REFACTOR_TASKBOOK R25):

- **Registry transitions**: initialize -> forward switch (requires the
  explicit legacy-disable marker, refuses a dual-scheduler window) ->
  freeze -> rollback switch (requires the freeze) -> thaw/re-enable, each
  audited in the append-only event log with a monotonic generation and a
  compare-and-set on the expected generation.
- **Stop conditions CONSTRAIN the switch action**: a forward switch runs the
  cutover gate against the actual state and refuses (owner/generation/audit
  untouched) on an unexplained unknown, a dual scheduler, a non-rollbackable
  schema or insufficient capacity. A rollback switch requires the retiring
  side to have drained and the increment reconciliation to be recorded.
- **Unique-scheduler guard**: a foreign or unregistered scheduler is refused;
  a fresh foreign heartbeat aborts the owner too (双调度). A successful
  switch RETIRES the outgoing scheduler's heartbeat, so the new owner can
  take over immediately after a rollback (no stale-window deadlock) while
  the retired side's own beats are refused.
- **Freeze semantics**: new intake is refused while frozen.
- **Same-slot no-duplicate (offline half)**: creating the SAME business
  intent twice — in one process and across "restarts" (fresh store
  connections) — converges on ONE task with ONE execution's outbox/billing
  footprint. The cross-RESTART container half stays with the drill.
"""
import io
import json
import sys
import sqlite3
import tempfile
import unittest
from contextlib import closing, redirect_stdout
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from stock_agent.config import project_path_override
from stock_agent.scheduler_ownership import (
    CONTAINER_SCHEDULER_ID,
    CutoverBlockedError,
    DualSchedulerError,
    IntakeFrozenError,
    LEGACY_SCHEDULER_ID,
    NotActiveSchedulerError,
    OwnershipConflictError,
    assert_intake_allowed,
    assert_unique_scheduler,
    default_state,
    disable_legacy,
    enable_legacy,
    evaluate_cutover_gate,
    freeze_intake,
    heartbeat,
    initialize_registry,
    intake_allowed,
    load_state,
    record_rollback_reconciliation,
    record_switch,
    scheduled_intake,
    scheduler_ownership_config,
    thaw_intake,
)
from stock_agent.task_runtime import TaskStore
from stock_agent.task_runtime.store import TASK_SCHEMA_VERSION
from stock_agent.task_supply import build_task_spec

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _vault_fixture import vault_config


def _replay_spec(config, question: str) -> dict:
    spec = build_task_spec(research_question=question, symbols=["600519"],
                           execution_mode="replay", config=config)
    spec["bound_signal_ids"] = []
    spec["public_context"]["evidence_summaries"] = {}
    spec["replay_response"] = {
        "schema_version": "1.0", "task_id": "placeholder",
        "summary": "结论", "key_findings": [], "unknowns": [],
        "evidence_refs": [], "human_questions": [],
        "model_self_assessed_confidence": 0.3}
    return spec


class R25TestCase(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.enterContext(project_path_override(lambda value: self.root / value))
        self.config = vault_config(self.root, {})
        self.registry = self.root / "scheduler_ownership"
        self.db_path = self.root / "tasks.sqlite3"

    def make_store(self, *, queued_tasks: int = 0) -> None:
        store = TaskStore(self.db_path)
        try:
            for index in range(queued_tasks):
                spec = _replay_spec(self.config, "R25 队列问题 {0}-{1}".format(index, id(self)))
                store.create_task(title="R25-queued-{0}".format(index),
                                  input_payload=spec, execution_mode="replay")
        finally:
            store.close()

    def gate_config(self, **overrides) -> dict:
        config = {
            "scheduler_ownership": {
                "enabled": True,
                "state_dir": "scheduler_ownership",
                "heartbeat_stale_seconds": 900,
                "rollback_supported_schema": TASK_SCHEMA_VERSION,
                "min_free_bytes": 1,
            },
        }
        config["scheduler_ownership"].update(overrides)
        return config

    def forward_switch(self, **kwargs):
        params = dict(to=CONTAINER_SCHEDULER_ID, actor="test",
                      tasks_db_path=self.db_path, state_root=self.root,
                      runtime_schema_version=TASK_SCHEMA_VERSION,
                      config=self.gate_config())
        params.update(kwargs)
        return record_switch(self.registry, **params)

    def rollback_switch(self, **kwargs):
        params = dict(to=LEGACY_SCHEDULER_ID, actor="test",
                      tasks_db_path=self.db_path, state_root=self.root,
                      runtime_schema_version=TASK_SCHEMA_VERSION,
                      config=self.gate_config())
        params.update(kwargs)
        return record_switch(self.registry, **params)

    def write_manifest(self):
        baseline = self.root / "rollback_baseline.sqlite3"
        TaskStore(baseline).close()
        return record_rollback_reconciliation(self.registry, baseline, self.db_path)


class SchedulerOwnershipConfigTests(R25TestCase):
    def test_disabled_by_default_with_normalized_fields(self):
        cfg = scheduler_ownership_config(None)
        self.assertFalse(cfg["enabled"])
        self.assertEqual(cfg["state_dir"], "data/scheduler_ownership")
        self.assertGreaterEqual(cfg["heartbeat_stale_seconds"], 5)
        self.assertGreater(cfg["min_free_bytes"], 0)

    def test_missing_rollback_schema_ceiling_is_explicit(self):
        cfg = scheduler_ownership_config(None)
        self.assertEqual(cfg["rollback_supported_schema"], "")


class RegistryTransitionTests(R25TestCase):
    def test_initialize_records_first_generation_and_audit_event(self):
        state = initialize_registry(self.registry, first_scheduler=LEGACY_SCHEDULER_ID,
                                    actor="test", reason="first")
        self.assertEqual(state["active_scheduler"], LEGACY_SCHEDULER_ID)
        self.assertEqual(state["generation"], 1)
        events = (self.registry / "events.jsonl").read_text(encoding="utf-8").splitlines()
        self.assertEqual(len(events), 1)
        self.assertEqual(json.loads(events[0])["event"], "initialize")

    def test_initialize_refuses_twice(self):
        initialize_registry(self.registry, first_scheduler=LEGACY_SCHEDULER_ID,
                            actor="test", reason="first")
        with self.assertRaises(OwnershipConflictError):
            initialize_registry(self.registry, first_scheduler=CONTAINER_SCHEDULER_ID,
                                actor="test", reason="second")

    def test_forward_switch_requires_explicit_legacy_disable_marker(self):
        self.make_store()
        initialize_registry(self.registry, first_scheduler=LEGACY_SCHEDULER_ID,
                            actor="test", reason="first")
        with self.assertRaises(OwnershipConflictError):
            self.forward_switch()
        # State unchanged after the refusal.
        self.assertEqual(load_state(self.registry)["active_scheduler"], LEGACY_SCHEDULER_ID)

    def test_forward_switch_requires_gate_parameters(self):
        self.make_store()
        initialize_registry(self.registry, first_scheduler=LEGACY_SCHEDULER_ID,
                            actor="test", reason="first")
        disable_legacy(self.registry, actor="test", simulated=True)
        with self.assertRaises(OwnershipConflictError):
            record_switch(self.registry, to=CONTAINER_SCHEDULER_ID, actor="test")

    def test_forward_switch_with_marker_and_generation_cas(self):
        self.make_store()
        initialize_registry(self.registry, first_scheduler=LEGACY_SCHEDULER_ID,
                            actor="test", reason="first")
        disable_legacy(self.registry, actor="test", simulated=True)
        with self.assertRaises(OwnershipConflictError):
            self.forward_switch(expected_generation=99)
        state = self.forward_switch(expected_generation=1, cutover_id="cutover_x")
        self.assertEqual(state["active_scheduler"], CONTAINER_SCHEDULER_ID)
        self.assertEqual(state["generation"], 2)
        self.assertEqual(state["cutover_id"], "cutover_x")
        events = [json.loads(line) for line in
                  (self.registry / "events.jsonl").read_text(encoding="utf-8").splitlines()]
        self.assertEqual([e["event"] for e in events], ["initialize", "switch"])
        self.assertEqual(events[-1]["from"], LEGACY_SCHEDULER_ID)
        self.assertEqual(events[-1]["to"], CONTAINER_SCHEDULER_ID)
        self.assertTrue(events[-1]["gate_ok"])

    def test_forward_switch_refuses_while_intake_frozen(self):
        self.make_store()
        initialize_registry(self.registry, first_scheduler=LEGACY_SCHEDULER_ID,
                            actor="test", reason="first")
        disable_legacy(self.registry, actor="test", simulated=True)
        freeze_intake(self.registry, actor="test", reason="rollback started")
        with self.assertRaises(OwnershipConflictError):
            self.forward_switch()


class SwitchConstrainedByStopConditionsTests(R25TestCase):
    """The stop conditions gate the SWITCH action itself (taskbook R25)."""

    def setUp(self):
        super().setUp()
        self.make_store(queued_tasks=2)
        initialize_registry(self.registry, first_scheduler=LEGACY_SCHEDULER_ID,
                            actor="test", reason="first")
        disable_legacy(self.registry, actor="test", simulated=True)
        self.events_before = len((self.registry / "events.jsonl")
                                  .read_text(encoding="utf-8").splitlines())

    def _unchanged(self) -> bool:
        state = load_state(self.registry)
        events = len((self.registry / "events.jsonl")
                     .read_text(encoding="utf-8").splitlines())
        return (state["active_scheduler"] == LEGACY_SCHEDULER_ID
                and state["generation"] == 1 and events == self.events_before)

    def test_clean_state_switch_succeeds(self):
        state = self.forward_switch()
        self.assertEqual(state["generation"], 2)

    def test_unexplained_unknown_attempts_refuse_the_switch(self):
        store = TaskStore(self.db_path)
        try:
            claimed = store.claim_next("w", ttl_seconds=60)
            attempt = store.prepare_invocation_attempt(
                str(claimed["task_id"]), worker_id="w",
                fencing_token=int(claimed["fencing_token"]), round_index=1,
                provider="synthetic", request_model="replay", prompt_sha256="0" * 64)
            store.mark_invocation_dispatched(str(attempt["attempt_id"]),
                                              worker_id="w",
                                              fencing_token=int(claimed["fencing_token"]))
        finally:
            store.close()
        with self.assertRaises(CutoverBlockedError) as caught:
            self.forward_switch()
        self.assertTrue(any("unexplained unknown" in item for item in caught.exception.blockers))
        self.assertTrue(self._unchanged())

    def test_dual_scheduler_refuses_the_switch(self):
        state_path = self.registry / "state.json"
        state = json.loads(state_path.read_text(encoding="utf-8"))
        state["actors_seen"][CONTAINER_SCHEDULER_ID] = \
            datetime.now(timezone.utc).isoformat(timespec="seconds")
        state_path.write_text(json.dumps(state), encoding="utf-8")
        with self.assertRaises(CutoverBlockedError) as caught:
            self.forward_switch()
        self.assertTrue(any("dual scheduler" in item for item in caught.exception.blockers))
        self.assertTrue(self._unchanged())

    def test_schema_above_rollback_ceiling_refuses_the_switch(self):
        with self.assertRaises(CutoverBlockedError) as caught:
            self.forward_switch(config=self.gate_config(rollback_supported_schema="1.0"))
        self.assertTrue(any("non-rollbackable schema" in item for item in caught.exception.blockers))
        self.assertTrue(self._unchanged())

    def test_insufficient_capacity_refuses_the_switch(self):
        with self.assertRaises(CutoverBlockedError) as caught:
            self.forward_switch(config=self.gate_config(min_free_bytes=10 ** 15))
        self.assertTrue(any("insufficient capacity" in item for item in caught.exception.blockers))
        self.assertTrue(self._unchanged())


class RollbackReadinessTests(R25TestCase):
    def setUp(self):
        super().setUp()
        self.make_store(queued_tasks=1)
        initialize_registry(self.registry, first_scheduler=LEGACY_SCHEDULER_ID,
                            actor="test", reason="first")
        disable_legacy(self.registry, actor="test", simulated=True)
        self.forward_switch()

    def test_rollback_requires_freeze_drain_and_manifest(self):
        # Not frozen yet.
        with self.assertRaises(OwnershipConflictError):
            self.rollback_switch()
        freeze_intake(self.registry, actor="test", reason="rollback")
        # Still a queued task on the retiring side.
        with self.assertRaises(CutoverBlockedError) as caught:
            self.rollback_switch()
        self.assertTrue(any("not drained" in item for item in caught.exception.blockers))
        # Drained (task succeeded) but no increment manifest yet.
        store = TaskStore(self.db_path)
        try:
            claimed = store.claim_next("w-drain", ttl_seconds=60)
            from stock_agent.research_worker import run_research_task

            run_research_task(store, str(claimed["task_id"]),
                              worker_id="w-drain", config=self.config, ttl_seconds=60)
        finally:
            store.close()
        with self.assertRaises(CutoverBlockedError) as caught:
            self.rollback_switch()
        self.assertTrue(any("increment reconciliation not recorded"
                            in item for item in caught.exception.blockers))
        # Manifest recorded -> the switch goes through.
        self.write_manifest()
        state = self.rollback_switch()
        self.assertEqual(state["active_scheduler"], LEGACY_SCHEDULER_ID)
        self.assertEqual(state["generation"], 3)

    def test_stale_or_changed_reconciliation_refuses_switch(self):
        import sqlite3

        with sqlite3.connect(self.db_path) as connection:
            connection.execute("UPDATE research_tasks SET status = 'succeeded'")
        freeze_intake(self.registry, actor="test", reason="first freeze")
        self.write_manifest()
        thaw_intake(self.registry, actor="test")
        freeze_intake(self.registry, actor="test", reason="second freeze")
        with self.assertRaises(CutoverBlockedError) as caught:
            self.rollback_switch()
        self.assertTrue(any("no longer matches" in blocker for blocker in caught.exception.blockers))
        self.write_manifest()
        with sqlite3.connect(self.db_path) as connection:
            connection.execute("UPDATE task_outbox SET payload = '{}'")
        with self.assertRaises(CutoverBlockedError) as caught:
            self.rollback_switch()
        self.assertTrue(any("no longer matches" in blocker for blocker in caught.exception.blockers))
        self.assertEqual(load_state(self.registry)["generation"], 2)

    def test_rollback_switch_retires_outgoing_heartbeat(self):
        heartbeat(self.registry, CONTAINER_SCHEDULER_ID)
        freeze_intake(self.registry, actor="test", reason="rollback")
        # Drain the queued task first (readiness requires it).
        store = TaskStore(self.db_path)
        try:
            claimed = store.claim_next("w-drain", ttl_seconds=60)
            from stock_agent.research_worker import run_research_task

            run_research_task(store, str(claimed["task_id"]),
                              worker_id="w-drain", config=self.config, ttl_seconds=60)
        finally:
            store.close()
        self.write_manifest()
        self.rollback_switch()
        # The container's pre-switch beat was retired: the OLD owner passes
        # immediately — no stale-window deadlock.
        state = assert_unique_scheduler(self.registry, LEGACY_SCHEDULER_ID)
        self.assertEqual(state["active_scheduler"], LEGACY_SCHEDULER_ID)
        heartbeat(self.registry, LEGACY_SCHEDULER_ID)
        # The retired container's own heartbeat is refused.
        with self.assertRaises(DualSchedulerError):
            heartbeat(self.registry, CONTAINER_SCHEDULER_ID)


class UniqueSchedulerGuardTests(R25TestCase):
    def setUp(self):
        super().setUp()
        self.make_store()
        initialize_registry(self.registry, first_scheduler=LEGACY_SCHEDULER_ID,
                            actor="test", reason="first")
        disable_legacy(self.registry, actor="test", simulated=True)
        self.forward_switch()

    def test_owner_passes_and_foreign_scheduler_is_refused(self):
        state = assert_unique_scheduler(self.registry, CONTAINER_SCHEDULER_ID)
        self.assertEqual(state["active_scheduler"], CONTAINER_SCHEDULER_ID)
        with self.assertRaises(DualSchedulerError):
            assert_unique_scheduler(self.registry, LEGACY_SCHEDULER_ID)

    def test_heartbeat_refuses_foreign_scheduler_and_records_nothing(self):
        with self.assertRaises(DualSchedulerError):
            heartbeat(self.registry, LEGACY_SCHEDULER_ID)
        seen = load_state(self.registry)["actors_seen"]
        self.assertNotIn(LEGACY_SCHEDULER_ID, seen)

    def test_owner_heartbeat_refuses_fresh_foreign_beat(self):
        heartbeat(self.registry, CONTAINER_SCHEDULER_ID)
        state_path = self.registry / "state.json"
        state = json.loads(state_path.read_text(encoding="utf-8"))
        state["actors_seen"][LEGACY_SCHEDULER_ID] = \
            datetime.now(timezone.utc).isoformat(timespec="seconds")
        state_path.write_text(json.dumps(state), encoding="utf-8")
        with self.assertRaises(DualSchedulerError):
            heartbeat(self.registry, CONTAINER_SCHEDULER_ID)

    def test_fresh_foreign_heartbeat_aborts_the_guard(self):
        heartbeat(self.registry, CONTAINER_SCHEDULER_ID)
        state_path = self.registry / "state.json"
        state = json.loads(state_path.read_text(encoding="utf-8"))
        state["actors_seen"][LEGACY_SCHEDULER_ID] = \
            datetime.now(timezone.utc).isoformat(timespec="seconds")
        state_path.write_text(json.dumps(state), encoding="utf-8")
        with self.assertRaises(DualSchedulerError):
            assert_unique_scheduler(self.registry, CONTAINER_SCHEDULER_ID)

    def test_stale_foreign_heartbeat_does_not_abort(self):
        state_path = self.registry / "state.json"
        state = json.loads(state_path.read_text(encoding="utf-8"))
        stale = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat(timespec="seconds")
        state["actors_seen"][LEGACY_SCHEDULER_ID] = stale
        state_path.write_text(json.dumps(state), encoding="utf-8")
        assert_unique_scheduler(self.registry, CONTAINER_SCHEDULER_ID)

    def test_unregistered_registry_refuses_everyone(self):
        empty = self.root / "other_registry"
        with self.assertRaises(NotActiveSchedulerError):
            assert_unique_scheduler(empty, CONTAINER_SCHEDULER_ID)


class FreezeIntakeTests(R25TestCase):
    def test_scheduled_intake_refuses_non_owner_and_freeze_without_mutation(self):
        for scheduler, error in ((LEGACY_SCHEDULER_ID, DualSchedulerError),
                                 (CONTAINER_SCHEDULER_ID, IntakeFrozenError)):
            if scheduler == CONTAINER_SCHEDULER_ID:
                freeze_intake(self.registry, actor="test", reason="test freeze")
            before = (self.registry / "state.json").read_bytes()
            with self.assertRaises(error):
                with scheduled_intake(self.registry, scheduler):
                    self.fail("refused scheduler reached the write section")
            self.assertEqual((self.registry / "state.json").read_bytes(), before)

    def setUp(self):
        super().setUp()
        initialize_registry(self.registry, first_scheduler=CONTAINER_SCHEDULER_ID,
                            actor="test", reason="first")

    def test_freeze_blocks_new_intake_until_thaw(self):
        self.assertTrue(intake_allowed(self.registry))
        freeze_intake(self.registry, actor="test", reason="rollback window")
        self.assertFalse(intake_allowed(self.registry))
        with self.assertRaises(IntakeFrozenError):
            assert_intake_allowed(self.registry)
        thaw_intake(self.registry, actor="test", reason="rollback complete")
        self.assertTrue(intake_allowed(self.registry))

    def test_freeze_refuses_double_freeze_and_thaw_without_freeze(self):
        freeze_intake(self.registry, actor="test", reason="rollback window")
        with self.assertRaises(OwnershipConflictError):
            freeze_intake(self.registry, actor="test", reason="again")
        thaw_intake(self.registry, actor="test", reason="done")
        with self.assertRaises(OwnershipConflictError):
            thaw_intake(self.registry, actor="test", reason="again")


class RollbackSequenceTests(R25TestCase):
    def test_rollback_requires_freeze_then_switch_then_reenable(self):
        self.make_store()
        initialize_registry(self.registry, first_scheduler=LEGACY_SCHEDULER_ID,
                            actor="test", reason="first")
        marker = disable_legacy(self.registry, actor="test", simulated=True)
        self.assertTrue(marker["simulated"])
        self.forward_switch()
        # Rollback without freeze is refused.
        with self.assertRaises(OwnershipConflictError):
            self.rollback_switch()
        freeze_intake(self.registry, actor="test", reason="rollback")
        self.write_manifest()
        state = self.rollback_switch()
        self.assertEqual(state["active_scheduler"], LEGACY_SCHEDULER_ID)
        self.assertEqual(state["generation"], 3)
        reenabled = enable_legacy(self.registry, actor="test", reason="rollback done")
        self.assertEqual(reenabled["disabled_at"], marker["disabled_at"])
        thaw_intake(self.registry, actor="test", reason="rollback done")
        final = load_state(self.registry)
        self.assertEqual(final["active_scheduler"], LEGACY_SCHEDULER_ID)
        self.assertFalse(final["intake_frozen"])
        self.assertFalse((self.registry / "legacy_disabled.json").exists())

    def test_enable_legacy_without_marker_refuses(self):
        initialize_registry(self.registry, first_scheduler=LEGACY_SCHEDULER_ID,
                            actor="test", reason="first")
        with self.assertRaises(OwnershipConflictError):
            enable_legacy(self.registry, actor="test", reason="nothing to clear")


class CutoverGateTests(R25TestCase):
    def setUp(self):
        super().setUp()
        self.make_store(queued_tasks=1)
        initialize_registry(self.registry, first_scheduler=LEGACY_SCHEDULER_ID,
                            actor="test", reason="first")

    def gate(self, **overrides):
        return evaluate_cutover_gate(
            tasks_db_path=self.db_path, ownership_root=self.registry,
            state_root=self.root, runtime_schema_version=TASK_SCHEMA_VERSION,
            config=self.gate_config(**overrides))

    def test_clean_state_passes_all_four_checks(self):
        verdict = self.gate()
        self.assertTrue(verdict["ok"], verdict["blockers"])
        self.assertEqual(sorted(verdict["checks"]), [
            "capacity", "dual_scheduler", "schema_rollback", "unexplained_unknown"])

    def test_unexplained_unknown_attempts_abort(self):
        store = TaskStore(self.db_path)
        try:
            claimed = store.claim_next("w-gate", ttl_seconds=60)
            attempt = store.prepare_invocation_attempt(
                str(claimed["task_id"]), worker_id="w-gate",
                fencing_token=int(claimed["fencing_token"]), round_index=1,
                provider="synthetic", request_model="replay", prompt_sha256="0" * 64)
            store.mark_invocation_dispatched(str(attempt["attempt_id"]),
                                              worker_id="w-gate",
                                              fencing_token=int(claimed["fencing_token"]))
        finally:
            store.close()
        verdict = self.gate()
        self.assertFalse(verdict["ok"])
        self.assertTrue(any("unexplained unknown" in item for item in verdict["blockers"]),
                        verdict["blockers"])

    def test_dual_scheduler_aborts(self):
        state_path = self.registry / "state.json"
        state = json.loads(state_path.read_text(encoding="utf-8"))
        state["actors_seen"][CONTAINER_SCHEDULER_ID] = \
            datetime.now(timezone.utc).isoformat(timespec="seconds")
        state_path.write_text(json.dumps(state), encoding="utf-8")
        verdict = self.gate()
        self.assertFalse(verdict["ok"])
        self.assertTrue(any("dual scheduler" in item for item in verdict["blockers"]),
                        verdict["blockers"])

    def test_no_active_scheduler_aborts(self):
        empty = self.root / "empty_registry"
        empty.mkdir()
        verdict = evaluate_cutover_gate(
            tasks_db_path=self.db_path, ownership_root=empty, state_root=self.root,
            runtime_schema_version=TASK_SCHEMA_VERSION, config=self.gate_config())
        self.assertFalse(verdict["ok"])
        self.assertTrue(any("dual scheduler" in item for item in verdict["blockers"]))

    def test_schema_above_rollback_ceiling_aborts(self):
        verdict = self.gate(rollback_supported_schema="1.0")
        self.assertFalse(verdict["ok"])
        self.assertTrue(any("non-rollbackable schema" in item for item in verdict["blockers"]),
                        verdict["blockers"])

    def test_missing_schema_ceiling_aborts(self):
        verdict = self.gate(rollback_supported_schema="")
        self.assertFalse(verdict["ok"])
        self.assertTrue(any("non-rollbackable schema" in item for item in verdict["blockers"]))

    def test_insufficient_capacity_aborts(self):
        verdict = self.gate(min_free_bytes=10 ** 15)
        self.assertFalse(verdict["ok"])
        self.assertTrue(any("insufficient capacity" in item for item in verdict["blockers"]),
                        verdict["blockers"])

    def test_missing_task_store_aborts(self):
        verdict = evaluate_cutover_gate(
            tasks_db_path=self.root / "missing.sqlite3", ownership_root=self.registry,
            state_root=self.root, runtime_schema_version=TASK_SCHEMA_VERSION,
            config=self.gate_config())
        self.assertFalse(verdict["ok"])


class SameSlotNoDuplicateTests(R25TestCase):
    """The offline half of 跨重启无重复意图/计费 (taskbook R25 acceptance)."""

    def setUp(self):
        super().setUp()
        from stock_agent.research_worker import run_research_task
        from stock_agent.worker_service import scheduled_slot_key

        self.run_research_task = run_research_task
        self.slot_intent = scheduled_slot_key(
            "legacy-daily-research", "2026-11-20T08:30:00+08:00",
            scope_version="r25-test-v1")

    def _create_slot_intent(self) -> tuple:
        store = TaskStore(self.db_path)
        try:
            spec = _replay_spec(self.config,
                                "R25 同时隙 {0}".format(self.slot_intent[:16]))
            task, dedup = store.create_task(title="R25-slot", input_payload=spec,
                                            execution_mode="replay",
                                            business_intent_id=self.slot_intent)
            self.assertIn(dedup, ("RECORDED", "DUPLICATE"))
            return str(task["task_id"]), dedup
        finally:
            store.close()

    def _counts(self) -> dict:
        connection = sqlite3.connect(self.db_path)
        try:
            tasks = int(connection.execute(
                "SELECT COUNT(*) FROM research_tasks WHERE business_intent_id = ?",
                (self.slot_intent,)).fetchone()[0])
            outbox = int(connection.execute(
                "SELECT COUNT(*) FROM task_outbox o JOIN research_tasks t"
                " ON o.task_id = t.task_id WHERE t.business_intent_id = ?",
                (self.slot_intent,)).fetchone()[0])
            return {"tasks": tasks, "outbox": outbox}
        finally:
            connection.close()

    def test_same_intent_converges_in_process_and_across_restart(self):
        first_id, first_dedup = self._create_slot_intent()
        self.assertEqual(first_dedup, "RECORDED")
        second_id, second_dedup = self._create_slot_intent()  # same process
        self.assertEqual(first_id, second_id)
        self.assertEqual(second_dedup, "DUPLICATE")

        # "Restart": a fresh connection/process must still converge.
        third_id, third_dedup = self._create_slot_intent()
        self.assertEqual(first_id, third_id)
        self.assertEqual(third_dedup, "DUPLICATE")

        # ONE execution through the real worker entry.
        store = TaskStore(self.db_path)
        try:
            claimed = store.claim_next("w-r25", ttl_seconds=60)
            outcome = self.run_research_task(store, str(claimed["task_id"]),
                                             worker_id="w-r25", config=self.config,
                                             ttl_seconds=60)
        finally:
            store.close()
        self.assertEqual(outcome["status"], "succeeded")

        after_run = self._counts()
        # Re-triggering the slot AFTER execution adds nothing (no duplicate
        # intent, execution, or outbox delivery).
        fourth_id, fourth_dedup = self._create_slot_intent()
        self.assertEqual(first_id, fourth_id)
        self.assertEqual(fourth_dedup, "DUPLICATE")
        self.assertEqual(self._counts(), after_run)
        self.assertEqual(after_run["tasks"], 1)
        self.assertGreater(after_run["outbox"], 0)

        store = TaskStore(self.db_path, read_only=True)
        try:
            final = store.get_task(first_id)
        finally:
            store.close()
        self.assertEqual(final["status"], "succeeded")


class ScheduledSlotReplayCliTests(R25TestCase):
    shanghai_slot = "2026-11-20T08:30:00+08:00"
    utc_slot = "2026-11-20T00:30:00Z"
    schedule = "legacy-daily-research"
    scope = "r25-test-v1"

    def setUp(self):
        super().setUp()
        from stock_agent.research_worker import run_research_task
        from stock_agent.worker_service import scheduled_slot_key

        cli_config = self.gate_config(scheduler_id=CONTAINER_SCHEDULER_ID)
        cli_config["task_runtime"] = {"db_path": str(self.db_path)}
        self.config_path = self.root / "schedule.json"
        self.config_path.write_text(json.dumps(cli_config), encoding="utf-8")
        initialize_registry(self.registry, first_scheduler=CONTAINER_SCHEDULER_ID, actor="test")
        self.spec = _replay_spec(self.config, "R25 研究一个已执行的旧时隙")
        self.intent = scheduled_slot_key(self.schedule, self.shanghai_slot, scope_version=self.scope)
        # Reproduce the original CLI's stored payload, including its raw offset.
        self.legacy_slot = {"schedule_id": self.schedule, "planned_slot": self.shanghai_slot,
                            "scope_version": self.scope}
        with closing(TaskStore(self.db_path)) as store:
            task, _ = store.create_task(
                title="Legacy slot", input_payload=dict(self.spec, scheduled_slot=self.legacy_slot),
                execution_mode="replay", business_intent_id=self.intent)
            self.task_id = task["task_id"]
            store.claim_next("w-r25", ttl_seconds=60)
            outcome = run_research_task(store, self.task_id, worker_id="w-r25",
                                        config=self.config, ttl_seconds=60)
            self.assertEqual(outcome["status"], "succeeded")

    def _submit(self, planned_slot, spec):
        from stock_agent.cli import main

        path = self.root / "spec.json"
        path.write_text(json.dumps(spec), encoding="utf-8")
        argv = ["stock_agent", "scheduled-task-submit", "--config", str(self.config_path),
                "--scheduler", CONTAINER_SCHEDULER_ID, "--schedule-id", self.schedule,
                "--scope-version", self.scope, "--spec", str(path), "--planned-slot", planned_slot]
        with patch.object(sys, "argv", argv), redirect_stdout(io.StringIO()) as output:
            main()
        return json.loads(output.getvalue())

    def _accounting(self):
        with closing(TaskStore(self.db_path, read_only=True)) as store:
            task = store.get_task(self.task_id)
            self.assertEqual(task["input_payload"]["scheduled_slot"], self.legacy_slot)
            self.assertEqual(task["status"], "succeeded")
            return (len(store.list_tasks()), task["attempt_count"], len(store.events_after(0)),
                    sum(bool(attempt["budget_event_id"])
                        for attempt in store.invocation_attempts(self.task_id)))

    def test_public_entry_reuses_old_payload_for_both_timezones(self):
        before = self._accounting()
        self.assertEqual(before, (1, 1, 4, 0))
        for planned_slot in (self.shanghai_slot, self.utc_slot):
            with self.subTest(planned_slot=planned_slot):
                self.assertEqual(self._submit(planned_slot, self.spec), {
                    "task_id": self.task_id, "intent": self.intent, "dedup": "DUPLICATE",
                    "status": "succeeded",
                })
                self.assertEqual(self._accounting(), before)

    def test_public_entry_still_rejects_a_different_request_under_the_same_intent(self):
        before = self._accounting()
        for planned_slot, spec in (
            (self.utc_slot, dict(self.spec, research_question="不同的研究问题")),
            ("2026-11-20T00:30:00.125Z", self.spec),
        ):
            with self.subTest(planned_slot=planned_slot):
                with self.assertRaisesRegex(SystemExit, "already names a different request"):
                    self._submit(planned_slot, spec)
                self.assertEqual(self._accounting(), before)


class DefaultStateTests(unittest.TestCase):
    def test_default_state_shape(self):
        state = default_state()
        self.assertIsNone(state["active_scheduler"])
        self.assertEqual(state["generation"], 0)
        self.assertFalse(state["intake_frozen"])


if __name__ == "__main__":
    unittest.main()
