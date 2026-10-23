"""R24: backup consistency — the R01—R13 operational state joins the bundle.

The default SQLite source set now includes the task runtime (tasks/outbox/
snapshots/platform runs/attempt chain/verifications/dispatches) and the
connector query intake; the personal evidence namespace objects and indexes
travel with their own kinds; and the restore DRILL verifies the restored task
runtime table-by-table against the live source, explicitly confirming that
unknown-outcome attempts (open reconciliation work) survived.
"""
import sys
import tempfile
import unittest
from pathlib import Path

from stock_agent.backup_restore import (
    backup_restore_config,
    create_backup,
    run_restore_drill,
    verify_backup,
)
from stock_agent.config import project_path, project_path_override
from stock_agent.personal_evidence import import_personal_document
from stock_agent.task_runtime import TaskStore
from stock_agent.task_runtime.store import ATTEMPT_OUTCOME_UNKNOWN

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _vault_fixture import vault_config


def _spec(question):
    from stock_agent.task_supply import build_task_spec

    spec = build_task_spec(research_question=question, symbols=["600519"],
                           execution_mode="replay", config=None)
    spec["bound_signal_ids"] = []
    spec["public_context"]["evidence_summaries"] = {}
    return spec


class R24BackupTestCase(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.enterContext(project_path_override(lambda value: self.root / value))
        # Required static recovery inputs + a valid signal store (mirrors the
        # existing backup test fixture's approach).
        for relative, content in (
            ("pyproject.toml", "[project]\nname='r24-test'\n"),
            ("requirements.lock.txt", "example-package==1.0\n"),
        ):
            target = self.root / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8")
        (self.root / "config").mkdir(parents=True, exist_ok=True)
        (self.root / "config/watchlist.example.json").write_text(
            "{\"backup_restore\": {\"enabled\": false}}", encoding="utf-8")
        import sqlite3

        signal_db = self.root / "data/stock_agent.sqlite3"
        signal_db.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(signal_db))
        conn.execute("CREATE TABLE signals (id INTEGER PRIMARY KEY, title TEXT)")
        conn.commit()
        conn.close()
        query_db = self.root / "data/kimi/query_intake.sqlite3"
        query_db.parent.mkdir(parents=True, exist_ok=True)
        query_db.write_bytes(b"")
        self.config = vault_config(self.root, {
            "backup_restore": {"enabled": True},
            "storage": {"sqlite_path": "data/stock_agent.sqlite3"},
            "task_runtime": {"db_path": "data/task_runtime/tasks.sqlite3"},
            "kimi_sources": {"query_store_path": "data/kimi/query_intake.sqlite3"},
            "pi_agent": {"enabled": False},
        })
        self.store = TaskStore(self.root / "data/task_runtime/tasks.sqlite3")
        self.addCleanup(self.store.close)

    def _seed_research_state(self):
        task, _ = self.store.create_task(title="R24", input_payload=_spec("备份一致性?"),
                                         execution_mode="replay",
                                         business_intent_id="bi-r24")
        claimed = self.store.claim_next("w")
        attempt = self.store.prepare_invocation_attempt(
            task["task_id"], worker_id="w", fencing_token=claimed["fencing_token"],
            round_index=1, provider="p", request_model="m", prompt_sha256="a" * 64)
        self.store.mark_invocation_dispatched(attempt["attempt_id"], worker_id="w",
                                              fencing_token=claimed["fencing_token"])
        self.store.complete_invocation_attempt(
            attempt["attempt_id"], worker_id="w", fencing_token=claimed["fencing_token"],
            state=ATTEMPT_OUTCOME_UNKNOWN, sent="unknown", error="timeout")
        self.store.mark_blocked(task["task_id"], worker_id="w",
                                fencing_token=claimed["fencing_token"],
                                error="model invocation outcome unknown; manual reconciliation required")
        return task, attempt

    def _backup(self):
        result = create_backup(self.config)
        self.assertEqual(result["status"], "PASS", result.get("errors"))
        return result


class DefaultSourceSetTests(R24BackupTestCase):
    def test_defaults_include_task_runtime_and_query_intake(self):
        cfg = backup_restore_config(self.config)
        self.assertTrue(any("task_runtime" in p for p in cfg["sqlite_paths"]))
        self.assertTrue(any("kimi" in p for p in cfg["sqlite_paths"]))
        explicit = backup_restore_config(dict(self.config, backup_restore={
            "enabled": True, "sqlite_paths": ["data/only-this.sqlite3"]}))
        self.assertEqual(explicit["sqlite_paths"], ["data/only-this.sqlite3"],
                         "an operator-provided list stays authoritative")

    def test_personal_evidence_objects_travel_with_own_kind(self):
        source = self.root / "incoming"
        source.mkdir(parents=True, exist_ok=True)
        path = source / "notice.html"
        path.write_text("<html><body><p>个人公告正文 123.45 万元</p></body></html>",
                        encoding="utf-8")
        import_personal_document(
            self.config, "lead-analyst", str(path),
            source_url="https://www.cninfo.com.cn/x?r24", title="R24 个人公告",
            published_at="2026-10-23T09:00:00+08:00")
        result = self._backup()
        kinds = {entry["kind"] for entry in _manifest_entries(self.config, result)}
        self.assertIn("personal_evidence_object", kinds)
        self.assertIn("personal_evidence_index", kinds)


class DrillIntegrityTests(R24BackupTestCase):
    def test_drill_verifies_task_runtime_tables_and_unknown_survives(self):
        task, attempt = self._seed_research_state()
        self._backup()
        verification = verify_backup(self.config)
        self.assertEqual(verification["status"], "PASS", verification.get("errors"))
        drill = run_restore_drill(self.config)
        self.assertEqual(drill["status"], "PASS", drill.get("errors"))
        integrity = drill["task_runtime_integrity"]
        self.assertEqual(integrity["status"], "PASS", integrity["errors"])
        self.assertEqual(integrity["tables"]["tasks"], {"source": 1, "restored": 1})
        self.assertEqual(integrity["tables"]["invocation_attempts"],
                         {"source": 1, "restored": 1})
        self.assertEqual(integrity["tables"]["platform_runs"], {"source": 0, "restored": 0})
        self.assertEqual(integrity["unknown_attempts_restored"], 1,
                         "open reconciliation work must survive the restore")
        self.assertEqual(drill["source_overwrite_performed"], False)
        self.assertEqual(drill["live_project_modified"], False)


def _manifest_entries(config, result):
    import json

    bundle = project_path(str(result.get("bundle_path") or ""))
    manifest = json.loads((bundle / "manifest.json").read_text(encoding="utf-8-sig"))
    return manifest.get("files") or []


if __name__ == "__main__":
    unittest.main()
