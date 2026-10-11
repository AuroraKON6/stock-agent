import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from stock_agent.config import project_path_override
from stock_agent.models import AnalyzedSignal, now_iso


def _signal(signal_id="s001", priority="P1", score=80):
    return AnalyzedSignal(
        signal_id=signal_id,
        source="official",
        source_type="filing",
        title="Material public filing",
        summary="Public evidence summary",
        url="https://example.test/filing",
        published_at=now_iso(),
        collected_at=now_iso(),
        priority=priority,
        priority_score=score,
    )


def _config():
    return {
        "model_operations": {
            "enabled": True,
            "report_dir": "reports/model_operations",
            "usage_dir": "data/model_operations/usage",
            "service_levels": {"agent_target_seconds": 180, "scheduled_delivery_target_seconds": 300},
            "deadline_policy": {
                "post_llm_reserve_seconds": 45,
                "main_min_start_remaining_seconds": 75,
                "main_reduce_below_seconds": 105,
                "reduced_main_max_signals": 1,
                "role_min_start_remaining_seconds": 75,
                "role_reduce_below_seconds": 120,
                "reduced_role_max_signals": 1,
                "retry_min_remaining_seconds": 75,
            },
            "quality_thresholds": {
                "main_success_rate_min": 0.95,
                "role_success_rate_min": 0.9,
                "usable_grounding_rate_min": 0.95,
                "fully_grounded_rate_min": 0.75,
                "usage_metadata_rate_min": 1.0,
            },
            "pricing": {
                "enabled": True,
                "currency": "USD",
                "effective_date": "2026-08-12",
                "source_url": "https://api-docs.deepseek.com/quick_start/pricing",
                "models": {
                    "deepseek-v4-pro": {
                        "input_cache_hit_per_1m": 0.003625,
                        "input_cache_miss_per_1m": 0.435,
                        "output_per_1m": 0.87,
                    }
                },
            },
        },
        "llm_agent": {
            "enabled": True,
            "base_url": "https://api.deepseek.com/v1",
            "model": "deepseek-v4-pro",
            "min_priority": "P2",
            "max_signals_per_run": 10,
        },
        "multi_role_agent": {
            "enabled": True,
            "base_url": "https://api.deepseek.com/v1",
            "model": "deepseek-v4-pro",
            "min_priority": "P2",
            "max_signals_per_run": 3,
        },
    }


class FakeOps:
    def __init__(self, elapsed):
        self.elapsed = elapsed
        self.degradations = []

    def elapsed_seconds(self):
        return self.elapsed

    def record_degradation(self, component, reason, severity="WARN", detail=""):
        self.degradations.append((component, reason, severity, detail))


class FakeResponse:
    def __init__(self, body):
        self.body = json.dumps(body).encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def read(self):
        return self.body


class ResponseTelemetryTests(unittest.TestCase):
    def test_chat_completion_returns_string_compatible_metadata_without_prompt_or_key(self):
        from stock_agent.llm_agent import _call_llm

        captured = {}
        body = {
            "id": "req-123",
            "model": "deepseek-v4-pro",
            "system_fingerprint": "fp-a",
            "choices": [{"finish_reason": "stop", "message": {"content": '{"results": []}'}}],
            "usage": {
                "prompt_tokens": 100,
                "completion_tokens": 20,
                "total_tokens": 120,
                "prompt_cache_hit_tokens": 80,
                "prompt_cache_miss_tokens": 20,
                "completion_tokens_details": {"reasoning_tokens": 7},
            },
        }

        def fake_urlopen(request, timeout):
            captured["payload"] = json.loads(request.data.decode("utf-8"))
            captured["authorization"] = request.headers.get("Authorization")
            return FakeResponse(body)

        with patch("stock_agent.llm_agent.urllib.request.urlopen", side_effect=fake_urlopen):
            response = _call_llm(
                "https://api.deepseek.com/v1",
                "deepseek-v4-pro",
                "secret-key",
                30,
                0.2,
                "private prompt must not enter telemetry",
                request_options={
                    "json_mode": True,
                    "thinking": "enabled",
                    "reasoning_effort": "high",
                    "max_tokens": 2048,
                    "user_id": "stock-agent-test",
                },
            )

        self.assertIsInstance(response, str)
        self.assertEqual(response.metadata["request_id"], "req-123")
        self.assertEqual(response.metadata["usage"]["reasoning_tokens"], 7)
        self.assertNotIn("private prompt", json.dumps(response.metadata))
        self.assertNotIn("secret-key", json.dumps(response.metadata))
        self.assertEqual(captured["payload"]["response_format"], {"type": "json_object"})
        self.assertEqual(captured["payload"]["thinking"], {"type": "enabled"})
        self.assertEqual(captured["payload"]["user_id"], "stock-agent-test")
        self.assertEqual(captured["authorization"], "Bearer secret-key")

    def test_empty_sent_container_is_a_valid_evidence_reference(self):
        from stock_agent.llm_governance import _flatten_evidence_refs

        refs = _flatten_evidence_refs({"signal_id": "s001", "symbols": [], "evidence": {}})
        self.assertIn("symbols", refs)
        self.assertIn("evidence", refs)


