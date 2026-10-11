from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from stock_agent.audit_log import load_hash_chained_jsonl
from stock_agent.config import project_path_override
from stock_agent.models import RawSignal, now_iso
from stock_agent.run_coordination import (
    ActiveLeaseError,
    CheckpointConflictError,
    CheckpointIntegrityError,
    CheckpointStore,
    LeaseConflictError,
    RunCoordinationError,
    RunLease,
    begin_coordinated_run,
    build_run_coordination_status,
    checkpoint_output_refs,
    checkpoint_stage,
    fingerprint,
    recover_stale_lease,
    write_run_coordination_status,
)


class RunCoordinationTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.config = {
            "run_coordination": {
                "enabled": True,
                "lease_root": str(self.root / "data" / "leases"),
                "recovered_lease_root": str(self.root / "data" / "recovered"),
                "checkpoint_path": str(self.root / "data" / "checkpoints.jsonl"),
                "report_dir": str(self.root / "reports" / "coordination"),
                "lease_ttl_seconds": 30,
                "require_explicit_stale_recovery": True,
                "fail_fast_on_lease_conflict": True,
            }
        }

    def tearDown(self):
        self.tmp.cleanup()

    def scoped(self):
        return project_path_override(lambda value, root=self.root: root / value)


class RunLeaseTests(RunCoordinationTestCase):
    def test_run_ids_are_unique_within_same_second(self):
        with self.scoped():
            first = RunLease(self.config, "run")
            second = RunLease(self.config, "run")
            self.assertNotEqual(first.run_id, second.run_id)

    def test_same_mode_has_one_owner(self):
        with self.scoped():
            first = RunLease(self.config, "run").acquire()
            try:
                with self.assertRaises(LeaseConflictError) as caught:
                    RunLease(self.config, "run").acquire()
                self.assertFalse(caught.exception.details.get("stale"))
            finally:
                first.release()

    def test_different_modes_can_coexist(self):
        with self.scoped():
            first = RunLease(self.config, "run").acquire()
            second = RunLease(self.config, "agent-run").acquire()
            first.release()
            second.release()

    def test_release_removes_only_own_lease(self):
        with self.scoped():
            lease = RunLease(self.config, "run").acquire()
            path = lease.path
            self.assertTrue(path.exists())
            self.assertTrue(lease.release())
            self.assertFalse(path.exists())
            self.assertFalse(lease.release())

    def test_stale_lease_blocks_acquire_without_recovery(self):
        old = datetime(2020, 1, 1, tzinfo=timezone.utc)
        with self.scoped():
            RunLease(self.config, "run", now=old).acquire()
            with self.assertRaises(LeaseConflictError) as caught:
                RunLease(self.config, "run", now=old + timedelta(seconds=31)).acquire()
            self.assertTrue(caught.exception.details.get("stale"))

    def test_explicit_recovery_archives_stale_lease_and_logs_reason(self):
        old = datetime(2020, 1, 1, tzinfo=timezone.utc)
        with self.scoped():
            RunLease(self.config, "run", now=old).acquire()
            result = recover_stale_lease(
                self.config,
                "run",
                "operator confirmed the prior process was terminated",
                now=old + timedelta(seconds=31),
            )
            self.assertEqual(result["status"], "RECOVERED")
            self.assertFalse((self.root / "data" / "leases" / "run.json").exists())
            self.assertTrue((self.root / result["archived_path"]).exists())
            chain = load_hash_chained_jsonl(self.root / "data" / "checkpoints.jsonl")
            self.assertEqual(chain["integrity_status"], "OK")
            self.assertEqual(chain["records"][-1]["event_type"], "lease_recovered")
            self.assertNotIn("sk-", (self.root / "data" / "checkpoints.jsonl").read_text(encoding="utf-8"))

    def test_active_lease_cannot_be_recovered(self):
        current = datetime.now(timezone.utc)
        with self.scoped():
            lease = RunLease(self.config, "run", now=current).acquire()
            try:
                with self.assertRaises(ActiveLeaseError):
                    recover_stale_lease(self.config, "run", "not stale", now=current)
            finally:
                lease.release()

    def test_empty_recovery_reason_is_rejected(self):
        with self.scoped():
            with self.assertRaises(RunCoordinationError):
                recover_stale_lease(self.config, "run", "")


