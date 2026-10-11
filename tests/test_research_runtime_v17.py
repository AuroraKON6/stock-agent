from __future__ import annotations

import io
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from stock_agent.models import AnalyzedSignal, now_iso
from stock_agent.research_runtime import (
    DEFAULT_COMPLIANCE_CHECKS,
    load_research_runtime,
    record_runtime_action,
    sync_research_runtime,
)
from stock_agent.storage import SignalStore


def _case(priority="P1", source_types=None, with_analysis=True):
    analysis = {}
    if with_analysis:
        analysis = {
            "hypotheses": [
                {
                    "name": "base",
                    "thesis": "订单对未来收入形成支持，但交付节奏仍需跟踪。",
                    "probability": 0.7,
                    "evidence_for": ["official filing"],
                    "evidence_against": ["delivery uncertainty"],
                }
            ],
            "evidence_gaps": [],
            "counter_questions": ["订单是否可撤销？"],
            "human_decision_needed": True,
        }
    return {
        "case_id": "case001",
        "title": "测试公司重大合同",
        "entity": "688888",
        "topic": "AI服务器",
        "catalyst_path": "filing:major_contract",
        "status": "active",
        "top_priority": priority,
        "source_types": source_types or ["filing", "price"],
        "signal_ids": ["sig001"],
        "signal_count": 1,
        "latest_signal_at": now_iso(),
        "case_analysis": analysis,
    }


def _signal_dict(primary=True, with_roles=True):
    roles = []
    if with_roles:
        roles = [
            {"role_id": "fundamental_analyst", "thesis": "contract supports revenue"},
            {"role_id": "market_analyst", "thesis": "market reaction is moderate"},
            {"role_id": "risk_reviewer", "risk_points": ["delivery risk"]},
        ]
    return {
        "signal_id": "sig001",
        "source": "eastmoney_filing" if primary else "manual",
        "source_type": "filing" if primary else "news",
        "title": "测试公告",
        "summary": "重大合同",
        "url": "https://example.test/official.pdf" if primary else "https://example.test/news",
        "priority": "P1",
        "payload": {
            "filing_evidence": {"status": "ok" if primary else "unknown"},
        },
        "evidence_chain": {
            "unresolved_refs": [],
            "evidence_gap_tasks": [],
            "quality": {"status": "OK", "score": 1.0},
        },
        "llm_analysis": {"key_thesis": "合同带来收入增量", "risk_points": ["交付风险"]},
        "role_analyses": roles,
        "role_synthesis": {"conflict_level": "medium", "key_disagreement": "交付节奏"},
    }


def _config(root=None):
    config = {
        "research_runtime": {
            "enabled": True,
            "auto_sync": True,
            "retention_years": 5,
            "require_primary_source_for": ["P0", "P1"],
            "required_compliance_checks": list(DEFAULT_COMPLIANCE_CHECKS),
            "external_publication_enabled": False,
        }
    }
    if root is not None:
        config["storage"] = {"sqlite_path": str(root / "data" / "signals.sqlite3")}
    return config


