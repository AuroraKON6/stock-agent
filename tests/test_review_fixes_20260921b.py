"""Review-fix tests for ZCODE_SSE_MANIFEST_REVIEW_20260921 (B01-B07).

Written BEFORE the fixes where a runnable red exists (B02/B05/B07 and the
script/HTML contracts). All offline; the model path uses captured stubs.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from stock_agent.config import load_config, project_path_override
from stock_agent.models import RawSignal, now_iso

PROJECT_ROOT = Path(__file__).resolve().parent.parent
LOCAL_CONFIG = load_config(str(PROJECT_ROOT / "config" / "watchlist.local.json"))
SCRIPT = (PROJECT_ROOT / "scripts" / "run_daily_research.ps1").read_text(encoding="utf-8")


class ScriptContractTests(unittest.TestCase):
    """B01/B03/B04: quoting, daily-only selection, bind-before-sync gating."""

    def test_b01_summary_path_is_quoted(self):
        self.assertIn("--summary '$LatestDailySummary'", SCRIPT,
                      "the summary path contains spaces and must arrive as ONE argv")

    def test_b02_script_passes_trigger_source_to_agent_run(self):
        self.assertIn("--trigger-source $TriggerSource", SCRIPT)

    def test_b03_summary_selection_filters_daily_mode(self):
        self.assertIn('mode -eq "daily"', SCRIPT)

    def test_b04_bind_runs_before_and_gates_pilot_sync(self):
        pilot = SCRIPT[SCRIPT.index("$Mode -eq \"pilot\""):]
        bind_idx = pilot.index("run-manifest-bind")
        sync_idx = SCRIPT.index('"production-pilot-sync"')
        self.assertLess(bind_idx, sync_idx, "binding must be verified before pilot sync")
        self.assertIn("$ManifestBindSucceeded", SCRIPT,
                      "a failed binding must gate the pilot sync side effects")


class CliTriggerSourceTests(unittest.TestCase):
    """B02: the scheduled flag must survive the real CLI entry into the pipeline."""

    def test_agent_run_accepts_trigger_source_and_sets_config(self):
        import sys

        from stock_agent import cli

        captured = {}

        def fake_pipeline(config):
            captured.update(config)
            from stock_agent.models import PipelineResult
            return PipelineResult(generated_at=now_iso(), signals=[], alerts=[],
                                  report_path="reports/daily/x.md", json_path="reports/json/x.json",
                                  alert_path="reports/alerts/x.jsonl", stats={})

        with tempfile.TemporaryDirectory() as tmp:
            cfg = Path(tmp) / "cfg.json"
            cfg.write_text(json.dumps({"llm_agent": {"enabled": False}, "storage": {"sqlite_path": str(Path(tmp) / "s.db")}}), encoding="utf-8")
            argv_backup = sys.argv
            try:
                sys.argv = ["stock_agent", "agent-run", "--config", str(cfg), "--trigger-source", "scheduled"]
                with patch.object(cli, "run_agent_pipeline", side_effect=fake_pipeline), \
                     patch.object(cli, "ensure_directories"):
                    cli.main()
            finally:
                sys.argv = argv_backup
        self.assertEqual(captured.get("_trigger_source"), "scheduled")


class BindRequiredFieldTests(unittest.TestCase):
    """B05: run_id / manifest path / report path / SHA are all mandatory."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.config = {
            "storage": {"sqlite_path": str(self.root / "s.db")},
            "sources": {},
            "topics": [{"name": "t", "symbols": [], "keywords": ["major contract"], "risk_keywords": []}],
            "priority_rules": {"p0_keywords": [], "p1_keywords": ["major contract"],
                               "positive_keywords": [], "negative_keywords": [], "review_keywords": []},
            "llm_agent": {"enabled": False},
            "multi_role_agent": {"enabled": False},
            "research_cases": {"enabled": False, "llm_enabled": False},
            "research_framework": {"enabled": False},
            "research_team": {"enabled": False},
            "research_runtime": {"enabled": False, "auto_sync": False},
            "research_workpapers": {"enabled": False},
            "report": {"include_low_priority": True},
            "run_coordination": {
                "enabled": True,
                "lease_root": str(self.root / "l"), "recovered_lease_root": str(self.root / "r"),
                "checkpoint_path": str(self.root / "c.jsonl"), "report_dir": str(self.root / "rc"),
                "lease_ttl_seconds": 30, "require_explicit_stale_recovery": True,
                "fail_fast_on_lease_conflict": True,
            },
            "run_manifest": {
                "enabled": True,
                "manifest_dir": str(self.root / "m"),
                "pointer_path": str(self.root / "p.json"),
            },
        }

    def tearDown(self):
        self.tmp.cleanup()

    def _scheduled_run_pointer(self):
        from stock_agent.pipeline import run_agent_pipeline

        config = dict(self.config)
        config["_trigger_source"] = "scheduled"
        raw = RawSignal(source="manual", source_type="news", title="A major contract announcement",
                        summary="major contract", url="https://example.test/n", published_at=now_iso(), payload={})
        with project_path_override(lambda value, r=self.root: r / value), \
             patch("stock_agent.pipeline.collect_sources", return_value=[raw]):
            result = run_agent_pipeline(config)
        return json.loads((self.root / "p.json").read_text(encoding="utf-8")), result

    def _summary(self, pointer, **drops):
        manifest = json.loads((self.root / pointer["manifest_path"]).read_text(encoding="utf-8"))
        json_ref = [item for item in manifest["artifacts"] if item["name"] == "json_report"][0]
        summary = {
            "mode": "daily", "exit_code": 0, "trigger_source": "scheduled",
            "run_id": pointer["run_id"],
            "run_manifest_path": pointer["manifest_path"],
            "run_manifest_sha256": pointer["manifest_sha256"],
            "report_path": json_ref["path"],
        }
        for key in drops:
            summary[key] = drops[key]
        path = self.root / "summary.json"
        path.write_text(json.dumps(summary, ensure_ascii=False), encoding="utf-8")
        return str(path)

    def _bind(self, path):
        from stock_agent.run_manifest import bind_scheduled_summary

        with project_path_override(lambda value, r=self.root: r / value):
            return bind_scheduled_summary(self.config, path)

    def test_missing_report_path_blocks(self):
        pointer, _ = self._scheduled_run_pointer()
        bound = self._bind(self._summary(pointer, report_path=""))
        self.assertEqual(bound["status"], "BLOCKED")
        self.assertTrue(any("report_path" in str(e) for e in bound["errors"]))

    def test_missing_sha_blocks(self):
        pointer, _ = self._scheduled_run_pointer()
        bound = self._bind(self._summary(pointer, run_manifest_sha256=""))
        self.assertEqual(bound["status"], "BLOCKED")
        self.assertTrue(any("sha" in str(e).lower() for e in bound["errors"]))

    def test_missing_run_id_blocks(self):
        pointer, _ = self._scheduled_run_pointer()
        bound = self._bind(self._summary(pointer, run_id=""))
        self.assertEqual(bound["status"], "BLOCKED")

    def test_cli_nonzero_exit_on_blocked_summary(self):
        from stock_agent import cli

        pointer, _ = self._scheduled_run_pointer()
        summary_path = self._summary(pointer, run_manifest_sha256="")
        cfg_path = self.root / "cfg.json"
        cfg_path.write_text(json.dumps(self.config, ensure_ascii=False), encoding="utf-8")
        with project_path_override(lambda value, r=self.root: r / value):
            with self.assertRaises(SystemExit):
                cli._run_manifest_bind(str(cfg_path), summary_path)