class PriceSnapshotIdentityTests(unittest.TestCase):
    def _quote(self, source="eastmoney", published_at="2026-08-13T09:30:00+08:00", **changes):
        from stock_agent.models import RawSignal

        payload = {
            "code": "600519",
            "name": "贵州茅台",
            "latest": 1500.0,
            "pct_change": 3.2,
            "price_change": 46.5,
            "amount": 123456789.0,
            "main_net_inflow": 120000000.0,
            "raw": {"f5": 81234, "f15": 1510, "f16": 1450, "f17": 1460, "f18": 1453.5},
        }
        payload.update(changes)
        return RawSignal(
            source=source,
            source_type="price",
            title="贵州茅台行情快照" if source == "eastmoney" else "600519 cached quote",
            summary="provider-specific presentation",
            published_at=published_at,
            payload=payload,
        )

    def test_identical_quote_is_same_across_provider_cache_and_fetch_time(self):
        from stock_agent.analysis import signal_id

        direct = self._quote()
        cached = self._quote(source="market_cache", published_at="2026-08-13T10:00:00+08:00")
        cached.payload["source_evidence"] = {"provider": "market_cache", "age_seconds": 120}
        self.assertEqual(signal_id(direct), signal_id(cached))

    def test_material_quote_change_creates_new_snapshot(self):
        from stock_agent.analysis import signal_id

        original = self._quote()
        changed = self._quote(latest=1501.0, amount=124000000.0)
        self.assertNotEqual(signal_id(original), signal_id(changed))

    def test_price_without_structured_quote_uses_legacy_identity(self):
        from stock_agent.analysis import signal_id
        from stock_agent.models import RawSignal

        first = RawSignal(source="one", source_type="price", title="unstructured", published_at="a")
        second = RawSignal(source="two", source_type="price", title="unstructured", published_at="b")
        self.assertNotEqual(signal_id(first), signal_id(second))


class DeadlinePolicyTests(unittest.TestCase):
    def test_role_batch_is_reduced_and_highest_priority_is_selected(self):
        from stock_agent.model_operations import select_deadline_candidates

        signals = [_signal("p2", "P2", 30), _signal("p0", "P0", 95), _signal("p1", "P1", 70)]
        selected, skipped, decision = select_deadline_candidates(signals, _config(), FakeOps(80), "role_llm")
        self.assertEqual([item.signal_id for item in selected], ["p0"])
        self.assertEqual(len(skipped), 2)
        self.assertEqual(decision["action"], "reduce")

    def test_role_batch_is_skipped_before_delivery_reserve_is_consumed(self):
        from stock_agent.model_operations import select_deadline_candidates

        selected, skipped, decision = select_deadline_candidates([_signal()], _config(), FakeOps(110), "role_llm")
        self.assertEqual(selected, [])
        self.assertEqual(len(skipped), 1)
        self.assertEqual(decision["action"], "skip")

    @patch.dict(os.environ, {"DEEPSEEK_API_KEY": "test-key"}, clear=False)
    @patch("stock_agent.llm_agent._call_llm")
    def test_main_agent_deadline_skip_does_not_call_provider(self, mock_call):
        from stock_agent.llm_agent import enrich_signals

        signal = _signal()
        signals, stats = enrich_signals([signal], _config(), ops=FakeOps(110))
        mock_call.assert_not_called()
        self.assertEqual(stats["llm_skipped_deadline"], 1)
        self.assertTrue(signals[0].needs_review)
        self.assertIn("delivery deadline", signals[0].llm_error)

    @patch.dict(os.environ, {"DEEPSEEK_API_KEY": "test-key"}, clear=False)
    @patch("stock_agent.role_analyst._call_role_llm")
    def test_role_agent_deadline_skip_does_not_call_provider(self, mock_call):
        from stock_agent.role_analyst import enrich_signals_with_role_analysts

        signal = _signal()
        signals, stats = enrich_signals_with_role_analysts([signal], _config(), ops=FakeOps(110))
        mock_call.assert_not_called()
        self.assertEqual(stats["role_llm_skipped_deadline"], 1)
        self.assertTrue(signals[0].needs_review)
        self.assertIn("delivery deadline", signals[0].role_analysis_error)


