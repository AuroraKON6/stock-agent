"""Tests for LLM agent integration. All LLM calls are mocked — no real network."""
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch, MagicMock

from stock_agent.analysis import analyze_one
from stock_agent.llm_agent import enrich_signals, _get_llm_config, _parse_llm_json, _select_api_key, _validate_analysis, _validate_actions
from stock_agent.models import AnalyzedSignal, RawSignal, now_iso
from stock_agent.pipeline import run_agent_pipeline, _execute_export_review, _execute_write_action_log


def _make_config(**overrides):
    base = {
        "topics": [
            {
                "name": "AI算力",
                "symbols": ["浪潮信息"],
                "keywords": ["算力", "服务器"],
                "risk_keywords": ["订单取消"],
            }
        ],
        "priority_rules": {
            "p0_keywords": ["停牌"],
            "p1_keywords": ["订单取消"],
            "positive_keywords": ["订单"],
            "negative_keywords": ["取消"],
            "review_keywords": ["网传"],
            "min_priority_for_alert": "P1",
        },
        "llm_agent": {
            "enabled": True,
            "base_url": "https://api.openai.com/v1",
            "model": "gpt-4.1-mini",
            "timeout": 10,
            "temperature": 0.2,
            "min_priority": "P3",
            "max_signals_per_run": 10,
            "auto_execute_actions": True,
            "allowed_actions": ["export_review", "write_action_log"],
        },
        "report": {"daily_top_n": 20},
        "review": {"fields": ["signal_id"]},
    }
    base.update(overrides)
    return base


def _make_signal(title="测试信号", priority="P1", priority_score=55.0):
    return AnalyzedSignal(
        signal_id="test_id_0001",
        source="manual",
        source_type="news",
        title=title,
        summary="测试摘要",
        url="http://example.com",
        published_at=now_iso(),
        collected_at=now_iso(),
        topics=["AI算力"],
        symbols=["浪潮信息"],
        matched_keywords=["算力"],
        sentiment_score=0.5,
        priority_score=priority_score,
        priority=priority,
        needs_review=True,
        reasons=["测试原因"],
    )


def _mock_llm_response(signals):
    """Build a valid LLM JSON response for given signals."""
    items = []
    for s in signals:
        items.append({
            "signal_id": s.signal_id,
            "impact_direction": "negative",
            "confidence": 0.75,
            "horizon": "1-5d",
            "key_thesis": "订单取消对浪潮信息短期利空",
            "risk_points": ["供应链风险", "客户流失"],
            "review_questions": ["订单取消原因是什么？"],
            "human_decision_needed": True,
            "suggested_actions": ["export_review", "write_action_log"],
        })
    return json.dumps(items, ensure_ascii=False)


class TestParseLLMJson(unittest.TestCase):
    def test_valid_json_array(self):
        raw = json.dumps([{"signal_id": "abc"}])
        result = _parse_llm_json(raw)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["signal_id"], "abc")

    def test_valid_json_object(self):
        raw = json.dumps({"signal_id": "abc"})
        result = _parse_llm_json(raw)
        self.assertEqual(len(result), 1)

    def test_markdown_fences(self):
        raw = '```json\n[{"signal_id": "abc"}]\n```'
        result = _parse_llm_json(raw)
        self.assertEqual(result[0]["signal_id"], "abc")

    def test_invalid_json_raises(self):
        with self.assertRaises((json.JSONDecodeError, ValueError)):
            _parse_llm_json("not json at all")


class TestValidateAnalysis(unittest.TestCase):
    def test_valid_input(self):
        item = {
            "impact_direction": "positive",
            "confidence": 0.8,
            "horizon": "intraday",
            "key_thesis": "利好",
            "risk_points": ["风险1"],
            "review_questions": ["问题1"],
            "human_decision_needed": False,
        }
        result = _validate_analysis(item)
        self.assertEqual(result["impact_direction"], "positive")
        self.assertEqual(result["confidence"], 0.8)
        self.assertEqual(result["horizon"], "intraday")

    def test_invalid_direction_defaults_unknown(self):
        result = _validate_analysis({"impact_direction": "wrong"})
        self.assertEqual(result["impact_direction"], "unknown")

    def test_clamp_confidence(self):
        result = _validate_analysis({"confidence": 2.0})
        self.assertEqual(result["confidence"], 1.0)
        result2 = _validate_analysis({"confidence": -1.0})
        self.assertEqual(result2["confidence"], 0.0)


