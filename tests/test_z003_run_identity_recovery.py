"""Z-003 fault-injection tests: run identity and failure recovery.

Written BEFORE the coordination/manifest fixes, per the handover brief:
fault cases first, then code changes. Coverage maps to the six risk areas —
shared writes, lease renewal/expiry, manifest wiring, un-analyzed duplicate
signals, partial artifacts, and concurrent file names — plus Windows
junction/parent-reparse path checks. All offline; no network or model calls.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from stock_agent.audit_log import load_hash_chained_jsonl
from stock_agent.config import project_path_override
from stock_agent.models import AnalyzedSignal, RawSignal, now_iso
from stock_agent.run_coordination import CheckpointStore, begin_coordinated_run
from stock_agent.run_manifest import (
    RunManifestError,
    build_run_manifest,
    load_run_manifest,
    write_run_manifest,
)

PROJECT_ROOT = Path(__file__).resolve().parent.parent

IS_WINDOWS = sys.platform.startswith("win")


class Z003WorkspaceTestCase(unittest.TestCase):
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
            },
            "run_manifest": {
                "enabled": True,
                "manifest_dir": str(self.root / "data" / "manifests"),
                "pointer_path": str(self.root / "reports" / "latest_manifest.json"),
            },
        }

    def tearDown(self):
        self.tmp.cleanup()

    def scoped(self):
        return project_path_override(lambda value, root=self.root: root / value)


def _make_report(root: Path, stem: str, run_id: str, mode: str = "agent-run") -> Path:
    """Write a minimal JSON report plus its daily/alert/run_ops siblings."""

    for rel in ("reports/json", "reports/daily", "reports/alerts", "logs/run_ops"):
        (root / rel).mkdir(parents=True, exist_ok=True)
    run_ops_path = root / "logs" / "run_ops" / "run_ops_{0}.json".format(run_id)
    run_ops_path.write_text(json.dumps({"run_id": run_id}), encoding="utf-8")
    report = {
        "generated_at": now_iso(),
        "stats": {
            "run_coordination": {"run_id": run_id, "mode": mode, "status": "COMPLETED"},
            "run_ops_timestamped_path": str(run_ops_path),
        },
    }
    report_path = root / "reports" / "json" / "{0}.json".format(stem)
    report_path.write_text(json.dumps(report), encoding="utf-8")
    (root / "reports" / "daily" / "{0}.md".format(stem)).write_text("# daily\n", encoding="utf-8")
    (root / "reports" / "alerts" / "{0}.jsonl".format(stem)).write_text("", encoding="utf-8")
    return report_path


def _complete_run(config: dict, run_id: str, root: Path) -> str:
    """Complete a coordinated agent-run and return its actual generated run_id."""

    with project_path_override(lambda value, r=root: r / value):
        coordinator = begin_coordinated_run(config, "agent-run")
        if coordinator is None:
            raise AssertionError("expected a coordinated run lease")
        actual = str(getattr(coordinator, "run_id", "") or "")
        coordinator.complete({})
        coordinator.release()
        return actual


def _completed_report(root: Path, config: dict, stem: str) -> tuple:
    actual_run_id = _complete_run(config, stem, root)
    return _make_report(root, stem, actual_run_id), actual_run_id


class TestParentJunctionRejected(Z003WorkspaceTestCase):
    """Path checks must cover junctions/reparse points on ANY path component."""

    @unittest.skipUnless(IS_WINDOWS, "junctions are a Windows reparse point")
    def test_report_under_parent_junction_is_rejected(self):
        target = self.root / "real_reports"
        target.mkdir(parents=True, exist_ok=True)
        report_file = target / "r1.json"
        report_file.write_text("{}", encoding="utf-8")
        junction = self.root / "junction_reports"
        result = subprocess.run(
            ["cmd", "/c", "mklink", "/J", str(junction), str(target)],
            capture_output=True, text=True, timeout=30,
        )
        self.assertEqual(result.returncode, 0, str(result.stdout or "") + str(result.stderr or ""))

        with self.scoped():
            with self.assertRaises(RunManifestError):
                # Even though the junction resolves inside the workspace, the
                # path itself contains a reparse-point component and must be
                # refused rather than silently canonicalized.
                build_run_manifest(
                    dict(self.config, run_manifest={"enabled": False}),
                    str(junction / "r1.json"),
                    run_id="run-junction-1",
                )

    @unittest.skipUnless(IS_WINDOWS, "junctions are a Windows reparse point")
    def test_leaf_junction_is_rejected(self):
        target = self.root / "leaf_real"
        target.mkdir(parents=True, exist_ok=True)
        report_file = target / "r2.json"
        report_file.write_text("{}", encoding="utf-8")
        junction = self.root / "leaf_junction"
        subprocess.run(["cmd", "/c", "mklink", "/J", str(junction), str(target)], capture_output=True, timeout=30)
        with self.scoped():
            with self.assertRaises(RunManifestError):
                build_run_manifest(
                    dict(self.config, run_manifest={"enabled": False}),
                    str(junction / "r2.json"),
                    run_id="run-junction-2",
                )


class TestSameSecondReportCollision(Z003WorkspaceTestCase):
    """run and agent-run may legally coexist; same-second outputs must not overwrite."""

    def test_two_outputs_in_same_second_do_not_collide(self):
        from stock_agent.reports import write_outputs

        fixed = "2026-09-18T02:00:00+08:00"
        stats = {"run_coordination": {"run_id": "r-collide", "mode": "run", "status": "COMPLETED"}}
        with self.scoped(), patch("stock_agent.reports.now_iso", return_value=fixed):
            first = write_outputs([], dict(self.config, report={}), stats)
            second = write_outputs([], dict(self.config, report={}), stats)
        self.assertNotEqual(first[1], second[1], "second same-second run overwrote the first JSON report")
        for path in (first[0], first[1], first[2], second[0], second[1], second[2]):
            self.assertTrue(Path(path).exists(), "missing output {0}".format(path))

    def test_outputs_use_atomic_writes(self):
        from stock_agent import reports as reports_module
        from stock_agent.reports import write_outputs

        stats = {"run_coordination": {"run_id": "r-atomic", "mode": "run", "status": "COMPLETED"}}
        with self.scoped(), patch.object(
            reports_module, "atomic_write_text", wraps=reports_module.atomic_write_text
        ) as spy:
            write_outputs([], dict(self.config, report={}), stats)
            self.assertGreaterEqual(spy.call_count, 3, "daily/json/alert writes must be atomic")


class TestManifestWiring(Z003WorkspaceTestCase):
    """A completed coordinated pipeline run must record an exact manifest."""

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
            "run_manifest": self.config["run_manifest"],
        }

    def _raw(self):
        return RawSignal(
            source="manual", source_type="news", title="A major contract announcement",
            summary="major contract", url="https://example.test/news", published_at=now_iso(), payload={},
        )

    def test_completed_pipeline_records_manifest_and_pointer(self):
        from stock_agent.pipeline import run_pipeline

        config = self._pipeline_config()
        with self.scoped(), patch("stock_agent.pipeline.collect_sources", return_value=[self._raw()]):
            result = run_pipeline(config)

        manifest_stats = result.stats.get("run_manifest") or {}
        self.assertEqual(manifest_stats.get("status"), "RECORDED", "pipeline must write a per-run manifest")
        manifest_path = Path(str(manifest_stats.get("manifest_path") or ""))
        self.assertTrue(manifest_path.is_file(), "manifest file missing")
        pointer_path = Path(str(self.config["run_manifest"]["pointer_path"]))
        self.assertTrue(pointer_path.is_file(), "manifest pointer missing")

        pointer = json.loads(pointer_path.read_text(encoding="utf-8"))
        self.assertEqual(pointer["run_id"], result.stats["run_coordination"]["run_id"])

        with self.scoped():
            verified = load_run_manifest(str(manifest_path), verify_files=True)
        self.assertEqual(verified["run_id"], result.stats["run_coordination"]["run_id"])
        self.assertEqual(verified["status"], "COMPLETED")

    def test_manifest_error_is_surfaced_not_swallowed(self):
        from stock_agent.pipeline import _record_run_manifest
        from stock_agent.models import PipelineResult

        result = PipelineResult(
            generated_at=now_iso(), signals=[], alerts=[],
            report_path="reports/daily/missing.md",
            json_path="reports/json/missing.json",
            alert_path="reports/alerts/missing.jsonl",
            stats={},
        )
        with self.scoped():
            outcome = _record_run_manifest(self.config, result, mode="agent-run")
        self.assertEqual(outcome.get("status"), "ERROR")
        self.assertTrue(str(outcome.get("error") or ""))


class TestManifestIntegrityGuards(Z003WorkspaceTestCase):
    def test_partial_artifacts_block_manifest(self):
        report_path, run_id = _completed_report(self.root, self.config, "partial1")
        (self.root / "reports" / "alerts" / "partial1.jsonl").unlink()
        with self.scoped():
            with self.assertRaises(RunManifestError):
                build_run_manifest(self.config, str(report_path), run_id=run_id)

    def test_corrupt_ledger_blocks_manifest(self):
        report_path, run_id = _completed_report(self.root, self.config, "corrupt1")
        ledger = Path(self.config["run_coordination"]["checkpoint_path"])
        lines = ledger.read_text(encoding="utf-8").splitlines()
        lines[0] = lines[0][: max(0, len(lines[0]) - 4)] + '"}'  # break the first hash link
        ledger.write_text("\n".join(lines) + "\n", encoding="utf-8")
        with self.scoped():
            with self.assertRaises(Exception):
                build_run_manifest(self.config, str(report_path), run_id=run_id)

    def test_manifest_immutable_across_unrelated_later_runs(self):
        report_path, run_id = _completed_report(self.root, self.config, "stable1")
        with self.scoped():
            first = write_run_manifest(self.config, str(report_path), run_id=run_id)
        self.assertEqual(first["status"], "RECORDED")

        # An unrelated later run appends to the ledger; the old manifest must
        # still verify byte-for-byte against its recorded identity.
        other_report, other_run_id = _completed_report(self.root, self.config, "stable2")
        with self.scoped():
            write_run_manifest(self.config, str(other_report), run_id=other_run_id)

        with self.scoped():
            verified = load_run_manifest(first["manifest_path"], expected_run_id=run_id, verify_files=False)
        self.assertEqual(verified["verified_sha256"], first["manifest_sha256"])

    def test_conflicting_rewrite_of_same_run_id_is_refused(self):
        report_path, run_id = _completed_report(self.root, self.config, "conflict1")
        with self.scoped():
            write_run_manifest(self.config, str(report_path), run_id=run_id)
        # Tamper the source report and attempt a second manifest for the same run.
        payload = json.loads(report_path.read_text(encoding="utf-8"))
        payload["tampered"] = True
        report_path.write_text(json.dumps(payload), encoding="utf-8")
        with self.scoped():
            with self.assertRaises(Exception):
                write_run_manifest(self.config, str(report_path), run_id=run_id)


class TestUnanalyzedDuplicateSignals(Z003WorkspaceTestCase):
    """A stored-but-unanalyzed signal must stay flagged for human review."""

    def test_llm_error_and_needs_review_survive_duplicate_upsert(self):
        from stock_agent.storage import SignalStore

        def signal() -> AnalyzedSignal:
            return AnalyzedSignal(
                signal_id="dup-0001", source="manual", source_type="news",
                title="duplicate probe", summary="s", url="https://example.test/d",
                published_at=now_iso(), collected_at=now_iso(),
                priority="P1", priority_score=55.0, needs_review=True,
                llm_error="LLM call failed after provider fallback: HTTP Error 401",
            )

        store = SignalStore(self.root / "signals.sqlite3")
        try:
            first, _ = store.upsert_many([signal()])
            second, dup = store.upsert_many([signal()])
            self.assertEqual(len(first), 1)
            self.assertEqual(len(second), 0)
            self.assertEqual(int(dup or 0), 1)
            loaded = store.fetch_many(["dup-0001"])
            self.assertTrue(loaded, "stored signal missing")
            row = loaded[0]
            self.assertTrue(bool(row.get("needs_review")), "duplicate upsert cleared needs_review")
            self.assertIn("401", str(row.get("llm_error") or ""), "duplicate upsert cleared llm_error")
        finally:
            store.close()


class TestSchedulerManifestBinding(unittest.TestCase):
    """The scheduled runner must bind its summary to the exact manifest."""

    def test_daily_runner_reads_manifest_pointer_and_cross_checks_run_id(self):
        script = (PROJECT_ROOT / "scripts" / "run_daily_research.ps1").read_text(encoding="utf-8")
        self.assertIn("latest_manifest.json", script)
        self.assertIn("run_manifest_path", script)
        self.assertIn("run_manifest_sha256", script)
        self.assertIn("run_id", script)


if __name__ == "__main__":
    unittest.main()
