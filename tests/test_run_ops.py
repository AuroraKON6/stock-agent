import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from stock_agent.models import AnalyzedSignal, RawSignal, now_iso


class FakeClock:
    def __init__(self):
        self.value = 100.0

    def __call__(self):
        return self.value

    def advance(self, seconds):
        self.value += seconds


class RunOpsContextTests(unittest.TestCase):
    def test_records_stage_timing_and_warns_when_stage_exceeds_budget(self):
        from stock_agent.config import project_path_override
        from stock_agent.run_ops import RunOpsContext

        clock = FakeClock()
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            with project_path_override(lambda p: root / p):
                ops = RunOpsContext(
                    {"run_ops": {"enabled": True, "max_run_seconds": 30, "stage_warn_seconds": {"collect": 1}}},
                    mode="agent-run",
                    clock=clock,
                )

                with ops.stage("collect"):
                    clock.advance(1.5)

                summary = ops.summary()
                progress = root / "logs" / "run_ops" / "progress_latest.jsonl"
                self.assertTrue(progress.exists())
                self.assertIn("stage_started", progress.read_text(encoding="utf-8"))

        self.assertEqual(summary["overall_status"], "WARN")
        self.assertEqual(summary["stages"][0]["name"], "collect")
        self.assertEqual(summary["stages"][0]["status"], "WARN")
        self.assertEqual(summary["stages"][0]["duration_ms"], 1500)
        self.assertIn("stage_slow", summary["degradations"][0]["reason"])

    def test_llm_call_budget_blocks_extra_calls_and_records_degradation(self):
        from stock_agent.run_ops import RunOpsContext

        ops = RunOpsContext({"run_ops": {"enabled": True, "llm_max_calls": 1, "llm_max_seconds": 10}})

        self.assertTrue(ops.reserve_llm_call("signal_llm", timeout_seconds=3, item_count=2))
        self.assertFalse(ops.reserve_llm_call("research_case_llm", timeout_seconds=3, item_count=5))

        summary = ops.summary()
        self.assertEqual(summary["llm_budget"]["reserved_calls"], 1)
        self.assertEqual(summary["degradations"][-1]["reason"], "llm_call_budget_exhausted")
        self.assertEqual(summary["degradations"][-1]["component"], "research_case_llm")

    def test_write_run_summary_creates_latest_json_and_markdown(self):
        from stock_agent.run_ops import RunOpsContext, write_run_summary

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            ops = RunOpsContext({"run_ops": {"enabled": True}}, mode="agent-run")
            ops.record_degradation("eastmoney", "source_error", severity="FAIL", detail="timeout")

            with patch("stock_agent.run_ops.project_path", side_effect=lambda p: root / p):
                paths = write_run_summary(ops)

            latest_json = root / "logs" / "run_ops" / "latest.json"
            latest_md = root / "logs" / "run_ops" / "latest.md"
            self.assertTrue(latest_json.exists())
            self.assertTrue(latest_md.exists())
            data = json.loads(latest_json.read_text(encoding="utf-8"))
            markdown = latest_md.read_text(encoding="utf-8")

        self.assertEqual(paths["run_ops_path"], str(latest_json))
        self.assertEqual(data["overall_status"], "FAIL")
        self.assertIn("source_error", markdown)


