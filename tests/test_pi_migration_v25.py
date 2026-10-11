import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from stock_agent.agent_handoff import (
    build_agent_handoff,
    check_latest_agent_handoff,
    run_pi_public_handoff,
    validate_agent_handoff,
    validate_pi_public_summary,
    write_agent_handoff,
)
from stock_agent.config import project_path_override
from stock_agent.model_operations import LLMResponse
from stock_agent.production_pilot import _critical_input_hashes


class PiMigrationV25Tests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        (self.root / "reports" / "json").mkdir(parents=True)

    def tearDown(self):
        self.temp_dir.cleanup()

    def _resolver(self, value):
        return self.root / str(value)

    def _signal(self):
        return {
            "signal_id": "sig-pi-public-1",
            "title": "上市公司披露公开合同",
            "summary": "交易所公开公告显示公司签署合同。",
            "source": "sse",
            "source_type": "announcement",
            "url": "https://www.sse.com.cn/example",
            "published_at": "2026-08-18T08:00:00+08:00",
            "priority": "P1",
            "topics": ["人工智能"],
            "symbols": ["600000"],
            "reasons": ["重大公开事件"],
            "llm_analysis": {
                "impact_direction": "positive",
                "confidence": 0.8,
                "horizon": "1-4w",
                "key_thesis": "合同提升订单可见度。",
                "risk_points": ["执行不确定性"],
                "review_questions": ["金额口径是否含税？"],
                "human_decision_needed": True,
                "grounding_status": "grounded",
            },
            "llm_governance": {
                "signal_llm": {
                    "classification": "public",
                    "decision": "approved",
                    "sensitive_categories": [],
                    "prompt_injection_detected": False,
                    "postflight": {"status": "grounded"},
                }
            },
        }

    def _write_report(self, signals):
        path = self.root / "reports" / "json" / "20260818T083000_0800.json"
        path.write_text(json.dumps({"signals": signals}, ensure_ascii=False), encoding="utf-8")
        return path

    def _config(self):
        return {
            "agent_handoff": {"enabled": True, "max_public_signals": 10},
            "pi_agent": {"enabled": True},
            "llm_agent": {
                "enabled": True,
                "runtime": "pi",
                "providers": [{
                    "name": "deepseek_primary",
                    "runtime": "pi",
                    "pi_provider": "deepseek",
                    "base_url": "https://api.deepseek.com/v1",
                    "model": "deepseek-v4-pro",
                    "api_key_env": "DEEPSEEK_API_KEY",
                }],
            },
        }

    def test_runtime_neutral_handoff_preserves_public_boundary(self):
        self._write_report([self._signal()])
        with project_path_override(self._resolver):
            payload = build_agent_handoff(self._config())
        self.assertEqual(payload["runtime"], "pi")
        self.assertEqual(payload["schema_version"], "25.0")
        self.assertEqual(payload["public_signal_count"], 1)
        self.assertEqual(validate_agent_handoff(payload), [])

    def test_writer_does_not_touch_openclaw_workspace(self):
        self._write_report([self._signal()])
        with project_path_override(self._resolver):
            write_agent_handoff(self._config())
        self.assertTrue((self.root / "reports" / "agent_handoff" / "latest.json").exists())
        self.assertTrue((self.root / "data" / "pi_runtime" / "public_handoff" / "handoff.json").exists())
        self.assertFalse((self.root / "openclaw").exists())

    def test_zero_public_signals_means_zero_pi_calls(self):
        self._write_report([])
        with project_path_override(self._resolver), patch("stock_agent.agent_handoff.call_pi_agent") as call:
            result = run_pi_public_handoff(self._config())
        self.assertFalse(result["called"])
        self.assertEqual(result["model_call_count"], 0)
        call.assert_not_called()

    def test_read_only_handoff_check_validates_complete_zero_call_chain(self):
        self._write_report([])
        with project_path_override(self._resolver), patch("stock_agent.agent_handoff.call_pi_agent") as call:
            run_pi_public_handoff(self._config())
            before_handoff = (self.root / "reports" / "agent_handoff" / "latest.json").read_bytes()
            before_result = (self.root / "reports" / "pi_agent" / "public_handoff" / "latest.json").read_bytes()
            status = check_latest_agent_handoff()
            after_handoff = (self.root / "reports" / "agent_handoff" / "latest.json").read_bytes()
            after_result = (self.root / "reports" / "pi_agent" / "public_handoff" / "latest.json").read_bytes()
        self.assertEqual(status["status"], "READY")
        self.assertFalse(status["called"])
        self.assertEqual(status["model_call_count"], 0)
        self.assertEqual(before_handoff, after_handoff)
        self.assertEqual(before_result, after_result)
        call.assert_not_called()

    def test_read_only_handoff_check_rejects_mismatched_result(self):
        self._write_report([])
        with project_path_override(self._resolver):
            run_pi_public_handoff(self._config())
            result_path = self.root / "reports" / "pi_agent" / "public_handoff" / "latest.json"
            result = json.loads(result_path.read_text(encoding="utf-8"))
            result["handoff_payload_sha256"] = "0" * 64
            result_path.write_text(json.dumps(result), encoding="utf-8")
            status = check_latest_agent_handoff()
        self.assertEqual(status["status"], "BLOCKED")
        self.assertIn("Pi result handoff hash mismatch", status["errors"])

    def test_governed_public_signal_calls_pi_once_and_validates_output(self):
        self._write_report([self._signal()])
        output = json.dumps({
            "summary": "公开合同可能提升订单可见度，仍需核验执行条款。",
            "signal_ids": ["sig-pi-public-1"],
            "evidence_limitations": ["仅有公开公告摘要"],
            "human_review_questions": ["核验合同金额和执行周期"],
            "human_decision_required": True,
        }, ensure_ascii=False)
        response = LLMResponse(output, {
            "request_id": "req-pi-1",
            "model": "deepseek-v4-pro",
            "finish_reason": "stop",
            "usage": {"prompt_tokens": 10, "completion_tokens": 8, "total_tokens": 18},
        })
        with project_path_override(self._resolver), patch.dict(os.environ, {
            "DEEPSEEK_API_KEY": "test-key-not-real",
        }, clear=False), patch("stock_agent.agent_handoff.call_pi_agent", return_value=response) as call:
            result = run_pi_public_handoff(self._config())
        self.assertTrue(result["called"])
        self.assertEqual(result["model_call_count"], 1)
        self.assertEqual(result["analysis"]["signal_ids"], ["sig-pi-public-1"])
        call.assert_called_once()

    def test_public_summary_rejects_signal_substitution_and_trading(self):
        handoff = {"signals": [{"signal_id": "s1"}]}
        summary = {
            "summary": "立即买入",
            "signal_ids": ["other"],
            "evidence_limitations": [],
            "human_review_questions": [],
            "human_decision_required": True,
        }
        errors = validate_pi_public_summary(summary, handoff)
        self.assertTrue(any("signal_ids" in item for item in errors))
        self.assertTrue(any("prohibited action" in item for item in errors))

    def test_public_summary_rejects_unexpected_or_oversized_fields(self):
        handoff = {"signals": [{"signal_id": "s1"}]}
        summary = {
            "summary": "x" * 2001,
            "signal_ids": ["s1"],
            "evidence_limitations": [],
            "human_review_questions": [],
            "human_decision_required": True,
            "trade_plan": "none",
        }
        errors = validate_pi_public_summary(summary, handoff)
        self.assertTrue(any("unexpected fields" in item for item in errors))
        self.assertTrue(any("2000" in item for item in errors))

    def test_scheduled_runner_has_no_openclaw_execution(self):
        script = (Path(__file__).resolve().parents[1] / "scripts" / "run_daily_research.ps1").read_text(encoding="utf-8")
        self.assertIn('Run-Step -Name "pi-handoff-run"', script)
        self.assertIn("agent_handoff_public_signal_count", script)
        self.assertNotIn("openclaw agent", script.lower())
        self.assertNotIn('Run-Step -Name "openclaw-handoff"', script)

    def test_go_live_check_validates_pi_without_running_model(self):
        script = (Path(__file__).resolve().parents[1] / "scripts" / "go_live_check.ps1").read_text(encoding="utf-8")
        self.assertIn('Run-Step -Name "pi_runtime_validation"', script)
        self.assertIn('Run-Step -Name "pi_handoff_validation"', script)
        self.assertIn("pi-runtime-status", script)
        self.assertIn("agent-handoff", script)
        self.assertIn("--check", script)
        self.assertNotIn("pi-handoff-run", script)
        self.assertNotIn("openclaw_validation", script)
        self.assertNotIn("validate_openclaw_stock_agent", script)

    def test_pi_policy_changes_invalidate_production_candidate_fingerprint(self):
        base = {
            "pi_agent": {"version": "0.84.2", "enabled": True},
            "agent_handoff": {"enabled": True, "max_public_signals": 10},
            "llm_agent": {"runtime": "pi"},
        }
        changed = json.loads(json.dumps(base))
        changed["pi_agent"]["version"] = "0.84.3"
        with project_path_override(self._resolver):
            first = _critical_input_hashes(base, "config.json", {"providers": {}}, {})
            second = _critical_input_hashes(changed, "config.json", {"providers": {}}, {})
        self.assertNotEqual(first["critical_config"], second["critical_config"])

    def test_migration_and_retirement_scripts_are_fail_closed(self):
        root = Path(__file__).resolve().parents[1]
        migrate = (root / "scripts" / "migrate_to_pi_agent.ps1").read_text(encoding="utf-8")
        rollback = (root / "scripts" / "rollback_pi_agent.ps1").read_text(encoding="utf-8")
        retire = (root / "scripts" / "retire_openclaw_stock_agent.ps1").read_text(encoding="utf-8")
        self.assertIn("--ignore-scripts", migrate)
        self.assertIn("registry.npmjs.org", migrate)
        self.assertIn("openclaw_registry_modified = $false", migrate)
        self.assertIn("llm_agent.enabled = $false", rollback)
        self.assertIn("openclaw_handoff.enabled = $false", rollback)
        self.assertNotIn("enabled = $true", rollback)
        self.assertIn('id -like "stock_agent_*"', retire)
        self.assertIn('id -notlike "stock_agent_*"', retire)


if __name__ == "__main__":
    unittest.main()
