"""R07: runtime layout — five frozen roots, path safety, UTC/Shanghai clock.

Covers A35 (concurrent threads resolve the frozen layout and write ONLY into
the declared roots — never back into the source tree) and the O-layer half of
A36 (Chinese/space path mapping; separator/parent/absolute/escape rejection),
plus the clock semantics: UTC persistence, the explicit Asia/Shanghai
business/budget day with a fixed midnight boundary, naive/unknown-timezone
rejection, one skew tolerance, and the legacy-path map that never rewrites
stored history. The C-layer Linux case-sensitivity half of A36 stays with
R08's container acceptance.
"""
import sys
import tempfile
import threading
import unittest
from datetime import datetime, timezone
from pathlib import Path

from stock_agent.runtime_layout import (
    CLOCK_SKEW_TOLERANCE_SECONDS,
    LayoutError,
    PathMap,
    RuntimeLayout,
    business_date,
    business_date_key,
    clock_skew_status,
    ensure_within,
    resolve_layout,
    utc_now,
    utc_now_iso,
)

sys.path.insert(0, str(Path(__file__).resolve().parent))


class LayoutResolutionTests(unittest.TestCase):
    def test_defaults_anchor_under_project_root(self):
        layout = resolve_layout({}, env={})
        self.assertEqual(layout.project_root.name, "stock agent")
        self.assertEqual(layout.config_root, layout.project_root / "config")
        self.assertEqual(layout.state_root, layout.project_root / "data")
        self.assertEqual(layout.evidence_root, layout.project_root / "data" / "evidence_vault")
        self.assertEqual(layout.report_root, layout.project_root / "reports")
        self.assertNotIn(str(layout.project_root), str(layout.temp_root.relative_to(layout.temp_root.anchor)))

    def test_env_overrides_win_and_freeze_is_immutable(self):
        with tempfile.TemporaryDirectory() as tmp:
            roots = {name: Path(tmp) / name for name in
                     ("config", "state", "evidence", "report", "temp")}
            env = {
                "APP_CONFIG_ROOT": str(roots["config"]),
                "APP_STATE_ROOT": str(roots["state"]),
                "APP_EVIDENCE_ROOT": str(roots["evidence"]),
                "APP_REPORT_ROOT": str(roots["report"]),
                "APP_TEMP_ROOT": str(roots["temp"]),
            }
            layout = resolve_layout({}, env=env)
            self.assertEqual(layout.state_root, roots["state"].resolve())
            with self.assertRaises(Exception):
                # frozen dataclass: mutation is impossible, not merely discouraged
                layout.state_root = Path(tmp) / "elsewhere"  # type: ignore[misc]

    def test_config_section_and_unknown_keys(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = {"runtime_layout": {"state_root": str(Path(tmp) / "st")}}
            layout = resolve_layout(config, env={})
            self.assertEqual(layout.state_root, (Path(tmp) / "st").resolve())
            with self.assertRaises(LayoutError):
                resolve_layout({"runtime_layout": {"surprise_root": "/x"}}, env={})
            with self.assertRaises(LayoutError):
                resolve_layout({"runtime_layout": "not-an-object"}, env={})

    def test_snapshot_is_plain_strings(self):
        snapshot = resolve_layout({}, env={}).snapshot()
        self.assertIn("state_root", snapshot)
        self.assertTrue(all(isinstance(value, str) for key, value in snapshot.items()
                            if key != "schema_version"))


class PathSafetyTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        # Chinese + space in the root name: ordinary paths, mapped as-is.
        self.root = Path(temporary.name) / "中文 根目录"
        self.root.mkdir(parents=True, exist_ok=True)

    def test_a36_chinese_and_space_paths_resolve_correctly(self):
        layout = RuntimeLayout(
            project_root=self.root, config_root=self.root / "配置",
            state_root=self.root / "状态 数据", evidence_root=self.root / "证据",
            report_root=self.root / "报告", temp_root=self.root / "临时")
        target = layout.resolve_within_root(layout.state_root, "任务 档案", "档案.json")
        self.assertEqual(target.parent.name, "任务 档案")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("{}", encoding="utf-8")
        self.assertTrue(target.is_file())
        ensure_within(layout.state_root, target)

    def test_a36_separator_parent_and_absolute_segments_rejected(self):
        layout = resolve_layout({}, env={})
        for bad in ("..", "a/b", "a\\b", "", ".", "C:drive", "/abs"):
            with self.subTest(segment=bad):
                with self.assertRaises(LayoutError):
                    layout.resolve_within_root(layout.temp_root, bad)

    def test_a36_escape_attempt_outside_root_rejected(self):
        layout = resolve_layout({}, env={})
        outside = self.root / "escaped.json"
        with self.assertRaises(LayoutError):
            ensure_within(layout.temp_root, outside)

    def test_case_is_preserved_not_folded(self):
        # Linux sensitivity: the layout never lowercases; what was written is
        # what compares (the C-layer half runs under a real Linux root in R08).
        layout = resolve_layout({}, env={})
        target = layout.resolve_within_root(layout.temp_root, "CaseSensitive.json")
        self.assertEqual(target.name, "CaseSensitive.json")


class ClockTests(unittest.TestCase):
    def test_persisted_timestamps_are_utc(self):
        stamp = utc_now_iso()
        self.assertTrue(stamp.endswith("+00:00"))
        self.assertEqual(utc_now().tzinfo, timezone.utc)

    def test_business_day_boundary_is_shanghai_midnight(self):
        # 2026-10-20T17:00+00:00 == 2026-10-21 01:00 Shanghai: next day.
        self.assertEqual(business_date("2026-10-20T17:00:00+00:00").isoformat(), "2026-10-21")
        # 15:59+00:00 == 23:59 Shanghai: still the 20th.
        self.assertEqual(business_date("2026-10-20T15:59:00+00:00").isoformat(), "2026-10-20")
        # An explicit Shanghai timestamp maps to its own calendar date.
        self.assertEqual(business_date("2026-10-20T00:30:00+08:00").isoformat(), "2026-10-20")

    def test_naive_and_unknown_timezones_rejected(self):
        with self.assertRaises(LayoutError):
            business_date("2026-10-20T17:00:00")  # naive = unknown tz
        with self.assertRaises(LayoutError):
            business_date("2026-10-20T17:00:00+00:00", tz_name="Mars/Olympus")

    def test_clock_skew_fixed_tolerance(self):
        ok = clock_skew_status("2026-10-20T10:00:00+00:00", "2026-10-20T10:02:00+00:00")
        self.assertEqual(ok["status"], "ok")
        skewed = clock_skew_status("2026-10-20T10:00:00+00:00", "2026-10-20T10:10:00+00:00")
        self.assertEqual(skewed["status"], "skewed")
        self.assertEqual(skewed["tolerance_seconds"], CLOCK_SKEW_TOLERANCE_SECONDS)


class BudgetDayWiringTests(unittest.TestCase):
    """The Shanghai business day is the counting key everywhere."""

    def test_connector_quota_keys_use_business_date(self):
        from stock_agent.connector_runtime import ConnectorQueryStore

        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        store = ConnectorQueryStore(Path(temporary.name) / "q.sqlite3")
        try:
            from stock_agent.runtime_layout import business_date_key

            verdict = store.reserve_query(
                "k-r07", source_id="s", upstream="ifind", purpose="p",
                per_source_rpm=60, per_source_rpd=10, group_daily_limit=5)
            self.assertTrue(verdict["reserved"])
            group = "upstream::ifind"
            self.assertEqual(store.quota_usage(group), 1)
            self.assertEqual(store.quota_usage(group), 1)
            # The stored counter's date key IS the Shanghai business day.
            row = store._conn.execute(
                "SELECT usage_date FROM query_quota_counters WHERE scope = ?",
                (group,)).fetchone()
            self.assertEqual(str(row["usage_date"]), business_date_key())
        finally:
            store.close()

    def test_store_budget_day_counts_by_business_date(self):
        from stock_agent.task_runtime import TaskStore

        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        store = TaskStore(Path(temporary.name) / "t.sqlite3")
        try:
            task, _ = store.create_task(
                title="预算日", input_payload={"data_classification": "public",
                                               "research_question": "q"}, execution_mode="replay")
            claimed = store.claim_next("w")
            for _ in range(2):
                store.reserve_model_attempt(
                    task["task_id"], worker_id="w", fencing_token=claimed["fencing_token"],
                    attempt={"round": 1}, daily_limit=5)
            third = store.reserve_model_attempt(
                task["task_id"], worker_id="w", fencing_token=claimed["fencing_token"],
                attempt={"round": 1}, daily_limit=2)
            self.assertFalse(third["reserved"])
            self.assertIn("2/2", str(third["reason"]))
        finally:
            store.close()


class PathMapTests(unittest.TestCase):
    def test_legacy_paths_relocate_without_rewrite(self):
        with tempfile.TemporaryDirectory() as tmp:
            layout = resolve_layout({}, env={
                "APP_STATE_ROOT": str(Path(tmp) / "new-state"),
                "APP_REPORT_ROOT": str(Path(tmp) / "new-reports"),
            })
            mapping = PathMap.from_layout(
                layout, legacy_project_root="E:/stock agent")
            moved = mapping.resolve("E:/stock agent/data/events.jsonl")
            self.assertTrue(moved["relocated"])
            self.assertIn("new-state", moved["resolved"])
            self.assertEqual(moved["stored"], "E:/stock agent/data/events.jsonl",
                             "stored history text is never rewritten")
            foreign = mapping.resolve("D:/elsewhere/x.json")
            self.assertFalse(foreign["relocated"])
            self.assertEqual(foreign["resolved"], "D:/elsewhere/x.json")
            self.assertTrue(foreign["read_only_original"])


class A35ThreadedWriteTests(unittest.TestCase):
    def test_concurrent_threads_write_only_into_declared_roots(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        base = Path(temporary.name)
        env = {
            "APP_CONFIG_ROOT": str(base / "配置 根"),
            "APP_STATE_ROOT": str(base / "状态 根"),
            "APP_EVIDENCE_ROOT": str(base / "证据 根"),
            "APP_REPORT_ROOT": str(base / "报告 根"),
            "APP_TEMP_ROOT": str(base / "临时 根"),
        }
        # Snapshot resolved ONCE in the main thread; threads receive it
        # explicitly — nobody re-derives production paths from ambient state.
        shared_layout = resolve_layout({}, env=env)
        source_root = shared_layout.project_root
        probe_before = sorted(p.name for p in source_root.glob("R07-LEAK-*"))
        self.assertEqual(probe_before, [])
        errors = []
        barrier = threading.Barrier(8)

        def worker(index):
            try:
                barrier.wait()
                # A35: derive paths ONLY through the frozen layout helpers.
                target = shared_layout.resolve_within_root(
                    shared_layout.temp_root, "worker-{0}".format(index), "tick.json")
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text("{}", encoding="utf-8")
                ensure_within(shared_layout.temp_root, target)
            except Exception as exc:  # noqa: BLE001
                errors.append(repr(exc))

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=30)
        self.assertEqual(errors, [])
        written = list((base / "临时 根").rglob("tick.json"))
        self.assertEqual(len(written), 8,
                         "every thread wrote inside the declared temp root")
        self.assertEqual(sorted(p.name for p in source_root.glob("R07-LEAK-*")), [],
                         "nothing leaked back into the source tree")
        # And nothing landed outside the five roots at all.
        outside = [p for p in base.rglob("tick.json")
                   if "临时 根" not in str(p)]
        self.assertEqual(outside, [])


if __name__ == "__main__":
    unittest.main()