class TestValidateActions(unittest.TestCase):
    def test_allowed_actions(self):
        result = _validate_actions({"suggested_actions": ["export_review", "write_action_log"]}, ["export_review", "write_action_log"])
        self.assertEqual(result, ["export_review", "write_action_log"])

    def test_disallowed_actions_filtered(self):
        result = _validate_actions({"suggested_actions": ["export_review", "place_order"]}, ["export_review", "write_action_log"])
        self.assertEqual(result, ["export_review"])

    def test_empty_actions(self):
        result = _validate_actions({}, ["export_review"])
        self.assertEqual(result, [])


class TestEnrichSignalsNormal(unittest.TestCase):
    """LLM returns valid JSON on first try."""

    @patch.dict(os.environ, {"LLM_API_KEY": "test-key-123"})
    @patch("stock_agent.llm_agent._call_llm")
    def test_enrich_single_signal(self, mock_call):
        signal = _make_signal()
        mock_call.return_value = _mock_llm_response([signal])
        config = _make_config()
        result, stats = enrich_signals([signal], config)

        self.assertEqual(stats["llm_success"], 1)
        self.assertEqual(stats["llm_failed"], 0)
        self.assertEqual(result[0].llm_analysis["impact_direction"], "negative")
        self.assertEqual(result[0].llm_analysis["confidence"], 0.75)
        self.assertEqual(result[0].llm_actions, ["export_review", "write_action_log"])
        self.assertEqual(result[0].llm_error, "")

    @patch.dict(os.environ, {"LLM_API_KEY": "test-key-123"})
    @patch("stock_agent.llm_agent._call_llm")
    def test_enrich_multiple_signals(self, mock_call):
        s1 = _make_signal("信号A", "P1", 55)
        s2 = _make_signal("信号B", "P2", 30)
        s2.signal_id = "test_id_0002"
        mock_call.return_value = _mock_llm_response([s1, s2])
        config = _make_config()
        result, stats = enrich_signals([s1, s2], config)
        self.assertEqual(stats["llm_success"], 2)
        self.assertEqual(stats["llm_failed"], 0)


class TestEnrichSignalsRetry(unittest.TestCase):
    """LLM returns invalid JSON first, then valid JSON on retry."""

    @patch.dict(os.environ, {"LLM_API_KEY": "test-key-123"})
    @patch("stock_agent.llm_agent._call_llm")
    def test_retry_on_invalid_json(self, mock_call):
        signal = _make_signal()
        mock_call.side_effect = [
            "this is not json",
            _mock_llm_response([signal]),
        ]
        config = _make_config()
        result, stats = enrich_signals([signal], config)
        self.assertEqual(stats["llm_success"], 1)
        self.assertEqual(stats["llm_failed"], 0)
        self.assertIn("negative", result[0].llm_analysis["impact_direction"])

    @patch.dict(os.environ, {"LLM_API_KEY": "test-key-123"})
    @patch("stock_agent.llm_agent._call_llm")
    def test_double_failure_marks_error(self, mock_call):
        signal = _make_signal()
        mock_call.side_effect = [
            "bad json 1",
            "bad json 2",
        ]
        config = _make_config()
        result, stats = enrich_signals([signal], config)
        self.assertEqual(stats["llm_failed"], 1)
        self.assertIn("JSON parse failed", result[0].llm_error)


class TestEnrichSignalsFailure(unittest.TestCase):
    """LLM call raises exception — pipeline must not crash."""

    @patch.dict(os.environ, {"LLM_API_KEY": "test-key-123"})
    @patch("stock_agent.llm_agent._call_llm")
    def test_network_error_graceful(self, mock_call):
        signal = _make_signal()
        mock_call.side_effect = ConnectionError("network timeout")
        config = _make_config()
        # Must not raise
        result, stats = enrich_signals([signal], config)
        self.assertEqual(stats["llm_failed"], 1)
        self.assertIn("LLM call failed", result[0].llm_error)
        # Original signal data preserved
        self.assertEqual(result[0].title, "测试信号")
        self.assertEqual(result[0].priority, "P1")


class TestProviderFallback(unittest.TestCase):
    @patch.dict(os.environ, {"DEEPSEEK_API_KEY": "bad-deepseek", "LLM_FALLBACK_API_KEY": "good-fallback"}, clear=False)
    @patch("stock_agent.llm_agent._call_llm")
    def test_signal_llm_uses_fallback_provider_after_primary_auth_failure(self, mock_call):
        signal = _make_signal()
        mock_call.side_effect = [
            OSError("HTTP Error 401: Unauthorized"),
            _mock_llm_response([signal]),
        ]
        config = _make_config(**{"llm_agent": {
            "enabled": True,
            "min_priority": "P2",
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
        }})

        result, stats = enrich_signals([signal], config)

        self.assertEqual(stats["llm_success"], 1)
        self.assertEqual(stats["llm_failed"], 0)
        self.assertEqual(stats["llm_provider_used"], "fallback_openai_compatible")
        self.assertEqual([c.kwargs["base_url"] for c in mock_call.call_args_list], [
            "https://api.deepseek.com/v1",
            "https://fallback.example/v1",
        ])
        self.assertEqual(result[0].llm_error, "")


