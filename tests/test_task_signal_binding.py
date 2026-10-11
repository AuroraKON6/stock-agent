"""Real producer -> supply -> worker evidence linkage, without external services."""
import contextlib
import io
import hashlib
import json
from pathlib import Path
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from stock_agent.config import load_config, project_path_override
from stock_agent.models import AnalyzedSignal
from stock_agent.research_worker import build_safe_research_context, run_research_task
from stock_agent.storage import SignalStore
from stock_agent.task_runtime import TaskStore
from stock_agent.task_supply import build_task_spec

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _vault_fixture import import_document, vault_config

NOW = datetime.now(timezone.utc)


class TaskSignalBindingTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.enterContext(project_path_override(lambda value: self.root / value))
        base = load_config(str(Path(__file__).resolve().parents[1] / "config/watchlist.example.json"))
        self.config = vault_config(self.root, base)
        self.config["storage"] = {"sqlite_path": str(self.root / "custom signals.sqlite3")}
        self.config["llm_governance"].update({"enabled": True, "enforce": True})
        self.config["llm_governance"]["audit_path"] = str(self.root / "audit")
        self.config["llm_governance"]["exception_path"] = str(self.root / "exceptions")
        self.config["task_runtime"] = {"db_path": str(self.root / "tasks.sqlite3")}

    def signal(self, sid="sig-test-0001", symbols=("600519",), *, hours=1,
               classification="public", provenance="", timestamp=None, kind="filing"):
        store = SignalStore(Path(self.config["storage"]["sqlite_path"]))
        try:
            store.upsert_many([AnalyzedSignal(
                signal_id=sid, source="manual", source_type=kind, title="Synthetic public filing",
                summary="Fixture only", url="https://www.cninfo.com.cn/disclosure/fixture",
                published_at=timestamp if timestamp is not None else (NOW - timedelta(hours=hours)).isoformat(),
                collected_at=NOW.isoformat(), symbols=list(symbols),
                payload={"data_classification": classification, "data_provenance": provenance},
            )])
        finally:
            store.close()

    def document(self, sid="sig-test-0001", content="Fixture body"):
        return import_document(self.config, self.root, content=content, title="Fixture disclosure",
                               signal_ids=[sid], seed_signals=False)

    def spec(self):
        return build_task_spec(research_question="Assess this public filing", symbols=["600519"],
                               config=self.config)

    def test_configured_store_to_vault_to_worker(self):
        self.signal()
        ref, sha = self.document()
        spec = self.spec()
        self.assertEqual(spec["bound_signal_ids"], ["sig-test-0001"])
        self.assertEqual(set(spec["public_context"]["evidence_summaries"]), {ref})
        context, delivery = build_safe_research_context(spec, self.config)
        self.assertEqual(delivery["delivered_evidence"], [ref])
        self.assertEqual(context["evidence_summaries"][ref]["sha256"], sha)
        spec["replay_response"]["evidence_refs"] = [ref]
        store = TaskStore(self.root / "tasks.sqlite3")
        try:
            task, _ = store.create_task(title="Supply replay", input_payload=spec, execution_mode="replay")
            store.claim_next("binding-test")
            result = run_research_task(store, task["task_id"], worker_id="binding-test", config=self.config)
            self.assertEqual(result["status"], "awaiting_human")
            self.assertFalse(result["model_call"]["called"])
            self.assertEqual(result["model_call"]["context_delivery"]["delivered_evidence"], [ref])
        finally:
            store.close()

    def test_missing_store_does_not_create_database_or_bind_from_vault(self):
        self.document()
        spec = self.spec()
        self.assertEqual(spec["bound_signal_ids"], [])
        self.assertEqual(spec["public_context"]["evidence_summaries"], {})
        self.assertFalse(Path(self.config["storage"]["sqlite_path"]).exists())
        self.assertEqual(spec["public_context"]["context_notes"]["signal_selection"]["status"], "missing")

    def test_corrupt_store_fails_closed(self):
        Path(self.config["storage"]["sqlite_path"]).write_text("not sqlite", encoding="utf-8")
        spec = self.spec()
        self.assertEqual(spec["bound_signal_ids"], [])
        self.assertEqual(spec["public_context"]["context_notes"]["signal_selection"]["status"], "unavailable")

    def test_unrelated_evidence_is_filtered_before_document_limit(self):
        for i in range(7):
            self.signal("other-" + str(i), symbols=("000001",))
            self.document("other-" + str(i), content="Unrelated fixture " + str(i))
        self.signal()
        ref, _ = self.document()
        spec = self.spec()
        self.assertEqual(set(spec["public_context"]["evidence_summaries"]), {ref})
        self.assertEqual(spec["bound_signal_ids"], ["sig-test-0001"])

    def test_private_stale_future_example_and_unscoped_signals_excluded(self):
        self.signal("private", classification="internal")
        self.signal("stale", hours=72)
        self.signal("future", hours=-2)
        self.signal("invalid", timestamp="not-a-date")
        self.signal("example", provenance="example")
        self.signal("unscoped", symbols=())
        self.assertEqual(self.spec()["bound_signal_ids"], [])

    def test_worker_rejects_deleted_signal_even_with_valid_document(self):
        self.signal()
        self.document()
        spec = self.spec()
        store = SignalStore(Path(self.config["storage"]["sqlite_path"]))
        store.conn.execute("DELETE FROM signals")  # disposable test DB only
        store.conn.commit()
        store.close()
        context, _ = build_safe_research_context(spec, self.config)
        self.assertEqual(context["evidence_summaries"], {})

    def test_worker_does_not_trust_forged_binding_or_scope(self):
        self.signal()
        self.document()
        spec = self.spec()
        spec["symbols"] = ["000001"]
        context, _ = build_safe_research_context(spec, self.config)
        self.assertEqual(context["evidence_summaries"], {})

    def test_missing_null_or_malformed_binding_never_means_all_signals(self):
        self.signal()
        self.document()
        spec = self.spec()
        for value in (None, [], "sig-test-0001", {"sig-test-0001": True}):
            with self.subTest(value=value):
                spec["bound_signal_ids"] = value
                context, _ = build_safe_research_context(spec, self.config)
                self.assertEqual(context["evidence_summaries"], {})

    def test_supply_does_not_mutate_stored_signals(self):
        self.signal()
        database = Path(self.config["storage"]["sqlite_path"])
        before = hashlib.sha256(database.read_bytes()).hexdigest()
        self.spec()
        self.assertEqual(before, hashlib.sha256(database.read_bytes()).hexdigest())

    def test_bad_lookback_fails_closed_without_overflow(self):
        self.signal()
        self.document()
        spec = self.spec()
        for value in (10**50, "forty-eight", True, -1):
            with self.subTest(value=value):
                spec["signal_lookback_hours"] = value
                context, delivery = build_safe_research_context(spec, self.config)
                self.assertEqual(context["evidence_summaries"], {})
                self.assertEqual(delivery["signal_binding"]["status"], "invalid_lookback")

    def test_unlinked_vault_document_does_not_authorize_itself(self):
        self.signal()
        ref, sha = import_document(self.config, self.root, content="Unlinked fixture", title="Unlinked",
                                  signal_ids=[], seed_signals=False)
        spec = self.spec()
        self.assertEqual(spec["public_context"]["evidence_summaries"], {})
        spec["public_context"]["evidence_summaries"][ref] = {"sha256": sha, "summary": "Unlinked"}
        context, _ = build_safe_research_context(spec, self.config)
        self.assertEqual(context["evidence_summaries"], {})

    def test_worker_rechecks_stored_classification(self):
        self.signal()
        self.document()
        spec = self.spec()
        store = SignalStore(Path(self.config["storage"]["sqlite_path"]))
        store.conn.execute("UPDATE signals SET payload_json = ?", ('{"data_classification":"internal"}',))
        store.conn.commit()
        store.close()
        context, _ = build_safe_research_context(spec, self.config)
        self.assertEqual(context["evidence_summaries"], {})

    def test_cli_from_symbols_preserves_binding(self):
        from stock_agent.cli import main
        self.signal()
        ref, _ = self.document()
        config_file = self.root / "config.json"
        config_file.write_text(json.dumps(self.config), encoding="utf-8")
        with patch.object(sys, "argv", ["stock_agent", "task-create", "--config", str(config_file),
                                      "--title", "CLI fixture", "--from-symbols", "600519",
                                      "--execution-mode", "replay"]), contextlib.redirect_stdout(io.StringIO()):
            main()
        store = TaskStore(self.root / "tasks.sqlite3")
        try:
            spec = store.list_tasks()[0]["input_payload"]
            self.assertEqual(spec["bound_signal_ids"], ["sig-test-0001"])
            context, _ = build_safe_research_context(spec, self.config)
            self.assertIn(ref, context["evidence_summaries"])
        finally:
            store.close()

    def test_restricted_signal_cannot_hide_by_omitting_task_symbols(self):
        from stock_agent.research_worker import govern_egress

        self.signal()
        self.document()
        spec = self.spec()
        restrictions = Path(self.config["research_operations"]["restrictions_path"])
        restrictions.write_text(json.dumps({"entries": [
            {"entity": "600519", "list_type": "restricted", "status": "active"}
        ]}), encoding="utf-8")
        for omit in (False, True):
            with self.subTest(omit=omit):
                if omit:
                    spec.pop("symbols", None)
                allowed, reason, audit, _ = govern_egress(
                    {"task_id": "restriction-fixture", "input_payload": spec}, self.config)
                self.assertFalse(allowed)
                self.assertIn("restricted list", reason)