class SnapshotTests(unittest.TestCase):
    def _run_ops(self, finish_reason="stop", include_usage=True):
        usage = {
            "prompt_tokens": 1000,
            "completion_tokens": 500,
            "total_tokens": 1500,
            "prompt_cache_hit_tokens": 600,
            "prompt_cache_miss_tokens": 400,
            "reasoning_tokens": 200,
        } if include_usage else {}
        return {
            "run_id": "run-v22",
            "mode": "agent-run",
            "elapsed_ms": 60000,
            "llm_calls": [{
                "component": "signal_llm",
                "provider": "deepseek_primary",
                "model": "deepseek-v4-pro",
                "request_id": "req-1",
                "attempt": 1,
                "item_count": 1,
                "success": True,
                "duration_ms": 12000,
                "finish_reason": finish_reason,
                "usage": usage,
            }],
        }

    def test_snapshot_scores_quality_usage_cost_and_safety(self):
        from stock_agent.model_operations import build_model_operations_snapshot

        signal = _signal()
        signal.llm_analysis = {"grounding_status": "grounded"}
        signal.role_analyses = [{"grounding_status": "grounded"}]
        snapshot = build_model_operations_snapshot(
            [signal], {"llm_selected": 1, "llm_success": 1, "role_llm_selected": 1, "role_llm_success": 1},
            self._run_ops(), _config(),
        )
        self.assertEqual(snapshot["status"], "PASS")
        self.assertEqual(snapshot["usage"]["total_tokens"], 1500)
        self.assertGreater(snapshot["estimated_cost"]["amount"], 0)
        self.assertFalse(snapshot["human_control"]["automatic_trade"])
        self.assertFalse(snapshot["human_control"]["automatic_assignment"])
        serialized = json.dumps(snapshot)
        self.assertNotIn("raw_prompt", serialized)
        self.assertNotIn("reasoning_content", serialized)

    def test_missing_usage_metadata_is_attention_not_pipeline_failure(self):
        from stock_agent.model_operations import build_model_operations_snapshot

        signal = _signal()
        signal.llm_analysis = {"grounding_status": "grounded"}
        snapshot = build_model_operations_snapshot(
            [signal], {"llm_selected": 1, "llm_success": 1, "role_llm_selected": 0, "role_llm_success": 0},
            self._run_ops(include_usage=False), _config(),
        )
        self.assertEqual(snapshot["status"], "ATTENTION")
        self.assertEqual(snapshot["quality"]["usage_metadata_rate"], 0)

    def test_non_stop_finish_reason_is_quality_breach(self):
        from stock_agent.model_operations import build_model_operations_snapshot

        snapshot = build_model_operations_snapshot([], {}, self._run_ops(finish_reason="length"), _config())
        self.assertEqual(snapshot["status"], "BREACH")
        self.assertTrue(any(item["name"] == "finish_reason" for item in snapshot["checks"]))

    def test_writer_creates_latest_timestamped_and_append_only_usage(self):
        from stock_agent.model_operations import write_model_operations_summary

        signal = _signal()
        signal.llm_analysis = {"grounding_status": "grounded"}
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            with project_path_override(lambda value: root / value):
                result = write_model_operations_summary(
                    [signal], {"llm_selected": 1, "llm_success": 1}, self._run_ops(), _config()
                )
            latest = Path(result["model_operations_latest_json_path"])
            usage_path = Path(result["model_operations_usage_path"])
            self.assertTrue(latest.exists())
            self.assertTrue(Path(result["model_operations_latest_path"]).exists())
            self.assertTrue(usage_path.exists())
            usage_row = json.loads(usage_path.read_text(encoding="utf-8").splitlines()[0])
            self.assertEqual(usage_row["request_id"], "req-1")
            self.assertNotIn("prompt_content", usage_row)
            self.assertNotIn("api_key", usage_row)