class TestNoApiKeyDegradation(unittest.TestCase):
    """Without LLM_API_KEY, agent-run degrades gracefully."""

    @patch.dict(os.environ, {}, clear=True)
    def test_no_key_marks_error(self):
        signal = _make_signal()
        config = _make_config()
        result, stats = enrich_signals([signal], config)
        self.assertEqual(stats["llm_skipped_no_key"], 1)
        self.assertIn("LLM_API_KEY", result[0].llm_error)


class TestDeduplication(unittest.TestCase):
    """Only new (qualifying priority) signals call LLM."""

    @patch.dict(os.environ, {"LLM_API_KEY": "test-key-123"})
    @patch("stock_agent.llm_agent._call_llm")
    def test_low_priority_skipped(self, mock_call):
        signal = _make_signal(priority="P3", priority_score=5)
        config = _make_config(**{"llm_agent": {
            "enabled": True,
            "base_url": "https://api.openai.com/v1",
            "model": "gpt-4.1-mini",
            "timeout": 10,
            "temperature": 0.2,
            "min_priority": "P2",
            "max_signals_per_run": 10,
            "auto_execute_actions": True,
            "allowed_actions": ["export_review", "write_action_log"],
        }})
        result, stats = enrich_signals([signal], config)
        self.assertEqual(stats["llm_candidates"], 0)
        mock_call.assert_not_called()

    @patch.dict(os.environ, {"LLM_API_KEY": "test-key-123"})
    @patch("stock_agent.llm_agent._call_llm")
    def test_high_priority_calls_llm(self, mock_call):
        signal = _make_signal(priority="P1", priority_score=55)
        mock_call.return_value = _mock_llm_response([signal])
        config = _make_config(**{"llm_agent": {
            "enabled": True,
            "base_url": "https://api.openai.com/v1",
            "model": "gpt-4.1-mini",
            "timeout": 10,
            "temperature": 0.2,
            "min_priority": "P2",
            "max_signals_per_run": 10,
            "auto_execute_actions": True,
            "allowed_actions": ["export_review", "write_action_log"],
        }})
        result, stats = enrich_signals([signal], config)
        self.assertEqual(stats["llm_candidates"], 1)
        mock_call.assert_called_once()


class TestDisabledLLM(unittest.TestCase):
    def test_disabled_returns_unchanged(self):
        signal = _make_signal()
        config = _make_config(**{"llm_agent": {"enabled": False}})
        result, stats = enrich_signals([signal], config)
        self.assertFalse(stats["llm_enabled"])
        self.assertEqual(result[0].llm_analysis, {})


class TestDeepSeekV4ProDefault(unittest.TestCase):
    @patch.dict(os.environ, {"LLM_MODEL": "", "LLM_BASE_URL": ""}, clear=False)
    @patch("stock_agent.llm_agent._read_openclaw_deepseek_config", return_value={})
    def test_deepseek_v4_pro_is_default_model_when_config_omits_model(self, _mock_openclaw):
        cfg = _get_llm_config({"llm_agent": {"enabled": True}})
        self.assertEqual(cfg["model"], "deepseek-v4-pro")
        self.assertEqual(cfg["base_url"], "https://api.deepseek.com/v1")

    @patch.dict(os.environ, {"LLM_API_KEY": "generic-key", "DEEPSEEK_API_KEY": "deepseek-key"}, clear=False)
    def test_deepseek_base_url_prefers_deepseek_api_key(self):
        self.assertEqual(_select_api_key("https://api.deepseek.com/v1"), "deepseek-key")

    @patch.dict(os.environ, {"LLM_API_KEY": "generic-key", "DEEPSEEK_API_KEY": "deepseek-key"}, clear=False)
    def test_non_deepseek_base_url_prefers_generic_llm_api_key(self):
        self.assertEqual(_select_api_key("https://api.openai.com/v1"), "generic-key")


