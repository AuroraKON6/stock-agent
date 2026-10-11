import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from stock_agent.models import AnalyzedSignal, RawSignal, now_iso
from stock_agent.analysis import signal_id


try:
    from stock_agent.role_analyst import DEFAULT_ROLE_IDS, _load_roles, _get_role_config, _validate_role_views, enrich_signals_with_role_analysts
except ImportError:
    DEFAULT_ROLE_IDS = []
    enrich_signals_with_role_analysts = None


def _signal(signal_id="s001", priority="P1", needs_review=True):
    return AnalyzedSignal(
        signal_id=signal_id,
        source="manual",
        source_type="news",
        title="AI server order confirmed",
        summary="Company won a large AI server order. Source needs verification.",
        url="http://example.com/source",
        published_at=now_iso(),
        collected_at=now_iso(),
        topics=["AI"],
        symbols=["600000"],
        priority=priority,
        priority_score=80,
        needs_review=needs_review,
        reasons=["large order", "manual review"],
        payload={
            "source_evidence": {"provider": "manual", "url": "http://example.com/source"},
            "main_net_inflow": 120000000,
            "pct_change": 3.8,
        },
        llm_analysis={
            "impact_direction": "positive",
            "confidence": 0.76,
            "horizon": "1-4w",
            "key_thesis": "Order may lift revenue expectations.",
            "risk_points": ["delivery uncertainty"],
            "review_questions": ["Verify announcement link"],
            "human_decision_needed": True,
        },
    )


def _role_response():
    return json.dumps([
        {
            "signal_id": "s001",
            "roles": [
                {
                    "role_id": "fundamental_analyst",
                    "stance": "positive",
                    "confidence": 0.82,
                    "thesis": "Revenue visibility improves if the order is confirmed.",
                    "evidence_refs": ["title", "summary", "source_evidence"],
                    "missing_info": ["contract margin", "delivery schedule"],
                    "review_questions": ["Is there an official filing?"],
                    "human_decision_needed": True,
                },
                {
                    "role_id": "capital_flow_analyst",
                    "stance": "mixed",
                    "confidence": 0.66,
                    "thesis": "Price and inflow confirm attention but need persistence.",
                    "evidence_refs": ["pct_change", "main_net_inflow"],
                    "missing_info": ["volume persistence"],
                    "review_questions": ["Does inflow continue for two sessions?"],
                    "human_decision_needed": True,
                },
                {
                    "role_id": "risk_guard",
                    "stance": "neutral",
                    "confidence": 0.74,
                    "thesis": "Do not convert the signal into any trading action.",
                    "evidence_refs": ["needs_review", "missing_info"],
                    "missing_info": ["source quality"],
                    "review_questions": ["Could this be duplicated news?"],
                    "human_decision_needed": True,
                },
                {
                    "role_id": "contrarian_challenger",
                    "stance": "negative",
                    "confidence": 0.58,
                    "thesis": "The market may already price in the order.",
                    "evidence_refs": ["pct_change"],
                    "missing_info": ["valuation context"],
                    "review_questions": ["What is the base expectation?"],
                    "human_decision_needed": True,
                },
            ],
            "synthesis": {
                "consensus_direction": "mixed_positive",
                "conflict_level": "medium",
                "key_disagreement": "Fundamental upside versus valuation and execution risk.",
                "decision_gate": "human_review_required",
                "next_human_actions": ["Verify source", "Check margin impact"],
                "forbidden_actions_note": "No auto trading, order placement, or position rebalance.",
            },
        }
    ])


