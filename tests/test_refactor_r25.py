"""R25: unique-scheduler ownership registry, cutover gate, freeze/rollback.

O-layer coverage of the cutover discipline (GLM_REFACTOR_TASKBOOK R25):

- **Registry transitions**: initialize -> forward switch (requires the
  explicit legacy-disable marker, refuses a dual-scheduler window) ->
  freeze -> rollback switch (requires the freeze) -> thaw/re-enable, each
  audited in the append-only event log with a monotonic generation and a
  compare-and-set on the expected generation.
- **Unique-scheduler guard**: a foreign or unregistered scheduler is refused;
  a fresh foreign heartbeat aborts (双调度).
- **Freeze semantics**: new intake is refused while frozen; the freeze flag
  itself never blocks reading state.
- **Cutover gate stop conditions**: unexplained unknown attempts
  (dispatched-without-terminal, outcome_unknown), dual schedulers, a runtime
  schema above the pinned rollback ceiling, and insufficient capacity each
  abort the cutover; a clean state passes all four.
- **Same-slot no-duplicate (offline half)**: creating the SAME business
  intent twice — in one process and across "restarts" (fresh store
  connections) — converges on ONE task with ONE execution's outbox/billing
  footprint. The cross-RESTART container half stays with the drill.
"""
import json
import sys
import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from stock_agent.config import project_path_override
from stock_agent.scheduler_ownership import (
    CONTAINER_SCHEDULER_ID,
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
    record_switch,
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
        initialize_registry(self.registry, first_scheduler=LEGACY_SCHEDULER_ID,
                            actor="test", reason="first")
        with self.assertRaises(OwnershipConflictError):
            record_switch(self.registry, to=CONTAINER_SCHEDULER_ID, actor="test")
        # State unchanged after the refusal.
        self.assertEqual(load_state(self.registry)["active_scheduler"], LEGACY_SCHEDULER_ID)

    def test_forward_switch_with_marker_and_generation_cas(self):
        initialize_registry(self.registry, first_scheduler=LEGACY_SCHEDULER_ID,
                            actor="test", reason="first")
        disable_legacy(self.registry, actor="test", simulated=True)
        with self.assertRaises(OwnershipConflictError):
            record_switch(self.registry, to=CONTAINER_SCHEDULER_ID, actor="test",
                          expected_generation=99)
        state = record_switch(self.registry, to=CONTAINER_SCHEDULER_ID, actor="test",
                              expected_generation=1, cutover_id="cutover_x")
        self.assertEqual(state["active_scheduler"], CONTAINER_SCHEDULER_ID)
        self.assertEqual(state["generation"], 2)
        self.assertEqual(state["cutover_id"], "cutover_x")
        events = [json.loads(line) for line in
                  (self.registry / "events.jsonl").read_text(encoding="utf-8").splitlines()]
        self.assertEqual([e["event"] for e in events], ["initialize", "switch"])
        self.assertEqual(events[-1]["from"], LEGACY_SCHEDULER_ID)
        self.assertEqual(events[-1]["to"], CONTAINER_SCHEDULER_ID)

    def test_forward_switch_refuses_while_intake_frozen(self):
        initialize_registry(self.registry, first_scheduler=LEGACY_SCHEDULER_ID,
                            actor="test", reason="first")
        disable_legacy(self.registry, actor="test", simulated=True)
        freeze_intake(self.registry, actor="test", reason="rollback started")
        with self.assertRaises(OwnershipConflictError):
            record_switch(self.registry, to=CONTAINER_SCHEDULER_ID, actor="test")


class UniqueSchedulerGuardTests(R25TestCase):
    def setUp(self):
        super().setUp()
        initialize_registry(self.registry, first_scheduler=LEGACY_SCHEDULER_ID,
                            actor="test", reason="first")
        disable_legacy(self.registry, actor="test", simulated=True)
        record_switch(self.registry, to=CONTAINER_SCHEDULER_ID, actor="test")

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
        initialize_registry(self.registry, first_scheduler=LEGACY_SCHEDULER_ID,
                            actor="test", reason="first")
        marker = disable_legacy(self.registry, actor="test", simulated=True)
        self.assertTrue(marker["simulated"])
        record_switch(self.registry, to=CONTAINER_SCHEDULER_ID, actor="test")
        # Rollback without freeze is refused.
        with self.assertRaises(OwnershipConflictError):
            record_switch(self.registry, to=LEGACY_SCHEDULER_ID, actor="test")
        freeze_intake(self.registry, actor="test", reason="rollback")
        state = record_switch(self.registry, to=LEGACY_SCHEDULER_ID, actor="test")
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
        self.db_path = self.root / "tasks.sqlite3"
        store = TaskStore(self.db_path)
        try:
            spec = _replay_spec(self.config, "R25 门检问题 {0}".format(id(self)))
            task, _ = store.create_task(title="R25-gate", input_payload=spec,
                                        execution_mode="replay")
            self.task_id = str(task["task_id"])
        finally:
            store.close()
        initialize_registry(self.registry, first_scheduler=LEGACY_SCHEDULER_ID,
                            actor="test", reason="first")
        self.gate_config = {
            "scheduler_ownership": {
                "enabled": True,
                "state_dir": "scheduler_ownership",
                "heartbeat_stale_seconds": 900,
                "rollback_supported_schema": TASK_SCHEMA_VERSION,
                "min_free_bytes": 1,  # effectively unconstrained by default here
            },
        }

    def gate(self, **overrides):
        config = json.loads(json.dumps(self.gate_config))
        for key, value in overrides.items():
            config["scheduler_ownership"][key] = value
        return evaluate_cutover_gate(
            tasks_db_path=self.db_path, ownership_root=self.registry,
            state_root=self.root, runtime_schema_version=TASK_SCHEMA_VERSION,
            config=config)

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
            runtime_schema_version=TASK_SCHEMA_VERSION, config=self.gate_config)
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
            config=self.gate_config)
        self.assertFalse(verdict["ok"])


class SameSlotNoDuplicateTests(R25TestCase):
    """The offline half of 跨重启无重复意图/计费 (taskbook R25 acceptance)."""

    def setUp(self):
        super().setUp()
        self.db_path = self.root / "tasks.sqlite3"
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


class DefaultStateTests(unittest.TestCase):
    def test_default_state_shape(self):
        state = default_state()
        self.assertIsNone(state["active_scheduler"])
        self.assertEqual(state["generation"], 0)
        self.assertFalse(state["intake_frozen"])


if __name__ == "__main__":
    unittest.main()