class RunOpsPipelineTests(unittest.TestCase):
    def test_agent_pipeline_writes_run_ops_summary_to_stats_json_and_daily(self):
        from stock_agent.pipeline import run_agent_pipeline

        raw = RawSignal(
            source="manual",
            source_type="news",
            title="Runtime budget test signal",
            summary="major contract runtime budget",
            url="https://example.test/runtime",
            published_at=now_iso(),
        )
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            config = {
                "storage": {"sqlite_path": str(root / "data" / "signals.sqlite3")},
                "sources": {},
                "topics": [{"name": "runtime", "symbols": [], "keywords": ["major contract"], "risk_keywords": []}],
                "priority_rules": {
                    "p0_keywords": [],
                    "p1_keywords": ["major contract"],
                    "positive_keywords": [],
                    "negative_keywords": [],
                    "review_keywords": [],
                },
                "llm_agent": {"enabled": False},
                "research_cases": {"enabled": True, "llm_enabled": False},
                "research_framework": {"enabled": False},
                "research_team": {"enabled": False},
                "run_ops": {"enabled": True, "stage_warn_seconds": {"collect": 0}},
                "report": {"include_low_priority": True},
            }
            with patch("stock_agent.pipeline.collect_sources", return_value=[raw]), \
                 patch("stock_agent.config.PROJECT_ROOT", root), \
                 patch("stock_agent.pipeline.project_path", side_effect=lambda p: root / p), \
                 patch("stock_agent.market_resilience.project_path", side_effect=lambda p: root / p), \
                 patch("stock_agent.source_health.project_path", side_effect=lambda p: root / p), \
                 patch("stock_agent.source_remediation.project_path", side_effect=lambda p: root / p), \
                 patch("stock_agent.research_cases.project_path", side_effect=lambda p: root / p), \
                 patch("stock_agent.reports.project_path", side_effect=lambda p: root / p), \
                 patch("stock_agent.run_ops.project_path", side_effect=lambda p: root / p), \
                 patch("stock_agent.decisions.project_path", side_effect=lambda p: root / p), \
                 patch("stock_agent.team_ops.project_path", side_effect=lambda p: root / p):
                result = run_agent_pipeline(config)

            data = json.loads(Path(result.json_path).read_text(encoding="utf-8"))
            report_text = Path(result.report_path).read_text(encoding="utf-8")

        self.assertIn("run_ops", result.stats)
        self.assertTrue(result.stats["run_ops_path"].endswith("latest.json"))
        self.assertIn("run_ops", data["stats"])
        self.assertIn("Runtime Operations", report_text)


class RunOpsLLMBudgetTests(unittest.TestCase):
    @patch.dict("os.environ", {"DEEPSEEK_API_KEY": "test-key"})
    def test_research_case_llm_skips_when_runtime_budget_exhausted(self):
        from stock_agent.research_cases import build_research_cases, enrich_research_cases
        from stock_agent.run_ops import RunOpsContext

        signal = AnalyzedSignal(
            signal_id="s001",
            source="manual",
            source_type="filing",
            title="Budget case",
            summary="major contract",
            url="https://example.test/case",
            published_at=now_iso(),
            collected_at=now_iso(),
            topics=["runtime"],
            symbols=["688888"],
            priority="P1",
            priority_score=80,
            payload={"announcement_type": "major_contract"},
        )
        case = build_research_cases([signal], {})[0]
        ops = RunOpsContext({"run_ops": {"enabled": True, "llm_max_calls": 0}})

        enriched, stats = enrich_research_cases(
            [case],
            {
                "llm_agent": {"enabled": True, "timeout": 10, "base_url": "https://api.deepseek.com/v1", "model": "deepseek-v4-pro"},
                "research_cases": {"enabled": True, "llm_enabled": True, "min_priority": "P2"},
            },
            ops=ops,
        )

        self.assertEqual(stats["research_case_llm_skipped_budget"], 1)
        self.assertIn("runtime budget", enriched[0]["case_error"])
        self.assertEqual(ops.summary()["degradations"][-1]["reason"], "llm_call_budget_exhausted")

    @patch.dict("os.environ", {"DEEPSEEK_API_KEY": "test-key"})
    @patch("stock_agent.llm_agent._call_llm")
    def test_signal_llm_skips_when_runtime_budget_exhausted(self, mock_call):
        from stock_agent.llm_agent import enrich_signals
        from stock_agent.run_ops import RunOpsContext

        signal = AnalyzedSignal(
            signal_id="s001",
            source="manual",
            source_type="news",
            title="Budget signal",
            summary="major contract",
            url="https://example.test/signal",
            published_at=now_iso(),
            collected_at=now_iso(),
            priority="P1",
            priority_score=80,
        )
        ops = RunOpsContext({"run_ops": {"enabled": True, "llm_max_calls": 0}})

        enriched, stats = enrich_signals(
            [signal],
            {"llm_agent": {"enabled": True, "timeout": 10, "min_priority": "P2"}},
            ops=ops,
        )

        self.assertEqual(stats["llm_skipped_budget"], 1)
        self.assertIn("runtime budget", enriched[0].llm_error)
        mock_call.assert_not_called()

    @patch.dict("os.environ", {"DEEPSEEK_API_KEY": "test-key"})
    @patch("stock_agent.llm_agent.time.sleep")
    @patch("stock_agent.llm_agent._call_llm")
    def test_signal_llm_retry_uses_configured_backoff(self, mock_call, mock_sleep):
        from stock_agent.llm_agent import enrich_signals
        from stock_agent.run_ops import RunOpsContext

        signal = AnalyzedSignal(
            signal_id="s001",
            source="manual",
            source_type="news",
            title="Backoff signal",
            summary="major contract",
            url="https://example.test/signal",
            published_at=now_iso(),
            collected_at=now_iso(),
            priority="P1",
            priority_score=80,
        )
        mock_call.side_effect = [
            OSError("temporary network failure"),
            json.dumps([{
                "signal_id": "s001",
                "impact_direction": "neutral",
                "confidence": 0.5,
                "horizon": "unknown",
                "key_thesis": "retry worked",
                "risk_points": [],
                "review_questions": [],
                "human_decision_needed": False,
                "suggested_actions": [],
            }]),
        ]
        ops = RunOpsContext({"run_ops": {"enabled": True, "retry_backoff_seconds": 2, "llm_max_calls": 3}})

        _enriched, stats = enrich_signals(
            [signal],
            {"llm_agent": {"enabled": True, "timeout": 10, "min_priority": "P2"}},
            ops=ops,
        )

        self.assertEqual(stats["llm_success"], 1)
        mock_sleep.assert_called_once_with(2)