class ResearchRuntimeProjectionTests(unittest.TestCase):
    def test_good_case_reaches_independent_quality_gate(self):
        with tempfile.TemporaryDirectory() as tmpdir, patch(
            "stock_agent.research_runtime.project_path", side_effect=lambda value: Path(tmpdir) / value
        ):
            result = sync_research_runtime(_config(), cases=[_case()], signals=[_signal_dict()])
            runtime_case = result["research_runtime_cases"][0]

        self.assertEqual(runtime_case["stage"], "quality_review")
        self.assertEqual(runtime_case["gates"]["evidence"]["status"], "PASS")
        self.assertEqual(runtime_case["gates"]["analysis"]["status"], "PASS")
        self.assertEqual(runtime_case["gates"]["challenge"]["status"], "PASS")
        self.assertTrue(runtime_case["human_gate_required"])

    def test_p1_without_primary_source_is_blocked_at_evidence(self):
        with tempfile.TemporaryDirectory() as tmpdir, patch(
            "stock_agent.research_runtime.project_path", side_effect=lambda value: Path(tmpdir) / value
        ):
            result = sync_research_runtime(
                _config(),
                cases=[_case(source_types=["news"])],
                signals=[_signal_dict(primary=False)],
            )
            runtime_case = result["research_runtime_cases"][0]

        self.assertEqual(runtime_case["stage"], "evidence_collection")
        self.assertIn("P0/P1 case requires an official primary source", runtime_case["blockers"])

    def test_parallel_workstreams_and_protected_gates_are_present(self):
        with tempfile.TemporaryDirectory() as tmpdir, patch(
            "stock_agent.research_runtime.project_path", side_effect=lambda value: Path(tmpdir) / value
        ):
            result = sync_research_runtime(_config(), cases=[_case()], signals=[_signal_dict()])
            tasks = {item["task_id"]: item for item in result["research_runtime_cases"][0]["workstreams"]}

        self.assertEqual(tasks["fundamental_analysis"]["owner_role"], "fundamental_analyst")
        self.assertEqual(tasks["market_context"]["owner_role"], "market_analyst")
        self.assertEqual(tasks["independent_challenge"]["owner_role"], "risk_reviewer")
        self.assertEqual(tasks["quality_review"]["owner_role"], "quality_reviewer")
        self.assertEqual(tasks["compliance_review"]["owner_role"], "compliance_officer")
        self.assertEqual(tasks["publication_approval"]["owner_role"], "research_supervisor")

    def test_review_metadata_is_not_misclassified_as_evidence_gap(self):
        signal = _signal_dict()
        signal["evidence_chain"]["unresolved_refs"] = [
            "human_review_required",
            "topic_not_matched",
            "risk_points",
            "source_quality.trust_score=0.85",
        ]
        with tempfile.TemporaryDirectory() as tmpdir, patch(
            "stock_agent.research_runtime.project_path", side_effect=lambda value: Path(tmpdir) / value
        ):
            result = sync_research_runtime(_config(), cases=[_case()], signals=[signal])

        self.assertEqual(result["research_runtime_cases"][0]["stage"], "quality_review")

    def test_due_diligence_questions_do_not_block_source_provenance_gate(self):
        signal = _signal_dict()
        signal["evidence_chain"]["unresolved_refs"] = [
            "同行业公司估值如何变化",
            "后续订单兑现节奏",
            "毛利率是否承压",
        ]
        with tempfile.TemporaryDirectory() as tmpdir, patch(
            "stock_agent.research_runtime.project_path", side_effect=lambda value: Path(tmpdir) / value
        ):
            result = sync_research_runtime(_config(), cases=[_case()], signals=[signal])
            runtime_case = result["research_runtime_cases"][0]

        self.assertEqual(runtime_case["stage"], "quality_review")
        self.assertEqual(runtime_case["evidence_facts"]["evidence_gaps"], [])
        self.assertIn("毛利率是否承压", runtime_case["evidence_facts"]["analysis_questions"])

    def test_missing_original_source_remains_a_hard_evidence_blocker(self):
        case = _case(priority="P2", source_types=["news"])
        signal = _signal_dict(primary=False)
        signal["url"] = ""
        signal["evidence_chain"]["unresolved_refs"] = ["缺少来源原始链接"]
        with tempfile.TemporaryDirectory() as tmpdir, patch(
            "stock_agent.research_runtime.project_path", side_effect=lambda value: Path(tmpdir) / value
        ):
            result = sync_research_runtime(_config(), cases=[case], signals=[signal])
            runtime_case = result["research_runtime_cases"][0]

        self.assertEqual(runtime_case["stage"], "evidence_collection")
        self.assertIn("缺少来源原始链接", runtime_case["blockers"])

    def test_direct_market_snapshot_is_primary_evidence_for_price_case(self):
        case = _case(source_types=["price"])
        signal = _signal_dict(primary=False)
        signal.update({"source": "eastmoney", "source_type": "price", "url": ""})
        signal["payload"] = {"code": "688888", "latest": 25.8, "pct_change": 4.2}
        signal["role_analyses"] = [
            {"role_id": "market_analyst"},
            {"role_id": "risk_reviewer", "risk_points": ["reversal"]},
        ]
        with tempfile.TemporaryDirectory() as tmpdir, patch(
            "stock_agent.research_runtime.project_path", side_effect=lambda value: Path(tmpdir) / value
        ):
            result = sync_research_runtime(_config(), cases=[case], signals=[signal])
            runtime_case = result["research_runtime_cases"][0]

        self.assertEqual(runtime_case["evidence_facts"]["primary_source_count"], 1)
        self.assertEqual(runtime_case["stage"], "quality_review")

    def test_structured_market_snapshot_does_not_require_web_url(self):
        case = _case(priority="P3", source_types=["price"])
        signal = _signal_dict(primary=False)
        signal.update({"source": "market_cache", "source_type": "price", "url": ""})
        signal["payload"] = {"code": "688888", "latest": 25.8, "pct_change": 1.2}
        signal["evidence_chain"]["unresolved_refs"] = ["missing_url", "原始来源链接"]
        signal["role_analyses"] = [
            {"role_id": "market_analyst"},
            {"role_id": "risk_reviewer", "risk_points": ["cache staleness"]},
        ]
        with tempfile.TemporaryDirectory() as tmpdir, patch(
            "stock_agent.research_runtime.project_path", side_effect=lambda value: Path(tmpdir) / value
        ):
            result = sync_research_runtime(_config(), cases=[case], signals=[signal])
            runtime_case = result["research_runtime_cases"][0]

        self.assertEqual(runtime_case["gates"]["evidence"]["status"], "PASS")
        self.assertEqual(runtime_case["stage"], "quality_review")

    def test_snapshot_and_report_are_persisted(self):
        with tempfile.TemporaryDirectory() as tmpdir, patch(
            "stock_agent.research_runtime.project_path", side_effect=lambda value: Path(tmpdir) / value
        ):
            result = sync_research_runtime(_config(), cases=[_case()], signals=[_signal_dict()])
            snapshot = Path(tmpdir) / "data" / "research_runtime" / "runtime.json"
            latest = Path(result["research_runtime_latest_path"])
            markdown = latest.read_text(encoding="utf-8-sig")
            snapshot_exists = snapshot.exists()
            latest_exists = latest.exists()

        self.assertTrue(snapshot_exists)
        self.assertTrue(latest_exists)
        self.assertIn("quality_review", markdown)
        self.assertIn("不自动交易", markdown)


