import json
import tempfile
import unittest
from pathlib import Path

from stock_agent.config import project_path_override
from stock_agent.doctor import _check_openclaw_handoff
from stock_agent.dashboard import _load_last_scheduled_run
from stock_agent.openclaw_handoff import (
    build_openclaw_handoff,
    validate_openclaw_handoff,
    write_openclaw_handoff,
)


class OpenClawPublicHandoffV24Tests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        (self.root / "reports" / "json").mkdir(parents=True)

    def tearDown(self):
        self.temp_dir.cleanup()

    def _resolver(self, value):
        return self.root / str(value)

    def _signal(self, **updates):
        signal = {
            "signal_id": "sig-public-1",
            "title": "上市公司发布重大合同公告",
            "summary": "公司在交易所公告中披露已签署公开合同。",
            "source": "sse",
            "source_type": "announcement",
            "url": "https://www.sse.com.cn/example",
            "published_at": "2026-08-18T08:00:00+08:00",
            "priority": "P1",
            "topics": ["人工智能"],
            "symbols": ["600000"],
            "reasons": ["重大公开事件"],
            "payload": {"raw_internal_field": "must-not-leave"},
            "llm_analysis": {
                "impact_direction": "positive",
                "confidence": 0.81,
                "horizon": "1-4w",
                "key_thesis": "公开合同可能改善未来订单可见度。",
                "risk_points": ["合同执行仍有不确定性"],
                "review_questions": ["公告金额口径是否含税？"],
                "human_decision_needed": True,
                "grounding_status": "grounded",
                "private_chain_of_thought": "must-not-leave",
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
        signal.update(updates)
        return signal

    def _write_report(self, signals):
        path = self.root / "reports" / "json" / "20260818T083000_0800.json"
        path.write_text(json.dumps({"signals": signals}, ensure_ascii=False), encoding="utf-8")
        return path

    def test_only_governance_approved_public_fields_leave_boundary(self):
        self._write_report([self._signal()])
        with project_path_override(self._resolver):
            payload = build_openclaw_handoff({"openclaw_handoff": {"enabled": True}})
        self.assertEqual(payload["public_signal_count"], 1)
        self.assertTrue(payload["external_llm_call_required"])
        serialized = json.dumps(payload, ensure_ascii=False)
        self.assertNotIn("raw_internal_field", serialized)
        self.assertNotIn("private_chain_of_thought", serialized)
        self.assertEqual(validate_openclaw_handoff(payload), [])

    def test_denied_injected_ungrounded_and_blocked_price_signals_are_excluded(self):
        denied = self._signal(signal_id="denied")
        denied["llm_governance"]["signal_llm"]["decision"] = "denied"
        injected = self._signal(signal_id="injected")
        injected["llm_governance"]["signal_llm"]["prompt_injection_detected"] = True
        ungrounded = self._signal(signal_id="ungrounded")
        ungrounded["llm_governance"]["signal_llm"]["postflight"] = {"status": "blocked"}
        blocked_price = self._signal(
            signal_id="price",
            source_type="price",
            payload={"market_data_governance": {"enabled": True, "llm_eligible": False}},
        )
        self._write_report([denied, injected, ungrounded, blocked_price])
        with project_path_override(self._resolver):
            payload = build_openclaw_handoff({})
        self.assertEqual(payload["public_signal_count"], 0)
        self.assertFalse(payload["external_llm_call_required"])
        self.assertEqual(payload["excluded_signal_count"], 4)
        self.assertEqual(payload["excluded_reason_counts"]["not_governance_approved_public"], 1)
        self.assertEqual(payload["excluded_reason_counts"]["governance_sensitive_or_injection"], 1)
        self.assertEqual(payload["excluded_reason_counts"]["ungrounded_output"], 1)
        self.assertEqual(payload["excluded_reason_counts"]["market_data_governance_blocked"], 1)

    def test_secret_local_path_and_prohibited_trade_instruction_are_excluded(self):
        secret = self._signal(signal_id="secret", summary="token sk-1234567890abcdef")
        local_path = self._signal(signal_id="path", summary=r"证据位于 E:\private\memo.pdf")
        trade = self._signal(signal_id="trade", summary="请立即买入并自动下单")
        self._write_report([secret, local_path, trade])
        with project_path_override(self._resolver):
            payload = build_openclaw_handoff({})
        self.assertEqual(payload["public_signal_count"], 0)
        self.assertEqual(payload["excluded_reason_counts"]["sensitive_or_unsafe_output"], 3)

    def test_limit_is_audited_instead_of_silently_dropping_signals(self):
        signals = [self._signal(signal_id="sig-{0}".format(index)) for index in range(3)]
        self._write_report(signals)
        with project_path_override(self._resolver):
            payload = build_openclaw_handoff({"openclaw_handoff": {"max_public_signals": 1}})
        self.assertEqual(payload["public_signal_count"], 1)
        self.assertEqual(payload["excluded_reason_counts"]["max_public_signals_reached"], 2)

    def test_disabled_handoff_never_requires_external_llm(self):
        self._write_report([self._signal()])
        with project_path_override(self._resolver):
            payload = build_openclaw_handoff({"openclaw_handoff": {"enabled": False}})
        self.assertFalse(payload["external_llm_call_required"])
        self.assertEqual(payload["call_decision_reason"], "handoff_disabled")

    def test_writer_creates_identical_audit_and_isolated_workspace_payloads(self):
        self._write_report([self._signal()])
        with project_path_override(self._resolver):
            payload = write_openclaw_handoff({})
        latest = json.loads((self.root / "reports" / "handoff" / "latest.json").read_text(encoding="utf-8"))
        isolated = json.loads((self.root / "openclaw" / "public_handoff_workspace" / "handoff.json").read_text(encoding="utf-8"))
        self.assertEqual(latest, isolated)
        self.assertEqual(latest, payload)
        self.assertNotIn(str(self.root), json.dumps(latest))

    def test_payload_tampering_fails_integrity_validation(self):
        self._write_report([self._signal()])
        with project_path_override(self._resolver):
            payload = build_openclaw_handoff({})
        payload["signals"][0]["summary"] = "tampered"
        self.assertIn("payload hash mismatch", validate_openclaw_handoff(payload))

    def test_doctor_fails_if_zero_public_signals_still_invoked_openclaw(self):
        self._write_report([])
        with project_path_override(self._resolver):
            write_openclaw_handoff({})
        scheduled = self.root / "logs" / "scheduled"
        scheduled.mkdir(parents=True)
        (scheduled / "run_20260818T083000.summary.json").write_text(json.dumps({
            "mode": "daily",
            "used_openclaw": True,
            "openclaw_handoff_public_signal_count": 0,
        }), encoding="utf-8")
        with project_path_override(self._resolver):
            status, detail, _ = _check_openclaw_handoff({})
        self.assertEqual(status, "FAIL")
        self.assertIn("zero governance-approved public signals", detail)

    def test_openclaw_manifest_is_legacy_and_scheduled_runner_uses_pi(self):
        root = Path(__file__).resolve().parents[1]
        script = (root / "scripts" / "run_daily_research.ps1").read_text(encoding="utf-8")
        registry = json.loads((root / "openclaw" / "stock_agent_project" / "registry_export.json").read_text(encoding="utf-8"))
        agent = next(item for item in registry["agents_to_register"] if item["id"] == "stock_agent_public_handoff")
        self.assertNotIn('Run-Step -Name "openclaw-handoff"', script)
        self.assertNotIn("openclaw agent", script.lower())
        self.assertIn('Run-Step -Name "pi-handoff-run"', script)
        self.assertNotIn("--agent stock_agent_coordinator --local --timeout 300 --message", script)
        self.assertEqual(agent["workspace"], r"E:\stock agent\openclaw\public_handoff_workspace")
        self.assertEqual(agent["tools"]["allow"], ["read"])
        self.assertFalse("write" in agent["tools"]["allow"])

    def test_dashboard_selects_latest_daily_run_for_handoff_evidence(self):
        scheduled = self.root / "logs" / "scheduled"
        scheduled.mkdir(parents=True)
        (scheduled / "run_20260818T083000.summary.json").write_text(json.dumps({
            "mode": "daily", "openclaw_handoff_public_signal_count": 0,
        }), encoding="utf-8")
        (scheduled / "run_20260818T084500.summary.json").write_text(json.dumps({
            "mode": "pilot", "openclaw_skip_reason": "not_applicable_for_mode",
        }), encoding="utf-8")
        with project_path_override(self._resolver):
            selected = _load_last_scheduled_run("daily")
        self.assertEqual(selected["mode"], "daily")


if __name__ == "__main__":
    unittest.main()
