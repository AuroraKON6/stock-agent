"""Review-fix tests round 4 (ZCODE_VAULT_DOM_REVIEW D01-D06).

Real-producer vault fixtures (import_official_document, never hand-written
indexes), the real market-data-governance gate with a controlled clock for
the freshness positive case, fail-closed explicit summaries, and DOM-contract
assertions backed by a separate real-browser script. Offline; stubs only.
"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from stock_agent.config import load_config, project_path_override

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _vault_fixture import import_document, vault_config  # noqa: E402

PROJECT_ROOT = Path(__file__).resolve().parent.parent
LOCAL_CONFIG = load_config(str(PROJECT_ROOT / "config" / "watchlist.local.json"))

CST = timezone(timedelta(hours=8))


def _governance_config(root: Path, *, providers=None, consensus=True):
    cfg = vault_config(root, LOCAL_CONFIG)
    cfg["llm_governance"]["audit_path"] = str(root / "gov" / "audit")
    cfg["llm_governance"]["exception_path"] = str(root / "gov" / "exceptions")
    mdg = cfg.setdefault("market_data_governance", {})
    mdg["approved_internal_research_providers"] = list(providers or ["eastmoney"])
    mdg["use_production_pilot_provider_registry"] = False
    if not consensus:
        mdg["require_consensus_for_llm"] = False
    return cfg


class RealVaultContractTests(unittest.TestCase):
    """D01/D02: the real producer schema flows through supply and context."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.config = _governance_config(self.root)
        self.doc_id, self.sha = import_document(
            self.config, self.root, content="官方公告正文：测试内容", title="测试公司重大合同公告"
        )

    def tearDown(self):
        self.tmp.cleanup()

    def _payload(self, include_evidence=True, bound_signals=("sig-test-0001",)):
        evidence = {self.doc_id: {"summary": "官方公告摘要", "sha256": self.sha}} if include_evidence else {}
        return {
            "research_question": "q", "data_classification": "public",
            "bound_signal_ids": list(bound_signals),
            "public_context": {"quote_snapshots": {}, "evidence_summaries": evidence},
        }

    def test_real_producer_documents_reach_safe_context(self):
        from stock_agent.research_worker import build_safe_research_context

        with project_path_override(lambda value, r=self.root: r / value):
            context, delivery = build_safe_research_context(self._payload(), self.config)
        self.assertIn(self.doc_id, context["evidence_summaries"],
                      "documents from the REAL vault projection (dict keyed by document_id) must be delivered")
        self.assertIn(self.doc_id, delivery["delivered_evidence"])
        self.assertTrue(context["evidence_summaries"][self.doc_id]["vault_verified"])

    def test_supply_reads_real_vault(self):
        from stock_agent.task_supply import build_public_context

        with project_path_override(lambda value, r=self.root: r / value):
            supplied = build_public_context(["600519"], config=self.config)
        entry = supplied["evidence_summaries"].get(self.doc_id)
        self.assertIsNotNone(entry, "supply must expose real vault documents")
        self.assertEqual(entry["sha256"], self.sha)

    def test_missing_object_blocks_all_evidence_fail_closed(self):
        # D02: an index entry whose object bytes are gone must NOT verify.
        objects = self.root / "vault" / "objects"
        if objects.exists():
            for candidate in sorted(objects.rglob("*")):
                if candidate.is_file():
                    candidate.unlink()
                    break
        from stock_agent.research_worker import build_safe_research_context

        with project_path_override(lambda value, r=self.root: r / value):
            context, delivery = build_safe_research_context(self._payload(), self.config)
        self.assertEqual(context["evidence_summaries"], {},
                         "a missing/tampered object anywhere blocks ALL evidence delivery")
        self.assertTrue(any(item.startswith(self.doc_id + ":") for item in delivery["dropped_evidence"]))

    def test_hash_without_vault_entry_is_rejected(self):
        from stock_agent.research_worker import build_safe_research_context

        payload = self._payload()
        payload["public_context"]["evidence_summaries"] = {
            "ev-fake": {"summary": "伪造摘要", "sha256": "cd" * 32},
        }
        with project_path_override(lambda value, r=self.root: r / value):
            context, delivery = build_safe_research_context(payload, self.config)
        self.assertEqual(context["evidence_summaries"], {})

    def test_correct_hash_with_wrong_document_ref_is_rejected(self):
        from stock_agent.research_worker import build_safe_research_context

        payload = self._payload()
        payload["public_context"]["evidence_summaries"] = {
            "unrelated-case-ref": {"summary": "官方公告摘要", "sha256": self.sha},
        }
        with project_path_override(lambda value, r=self.root: r / value):
            context, delivery = build_safe_research_context(payload, self.config)
        self.assertEqual(context["evidence_summaries"], {})
        self.assertTrue(any("ref_document_id_mismatch" in item for item in delivery["dropped_evidence"]),
                        str(delivery["dropped_evidence"]))

    def test_document_bound_to_other_case_is_rejected(self):
        from stock_agent.research_worker import build_safe_research_context

        payload = self._payload(bound_signals=["sig-unrelated-0009"])
        with project_path_override(lambda value, r=self.root: r / value):
            context, delivery = build_safe_research_context(payload, self.config)
        self.assertEqual(context["evidence_summaries"], {})
        self.assertTrue(any("not_associated_with_task" in item for item in delivery["dropped_evidence"]),
                        str(delivery["dropped_evidence"]))

    def test_replaced_summary_is_never_verified_and_vault_text_wins(self):
        from stock_agent.research_worker import build_safe_research_context

        payload = self._payload()
        payload["public_context"]["evidence_summaries"][self.doc_id]["summary"] = "收入增长十倍（与原件相反的说法）"
        with project_path_override(lambda value, r=self.root: r / value):
            context, delivery = build_safe_research_context(payload, self.config)
        entry = context["evidence_summaries"].get(self.doc_id)
        self.assertIsNotNone(entry)
        self.assertEqual(entry["summary_source"], "vault_projection")
        self.assertNotIn("收入增长十倍", entry["summary"],
                         "the delivered summary must come from the vault document, not user text")
        self.assertIn("收入增长十倍", entry.get("human_note", ""),
                      "user text is preserved but only as an unverified human note")
        self.assertEqual(entry["human_note_status"], "unverified_human_judgment_not_official_fact")

    def test_summary_delivery_is_stable_across_input_note_changes(self):
        from stock_agent.research_worker import build_safe_research_context

        first = self._payload()
        second = self._payload()
        second["public_context"]["evidence_summaries"][self.doc_id]["summary"] = "另一个后来改写的备注"
        with project_path_override(lambda value, r=self.root: r / value):
            ctx_a, _ = build_safe_research_context(first, self.config)
            ctx_b, _ = build_safe_research_context(second, self.config)
        entry_a = ctx_a["evidence_summaries"][self.doc_id]
        entry_b = ctx_b["evidence_summaries"][self.doc_id]
        self.assertEqual(entry_a["summary"], entry_b["summary"],
                         "verified summary is vault-derived; later note edits cannot rewrite it")
        self.assertEqual(entry_a["summary_sha256"], entry_b["summary_sha256"])
        self.assertNotEqual(entry_a.get("human_note", ""), entry_b.get("human_note", ""))


