from __future__ import annotations

import json
import os
import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path

from stock_agent.audit_log import append_hash_chained_jsonl, file_hash, object_hash
from stock_agent.backup_restore import (
    build_backup_restore_status,
    create_backup,
    render_backup_restore_markdown,
    run_restore_drill,
    verify_backup,
)
from stock_agent.config import project_path_override
from stock_agent.dashboard import _section_backup_restore


class BackupRestoreTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.config = {
            "storage": {"sqlite_path": "data/stock_agent.sqlite3"},
            "backup_restore": {
                "enabled": True,
                "mode": "local_rehearsal",
                "bundle_root": "data/backup_restore/bundles",
                "drill_root": "data/backup_restore/drills",
                "event_path": "data/backup_restore/events.jsonl",
                "report_dir": "reports/backup_restore",
                "sqlite_paths": ["data/stock_agent.sqlite3"],
                "maximum_backup_age_minutes": 1440,
                "rpo_target_minutes": 1440,
                "rto_target_minutes": 30,
            },
            "pi_agent": {"enabled": False},
            "official_evidence_vault": {
                "objects_dir": "data/evidence_vault/objects/sha256",
            },
        }
        self._write("pyproject.toml", "[project]\nname='test-stock-agent'\n")
        self._write("requirements.lock.txt", "example-package==1.0\n")
        self._write_json("config/watchlist.example.json", {"backup_restore": {"enabled": False}})
        self._write_json("config/watchlist.local.json", self.config)
        self._write("stock_agent/example.py", "VALUE = 1\n")
        self._write(
            "data/events/events_20260905.jsonl",
            json.dumps({"event_id": "plain-1", "event_type": "signal", "payload": {"title": "公告"}}, ensure_ascii=False) + "\n",
        )
        chain = self.root / "data/research_workpapers/events.jsonl"
        append_hash_chained_jsonl(chain, {"event_type": "workpaper", "occurred_at": "2026-09-05T10:00:00+08:00"})
        database = self.root / "data/stock_agent.sqlite3"
        database.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(str(database))
        connection.execute("CREATE TABLE signals (id INTEGER PRIMARY KEY, title TEXT NOT NULL)")
        connection.executemany("INSERT INTO signals(title) VALUES (?)", [("alpha",), ("beta",)])
        connection.commit()
        connection.close()

    def tearDown(self) -> None:
        self.temp.cleanup()

    def _write(self, relative: str, text: str) -> Path:
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        return path

    def _write_json(self, relative: str, value) -> Path:
        return self._write(relative, json.dumps(value, ensure_ascii=False, indent=2) + "\n")

    def _create(self):
        with project_path_override(lambda value: self.root / str(value)):
            return create_backup(self.config, config_path="config/watchlist.local.json")

    def _bundle(self, result: dict) -> Path:
        return self.root / result["bundle_path"]

    @staticmethod
    def _rewrite_manifest(bundle: Path, manifest: dict) -> None:
        identity = dict(manifest)
        identity.pop("manifest_id", None)
        manifest["manifest_id"] = object_hash(identity)
        manifest_path = bundle / "manifest.json"
        manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        (bundle / "manifest.sha256").write_text(
            "{0}  manifest.json\n".format(file_hash(manifest_path)), encoding="ascii"
        )

    def test_create_and_verify_bundle_with_sqlite_and_jsonl(self):
        result = self._create()
        self.assertEqual(result["status"], "PASS", result.get("errors"))
        bundle = self._bundle(result)
        manifest = json.loads((bundle / "manifest.json").read_text(encoding="utf-8"))
        paths = {item["path"] for item in manifest["files"]}
        self.assertIn("data/stock_agent.sqlite3", paths)
        self.assertIn("data/events/events_20260905.jsonl", paths)
        self.assertIn("data/research_workpapers/events.jsonl", paths)
        self.assertIn("config/watchlist.restore.json", paths)
        self.assertNotIn("config/watchlist.local.json", paths)
        with project_path_override(lambda value: self.root / str(value)):
            verified = verify_backup(self.config, result["bundle_path"])
        self.assertEqual(verified["status"], "PASS", verified["errors"])
        self.assertEqual(verified["verified_file_count"], verified["file_count"])
        self.assertFalse(manifest["encryption"]["encrypted"])

    def test_sqlite_online_backup_preserves_rows_and_integrity(self):
        result = self._create()
        restored_db = self._bundle(result) / "payload/data/stock_agent.sqlite3"
        connection = sqlite3.connect(str(restored_db))
        try:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM signals").fetchone()[0], 2)
            self.assertEqual(connection.execute("PRAGMA quick_check").fetchone()[0], "ok")
        finally:
            connection.close()

    def test_restore_drill_is_isolated_and_reconciles_projection(self):
        backup = self._create()
        live_hash = file_hash(self.root / "data/stock_agent.sqlite3")
        with project_path_override(lambda value: self.root / str(value)):
            drill = run_restore_drill(self.config, backup["bundle_path"])
        self.assertEqual(drill["status"], "PASS", drill.get("errors"))
        self.assertFalse(drill["source_overwrite_performed"])
        self.assertFalse(drill["live_project_modified"])
        self.assertEqual(file_hash(self.root / "data/stock_agent.sqlite3"), live_hash)
        projection = json.loads((self.root / drill["recovery_projection_path"]).read_text(encoding="utf-8"))
        self.assertEqual(projection["fact_projection_hash"], backup["fact_projection_hash"])
        self.assertEqual(drill["rto"]["status"], "PASS")
        self.assertFalse(drill["rto"]["full_service_recovery_proven"])

    def test_same_size_same_mtime_tamper_is_detected_by_sha256(self):
        backup = self._create()
        target = self._bundle(backup) / "payload/pyproject.toml"
        original_stat = target.stat()
        raw = bytearray(target.read_bytes())
        raw[0] = ord("#") if raw[0] != ord("#") else ord("[")
        target.write_bytes(bytes(raw))
        os.utime(target, ns=(original_stat.st_atime_ns, original_stat.st_mtime_ns))
        with project_path_override(lambda value: self.root / str(value)):
            result = verify_backup(self.config, backup["bundle_path"])
        self.assertEqual(result["status"], "FAIL")
        self.assertIn("SHA-256 mismatch", "\n".join(result["errors"]))

    def test_extra_payload_file_is_rejected(self):
        backup = self._create()
        self._bundle(backup).joinpath("payload/extra.txt").write_text("extra", encoding="utf-8")
        with project_path_override(lambda value: self.root / str(value)):
            result = verify_backup(self.config, backup["bundle_path"])
        self.assertEqual(result["status"], "FAIL")
        self.assertIn("unmanifested payload file", "\n".join(result["errors"]))

    def test_manifest_path_traversal_is_rejected_even_with_recomputed_manifest_hash(self):
        backup = self._create()
        bundle = self._bundle(backup)
        manifest = json.loads((bundle / "manifest.json").read_text(encoding="utf-8"))
        manifest["files"][0]["path"] = "../escape.txt"
        self._rewrite_manifest(bundle, manifest)
        with project_path_override(lambda value: self.root / str(value)):
            result = verify_backup(self.config, backup["bundle_path"])
        self.assertEqual(result["status"], "FAIL")
        self.assertIn("unsafe manifest path", "\n".join(result["errors"]))

    def test_invalid_jsonl_blocks_backup_finalization(self):
        self._write("data/events/broken.jsonl", "{not-json}\n")
        result = self._create()
        self.assertEqual(result["status"], "FAIL")
        self.assertNotIn("bundle_path", result)
        self.assertIn("JSONL integrity", "\n".join(result["errors"]))

    def test_broken_hash_chain_blocks_backup(self):
        chain = self.root / "data/research_workpapers/events.jsonl"
        text = chain.read_text(encoding="utf-8")
        chain.write_text(text.replace("workpaper", "workpapar", 1), encoding="utf-8")
        result = self._create()
        self.assertEqual(result["status"], "FAIL")
        self.assertIn("JSONL integrity", "\n".join(result["errors"]))

    def test_secret_in_fact_file_blocks_without_recording_value(self):
        secret = "sk-" + "Z9Y8X7W6V5U4T3S2R1Q0"
        self._write("data/events/sensitive.jsonl", json.dumps({"value": secret}) + "\n")
        result = self._create()
        serialized = json.dumps(result, ensure_ascii=False)
        self.assertEqual(result["status"], "FAIL")
        self.assertNotIn(secret, serialized)
        self.assertIn("secret scan blocked backup", serialized)

    def test_secret_in_sqlite_blocks_without_recording_value(self):
        secret = "sk-" + "Q1W2E3R4T5Y6U7I8O9P0"
        database = self.root / "data/stock_agent.sqlite3"
        connection = sqlite3.connect(str(database))
        connection.execute("INSERT INTO signals(title) VALUES (?)", (secret,))
        connection.commit()
        connection.close()
        result = self._create()
        self.assertEqual(result["status"], "FAIL")
        self.assertNotIn(secret, json.dumps(result))

    def test_placeholder_secret_is_allowed_and_counted(self):
        self._write("data/events/placeholder.jsonl", json.dumps({"value": "sk-example-placeholder-123456"}) + "\n")
        result = self._create()
        self.assertEqual(result["status"], "PASS", result.get("errors"))
        self.assertGreater(result["secret_scan"]["placeholder_finding_count"], 0)

    def test_raw_local_config_and_sensitive_fields_are_not_exported(self):
        config = dict(self.config)
        config["llm_agent"] = {
            "api_key": "must-not-leave-this-process",
            "api_key_env": "DEEPSEEK_API_KEY",
            "base_url": "https://user:pass@example.invalid/v1?token=private#fragment",
        }
        self._write_json("config/watchlist.local.json", config)
        with project_path_override(lambda value: self.root / str(value)):
            result = create_backup(config, config_path="config/watchlist.local.json")
        restored = json.loads(
            (self._bundle(result) / "payload/config/watchlist.restore.json").read_text(encoding="utf-8")
        )
        self.assertEqual(restored["llm_agent"]["api_key"], "${REDACTED}")
        self.assertEqual(restored["llm_agent"]["api_key_env"], "DEEPSEEK_API_KEY")
        self.assertEqual(restored["llm_agent"]["base_url"], "https://example.invalid/v1")
        payload_text = "\n".join(
            path.read_text(encoding="utf-8", errors="ignore")
            for path in (self._bundle(result) / "payload").rglob("*") if path.is_file() and path.suffix != ".sqlite3"
        )
        self.assertNotIn("must-not-leave-this-process", payload_text)

    def test_status_separates_local_pass_from_production_blocked(self):
        backup = self._create()
        with project_path_override(lambda value: self.root / str(value)):
            drill = run_restore_drill(self.config, backup["bundle_path"])
            status = build_backup_restore_status(self.config)
        self.assertEqual(drill["status"], "PASS")
        self.assertEqual(status["technical_status"], "PASS", status["technical_blockers"])
        self.assertEqual(status["production_readiness"], "BLOCKED")
        self.assertFalse(status["production_readiness_asserted_by_software"])
        blockers = "\n".join(status["production_blockers"])
        self.assertIn("encryption/KMS", blockers)
        self.assertIn("Git source baseline", blockers)

    def test_source_change_makes_backup_and_drill_stale(self):
        backup = self._create()
        with project_path_override(lambda value: self.root / str(value)):
            run_restore_drill(self.config, backup["bundle_path"])
        self._write("stock_agent/example.py", "VALUE = 2\n")
        with project_path_override(lambda value: self.root / str(value)):
            status = build_backup_restore_status(self.config)
        self.assertEqual(status["technical_status"], "BLOCKED")
        self.assertIn("stale for the current release candidate", "\n".join(status["technical_blockers"]))

    def test_disabled_control_fails_closed(self):
        self.config["backup_restore"]["enabled"] = False
        result = self._create()
        self.assertEqual(result["status"], "BLOCKED")
        self.assertIn("disabled", "\n".join(result["blockers"]))

    def test_bundle_root_outside_workspace_is_rejected(self):
        self.config["backup_restore"]["bundle_root"] = str(self.root.parent / "outside-backups")
        result = self._create()
        self.assertEqual(result["status"], "FAIL")
        self.assertIn("inside the project workspace", "\n".join(result["errors"]))

    def test_operation_contract_forbids_external_and_business_actions(self):
        backup = self._create()
        contract = backup["operation_contract"]
        for key in (
            "network_access", "credential_export", "live_source_overwrite", "business_state_write",
            "approval_or_activation", "publication", "scheduled_task_change", "llm_call", "trading_or_order_action",
        ):
            self.assertFalse(contract[key], key)

    def test_markdown_and_dashboard_show_two_separate_readiness_states(self):
        snapshot = {
            "generated_at": datetime.now().astimezone().isoformat(),
            "technical_status": "PASS",
            "production_readiness": "BLOCKED",
            "backup": {"status": "PASS", "manifest_id": "a" * 64},
            "restore_drill": {"status": "PASS", "drill_id": "drill-1"},
            "rpo": {"age_status": "PASS", "latest_backup_age_minutes": 1, "recurring_interval_evidence": "NOT_EVIDENCED"},
            "rto": {"local_bundle_status": "PASS", "latest_drill_elapsed_seconds": 3},
            "technical_blockers": [],
            "production_blockers": ["encrypted offsite evidence missing"],
            "next_actions": [],
        }
        markdown = render_backup_restore_markdown(snapshot)
        html = _section_backup_restore(snapshot)
        self.assertIn("Local technical status: `PASS`", markdown)
        self.assertIn("Production DR readiness: `BLOCKED`", markdown)
        self.assertIn("本机技术演练", html)
        self.assertIn("生产灾备", html)

    def test_old_backup_exceeds_age_gate(self):
        backup = self._create()
        old = datetime.now().astimezone() + timedelta(days=2)
        with project_path_override(lambda value: self.root / str(value)):
            status = build_backup_restore_status(self.config, now=old)
        self.assertEqual(status["technical_status"], "BLOCKED")
        self.assertIn("maximum allowed age", "\n".join(status["technical_blockers"]))


if __name__ == "__main__":
    unittest.main()