class RoleAnalystModuleTests(unittest.TestCase):
    def setUp(self):
        if enrich_signals_with_role_analysts is None:
            self.fail("stock_agent.role_analyst is not implemented")

    @patch.dict(os.environ, {"LLM_API_KEY": "test-key", "DEEPSEEK_API_KEY": ""}, clear=False)
    def test_role_analyst_outputs_structured_views_for_default_roles(self):
        signal = _signal()
        with patch("stock_agent.role_analyst._call_role_llm", return_value=_role_response()) as call:
            signals, stats = enrich_signals_with_role_analysts(
                [signal],
                {
                    "multi_role_agent": {"enabled": True, "min_priority": "P2"},
                    "llm_agent": {"enabled": True, "base_url": "http://llm", "model": "deepseek-v4-pro"},
                },
            )

        self.assertEqual(call.call_count, 1)
        self.assertEqual(stats["role_llm_success"], 1)
        self.assertEqual({item["role_id"] for item in signals[0].role_analyses}, set(DEFAULT_ROLE_IDS))
        first = signals[0].role_analyses[0]
        for key in ["role_id", "stance", "confidence", "thesis", "evidence_refs", "missing_info", "review_questions", "human_decision_needed"]:
            self.assertIn(key, first)
        self.assertEqual(signals[0].role_synthesis["decision_gate"], "human_review_required")
        self.assertIn("No auto trading", signals[0].role_synthesis["forbidden_actions_note"])

    @patch.dict(os.environ, {"LLM_API_KEY": "test-key", "DEEPSEEK_API_KEY": ""}, clear=False)
    def test_invalid_role_json_degrades_without_breaking_signal(self):
        signal = _signal()
        with patch("stock_agent.role_analyst._call_role_llm", side_effect=["not json", "still not json"]):
            signals, stats = enrich_signals_with_role_analysts(
                [signal],
                {
                    "multi_role_agent": {"enabled": True, "min_priority": "P2"},
                    "llm_agent": {"enabled": True, "base_url": "http://llm", "model": "deepseek-v4-pro"},
                },
            )

        self.assertEqual(stats["role_llm_failed"], 1)
        self.assertEqual(signals[0].role_analyses, [])
        self.assertIn("parse failed", signals[0].role_analysis_error)

    @patch.dict(os.environ, {"LLM_API_KEY": "test-key", "DEEPSEEK_API_KEY": ""}, clear=False)
    def test_transient_role_llm_failure_recovered_by_retry_does_not_degrade_runtime(self):
        from stock_agent.run_ops import RunOpsContext

        signal = _signal()
        ops = RunOpsContext({"run_ops": {"enabled": True, "llm_max_calls": 3, "llm_max_seconds": 30}})
        with patch("stock_agent.role_analyst._call_role_llm", side_effect=[OSError("temporary read error"), _role_response()]):
            signals, stats = enrich_signals_with_role_analysts(
                [signal],
                {
                    "multi_role_agent": {"enabled": True, "min_priority": "P2", "timeout": 10, "max_attempts": 2},
                    "llm_agent": {"enabled": True, "base_url": "http://llm", "model": "deepseek-v4-pro"},
                },
                ops=ops,
            )

        self.assertEqual(stats["role_llm_success"], 1)
        self.assertEqual(signals[0].role_analysis_error, "")
        self.assertNotIn("llm_call_failed", [item["reason"] for item in ops.summary()["degradations"]])

    @patch.dict(os.environ, {"DEEPSEEK_API_KEY": "bad-deepseek", "LLM_FALLBACK_API_KEY": "good-fallback"}, clear=False)
    def test_role_llm_uses_fallback_provider_after_primary_auth_failure(self):
        signal = _signal()
        with patch("stock_agent.role_analyst._call_role_llm", side_effect=[
            OSError("HTTP Error 401: Unauthorized"),
            _role_response(),
        ]) as call:
            signals, stats = enrich_signals_with_role_analysts(
                [signal],
                {
                    "multi_role_agent": {"enabled": True, "min_priority": "P2", "timeout": 10, "max_attempts": 1},
                    "llm_agent": {
                        "enabled": True,
                        "providers": [
                            {
                                "name": "deepseek_primary",
                                "base_url": "https://api.deepseek.com/v1",
                                "model": "deepseek-v4-pro",
                                "api_key_env": "DEEPSEEK_API_KEY",
                                "timeout": 10,
                            },
                            {
                                "name": "fallback_openai_compatible",
                                "base_url": "https://fallback.example/v1",
                                "model": "fallback-model",
                                "api_key_env": "LLM_FALLBACK_API_KEY",
                                "timeout": 10,
                            },
                        ],
                    },
                },
            )

        self.assertEqual(stats["role_llm_success"], 1)
        self.assertEqual(stats["role_llm_failed"], 0)
        self.assertEqual(stats["role_llm_provider_used"], "fallback_openai_compatible")
        self.assertEqual([c.args[0] for c in call.call_args_list], [
            "https://api.deepseek.com/v1",
            "https://fallback.example/v1",
        ])
        self.assertEqual(signals[0].role_analysis_error, "")

    @patch.dict(os.environ, {"LLM_API_KEY": "test-key", "DEEPSEEK_API_KEY": ""}, clear=False)
    def test_role_llm_respects_configured_single_attempt_for_scheduled_runs(self):
        signal = _signal()
        with patch("stock_agent.role_analyst._call_role_llm", side_effect=[OSError("temporary read error"), _role_response()]) as call:
            signals, stats = enrich_signals_with_role_analysts(
                [signal],
                {
                    "multi_role_agent": {"enabled": True, "min_priority": "P2", "timeout": 10, "max_attempts": 1},
                    "llm_agent": {"enabled": True, "base_url": "http://llm", "model": "deepseek-v4-pro"},
                },
            )

        self.assertEqual(call.call_count, 1)
        self.assertEqual(stats["role_llm_failed"], 1)
        self.assertIn("failed after 1 attempts", signals[0].role_analysis_error)

    @patch.dict(os.environ, {"LLM_API_KEY": "test-key", "DEEPSEEK_API_KEY": ""}, clear=False)
    def test_single_signal_role_response_can_fallback_when_signal_id_is_missing(self):
        signal = _signal()
        payload = json.loads(_role_response())
        payload[0].pop("signal_id")
        with patch("stock_agent.role_analyst._call_role_llm", return_value=json.dumps(payload)):
            signals, stats = enrich_signals_with_role_analysts(
                [signal],
                {
                    "multi_role_agent": {"enabled": True, "min_priority": "P2"},
                    "llm_agent": {"enabled": True, "base_url": "http://llm", "model": "deepseek-v4-pro"},
                },
            )

        self.assertEqual(stats["role_llm_success"], 1)
        self.assertEqual(len(signals[0].role_analyses), 4)
        self.assertEqual(signals[0].role_analysis_error, "")

    @patch.dict(os.environ, {"LLM_API_KEY": "test-key", "DEEPSEEK_API_KEY": ""}, clear=False)
    def test_batch_response_without_ids_uses_exact_positional_contract(self):
        first = _signal("s001")
        second = _signal("s002")
        row = json.loads(_role_response())[0]
        row.pop("signal_id")
        payload = [dict(row), dict(row)]
        with patch("stock_agent.role_analyst._call_role_llm", return_value=json.dumps(payload)):
            signals, stats = enrich_signals_with_role_analysts(
                [first, second],
                {
                    "multi_role_agent": {"enabled": True, "min_priority": "P2", "max_signals_per_run": 2},
                    "llm_agent": {"enabled": True, "base_url": "http://llm", "model": "deepseek-v4-pro"},
                },
            )

        self.assertEqual(stats["role_llm_success"], 2)
        self.assertEqual(stats["role_llm_mapping_mode"], "positional_batch_contract")
        self.assertTrue(all(len(signal.role_analyses) == 4 for signal in signals))

    @patch.dict(os.environ, {"LLM_API_KEY": "test-key", "DEEPSEEK_API_KEY": ""}, clear=False)
    def test_wrapped_batch_response_maps_by_signal_id(self):
        first = _signal("s001")
        second = _signal("s002")
        rows = json.loads(_role_response())
        second_row = dict(rows[0])
        second_row["signal_id"] = "s002"
        payload = {"results": [rows[0], second_row]}
        with patch("stock_agent.role_analyst._call_role_llm", return_value=json.dumps(payload)):
            signals, stats = enrich_signals_with_role_analysts(
                [first, second],
                {
                    "multi_role_agent": {"enabled": True, "min_priority": "P2", "max_signals_per_run": 2},
                    "llm_agent": {"enabled": True, "base_url": "http://llm", "model": "deepseek-v4-pro"},
                },
            )

        self.assertEqual(stats["role_llm_success"], 2)
        self.assertEqual(stats["role_llm_mapping_mode"], "signal_id")
        self.assertTrue(all(len(signal.role_analyses) == 4 for signal in signals))

    @patch.dict(os.environ, {"LLM_API_KEY": "test-key", "DEEPSEEK_API_KEY": ""}, clear=False)
    def test_ambiguous_batch_response_remains_blocked(self):
        first = _signal("s001")
        second = _signal("s002")
        row = json.loads(_role_response())[0]
        row.pop("signal_id")
        with patch("stock_agent.role_analyst._call_role_llm", return_value=json.dumps([row])):
            signals, stats = enrich_signals_with_role_analysts(
                [first, second],
                {
                    "multi_role_agent": {"enabled": True, "min_priority": "P2", "max_signals_per_run": 2},
                    "llm_agent": {"enabled": True, "base_url": "http://llm", "model": "deepseek-v4-pro"},
                },
            )

        self.assertEqual(stats["role_llm_success"], 0)
        self.assertEqual(stats["role_llm_failed"], 2)
        self.assertEqual(stats["role_llm_mapping_mode"], "unmapped_ambiguous")
        self.assertTrue(all("missing signal_id" in signal.role_analysis_error for signal in signals))

    def test_role_views_accept_structured_object_map_and_explicit_aliases(self):
        roles = _load_roles(_get_role_config({"multi_role_agent": {}, "llm_agent": {}}))
        role_map = {item["role_id"]: item for item in roles}
        raw = {
            "fundamental": {"thesis": "fundamental", "evidence_refs": ["summary"]},
            "capital flow analyst": {"thesis": "flow", "evidence_refs": ["evidence.latest"]},
            "风控守卫": {"thesis": "risk", "evidence_refs": ["source"]},
            "contrarian": {"thesis": "counter", "evidence_refs": ["title"]},
        }
        views = _validate_role_views(raw, role_map)
        self.assertEqual({item["role_id"] for item in views}, set(DEFAULT_ROLE_IDS))