class CheckpointTests(RunCoordinationTestCase):
    def test_hash_chain_and_run_state(self):
        with self.scoped():
            store = CheckpointStore(self.config)
            store.start_run("run-1", "run")
            store.checkpoint("run-1", "run", "collect", fingerprint({"count": 2}))
            store.complete("run-1", "run", [])
            chain = load_hash_chained_jsonl(self.root / "data" / "checkpoints.jsonl")
            self.assertEqual(chain["integrity_status"], "OK")
            self.assertEqual(store.state_for_run("run-1")["status"], "COMPLETED")
            self.assertEqual(len(store.state_for_run("run-1")["checkpoints"]), 1)

    def test_duplicate_checkpoint_is_idempotent(self):
        with self.scoped():
            store = CheckpointStore(self.config)
            store.start_run("run-1", "run")
            digest = fingerprint({"count": 2})
            first = store.checkpoint("run-1", "run", "collect", digest)
            second = store.checkpoint("run-1", "run", "collect", digest)
            self.assertEqual(first["status"], "RECORDED")
            self.assertEqual(second["status"], "DUPLICATE")
            self.assertEqual(load_hash_chained_jsonl(store.path)["record_count"], 2)

    def test_conflicting_duplicate_is_rejected(self):
        with self.scoped():
            store = CheckpointStore(self.config)
            store.start_run("run-1", "run")
            digest = fingerprint({"count": 2})
            store.checkpoint("run-1", "run", "collect", digest, status="COMPLETED")
            with self.assertRaises(CheckpointConflictError):
                store.checkpoint("run-1", "run", "collect", digest, status="SKIPPED")

    def test_corrupt_ledger_fails_closed(self):
        with self.scoped():
            store = CheckpointStore(self.config)
            store.start_run("run-1", "run")
            store.path.write_text(store.path.read_text(encoding="utf-8") + "{broken\n", encoding="utf-8")
            with self.assertRaises(CheckpointIntegrityError):
                store.checkpoint("run-1", "run", "collect", fingerprint({"count": 1}))

    def test_events_do_not_store_signal_content_or_secrets(self):
        with self.scoped():
            store = CheckpointStore(self.config)
            store.start_run("run-1", "run")
            store.checkpoint("run-1", "run", "collect", fingerprint({"count": 1}))
            text = store.path.read_text(encoding="utf-8")
            self.assertNotIn("private investment thesis", text)
            self.assertNotIn("sk-test-secret-value", text)
            self.assertRegex(text, r"[0-9a-f]{64}")

    def test_terminal_failure_is_recorded(self):
        with self.scoped():
            store = CheckpointStore(self.config)
            store.start_run("run-1", "run")
            store.fail("run-1", "run", RuntimeError("collection failed"))
            self.assertEqual(store.state_for_run("run-1")["status"], "FAILED")


class OutputAndStatusTests(RunCoordinationTestCase):
    def test_output_refs_hash_files_without_content(self):
        with self.scoped():
            output = self.root / "reports" / "daily" / "latest.md"
            output.parent.mkdir(parents=True)
            output.write_text("confidential research body", encoding="utf-8")
            result = SimpleNamespace(report_path=str(output), json_path="", alert_path="", review_path="", actions_path="")
            refs = checkpoint_output_refs(result)
            self.assertEqual(refs[0]["path"], "reports/daily/latest.md")
            self.assertNotIn("confidential", json.dumps(refs))
            self.assertEqual(len(refs[0]["sha256"]), 64)

    def test_output_outside_workspace_is_rejected(self):
        with tempfile.TemporaryDirectory() as outside:
            result = SimpleNamespace(
                report_path=str(Path(outside) / "report.md"),
                json_path="", alert_path="", review_path="", actions_path="",
            )
            with self.scoped():
                with self.assertRaises(RunCoordinationError):
                    checkpoint_output_refs(result)

    def test_status_and_latest_projection(self):
        with self.scoped():
            store = CheckpointStore(self.config)
            store.start_run("run-1", "run")
            store.complete("run-1", "run")
            snapshot = build_run_coordination_status(self.config)
            self.assertEqual(snapshot["technical_status"], "PASS")
            self.assertEqual(snapshot["production_readiness"], "BLOCKED")
            written = write_run_coordination_status(self.config)
            self.assertTrue(Path(written["latest_path"]).exists())
            self.assertTrue(Path(written["latest_report_path"]).exists())

    def test_path_traversal_is_rejected(self):
        bad = dict(self.config)
        bad["run_coordination"] = dict(self.config["run_coordination"], lease_root="../outside")
        with self.scoped():
            with self.assertRaises(RunCoordinationError):
                RunLease(bad, "run").acquire()

    def test_disabled_config_is_noop(self):
        disabled = {"run_coordination": {"enabled": False}}
        with self.scoped():
            self.assertIsNone(begin_coordinated_run(disabled, "run"))

    def test_isolated_config_is_noop_even_when_enabled(self):
        isolated = dict(self.config, run_profile={"isolated": True})
        with self.scoped():
            self.assertIsNone(begin_coordinated_run(isolated, "run"))

    def test_checkpoint_stage_uses_metadata_fingerprint(self):
        with self.scoped():
            coordinator = begin_coordinated_run(self.config, "run")
            try:
                self.assertIsNotNone(coordinator)
                self.assertEqual(checkpoint_stage(self.config, "collect", {"raw": 3}), None)
                runtime = dict(self.config, _run_coordination=coordinator)
                first = checkpoint_stage(runtime, "collect", {"raw": 3})
                duplicate = checkpoint_stage(runtime, "collect", {"raw": 3})
                self.assertEqual(first["status"], "RECORDED")
                self.assertEqual(duplicate["status"], "DUPLICATE")
            finally:
                coordinator.fail("test stop")
                coordinator.release()


