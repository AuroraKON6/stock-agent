from __future__ import annotations

import json
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from stock_agent.config import PROJECT_ROOT, project_path, project_path_override
from stock_agent.models import RawSignal, now_iso


def _is_under(path: Path, base: Path) -> bool:
    """Return True when ``path`` lives under ``base`` (Python 3.8-safe)."""
    try:
        path.relative_to(base)
        return True
    except ValueError:
        return False


def _tree_state(base: Path):
    """Snapshot a tree as {relative path: (size, mtime_ns)} for cheap diffs."""
    if not base.exists():
        return None
    state = {}
    for path in base.rglob("*"):
        if path.is_file():
            stat = path.stat()
            state[str(path.relative_to(base))] = (stat.st_size, stat.st_mtime_ns)
    return state


def _tree_diff(before, after):
    """Return (added, removed, changed) relative paths between two snapshots."""
    before = before or {}
    after = after or {}
    added = sorted(set(after) - set(before))
    removed = sorted(set(before) - set(after))
    changed = sorted(key for key in set(before) & set(after) if before[key] != after[key])
    return added, removed, changed


def json_case_count(path: Path) -> int:
    data = json.loads(path.read_text(encoding="utf-8-sig"))
    return len(data.get("cases", []) or [])


class PipelineRuntimeIsolationTests(unittest.TestCase):
    """Pipeline runs must stay inside an isolated temp root.

    ``config.project_path`` is the one shared resolver every module imported at
    load time, so routing it through a contextvars override reroutes all writers
    at once. The pipeline installs that override for the entire run body; a test
    only needs to either patch ``stock_agent.pipeline.project_path`` or enter a
    ``project_path_override`` context around the call. Patching individual
    modules is neither needed nor sufficient.
    """

    def _config(self, root: Path):
        return {
            "storage": {"sqlite_path": str(root / "data" / "signals.sqlite3")},
            "sources": {},
            "topics": [{"name": "AI compute", "symbols": ["VerificationCo"], "keywords": ["major contract"], "risk_keywords": []}],
            "priority_rules": {
                "p0_keywords": [],
                "p1_keywords": ["major contract"],
                "positive_keywords": [],
                "negative_keywords": [],
                "review_keywords": [],
            },
            "research_cases": {"enabled": True, "llm_enabled": False},
            "research_framework": {"enabled": False},
            "research_team": {"enabled": False},
            "llm_agent": {"enabled": False},
            "multi_role_agent": {"enabled": False},
            "research_runtime": {"enabled": True, "auto_sync": True},
            "research_workpapers": {
                "enabled": True,
                "enforce": True,
                "events_path": "data/research_workpapers/events.jsonl",
                "projection_path": "data/research_workpapers/latest.json",
                "report_dir": "reports/research_workpapers",
                "retention_years": 5,
                "require_official_primary_for": ["P0", "P1"],
                "require_exact_evidence_refs": True,
                "require_market_governance_pass": True,
                "require_distinct_human_roles": True,
                "require_independent_challenge": True,
                "production_archive_approved": False,
                "production_archive_evidence_ref": "",
            },
            "report": {"include_low_priority": True},
        }

    def _raw_signal(self) -> RawSignal:
        return RawSignal(
            source="manual",
            source_type="filing",
            title="VerificationCo wins major AI server contract",
            summary="major contract",
            url="https://example.test/ann",
            published_at=now_iso(),
            payload={
                "symbol": "688888",
                "announcement_type": "major_contract",
                "filing_evidence": {"announcement_type": "major_contract", "status": "ok"},
            },
        )

    def _run_via_single_resolver_patch(self, pipeline_fn):
        """Inject by patching only ``pipeline.project_path``.

        ``run_pipeline`` / ``run_agent_pipeline`` install the patched resolver as
        the active contextvars override, so every module that imported the shared
        ``config.project_path`` follows it for the whole body.
        """

        def _run(root: Path, raw: RawSignal):
            config = self._config(root)
            with patch("stock_agent.pipeline.project_path", side_effect=lambda p, r=root: r / p):
                with patch("stock_agent.pipeline.collect_sources", return_value=[raw]):
                    return pipeline_fn(config)

        return _run

    def _run_via_context_override(self, pipeline_fn):
        """Inject by entering a ``project_path_override`` context (no patching).

        The pipeline's own inner ``project_path_override(project_path)`` is a
        no-op when no resolver is installed, so the caller's active override is
        preserved for the entire run body.
        """

        def _run(root: Path, raw: RawSignal):
            config = self._config(root)
            with patch("stock_agent.pipeline.collect_sources", return_value=[raw]):
                with project_path_override(lambda p, r=root: r / p):
                    return pipeline_fn(config)

        return _run

    def _assert_full_isolation(self, pipeline_fn, run):
        real_before = {name: _tree_state(PROJECT_ROOT / name) for name in ("data", "reports", "logs")}
        raw = self._raw_signal()
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            result = run(root, raw)
            # The run produced artifacts under the temp root.
            files = [str(path.relative_to(root)) for path in root.rglob("*") if path.is_file()]
            self.assertTrue(files, "pipeline produced no artifacts under the temp root")
            # Research runtime actually synced under the temp root (not skipped).
            runtime = root / "data" / "research_runtime" / "runtime.json"
            self.assertTrue(runtime.exists(), "research runtime not written under temp root")
            self.assertGreaterEqual(json_case_count(runtime), 1)
            workpaper_events = root / "data" / "research_workpapers" / "events.jsonl"
            workpaper_projection = root / "data" / "research_workpapers" / "latest.json"
            workpaper_report = root / "reports" / "research_workpapers" / "latest.json"
            self.assertTrue(workpaper_events.exists(), "research workpaper event ledger was not written")
            self.assertTrue(workpaper_projection.exists(), "research workpaper projection was not written")
            self.assertTrue(workpaper_report.exists(), "research workpaper report was not written")
            self.assertGreaterEqual(result.stats.get("research_workpaper_count", 0), 1)
            self.assertEqual(result.stats.get("research_workpaper_integrity_status"), "OK")
            diagnostics = result.stats.get("research_runtime_diagnostics", {})
            self.assertFalse(diagnostics.get("degraded", False))
            # Every reported artifact path lives under the temp root.
            for attr in ("report_path", "json_path", "alert_path", "review_path", "actions_path"):
                value = getattr(result, attr, None) or ""
                if value:
                    path = Path(value)
                    self.assertTrue(path.is_absolute(), "{0} is not absolute".format(value))
                    self.assertTrue(_is_under(path, root), "{0} escapes the temp root".format(value))
        # Nothing changed or was added in the real data/reports/logs trees.
        real_after = {name: _tree_state(PROJECT_ROOT / name) for name in ("data", "reports", "logs")}
        for name in ("data", "reports", "logs"):
            added, removed, changed = _tree_diff(real_before[name], real_after[name])
            self.assertEqual(
                (added, removed, changed),
                ([], [], []),
                "real {0} tree was changed by the run (added={1}, removed={2}, changed={3})".format(
                    name, added, removed, changed
                ),
            )

    def test_run_pipeline_isolates_all_artifacts_via_single_resolver_patch(self):
        from stock_agent.pipeline import run_pipeline

        self._assert_full_isolation(run_pipeline, self._run_via_single_resolver_patch(run_pipeline))

    def test_agent_pipeline_isolates_all_artifacts_via_single_resolver_patch(self):
        from stock_agent.pipeline import run_agent_pipeline

        self._assert_full_isolation(run_agent_pipeline, self._run_via_single_resolver_patch(run_agent_pipeline))

    def test_agent_pipeline_isolates_all_artifacts_via_context_override(self):
        from stock_agent.pipeline import run_agent_pipeline

        self._assert_full_isolation(run_agent_pipeline, self._run_via_context_override(run_agent_pipeline))

    def test_imported_project_path_follows_active_override(self):
        # Modules do ``from .config import project_path`` at import time; the
        # shared function must consult the active override on every call.
        imported = project_path
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            with project_path_override(lambda p, r=root: r / p):
                self.assertEqual(imported("data/from_import"), root / "data" / "from_import")
            self.assertEqual(imported("data/from_import"), PROJECT_ROOT / "data" / "from_import")

    def test_override_restored_after_exception(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            resolver = lambda p, r=root: r / p
            with self.assertRaises(RuntimeError):
                with project_path_override(resolver):
                    self.assertEqual(project_path("data/x"), root / "data" / "x")
                    raise RuntimeError("boom")
            self.assertEqual(project_path("data/x"), PROJECT_ROOT / "data" / "x")

    def test_pipeline_restores_resolver_after_exception(self):
        from stock_agent.pipeline import run_pipeline

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            with patch("stock_agent.pipeline.project_path", side_effect=lambda p, r=root: r / p):
                with patch("stock_agent.pipeline.collect_sources", side_effect=RuntimeError("collect failed")):
                    with self.assertRaises(RuntimeError):
                        run_pipeline(self._config(root))
                # The temp resolver must not leak after the failed run.
                self.assertEqual(project_path("data/x"), PROJECT_ROOT / "data" / "x")

    def test_concurrent_overrides_stay_isolated(self):
        results = {}
        errors = []

        def worker(name, root):
            try:
                with project_path_override(lambda p, r=root: r / name / p):
                    results[name] = project_path("data/concurrent")
            except Exception as exc:  # pragma: no cover - failure path
                errors.append((name, exc))

        with tempfile.TemporaryDirectory() as tmp1:
            with tempfile.TemporaryDirectory() as tmp2:
                root1, root2 = Path(tmp1), Path(tmp2)
                expected_one = root1 / "one" / "data" / "concurrent"
                expected_two = root2 / "two" / "data" / "concurrent"
                threads = [
                    threading.Thread(target=worker, args=("one", root1)),
                    threading.Thread(target=worker, args=("two", root2)),
                ]
                for thread in threads:
                    thread.start()
                for thread in threads:
                    thread.join()
        self.assertEqual(errors, [])
        self.assertEqual(results.get("one"), expected_one)
        self.assertEqual(results.get("two"), expected_two)
        # The main thread is unaffected by the worker contexts.
        self.assertEqual(project_path("data/concurrent"), PROJECT_ROOT / "data" / "concurrent")

    def test_normal_and_absolute_paths_outside_context(self):
        # Relative paths resolve to the project root when no override is active.
        self.assertEqual(project_path("data/plain"), PROJECT_ROOT / "data" / "plain")
        self.assertEqual(project_path("reports/daily/x.md"), PROJECT_ROOT / "reports" / "daily" / "x.md")
        # Absolute paths are always returned unchanged.
        absolute = str(PROJECT_ROOT / "tmp" / "absolute.json")
        self.assertEqual(project_path(absolute), Path(absolute))
        # Inside an override, relative paths reroute but absolute ones still pass through.
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            with project_path_override(lambda p, r=root: r / p):
                self.assertEqual(project_path("data/rel"), root / "data" / "rel")
                self.assertEqual(project_path(absolute), Path(absolute))

    def test_normal_resolver_override_is_noop(self):
        # project_path_override must treat None / the normal resolver as a no-op,
        # otherwise run_pipeline would clobber an already-active override or recurse.
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            with project_path_override(lambda p, r=root: r / p):
                with project_path_override(project_path):
                    self.assertEqual(project_path("data/x"), root / "data" / "x")
                with project_path_override(None):
                    self.assertEqual(project_path("data/x"), root / "data" / "x")


if __name__ == "__main__":
    unittest.main()