class SurfaceIntegrationTests(unittest.TestCase):
    def test_dashboard_renders_model_quality_and_scheduled_slo(self):
        from stock_agent.dashboard import _section_model_operations

        snapshot = {
            "status": "PASS",
            "quality": {"main_success": 1, "main_selected": 1, "role_success": 1, "role_selected": 1,
                        "grounding": {"grounded": 5, "partial": 0, "blocked": 0, "usable_rate": 1}},
            "usage": {"prompt_tokens": 10, "completion_tokens": 5, "reasoning_tokens": 2, "total_tokens": 15},
            "estimated_cost": {"amount": 0.01, "currency": "USD"},
            "checks": [{"name": "agent_delivery_seconds", "status": "PASS", "actual": 60, "target": 180}],
            "deadline_decisions": {},
            "rolling": {"window_runs": 3, "status_counts": {"PASS": 2, "IDLE": 1},
                        "total_tokens": 1500, "estimated_cost": 0.012},
        }
        html = _section_model_operations(snapshot, {
            "duration_seconds": 220, "scheduled_delivery_target_seconds": 300, "scheduled_slo_status": "PASS"
        })
        self.assertIn("Model Operations And Quality", html)
        self.assertIn("220", html)
        self.assertIn("no automatic trading", html)
        self.assertIn("rolling window", html)
        self.assertIn("1500", html)

    def test_dashboard_reads_powershell_utf8_bom_scheduled_summary(self):
        from stock_agent.dashboard import _load_last_scheduled_run

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            log_dir = root / "logs" / "scheduled"
            log_dir.mkdir(parents=True)
            summary = {
                "duration_seconds": 77.649,
                "scheduled_delivery_target_seconds": 300,
                "scheduled_slo_status": "PASS",
            }
            (log_dir / "run_20260813T011533.summary.json").write_text(
                json.dumps(summary), encoding="utf-8-sig"
            )
            with project_path_override(lambda value: root / value):
                loaded = _load_last_scheduled_run()
        self.assertEqual(loaded["scheduled_slo_status"], "PASS")
        self.assertEqual(loaded["duration_seconds"], 77.649)

    def test_doctor_rejects_sensitive_model_telemetry(self):
        from stock_agent.doctor import _check_model_operations

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            target = root / "reports/model_operations/latest.json"
            target.parent.mkdir(parents=True)
            target.write_text(json.dumps({
                "status": "PASS",
                "human_control": {"automatic_trade": False, "automatic_assignment": False,
                                  "automatic_approval": False, "automatic_publication": False},
                "raw_prompt": "must never be persisted",
            }), encoding="utf-8")
            with project_path_override(lambda value: root / value):
                status, detail, _ = _check_model_operations(_config())
        self.assertEqual(status, "FAIL")
        self.assertIn("raw_prompt", detail)

    def test_research_runtime_summary_carries_model_gate(self):
        from stock_agent.research_runtime import summarize_research_runtime

        summary = summarize_research_runtime({
            "runtime_version": "test",
            "policy": {},
            "cases": [],
            "model_operations": {"status": "ATTENTION", "run_id": "run-v22"},
        })
        self.assertEqual(summary["model_operations"]["status"], "ATTENTION")

    def test_scheduled_runner_records_step_and_delivery_durations(self):
        script = Path("scripts/run_daily_research.ps1").read_text(encoding="utf-8")
        self.assertIn("duration_seconds", script)
        self.assertIn("scheduled_delivery_target_seconds", script)
        self.assertIn("scheduled_slo_status", script)
        self.assertEqual(script.count('Run-Step -Name "agent-run"'), 1)


if __name__ == "__main__":
    unittest.main()