class VaultWriteGuardTests(unittest.TestCase):
    """Incident prevention: fixture imports must never touch the real ledger."""

    def test_default_real_paths_fail_before_any_write(self):
        import tempfile

        from _vault_fixture import import_document, vault_config

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            # Deliberately keep the REAL workspace vault paths (no temp override):
            poisoned = vault_config(root, LOCAL_CONFIG)
            poisoned["official_evidence_vault"]["events_path"] = "data/evidence_vault/events.jsonl"
            poisoned["official_evidence_vault"]["objects_dir"] = "data/evidence_vault/objects"
            with self.assertRaises(AssertionError):
                import_document(poisoned, root, content="x", title="must never land in the real ledger")


class QuoteGovernanceTests(unittest.TestCase):
    """D03: delivery follows the REAL market-data-governance verdict."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def _quote(self, source_time):
        return {"public_context": {"quote_snapshots": {
            "600519": {"price": 1500.0, "pct_change": 1.0, "provider": "eastmoney",
                       "source_time": source_time, "source_time_status": "verified",
                       "observed_at": "2026-09-21T10:29:30+08:00"},
        }}}

    def _safe(self, payload, config, now):
        from stock_agent.research_worker import build_safe_research_context

        with project_path_override(lambda value, r=self.root: r / value):
            return build_safe_research_context(payload, config, now=now)

    def test_fresh_quote_with_controlled_clock_passes(self):
        now = datetime(2026, 9, 21, 10, 30, 0, tzinfo=CST)  # Monday 10:30, active session
        config = _governance_config(self.root, consensus=False)
        source_time = "2026-09-21T10:29:30+08:00"  # 30s old, inside max_live_age
        context, delivery = self._safe(self._quote(source_time), config, now)
        self.assertIn("600519", context["quote_snapshots"], str(delivery["dropped_quotes"]))
        self.assertEqual(context["quote_snapshots"]["600519"]["governance_status"], "PASS")

    def test_stale_quote_from_2000_is_dropped(self):
        now = datetime(2026, 9, 21, 10, 30, 0, tzinfo=CST)
        config = _governance_config(self.root, consensus=False)
        context, delivery = self._safe(self._quote("2000-01-01T10:00:00+08:00"), config, now)
        self.assertEqual(context["quote_snapshots"], {})
        self.assertTrue(any("stale" in item or "older" in item for item in delivery["dropped_quotes"]),
                        str(delivery["dropped_quotes"]))

    def test_future_quote_is_dropped(self):
        now = datetime(2026, 9, 21, 10, 30, 0, tzinfo=CST)
        config = _governance_config(self.root, consensus=False)
        context, delivery = self._safe(self._quote("2026-09-21T11:30:00+08:00"), config, now)
        self.assertEqual(context["quote_snapshots"], {})

    def test_unparseable_time_is_dropped_as_missing(self):
        # A naive timestamp is localized by the configured timezone (policy);
        # only an UNPARSEABLE value counts as a missing quote time.
        now = datetime(2026, 9, 21, 10, 30, 0, tzinfo=CST)
        config = _governance_config(self.root, consensus=False)
        context, delivery = self._safe(self._quote("not-a-date"), config, now)
        self.assertEqual(context["quote_snapshots"], {})
        self.assertTrue(any("missing" in item for item in delivery["dropped_quotes"]),
                        str(delivery["dropped_quotes"]))

    def test_consensus_required_single_source_is_dropped(self):
        now = datetime(2026, 9, 21, 10, 30, 0, tzinfo=CST)
        config = _governance_config(self.root, consensus=True)  # local policy default
        context, delivery = self._safe(self._quote("2026-09-21T10:29:30+08:00"), config, now)
        self.assertEqual(context["quote_snapshots"], {})
        self.assertTrue(any("consensus" in item for item in delivery["dropped_quotes"]),
                        str(delivery["dropped_quotes"]))


class ExplicitSummaryFailClosedTests(unittest.TestCase):
    """D06: an explicitly named summary must never fall back to latest."""

    def _cfg(self, root):
        return {
            "report_dir": str(root / "reports" / "pilot"),
            "run_ledger_path": str(root / "l.jsonl"), "vendor_event_path": str(root / "v.jsonl"),
            "control_event_path": str(root / "c.jsonl"), "incident_event_path": str(root / "i.jsonl"),
        }

    def test_missing_summary_file_raises(self):
        from stock_agent.production_pilot import ProductionPilotPolicyError, sync_production_pilot

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = {"production_pilot": self._cfg(root)}
            with project_path_override(lambda value, r=root: r / value):
                with self.assertRaises(ProductionPilotPolicyError):
                    sync_production_pilot(config, scheduled_summary_path=str(root / "nope.json"))

    def test_summary_without_report_path_raises(self):
        from stock_agent.production_pilot import ProductionPilotPolicyError, sync_production_pilot

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            summary = root / "s.json"
            summary.write_text(json.dumps({"mode": "daily"}), encoding="utf-8")
            config = {"production_pilot": self._cfg(root)}
            with project_path_override(lambda value, r=root: r / value):
                with self.assertRaises(ProductionPilotPolicyError):
                    sync_production_pilot(config, scheduled_summary_path=str(summary))

    def test_bad_json_summary_raises(self):
        from stock_agent.production_pilot import ProductionPilotPolicyError, sync_production_pilot

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            summary = root / "s.json"
            summary.write_text("{not json", encoding="utf-8")
            config = {"production_pilot": self._cfg(root)}
            with project_path_override(lambda value, r=root: r / value):
                with self.assertRaises(ProductionPilotPolicyError):
                    sync_production_pilot(config, scheduled_summary_path=str(summary))


class BoardDomContractTests(unittest.TestCase):
    """D04/D05 source-level contract; real-browser evidence runs separately."""

    def test_ssr_rows_carry_client_keys_and_render_contract(self):
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
        self.assertIn('tr data-task=', html, "SSR rows must carry the client task key")
        self.assertIn("window.__taskBoardRender", html, "browser verification hook must exist")
        self.assertIn("Array.prototype.slice.call(holder.childNodes)", html,
                      "replaceGroup must snapshot the node list before moving")
        self.assertIn("containsActiveElement", html, "focus-holding groups must never be moved")
        self.assertIn("orderChanged", html, "reorder must be order-aware, not unconditional")
        self.assertIn("form.dataset.dirty = ''", html, "accepted submissions clear the dirty flag")


if __name__ == "__main__":
    unittest.main()