class ResearchRuntimeReliabilityTests(unittest.TestCase):
    """Reliability Milestone A: fail-closed degradation and diagnostics."""

    def test_empty_input_preserves_populated_runtime(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            with patch("stock_agent.research_runtime.project_path", side_effect=lambda value: root / value):
                first = sync_research_runtime(_config(), cases=[_case()], signals=[_signal_dict()])
                self.assertEqual(first["research_runtime_case_count"], 1)
                result = sync_research_runtime(_config(), cases=[], signals=[])
                snapshot = json.loads((root / "data" / "research_runtime" / "runtime.json").read_text(encoding="utf-8"))

        self.assertEqual(result["research_runtime_case_count"], 1)
        self.assertTrue(result["research_runtime_degraded"])
        diagnostics = result["research_runtime_diagnostics"]
        self.assertTrue(diagnostics["empty_input_preserved"])
        self.assertEqual(diagnostics["requested_cases"], 0)
        self.assertEqual(diagnostics["loaded_cases"], 0)
        # Empty input means no signals were requested or loaded, truthfully.
        self.assertEqual(diagnostics["requested_signal_count"], 0)
        self.assertEqual(diagnostics["loaded_signal_count"], 0)
        self.assertEqual(diagnostics["preserved_cases"], ["case001"])
        self.assertTrue(diagnostics["degradation_reasons"])
        # Preserved case keeps its stage, workpapers, approvals, and history.
        runtime_case = result["research_runtime_cases"][0]
        self.assertEqual(runtime_case["case_id"], "case001")
        self.assertEqual(runtime_case["stage"], "quality_review")
        self.assertEqual(runtime_case["approvals"]["quality_review"]["status"], "pending")
        self.assertEqual(snapshot["cases"][0]["case_id"], "case001")
        self.assertTrue(snapshot["degradation"]["empty_input_preserved"])

    def test_empty_input_is_not_degraded_on_fresh_runtime(self):
        with tempfile.TemporaryDirectory() as tmpdir, patch(
            "stock_agent.research_runtime.project_path", side_effect=lambda value: Path(tmpdir) / value
        ):
            result = sync_research_runtime(_config(), cases=[], signals=[])

        self.assertEqual(result["research_runtime_case_count"], 0)
        self.assertFalse(result["research_runtime_degraded"])
        diagnostics = result["research_runtime_diagnostics"]
        self.assertFalse(diagnostics["empty_input_preserved"])
        # A genuinely fresh runtime requested and loaded no signals: 0/0, no degradation.
        self.assertEqual(diagnostics["requested_signal_count"], 0)
        self.assertEqual(diagnostics["loaded_signal_count"], 0)
        self.assertEqual(diagnostics["missing_signal_ids"], [])

    def test_backfill_unavailable_preserves_verified_stage(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            with patch("stock_agent.research_runtime.project_path", side_effect=lambda value: root / value):
                first = sync_research_runtime(_config(), cases=[_case()], signals=[_signal_dict()])
                self.assertEqual(first["research_runtime_cases"][0]["stage"], "quality_review")
                # Upstream returns the case but no signals, and the historical
                # sqlite backfill is unavailable (no database file exists).
                result = sync_research_runtime(_config(), cases=[_case()], signals=[])

        self.assertEqual(result["research_runtime_case_count"], 1)
        self.assertTrue(result["research_runtime_degraded"])
        diagnostics = result["research_runtime_diagnostics"]
        self.assertEqual(diagnostics["missing_signal_ids"], ["sig001"])
        self.assertEqual(diagnostics["requested_signal_count"], 1)
        self.assertEqual(diagnostics["loaded_signal_count"], 0)
        self.assertTrue(diagnostics["backfill_unavailable"])
        self.assertEqual(diagnostics["preserved_cases"], ["case001"])
        # Verified evidence and stage are preserved, not regressed.
        runtime_case = result["research_runtime_cases"][0]
        self.assertEqual(runtime_case["stage"], "quality_review")
        self.assertEqual(runtime_case["evidence_facts"]["primary_source_count"], 1)
        self.assertEqual(runtime_case["evidence_facts"]["traceable_source_count"], 1)

    def test_new_case_with_unavailable_signals_is_not_invented(self):
        with tempfile.TemporaryDirectory() as tmpdir, patch(
            "stock_agent.research_runtime.project_path", side_effect=lambda value: Path(tmpdir) / value
        ):
            # No prior runtime for this case, and the referenced signal cannot be
            # backfilled. It must not invent evidence; it stays honestly blocked.
            result = sync_research_runtime(_config(), cases=[_case()], signals=[])

        self.assertEqual(result["research_runtime_case_count"], 1)
        self.assertTrue(result["research_runtime_degraded"])
        diagnostics = result["research_runtime_diagnostics"]
        self.assertEqual(diagnostics["missing_signal_ids"], ["sig001"])
        self.assertEqual(diagnostics["requested_signal_count"], 1)
        self.assertEqual(diagnostics["loaded_signal_count"], 0)
        runtime_case = result["research_runtime_cases"][0]
        self.assertEqual(runtime_case["stage"], "evidence_collection")
        self.assertEqual(runtime_case["evidence_facts"]["primary_source_count"], 0)

    def test_normal_sync_reports_no_degradation(self):
        with tempfile.TemporaryDirectory() as tmpdir, patch(
            "stock_agent.research_runtime.project_path", side_effect=lambda value: Path(tmpdir) / value
        ):
            result = sync_research_runtime(_config(), cases=[_case()], signals=[_signal_dict()])
            diagnostics = result["research_runtime_diagnostics"]

        self.assertFalse(result["research_runtime_degraded"])
        self.assertFalse(diagnostics["degraded"])
        self.assertEqual(diagnostics["requested_cases"], 1)
        self.assertEqual(diagnostics["loaded_cases"], 1)
        self.assertEqual(diagnostics["missing_signal_ids"], [])
        self.assertEqual(diagnostics["requested_signal_count"], 1)
        self.assertEqual(diagnostics["loaded_signal_count"], 1)
        self.assertEqual(diagnostics["preserved_cases"], [])
        self.assertFalse(diagnostics["backfill_unavailable"])
        self.assertFalse(diagnostics["empty_input_preserved"])

    def test_injected_resolver_is_restored_after_sync_error(self):
        import shutil

        from stock_agent.config import PROJECT_ROOT, project_path

        root = Path(tempfile.mkdtemp())
        try:
            def bad_resolver(value):
                raise RuntimeError("boom")

            with self.assertRaisesRegex(RuntimeError, "boom"):
                sync_research_runtime(_config(), cases=[_case()], signals=[_signal_dict()], project_path_fn=bad_resolver)
            # The temporary context override must not leak after the error; the
            # shared resolver resolves against the real project root again.
            self.assertEqual(
                project_path("data/research_runtime/runtime.json"),
                PROJECT_ROOT / "data/research_runtime/runtime.json",
            )
        finally:
            shutil.rmtree(root, ignore_errors=True)

    def test_mixed_partial_backfill_only_preserves_degraded_case(self):
        case_a = _case()
        case_b = _case()
        case_b["case_id"] = "case002"
        case_b["signal_ids"] = ["sig002"]
        signal_b = _signal_dict()
        signal_b["signal_id"] = "sig002"
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            with patch("stock_agent.research_runtime.project_path", side_effect=lambda value: root / value):
                # First sync verifies both cases with their own evidence.
                sync_research_runtime(_config(), cases=[case_a, case_b], signals=[_signal_dict(), signal_b])
                # Second run: only case A's signal is available; case B's signal
                # and its sqlite backfill are unavailable.
                case_b2 = dict(case_b)
                result = sync_research_runtime(_config(), cases=[case_a, case_b2], signals=[_signal_dict()])

        diagnostics = result["research_runtime_diagnostics"]
        self.assertEqual(diagnostics["missing_signal_ids"], ["sig002"])
        self.assertEqual(diagnostics["requested_signal_count"], 2)
        self.assertEqual(diagnostics["loaded_signal_count"], 1)
        self.assertEqual(diagnostics["preserved_cases"], ["case002"])
        self.assertTrue(result["research_runtime_degraded"])
        by_id = {item["case_id"]: item for item in result["research_runtime_cases"]}
        # Case A re-projected normally; case B preserved at its verified stage.
        self.assertEqual(by_id["case001"]["stage"], "quality_review")
        self.assertEqual(by_id["case002"]["stage"], "quality_review")
        self.assertEqual(by_id["case002"]["evidence_facts"]["primary_source_count"], 1)


class ResearchRuntimeHumanGateTests(unittest.TestCase):
    def _seed(self, root):
        with patch("stock_agent.research_runtime.project_path", side_effect=lambda value: root / value):
            sync_research_runtime(_config(), cases=[_case()], signals=[_signal_dict()])

    def _action(self, root, action, actor, actor_role, **kwargs):
        with patch("stock_agent.research_runtime.project_path", side_effect=lambda value: root / value):
            return record_runtime_action(
                "case001",
                action,
                actor,
                actor_role,
                config=_config(),
                **kwargs,
            )

    def _assign_all(self, root):
        self._action(root, "assign_lead", "supervisor-a", "research_supervisor", assignee="analyst-a")
        self._action(root, "assign_quality_reviewer", "supervisor-a", "research_supervisor", assignee="reviewer-a")
        self._action(root, "assign_compliance_officer", "supervisor-a", "research_supervisor", assignee="compliance-a")
        self._action(root, "assign_approver", "supervisor-a", "research_supervisor", assignee="supervisor-a")

    def test_full_controlled_approval_sequence_reaches_monitoring(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            self._seed(root)
            self._assign_all(root)
            quality = self._action(root, "quality_approve", "reviewer-a", "quality_reviewer", notes="workpaper checked")
            self.assertEqual(quality["case"]["stage"], "compliance_review")
            compliance = self._action(
                root,
                "compliance_clear",
                "compliance-a",
                "compliance_officer",
                notes="checks complete",
                checks=list(DEFAULT_COMPLIANCE_CHECKS),
            )
            self.assertEqual(compliance["case"]["stage"], "human_approval")
            published = self._action(root, "publication_approve", "supervisor-a", "research_supervisor", notes="internal release")
            self.assertEqual(published["case"]["stage"], "published")
            monitored = self._action(root, "start_monitoring", "supervisor-a", "research_supervisor", notes="track delivery")

        self.assertEqual(monitored["case"]["stage"], "monitoring")
        self.assertFalse(monitored["case"]["external_publication_allowed"])
        self.assertFalse(monitored["case"]["auto_publish"])

    def test_llm_cannot_approve_quality_gate(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            self._seed(root)
            self._action(root, "assign_lead", "supervisor-a", "research_supervisor", assignee="analyst-a")
            self._action(root, "assign_quality_reviewer", "supervisor-a", "research_supervisor", assignee="llm-reviewer")
            with self.assertRaisesRegex(ValueError, "cannot pass protected human gates"):
                self._action(root, "quality_approve", "llm-reviewer", "quality_reviewer", notes="auto approval")

    def test_role_separation_prevents_self_review(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            self._seed(root)
            self._action(root, "assign_lead", "supervisor-a", "research_supervisor", assignee="same-person")
            with self.assertRaisesRegex(ValueError, "must be separated"):
                self._action(root, "assign_quality_reviewer", "supervisor-a", "research_supervisor", assignee="same-person")

    def test_compliance_clear_requires_every_check(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            self._seed(root)
            self._assign_all(root)
            self._action(root, "quality_approve", "reviewer-a", "quality_reviewer", notes="checked")
            with self.assertRaisesRegex(ValueError, "Missing required compliance checks"):
                self._action(
                    root,
                    "compliance_clear",
                    "compliance-a",
                    "compliance_officer",
                    checks=["public_information_only"],
                )

    def test_requesting_new_evidence_invalidates_downstream_approvals(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            self._seed(root)
            self._assign_all(root)
            self._action(root, "quality_approve", "reviewer-a", "quality_reviewer", notes="checked")
            result = self._action(
                root,
                "request_evidence",
                "reviewer-a",
                "quality_reviewer",
                notes="需要核对合同是否可撤销",
            )

        self.assertEqual(result["case"]["stage"], "evidence_collection")
        self.assertEqual(result["case"]["approvals"]["quality_review"]["status"], "pending")

    def test_protected_task_cannot_be_completed_with_generic_action(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            self._seed(root)
            with self.assertRaisesRegex(ValueError, "dedicated approval action"):
                self._action(
                    root,
                    "complete_task",
                    "supervisor-a",
                    "research_supervisor",
                    task_id="publication_approval",
                    references=["research_note:attempt"],
                )

    def test_evidence_task_requires_retained_reference(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            self._seed(root)
            with self.assertRaisesRegex(ValueError, "requires at least one retained workpaper"):
                self._action(
                    root,
                    "complete_task",
                    "analyst-a",
                    "information_analyst",
                    task_id="source_verification",
                )


class ResearchRuntimeStorageAndCliTests(unittest.TestCase):
    def test_storage_fetch_many_reconstructs_json_fields(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "signals.sqlite3"
            signal = AnalyzedSignal(
                signal_id="sig001",
                source="official",
                source_type="filing",
                title="公告",
                summary="摘要",
                url="https://example.test/official.pdf",
                published_at=now_iso(),
                collected_at=now_iso(),
                priority="P1",
                topics=["AI"],
                symbols=["688888"],
                payload={"filing_evidence": {"status": "ok"}},
                llm_analysis={"key_thesis": "test"},
            )
            store = SignalStore(path)
            try:
                store.insert(signal)
                store.conn.commit()
                rows = store.fetch_many(["sig001"])
            finally:
                store.close()

        self.assertEqual(rows[0]["topics"], ["AI"])
        self.assertEqual(rows[0]["payload"]["filing_evidence"]["status"], "ok")
        self.assertEqual(rows[0]["llm_analysis"]["key_thesis"], "test")

    def test_sync_backfills_historical_signals_from_sqlite(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            db_path = root / "data" / "signals.sqlite3"
            signal = AnalyzedSignal(
                signal_id="sig001",
                source="eastmoney_filing",
                source_type="filing",
                title="公告",
                summary="摘要",
                url="https://example.test/official.pdf",
                published_at=now_iso(),
                collected_at=now_iso(),
                priority="P1",
                payload={"filing_evidence": {"status": "ok"}},
                role_analyses=[
                    {"role_id": "fundamental_analyst"},
                    {"role_id": "market_analyst"},
                    {"role_id": "risk_reviewer", "risk_points": ["risk"]},
                ],
                llm_analysis={"key_thesis": "test", "risk_points": ["risk"]},
            )
            store = SignalStore(db_path)
            try:
                store.insert(signal)
                store.conn.commit()
            finally:
                store.close()
            with patch("stock_agent.research_runtime.project_path", side_effect=lambda value: root / value):
                result = sync_research_runtime(_config(root), cases=[_case()], signals=[])

        self.assertEqual(result["research_runtime_cases"][0]["evidence_facts"]["primary_source_count"], 1)
        self.assertEqual(result["research_runtime_cases"][0]["stage"], "quality_review")
        # Successful SQLite backfill reports the requested signal as loaded.
        diagnostics = result["research_runtime_diagnostics"]
        self.assertEqual(diagnostics["requested_signal_count"], 1)
        self.assertEqual(diagnostics["loaded_signal_count"], 1)
        self.assertEqual(diagnostics["missing_signal_ids"], [])

    def test_cli_status_prints_stage_and_blockers(self):
        from stock_agent.cli import main

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            config_dir = root / "config"
            config_dir.mkdir(parents=True)
            (config_dir / "watchlist.local.json").write_text(json.dumps(_config()), encoding="utf-8")
            with patch("stock_agent.research_runtime.project_path", side_effect=lambda value: root / value):
                sync_research_runtime(_config(), cases=[_case()], signals=[_signal_dict()])
            output = io.StringIO()
            argv = ["stock_agent", "research-runtime-status", "--case-id", "case001"]
            with patch.object(sys, "argv", argv), patch("sys.stdout", output), patch(
                "stock_agent.config.PROJECT_ROOT", root
            ), patch("stock_agent.research_runtime.project_path", side_effect=lambda value: root / value):
                main()

        self.assertIn("quality_review", output.getvalue())
        self.assertIn("case001", output.getvalue())


if __name__ == "__main__":
    unittest.main()