class PipelineIntegrationTests(RunCoordinationTestCase):
    def _pipeline_config(self):
        return {
            "storage": {"sqlite_path": str(self.root / "data" / "signals.sqlite3")},
            "sources": {},
            "topics": [{"name": "runtime", "symbols": [], "keywords": ["major contract"], "risk_keywords": []}],
            "priority_rules": {
                "p0_keywords": [], "p1_keywords": ["major contract"],
                "positive_keywords": [], "negative_keywords": [], "review_keywords": [],
            },
            "llm_agent": {"enabled": False},
            "multi_role_agent": {"enabled": False},
            "research_cases": {"enabled": False, "llm_enabled": False},
            "research_framework": {"enabled": False},
            "research_team": {"enabled": False},
            "research_runtime": {"enabled": False, "auto_sync": False},
            "research_workpapers": {"enabled": False},
            "report": {"include_low_priority": True},
            "run_coordination": self.config["run_coordination"],
        }

    def _raw(self):
        return RawSignal(
            source="manual",
            source_type="news",
            title="A major contract announcement",
            summary="major contract",
            url="https://example.test/news",
            published_at=now_iso(),
            payload={},
        )

    def test_pipeline_records_completed_run_and_releases_lease(self):
        from stock_agent.pipeline import run_pipeline

        config = self._pipeline_config()
        with self.scoped(), patch("stock_agent.pipeline.collect_sources", return_value=[self._raw()]):
            result = run_pipeline(config)
            self.assertEqual(result.stats["run_coordination"]["status"], "COMPLETED")
            self.assertFalse((self.root / "data" / "leases" / "run.json").exists())
            report_payload = json.loads(Path(result.json_path).read_text(encoding="utf-8"))
            self.assertEqual(report_payload["stats"]["run_coordination"]["status"], "COMPLETED")
            chain = load_hash_chained_jsonl(self.root / "data" / "checkpoints.jsonl")
            self.assertEqual(chain["integrity_status"], "OK")
            self.assertEqual(chain["records"][-1]["event_type"], "run_completed")
            self.assertTrue((self.root / "reports" / "coordination" / "latest.json").exists())

    def test_pipeline_failure_records_failed_run_and_releases_lease(self):
        from stock_agent.pipeline import run_pipeline

        config = self._pipeline_config()
        with self.scoped(), patch("stock_agent.pipeline.collect_sources", side_effect=RuntimeError("source failure")):
            with self.assertRaises(RuntimeError):
                run_pipeline(config)
            self.assertFalse((self.root / "data" / "leases" / "run.json").exists())
            chain = load_hash_chained_jsonl(self.root / "data" / "checkpoints.jsonl")
            self.assertEqual(chain["records"][-1]["event_type"], "run_failed")

    def test_pipeline_lease_conflict_fails_before_collection(self):
        from stock_agent.pipeline import run_pipeline

        config = self._pipeline_config()
        with self.scoped():
            owner = RunLease(config, "run").acquire()
            try:
                with patch("stock_agent.pipeline.collect_sources") as collect:
                    with self.assertRaises(LeaseConflictError):
                        run_pipeline(config)
                    collect.assert_not_called()
            finally:
                owner.release()


if __name__ == "__main__":
    unittest.main()
