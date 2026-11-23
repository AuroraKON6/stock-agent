"""R25 image verification: relocated runtime roots and image packaging.

The candidate image runs with the APP_* roots of runtime_layout while the
code kept resolving project-relative ``data/...`` paths under the image's
``/app``. These tests pin the regression: path resolution follows the APP_*
roots (host runs without them are unchanged), backup/restore works on the
relocated trees with layout-neutral manifest paths, and the backup's required
static inputs ship in the image instead of being mounted from the host.
"""
import hashlib
import json
import os
import re
import shutil
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest import mock

from stock_agent.backup_restore import (
    REQUIRED_STATIC_PATHS,
    create_backup,
    run_restore_drill,
    verify_backup,
)
from stock_agent.config import (
    PROJECT_ROOT,
    project_path,
    project_relative_path,
    within_project_roots,
)
from stock_agent.task_runtime import TaskStore

ROOT = PROJECT_ROOT


def _spec(question):
    from stock_agent.task_supply import build_task_spec

    spec = build_task_spec(research_question=question, symbols=["600519"],
                           execution_mode="replay", config=None)
    spec["bound_signal_ids"] = []
    spec["public_context"]["evidence_summaries"] = {}
    return spec


class RelocatedProjectPathTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name).resolve()
        roots = {"APP_CONFIG_ROOT": "cfg", "APP_STATE_ROOT": "state",
                 "APP_EVIDENCE_ROOT": "evidence", "APP_REPORT_ROOT": "reports"}
        environment = {name: str(self.base / folder) for name, folder in roots.items()}
        self.enterContext(mock.patch.dict(os.environ, environment))

    def test_relative_trees_follow_the_app_roots(self):
        cases = {
            "data/task_runtime/tasks.sqlite3": "state/task_runtime/tasks.sqlite3",
            "data/evidence_vault/objects/sha256/ab/cd": "evidence/objects/sha256/ab/cd",
            "data/evidence_vault/index.json": "evidence/index.json",
            "config/watchlist.local.json": "cfg/watchlist.local.json",
            "reports/daily/x.json": "reports/daily/x.json",
        }
        for relative, expected in cases.items():
            with self.subTest(relative=relative):
                self.assertEqual(project_path(relative), self.base / expected)

    def test_code_and_absolute_paths_are_untouched(self):
        self.assertEqual(project_path("pyproject.toml"), PROJECT_ROOT / "pyproject.toml")
        self.assertEqual(project_path("stock_agent/cli.py"), PROJECT_ROOT / "stock_agent/cli.py")
        self.assertEqual(project_path("logs/scheduled"), PROJECT_ROOT / "logs/scheduled")
        self.assertEqual(project_path("."), PROJECT_ROOT)
        self.assertEqual(project_path("/etc/passwd"), Path("/etc/passwd"))

    def test_relative_spelling_round_trips_and_roots_are_known(self):
        for relative in ("data/task_runtime/tasks.sqlite3",
                         "data/evidence_vault/objects/sha256/ab/cdef", "config/a.json",
                         "reports/a/b.json", "pyproject.toml"):
            resolved = project_path(relative)
            with self.subTest(relative=relative):
                self.assertEqual(project_relative_path(resolved), relative)
                self.assertEqual(project_path(project_relative_path(resolved)), resolved)
                self.assertTrue(within_project_roots(resolved))
        self.assertFalse(within_project_roots(Path("/definitely/outside")))
        self.assertEqual(project_relative_path(Path("/definitely/outside")),
                         "/definitely/outside")

    def test_without_app_roots_the_host_layout_is_unchanged(self):
        with mock.patch.dict(os.environ, {name: "" for name in (
                "APP_CONFIG_ROOT", "APP_STATE_ROOT", "APP_EVIDENCE_ROOT", "APP_REPORT_ROOT")}):
            for relative in ("data/task_runtime/tasks.sqlite3",
                             "data/evidence_vault/objects/sha256/ab", "config/a.json",
                             "reports/a.json"):
                self.assertEqual(project_path(relative), PROJECT_ROOT / relative)
                self.assertEqual(project_relative_path(PROJECT_ROOT / relative), relative)

    def test_test_override_still_wins_over_everything(self):
        from stock_agent.config import project_path_override

        with project_path_override(lambda value: self.base / "override" / str(value)):
            self.assertEqual(project_path("data/x"), self.base / "override/data/x")
            self.assertEqual(project_relative_path(self.base / "override/data/x"), "data/x")
            self.assertFalse(within_project_roots(self.base / "state/x"))