class RunOpsVisibilityTests(unittest.TestCase):
    def test_alerts_trigger_on_runtime_degradation(self):
        from stock_agent.alerts import _check_triggers

        triggers = _check_triggers(
            [],
            [],
            [],
            [],
            run_ops={
                "overall_status": "WARN",
                "degradations": [{"component": "research_case_llm", "reason": "llm_call_budget_exhausted"}],
            },
        )

        self.assertIn("runtime_ops", [name for name, _ in triggers])

    def test_doctor_fails_on_latest_runtime_ops_fail(self):
        from stock_agent.doctor import _check_runtime_ops

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            d = root / "logs" / "run_ops"
            d.mkdir(parents=True)
            (d / "latest.json").write_text(
                json.dumps({"overall_status": "FAIL", "elapsed_ms": 999, "degradations": [{"reason": "timeout"}]}),
                encoding="utf-8",
            )
            with patch("stock_agent.doctor.project_path", side_effect=lambda p: root / p):
                status, detail, _ = _check_runtime_ops()

        self.assertEqual(status, "FAIL")
        self.assertIn("Runtime ops FAIL", detail)

    def test_dashboard_renders_runtime_operations_section(self):
        from stock_agent.dashboard import _render_dashboard_html

        html = _render_dashboard_html({
            "generated_at": now_iso(),
            "signals": [],
            "stats": {
                "run_ops": {
                    "overall_status": "WARN",
                    "elapsed_ms": 1200,
                    "stages": [{"name": "collect", "status": "WARN", "duration_ms": 1200}],
                    "degradations": [{"component": "collect", "reason": "stage_slow"}],
                }
            },
            "alerts_list": [],
            "pending_review": [],
            "review_summary": None,
        })

        self.assertIn("Runtime Operations", html)
        self.assertIn("collect", html)
        self.assertIn("stage_slow", html)


if __name__ == "__main__":
    unittest.main()
