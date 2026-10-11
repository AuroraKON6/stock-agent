import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from stock_agent.audit_log import load_hash_chained_jsonl
from stock_agent.config import project_path_override
from stock_agent.model_operations import LLMResponse
from stock_agent.pi_runtime import PiRuntimeError, call_pi_agent, parse_pi_json_stream, run_pi_runtime_uat


def _pi_stream(content='{"status":"ok"}', *, tool=False, stop_reason="stop"):
    events = [
        {"type": "session", "version": 3, "id": "session-uat", "cwd": "isolated"},
        {"type": "agent_start"},
    ]
    if tool:
        events.append({"type": "tool_execution_start", "toolCallId": "t1", "toolName": "read", "args": {}})
    events.append({
        "type": "message_end",
        "message": {
            "role": "assistant",
            "content": [{"type": "text", "text": content}],
            "provider": "deepseek",
            "model": "deepseek-v4-pro",
            "responseId": "request-uat",
            "usage": {"input": 12, "output": 7, "cacheRead": 2, "totalTokens": 19},
            "stopReason": stop_reason,
        },
    })
    events.append({"type": "agent_end", "messages": []})
    return "\n".join(json.dumps(item) for item in events) + "\n"


class PiRuntimeV25Tests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        settings = self.root / "pi_runtime" / "agent_home" / "settings.json"
        settings.parent.mkdir(parents=True)
        settings.write_text(json.dumps({
            "defaultProjectTrust": "never",
            "defaultTools": [],
            "packages": [],
            "extensions": [],
            "enableInstallTelemetry": False,
        }), encoding="utf-8")
        self.config = {
            "pi_agent": {
                "enabled": True,
                "version": "0.84.2",
                "agent_home": "pi_runtime/agent_home",
                "workspace": "data/pi_runtime/workspace",
                "audit_dir": "logs/pi_runtime",
            }
        }
        self.provider = {
            "name": "deepseek_primary",
            "runtime": "pi",
            "pi_provider": "deepseek",
            "model": "deepseek-v4-pro",
            "api_key_env": "DEEPSEEK_API_KEY",
            "thinking": "high",
        }

    def tearDown(self):
        self.temp_dir.cleanup()

    def _resolver(self, value):
        return self.root / str(value)

    def test_json_stream_extracts_final_text_and_usage(self):
        parsed = parse_pi_json_stream(_pi_stream())
        self.assertEqual(parsed["content"], '{"status":"ok"}')
        self.assertEqual(parsed["model"], "deepseek-v4-pro")
        self.assertEqual(parsed["tool_call_count"], 0)
        self.assertEqual(parsed["usage"]["totalTokens"], 19)

    def test_non_json_stdout_is_rejected(self):
        with self.assertRaises(PiRuntimeError):
            parse_pi_json_stream("startup chatter\n" + _pi_stream())

    def test_call_is_no_tool_isolated_and_hash_audited(self):
        captured = {}

        def fake_run(args, *, cwd, env, stdin_text, timeout):
            captured.update({"args": args, "cwd": cwd, "env": env, "stdin": stdin_text, "timeout": timeout})
            return 0, _pi_stream(), ""

        with project_path_override(self._resolver), patch.dict(os.environ, {
            "DEEPSEEK_API_KEY": "test-key-not-real",
            "UNRELATED_SECRET": "must-not-reach-child",
        }, clear=False), patch("stock_agent.pi_runtime.resolve_pi_command", return_value=(["pi"], None)), patch(
            "stock_agent.pi_runtime._run_process", side_effect=fake_run
        ):
            response = call_pi_agent(
                system_prompt="Return JSON only.",
                user_content="synthetic public payload",
                provider=self.provider,
                config=self.config,
                role_id="signal_analyst",
                timeout=12,
            )

        self.assertEqual(str(response), '{"status":"ok"}')
        self.assertIn("--no-tools", captured["args"])
        self.assertIn("--no-session", captured["args"])
        self.assertIn("--no-approve", captured["args"])
        self.assertIn("--no-extensions", captured["args"])
        self.assertIn("--no-skills", captured["args"])
        self.assertIn("--no-prompt-templates", captured["args"])
        self.assertIn("--no-themes", captured["args"])
        self.assertIn("--no-context-files", captured["args"])
        self.assertEqual(captured["env"]["DEEPSEEK_API_KEY"], "test-key-not-real")
        self.assertNotIn("UNRELATED_SECRET", captured["env"])
        audit_files = list((self.root / "logs" / "pi_runtime").glob("calls_*.jsonl"))
        self.assertEqual(len(audit_files), 1)
        audit = load_hash_chained_jsonl(audit_files[0])
        self.assertEqual(audit["integrity_status"], "OK")
        self.assertEqual(audit["record_count"], 1)
        serialized = audit_files[0].read_text(encoding="utf-8")
        self.assertNotIn("synthetic public payload", serialized)
        self.assertNotIn("test-key-not-real", serialized)

    def test_tool_event_discards_model_output(self):
        with project_path_override(self._resolver), patch.dict(os.environ, {
            "DEEPSEEK_API_KEY": "test-key-not-real",
        }, clear=False), patch("stock_agent.pi_runtime.resolve_pi_command", return_value=(["pi"], None)), patch(
            "stock_agent.pi_runtime._run_process", return_value=(0, _pi_stream(tool=True), "")
        ):
            with self.assertRaisesRegex(PiRuntimeError, "tool call"):
                call_pi_agent(
                    system_prompt="Return JSON only.",
                    user_content="synthetic",
                    provider=self.provider,
                    config=self.config,
                )

    def test_missing_key_fails_before_process_start(self):
        with project_path_override(self._resolver), patch.dict(os.environ, {"DEEPSEEK_API_KEY": ""}, clear=False), patch(
            "stock_agent.pi_runtime.resolve_pi_command", return_value=(["pi"], None)
        ), patch("stock_agent.pi_runtime._run_process") as run:
            with self.assertRaisesRegex(PiRuntimeError, "DEEPSEEK_API_KEY"):
                call_pi_agent(
                    system_prompt="Return JSON only.",
                    user_content="synthetic",
                    provider=self.provider,
                    config=self.config,
                )
        run.assert_not_called()

    def test_live_uat_requires_exact_safe_contract_and_writes_proof(self):
        response = LLMResponse(
            '{"status":"ok","runtime":"pi","human_decision_required":true}',
            {
                "request_id": "req-uat",
                "model": "deepseek-v4-pro",
                "usage": {"prompt_tokens": 5, "completion_tokens": 4, "total_tokens": 9},
            },
        )
        ready = {
            "runtime_ready": True,
            "installed_version": "0.84.2",
            "package_lock_sha256": "a" * 64,
        }
        with project_path_override(self._resolver), patch(
            "stock_agent.pi_runtime.pi_runtime_status", return_value=ready
        ), patch("stock_agent.pi_runtime.call_pi_agent", return_value=response) as call:
            result = run_pi_runtime_uat(self.config)
        self.assertEqual(result["status"], "PASS")
        self.assertTrue(result["live_model_call"])
        self.assertFalse(result["tools_enabled"])
        self.assertTrue((self.root / "reports" / "pi_agent" / "uat" / "latest.json").exists())
        call.assert_called_once()


if __name__ == "__main__":
    unittest.main()