class RelocatedBackupRestoreTests(unittest.TestCase):
    """backup-create / restore-drill with data, evidence and reports on
    relocated roots and only the code + static inputs under PROJECT_ROOT."""

    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name).resolve()
        self.state = self.base / "state"
        self.evidence = self.base / "evidence"
        self.reports = self.base / "reports"
        self.config_root = self.base / "cfg"
        environment = {
            "APP_CONFIG_ROOT": str(self.config_root),
            "APP_STATE_ROOT": str(self.state),
            "APP_EVIDENCE_ROOT": str(self.evidence),
            "APP_REPORT_ROOT": str(self.reports),
        }
        self.enterContext(mock.patch.dict(os.environ, environment))
        self.config = {
            "storage": {"sqlite_path": "data/stock_agent.sqlite3"},
            "task_runtime": {"db_path": "data/task_runtime/tasks.sqlite3"},
            "kimi_sources": {"query_store_path": "data/kimi/query_intake.sqlite3"},
            "backup_restore": {"enabled": True},
            "official_evidence_vault": {
                "objects_dir": "data/evidence_vault/objects/sha256",
            },
            "pi_agent": {"enabled": False},
        }
        for relative in ("data/stock_agent.sqlite3", "data/kimi/query_intake.sqlite3"):
            target = project_path(relative)
            target.parent.mkdir(parents=True, exist_ok=True)
            sqlite3.connect(str(target)).close()
        content = b"synthetic evidence"
        digest = hashlib.sha256(content).hexdigest()
        self.object_relative = "data/evidence_vault/objects/sha256/{0}/{1}".format(
            digest[:2], digest)
        object_path = project_path(self.object_relative)
        object_path.parent.mkdir(parents=True, exist_ok=True)
        object_path.write_bytes(content)
        (self.evidence / "events.jsonl").write_text(
            json.dumps({"event_id": "e1", "event_type": "evidence",
                        "payload": {"title": "公告"}}, ensure_ascii=False) + "\n",
            encoding="utf-8")
        store = TaskStore(project_path("data/task_runtime/tasks.sqlite3"))
        try:
            store.create_task(title="relocated", input_payload=_spec("relocated?"),
                              execution_mode="replay")
        finally:
            store.close()
        self.config_path = "config/watchlist.drill.json"
        self.config_root.mkdir(parents=True, exist_ok=True)
        (self.config_root / "watchlist.drill.json").write_text(
            json.dumps(self.config, ensure_ascii=False), encoding="utf-8")
        # The image bakes the example into its config root; the backup treats
        # it as a required static input (R24).
        (self.config_root / "watchlist.example.json").write_text(
            (PROJECT_ROOT / "config/watchlist.example.json").read_text(encoding="utf-8"),
            encoding="utf-8")

    def test_nothing_is_written_under_the_project_root(self):
        created = create_backup(self.config, config_path=self.config_path)
        self.assertEqual(created["status"], "PASS", created.get("errors"))
        self.assertTrue(created["bundle_path"].startswith("data/backup_restore/bundles/"))
        self.assertFalse((PROJECT_ROOT / "data/backup_restore").exists())
        self.assertEqual(project_path(created["bundle_path"]).resolve(),
                         (self.state / "backup_restore/bundles").resolve()
                         / Path(created["bundle_path"]).name)
        self.assertTrue((self.reports / "backup_restore").is_dir())

    def test_manifest_paths_stay_project_relative_and_restore_passes(self):
        created = create_backup(self.config, config_path=self.config_path)
        bundle = project_path(created["bundle_path"])
        manifest = json.loads((bundle / "manifest.json").read_text(encoding="utf-8"))
        paths = {item["path"]: item["kind"] for item in manifest["files"]}
        self.assertEqual(paths.get("data/task_runtime/tasks.sqlite3"), "sqlite")
        self.assertEqual(paths.get(self.object_relative), "evidence_object")
        self.assertEqual(paths.get("data/evidence_vault/events.jsonl"), "fact_jsonl")
        # The raw config is never exported; only its sanitized projection is.
        self.assertNotIn("config/watchlist.drill.json", paths)
        self.assertEqual(paths.get("config/watchlist.restore.json"), "sanitized_config")
        self.assertIn("config/watchlist.example.json", paths)
        self.assertEqual(manifest["runtime_layout"]["state_root"], str(self.state))
        self.assertEqual(verify_backup(self.config, created["bundle_path"])["status"], "PASS")

        drill = run_restore_drill(self.config, created["bundle_path"])
        self.assertEqual(drill["status"], "PASS", drill.get("errors"))
        self.assertTrue(drill["restored_root"].startswith("data/backup_restore/drills/"))
        content = b"synthetic evidence"
        # The LIVE task store is found on its relocated root, not skipped.
        self.assertEqual(drill["task_runtime_integrity"]["status"], "PASS")
        self.assertEqual(drill["task_runtime_integrity"]["tables"]["tasks"],
                         {"source": 1, "restored": 1})
        restored = project_path(drill["restored_root"])
        self.assertEqual((restored / self.object_relative).read_bytes(), content)

    def test_roots_outside_the_workspace_are_refused(self):
        from stock_agent.backup_restore import BackupRestoreError, _controlled_root

        with self.assertRaises(BackupRestoreError):
            _controlled_root(str(self.base / "elsewhere"), "bundle_root")
        with self.assertRaises(BackupRestoreError):
            _controlled_root(str(self.state), "bundle_root")
        self.assertEqual(_controlled_root("data/backup_restore/bundles", "bundle_root"),
                         self.state / "backup_restore/bundles")