class RoleAnalystPipelineTests(unittest.TestCase):
    @patch.dict(os.environ, {"LLM_API_KEY": "test-key", "DEEPSEEK_API_KEY": ""}, clear=False)
    @patch("stock_agent.pipeline.collect_sources")
    @patch("stock_agent.llm_agent._call_llm")
    @patch("stock_agent.role_analyst._call_role_llm")
    def test_agent_pipeline_persists_role_analysis_to_json_report_and_team_queue(
        self,
        mock_role_llm,
        mock_signal_llm,
        mock_collect,
    ):
        raw_signal = RawSignal(
            source="manual",
            source_type="news",
            title="major AI server order",
            summary="major order requires review",
            url="http://example.com/source",
            published_at="2026-06-17T09:30:00+08:00",
        )
        sid = signal_id(raw_signal)
        mock_collect.return_value = [raw_signal]
        mock_signal_llm.return_value = json.dumps([
            {
                "signal_id": sid,
                "impact_direction": "positive",
                "confidence": 0.8,
                "horizon": "1-4w",
                "key_thesis": "Order may improve expectations.",
                "risk_points": ["execution risk"],
                "review_questions": ["Verify official source"],
                "human_decision_needed": True,
                "suggested_actions": ["export_review", "write_action_log"],
            }
        ])
        role_payload = json.loads(_role_response())
        role_payload[0]["signal_id"] = sid
        mock_role_llm.return_value = json.dumps(role_payload)
        config = {
            "topics": [{"name": "AI", "keywords": ["AI", "server"]}],
            "priority_rules": {
                "p0_keywords": [],
                "p1_keywords": ["major"],
                "positive_keywords": ["order"],
                "negative_keywords": [],
                "review_keywords": ["requires review"],
                "min_priority_for_alert": "P1",
            },
            "llm_agent": {
                "enabled": True,
                "base_url": "http://llm",
                "model": "deepseek-v4-pro",
                "min_priority": "P3",
                "max_signals_per_run": 5,
            },
            "multi_role_agent": {"enabled": True, "min_priority": "P2", "max_signals_per_run": 5},
            "research_team": {"enabled": True},
            "report": {"daily_top_n": 20},
            "review": {"fields": ["signal_id"]},
            "storage": {"sqlite_path": "data/test_role_pipeline.sqlite3"},
            "run_ops": {"enabled": False},
        }
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            for sub in [
                "reports/daily",
                "reports/json",
                "reports/alerts",
                "reports/actions",
                "reports/team",
                "reports/decisions",
                "data/review",
                "data/decisions",
                "data",
                "logs/run_ops",
            ]:
                (tmp / sub).mkdir(parents=True, exist_ok=True)

            def project_path_side(path_value):
                return tmp / path_value

            with patch("stock_agent.pipeline.project_path", side_effect=project_path_side), \
                 patch("stock_agent.reports.project_path", side_effect=project_path_side), \
                 patch("stock_agent.market_resilience.project_path", side_effect=project_path_side), \
                 patch("stock_agent.source_health.project_path", side_effect=project_path_side), \
                 patch("stock_agent.source_remediation.project_path", side_effect=project_path_side), \
                 patch("stock_agent.team_ops.project_path", side_effect=project_path_side), \
                 patch("stock_agent.decisions.project_path", side_effect=project_path_side), \
                 patch("stock_agent.run_ops.project_path", side_effect=project_path_side), \
                 patch("stock_agent.pipeline.ensure_directories"):
                from stock_agent.pipeline import run_agent_pipeline

                result = run_agent_pipeline(config)

            data = json.loads(Path(result.json_path).read_text(encoding="utf-8"))
            report_text = Path(result.report_path).read_text(encoding="utf-8-sig")
            team_json = json.loads(Path(data["stats"]["team_brief_json_path"]).read_text(encoding="utf-8"))

        signal = data["signals"][0]
        self.assertEqual(data["stats"]["role_llm_success"], 1)
        self.assertEqual(len(signal["role_analyses"]), 4)
        self.assertEqual(signal["role_synthesis"]["decision_gate"], "human_review_required")
        self.assertIn("Multi-Role LLM", report_text)
        self.assertIn("fundamental_analyst", report_text)
        queue_item = team_json["human_intervention_queue"][0]
        self.assertIn("role_review_questions", queue_item)
        self.assertTrue(queue_item["role_review_questions"])


if __name__ == "__main__":
    unittest.main()