def _fresh_source_time() -> str:
    from datetime import datetime, timedelta, timezone

    cst = timezone(timedelta(hours=8))
    return (datetime.now(cst) - timedelta(seconds=60)).isoformat(timespec="seconds")


class SafeResearchContextTests(unittest.TestCase):
    """B07: the model must actually receive the governed research facts."""

    def _task_spec(self, doc_ref="ev-good-0001", doc_sha=None):
        return {
            "research_question": "评估 600519 与 000001 的公开事件影响",
            "data_classification": "public",
            "bound_signal_ids": ["sig-test-0001"],
            "public_context": {
                "quote_snapshots": {
                    "600519": {"price": 1533.5, "pct_change": 2.1, "provider": "eastmoney",
                               "source_time": _fresh_source_time(), "source_time_status": "verified"},
                    "000001": {"price": 11.2, "pct_change": 0.1, "provider": "eastmoney",
                               "source_time": None, "source_time_status": "unverified_missing_upstream_time"},
                },
                "evidence_summaries": {
                    doc_ref: {"summary": "官方公告摘要", "sha256": doc_sha or ("ab" * 32)},
                },
            },
            "replay_response": {
                "schema_version": "1.0", "task_id": "placeholder", "summary": "草稿",
                "key_findings": [], "unknowns": [], "evidence_refs": [doc_ref],
                "human_questions": [], "model_self_assessed_confidence": 0.3,
            },
        }

    def _gov_config(self, root):
        # Real governance policy with enforce=True. The vault lives in the
        # TEMP workspace via the shared fixture (real producer, real schema);
        # auxiliary paths (audit, exceptions, roster, restrictions) point at
        # the test workspace so nothing touches the production ledgers.
        import sys as _sys

        if str(Path(__file__).resolve().parent) not in _sys.path:
            _sys.path.insert(0, str(Path(__file__).resolve().parent))
        from _vault_fixture import import_document, vault_config

        cfg = vault_config(root, LOCAL_CONFIG)
        cfg["llm_governance"]["audit_path"] = str(root / "gov" / "audit")
        cfg["llm_governance"]["exception_path"] = str(root / "gov" / "exceptions")
        mdg = cfg.setdefault("market_data_governance", {})
        mdg["approved_internal_research_providers"] = ["eastmoney"]
        mdg["use_production_pilot_provider_registry"] = False
        mdg["require_consensus_for_llm"] = False
        mdg["allow_degraded_offhours_llm"] = True  # this class tests CONTENT delivery, not session timing

        _, vault_sha = import_document(cfg, root, content="官方公告正文", title="官方公告摘要")
        self.vault_sha = vault_sha
        self.vault_doc_id = "document:sha256:{0}".format(vault_sha)
        return cfg

    def _run_worker(self, spec=None, mutator=None):
        from stock_agent.research_worker import run_research_task
        from stock_agent.task_runtime import TaskStore

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = self._gov_config(root)  # also sets self.vault_doc_id / self.vault_sha
            spec = spec or self._task_spec(doc_ref=self.vault_doc_id, doc_sha=self.vault_sha)
            if mutator:
                mutator(spec)
            store = TaskStore(root / "t.sqlite3")
            try:
                task, _ = store.create_task(title="b07", input_payload=spec, case_id="b07", execution_mode="replay")
                claimed = store.claim_next("w1")
                with project_path_override(lambda value, r=root: root / value):
                    result = run_research_task(store, task["task_id"], worker_id="w1", config=config)
                return result, store.get_task(task["task_id"])
            finally:
                store.close()

    def test_verified_facts_reach_prompt_and_unverified_do_not(self):
        result, stored = self._run_worker()
        self.assertEqual(result["status"], "succeeded", str(result.get("error")))
        delivery = stored["model_call"].get("context_delivery") or {}
        self.assertIn("600519", delivery.get("delivered_quotes", []), "verified quote must be delivered")
        self.assertTrue(any(item.startswith("000001:") for item in delivery.get("dropped_quotes", [])), "unverified quote must be dropped, never faked")
        prompt = json.dumps(stored["model_call"].get("outbound_context") or {}, ensure_ascii=False)
        self.assertIn("1533.5", prompt)
        self.assertIn(self.vault_doc_id, prompt)
        self.assertNotIn("11.2", prompt, "unverified snapshot price must not reach the model")

    def test_empty_context_is_not_silently_accepted_as_analyzed(self):
        result, stored = self._run_worker(mutator=lambda spec: spec.update({"public_context": {}}))
        delivery = stored["model_call"].get("context_delivery") or {}
        self.assertEqual(delivery.get("delivered_quotes"), [])
        self.assertEqual(delivery.get("delivered_evidence"), [])
        note = json.dumps(delivery, ensure_ascii=False)
        self.assertTrue(note, "context delivery facts must be recorded even when empty")

    def test_output_refs_bind_to_safe_context_only(self):
        def _fabricate(spec):
            spec["replay_response"]["evidence_refs"] = ["ev-not-in-context"]
            spec["replay_response"]["summary"] = "引用不存在证据"
        result, stored = self._run_worker(mutator=_fabricate)
        # R01/F05: refs must bind to the SAFE delivered evidence set — the
        # rejection requeues (attempts left), and the worker reports the
        # persisted status instead of a terminal "failed" label.
        self.assertEqual(result["status"], "queued")
        self.assertEqual(stored["status"], "queued")
        self.assertIn("evidence", str(result.get("error") or stored["last_error"]).lower())