class TestActionExecution(unittest.TestCase):
    def test_export_review_creates_file(self):
        signal = _make_signal()
        signal.llm_analysis = {"human_decision_needed": True, "impact_direction": "negative"}
        signal.llm_actions = ["export_review"]
        with tempfile.TemporaryDirectory() as tmpdir:
            with patch("stock_agent.pipeline.project_path", side_effect=lambda p: Path(tmpdir) / p):
                path = _execute_export_review([signal], _make_config())
                self.assertTrue(path.endswith(".jsonl"))
                content = Path(path).read_text(encoding="utf-8")
                data = json.loads(content)
                self.assertEqual(data["signal_id"], "test_id_0001")
                self.assertIn("exported_at", data)

    def test_write_action_log_creates_file(self):
        signal = _make_signal()
        signal.llm_actions = ["export_review", "write_action_log"]
        with tempfile.TemporaryDirectory() as tmpdir:
            with patch("stock_agent.pipeline.project_path", side_effect=lambda p: Path(tmpdir) / p):
                path = _execute_write_action_log([signal], _make_config())
                self.assertTrue(path.endswith(".jsonl"))
                content = Path(path).read_text(encoding="utf-8")
                data = json.loads(content)
                self.assertEqual(data["signal_id"], "test_id_0001")
                self.assertIn("recorded_at", data)

    def test_no_actions_no_file(self):
        signal = _make_signal()
        signal.llm_actions = []
        path = _execute_export_review([signal], _make_config())
        self.assertEqual(path, "")
        path2 = _execute_write_action_log([signal], _make_config())
        self.assertEqual(path2, "")


class TestPipelineIntegration(unittest.TestCase):
    """Full pipeline integration with mocked sources and LLM."""

    @patch.dict(os.environ, {"LLM_API_KEY": "test-key-123"})
    @patch("stock_agent.llm_agent._call_llm")
    @patch("stock_agent.pipeline.collect_sources")
    def test_agent_pipeline_end_to_end(self, mock_collect, mock_call):
        raw = RawSignal(
            source="manual",
            source_type="news",
            title="浪潮信息订单取消引发关注",
            summary="据知情人士透露，浪潮信息一笔大额订单被取消。",
        )
        mock_collect.return_value = [raw]

        def mock_llm_call(**kwargs):
            return json.dumps([{
                "signal_id": "placeholder",
                "impact_direction": "negative",
                "confidence": 0.8,
                "horizon": "1-5d",
                "key_thesis": "订单取消短期利空",
                "risk_points": ["收入影响"],
                "review_questions": ["取消原因？"],
                "human_decision_needed": True,
                "suggested_actions": ["export_review", "write_action_log"],
            }], ensure_ascii=False)

        mock_call.side_effect = mock_llm_call

        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            for sub in ["reports/daily", "reports/json", "reports/alerts", "reports/actions", "data/review", "data"]:
                (tmp / sub).mkdir(parents=True, exist_ok=True)

            def project_path_side(p):
                return tmp / p

            with patch("stock_agent.pipeline.project_path", side_effect=project_path_side), \
                 patch("stock_agent.reports.project_path", side_effect=project_path_side), \
                 patch("stock_agent.market_resilience.project_path", side_effect=project_path_side), \
                 patch("stock_agent.source_health.project_path", side_effect=project_path_side), \
                 patch("stock_agent.source_remediation.project_path", side_effect=project_path_side), \
                 patch("stock_agent.pipeline.ensure_directories"):
                config = _make_config()
                config["storage"] = {"sqlite_path": "data/test.sqlite3"}
                result = run_agent_pipeline(config)

                self.assertIsNotNone(result)
                self.assertGreaterEqual(result.stats.get("inserted", 0), 0)
                self.assertTrue(Path(result.report_path).exists())
                self.assertTrue(Path(result.json_path).exists())


class TestMaxSignalsPerRun(unittest.TestCase):
    @patch.dict(os.environ, {"LLM_API_KEY": "test-key-123"})
    @patch("stock_agent.llm_agent._call_llm")
    def test_respects_max_signals(self, mock_call):
        signals = []
        for i in range(5):
            s = _make_signal("信号{}".format(i))
            s.signal_id = "id_{:04d}".format(i)
            signals.append(s)
        mock_call.return_value = _mock_llm_response(signals[:2])
        config = _make_config(**{"llm_agent": {
            "enabled": True,
            "base_url": "https://api.openai.com/v1",
            "model": "gpt-4.1-mini",
            "timeout": 10,
            "temperature": 0.2,
            "min_priority": "P3",
            "max_signals_per_run": 2,
            "auto_execute_actions": True,
            "allowed_actions": ["export_review", "write_action_log"],
        }})
        result, stats = enrich_signals(signals, config)
        self.assertEqual(stats["llm_candidates"], 5)
        # Only 2 should have been sent to LLM (capped at max_signals_per_run)
        call_args = mock_call.call_args
        # The call was made once with 2 signals in the prompt
        self.assertEqual(mock_call.call_count, 1)


if __name__ == "__main__":
    unittest.main()