class ReadOnlyBundleSqliteTests(unittest.TestCase):
    """A bundle handed to the recovering side on a read-only mount must still
    verify: WAL databases are opened immutably, never through sidecar files."""

    def test_wal_database_verifies_on_a_read_only_mount_without_sidecars(self):
        from stock_agent.backup_restore import _open_payload_sqlite, _sqlite_metadata

        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        bundle = Path(temporary.name).resolve() / "payload"
        bundle.mkdir()
        database = bundle / "tasks.sqlite3"
        connection = sqlite3.connect(str(database))
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("CREATE TABLE t (id INTEGER PRIMARY KEY)")
        connection.executemany("INSERT INTO t VALUES (?)", [(1,), (2,)])
        connection.commit()
        connection.close()
        self.assertEqual(sorted(path.name for path in bundle.iterdir()), ["tasks.sqlite3"])
        bundle.chmod(0o555)
        self.addCleanup(bundle.chmod, 0o755)

        with _open_payload_sqlite(database) as opened:
            self.assertEqual(opened.execute("SELECT COUNT(*) FROM t").fetchone()[0], 2)
        metadata = _sqlite_metadata(database)
        self.assertEqual(metadata["integrity_status"], "OK", metadata["errors"])
        self.assertEqual(metadata["table_counts"], {"t": 2})
        self.assertEqual(sorted(path.name for path in bundle.iterdir()), ["tasks.sqlite3"])


class ReadOnlyBaselineSnapshotTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.source = self.root / "source.sqlite3"
        self.connection = self.enterContext(closing(sqlite3.connect(self.source)))
        self.connection.execute("PRAGMA journal_mode=WAL")
        self.connection.execute("PRAGMA wal_autocheckpoint=0")
        self.connection.execute("CREATE TABLE research_tasks (task_id TEXT PRIMARY KEY, status TEXT)")
        self.connection.execute("CREATE TABLE task_outbox (sequence INTEGER PRIMARY KEY, task_id TEXT)")
        self.connection.commit()
        self.assertEqual(self.connection.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone(), (0, 0, 0))
        self.connection.execute("INSERT INTO research_tasks VALUES ('task_a', 'succeeded')")
        self.connection.execute("INSERT INTO task_outbox VALUES (1, 'task_a')")
        self.connection.commit()
        self.wal = self.source.with_name(self.source.name + "-wal")
        self.assertGreater(self.wal.stat().st_size, 0)
        self.assertEqual(self.connection.execute("SELECT COUNT(*) FROM research_tasks").fetchone(), (1,))
        self.assertEqual(self.connection.execute("SELECT COUNT(*) FROM task_outbox").fetchone(), (1,))

    def readonly_copy(self):
        directory = self.root / "recovered"
        directory.mkdir()
        database = directory / self.source.name
        for source in (self.source, self.wal):
            copied = directory / source.name
            shutil.copyfile(source, copied)
            copied.chmod(0o444)
            self.addCleanup(copied.chmod, 0o644)
        directory.chmod(0o555)
        self.addCleanup(directory.chmod, 0o755)
        if os.access(directory, os.W_OK):
            # Windows and privileged test runners may still create sidecars.
            # Model SQLite's refusal while keeping real fixture DB and WAL.
            connect = sqlite3.connect

            def connect_without_writable_sidecars(uri, *args, **kwargs):
                if uri == database.as_uri() + "?mode=ro":
                    error = sqlite3.OperationalError("cannot create SQLite shared memory")
                    error.sqlite_errorcode = sqlite3.SQLITE_READONLY
                    blocked = mock.Mock(spec=sqlite3.Connection)
                    blocked.execute.side_effect = error
                    return blocked
                return connect(uri, *args, **kwargs)

            self.enterContext(mock.patch.object(sqlite3, "connect", connect_without_writable_sidecars))
        return database

    def test_reconciliation_refuses_readonly_snapshot_with_unread_wal(self):
        from stock_agent.scheduler_ownership import (
            CONTAINER_SCHEDULER_ID,
            OwnershipConflictError,
            freeze_intake,
            initialize_registry,
            record_rollback_reconciliation,
        )

        baseline = self.readonly_copy()
        registry = self.root / "ownership"
        initialize_registry(registry, first_scheduler=CONTAINER_SCHEDULER_ID, actor="test")
        freeze_intake(registry, actor="test", reason="readonly WAL test")
        paths = (self.source, self.wal, baseline, baseline.with_name(self.wal.name))
        before = {path: path.read_bytes() for path in paths}
        with self.assertRaisesRegex(OwnershipConflictError, "nonempty WAL"):
            record_rollback_reconciliation(registry, baseline, self.source)
        self.assertFalse((registry / "rollback_increments.json").exists())
        self.assertEqual({path: path.read_bytes() for path in paths}, before)
        self.assertEqual(sorted(p.name for p in baseline.parent.iterdir()),
                         sorted((self.source.name, self.wal.name)))

    def test_rollback_snapshot_reads_fully_checkpointed_readonly_snapshot(self):
        from stock_agent.scheduler_ownership import _rollback_snapshot

        live = _rollback_snapshot(self.source)
        self.assertEqual(list(live["tasks"]), ["task_a"])
        self.assertEqual(list(live["outbox"]), [1])
        self.assertEqual(live["in_flight"], {})
        self.assertEqual(self.connection.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone(), (0, 0, 0))
        baseline = self.readonly_copy()
        self.assertEqual(self.wal.stat().st_size, 0)
        self.assertEqual(_rollback_snapshot(baseline), live)
        self.assertEqual(sorted(p.name for p in baseline.parent.iterdir()),
                         sorted((self.source.name, self.wal.name)))


class ImagePackagingContractTests(unittest.TestCase):
    def _read(self, *parts):
        return (ROOT.joinpath(*parts)).read_text(encoding="utf-8")

    def test_dockerfile_ships_every_required_backup_input(self):
        text = self._read("deploy", "docker", "Dockerfile")
        runtime = text.split("runtime: node base", 1)[1]
        copies = [line for line in runtime.splitlines() if line.startswith("COPY ")]
        for relative in REQUIRED_STATIC_PATHS:
            with self.subTest(relative=relative):
                self.assertTrue(any(re.search(r"(?:^|\s)" + re.escape(relative) + r"(?:\s|$)", line)
                                    and not line.startswith("COPY --from") for line in copies),
                                "{0} is not copied into the image".format(relative))

    def test_compose_mounts_every_app_root_except_temp(self):
        compose = self._read("deploy", "compose.yaml")
        dockerfile = self._read("deploy", "docker", "Dockerfile")
        environment = dict(re.findall(r"^\s+(APP_[A-Z_]+): (\S+)$", compose, re.M))
        image_environment = dict(re.findall(r"(APP_[A-Z_]+)=(\S+)", dockerfile))
        self.assertEqual(environment, image_environment)
        volumes = re.findall(r"^\s+- [^:]+:(/\S+)$", compose, re.M)
        for name, path in environment.items():
            if name == "APP_TEMP_ROOT":
                continue
            with self.subTest(root=name):
                self.assertIn(path, volumes)
        # The application never writes below /app except the mounted config
        # tree and the bounded logs mount.
        self.assertNotIn(":/app", [":" + mount for mount in volumes])
        self.assertRegex(compose, r"(?m)^\s+- /app/logs:size=\d+m,mode=1777$")

    def test_build_wrapper_needs_no_bare_python(self):
        text = self._read("scripts", "build_image.sh")
        self.assertNotRegex(text, r"(?<![\w\"${])-n python - ")
        self.assertIn('PYTHON:-$(command -v python || command -v python3', text)


if __name__ == "__main__":
    unittest.main()