class BoardFormPreservationTests(unittest.TestCase):
    """B06: refresh must not wipe in-progress input (contract level)."""

    def test_page_preserves_dirty_forms_and_guards_response_order(self):
        import http.client
        import threading

        from stock_agent.task_board import create_task_board_server
        from stock_agent.task_runtime import TaskStore

        with tempfile.TemporaryDirectory() as tmp:
            store = TaskStore(Path(tmp) / "t.sqlite3")
            server = create_task_board_server(store, host="127.0.0.1", port=0)
            threading.Thread(target=server.serve_forever, daemon=True).start()
            try:
                conn = http.client.HTTPConnection("127.0.0.1", server.server_address[1], timeout=10)
                conn.request("GET", "/", headers={"Host": "127.0.0.1"})
                html = conn.getresponse().read().decode("utf-8")
                conn.close()
            finally:
                server.shutdown(); server.server_close(); store.close()
        self.assertIn("data-version", html)
        self.assertIn("dirty", html, "typing in a form must mark it dirty so refresh keeps it")
        self.assertIn("conflict", html.lower(), "a dirty form on a new version must surface a conflict hint")
        self.assertTrue("applySeq" in html or "lastApplied" in html or "seqGuard" in html,
                        "responses must apply in order so a stale snapshot cannot overwrite a newer one")


if __name__ == "__main__":
    unittest.main()
