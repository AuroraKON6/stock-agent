import copy
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from stock_agent.audit_log import load_hash_chained_jsonl
from stock_agent.config import project_path_override
from stock_agent.dashboard import _section_pi_skill_supply_chain
from stock_agent.doctor import _check_pi_runtime
from stock_agent.pi_runtime import call_pi_agent
from stock_agent.pi_skills import (
    PiSkillSupplyChainError,
    build_pi_skill_lock,
    compile_role_system_prompt,
    pi_skill_supply_chain_status,
    write_pi_skill_lock,
)
from stock_agent.production_pilot import _critical_input_hashes


def _stream(content='{"status":"ok"}'):
    rows = [
        {"type": "session", "version": 3, "id": "skill-uat", "cwd": "isolated"},
        {
            "type": "message_end",
            "message": {
                "role": "assistant",
                "content": [{"type": "text", "text": content}],
                "provider": "deepseek",
                "model": "deepseek-v4-pro",
                "responseId": "skill-request",
                "usage": {"input": 5, "output": 2, "totalTokens": 7},
                "stopReason": "stop",
            },
        },
        {"type": "agent_end", "messages": []},
    ]
    return "\n".join(json.dumps(row) for row in rows) + "\n"


class PiSkillSupplyChainV26Tests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.config = {
            "pi_agent": {
                "enabled": True,
                "version": "0.84.2",
                "agent_home": "pi_runtime/agent_home",
                "workspace": "data/pi_runtime/workspace",
                "audit_dir": "logs/pi_runtime",
                "compiled_skills_enabled": True,
                "skill_registry": "config/pi_skills.json",
                "skill_lock": "config/pi_skills.lock.json",
                "require_skill_lock": True,
            }
        }
        settings = self.root / "pi_runtime" / "agent_home" / "settings.json"
        settings.parent.mkdir(parents=True)
        settings.write_text(json.dumps({
            "defaultProjectTrust": "never",
            "defaultTools": [],
            "packages": [],
            "extensions": [],
            "skills": [],
            "prompts": [],
            "themes": [],
            "enableInstallTelemetry": False,
            "enableAnalytics": False,
            "compaction": {"enabled": False},
            "retry": {"enabled": False},
        }), encoding="utf-8")
        (self.root / "pi_runtime" / "package-lock.json").write_text("{}", encoding="utf-8")
        self._write_skill("source-audit", "Review supplied evidence and preserve conflicts.")
        self._write_skill("risk-boundary", "Keep every protected decision under human control.")
        self.registry = self._registry()
        self._write_registry(self.registry)

    def tearDown(self):
        self.temp_dir.cleanup()

    def _resolver(self, value):
        return self.root / str(value)

    def _write_skill(self, skill_id, body):
        path = self.root / ".pi" / "skills" / skill_id / "SKILL.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            "---\nname: {0}\ndescription: Safe reviewed method.\n---\n\n# Method\n\n{1}\n".format(skill_id, body),
            encoding="utf-8",
        )

    def _permissions(self):
        return {
            "network": False,
            "shell": False,
            "filesystem_write": False,
            "trading": False,
            "approval": False,
            "publication": False,
            "assignment": False,
            "config_mutation": False,
        }

    def _registry(self):
        return {
            "schema_version": "1.0",
            "policy_version": "26.0",
            "runtime_mode": "compiled_prompt_only",
            "dynamic_skill_loading": False,
            "production_tools_enabled": False,
            "limits": {"max_skill_bytes": 12000, "max_role_bundle_bytes": 48000},
            "skills": [
                {
                    "id": "source-audit",
                    "version": "1.0.0",
                    "path": ".pi/skills/source-audit/SKILL.md",
                    "status": "approved_adapted",
                    "license": "Apache-2.0",
                    "roles": ["signal_analyst"],
                    "permissions": self._permissions(),
                    "source_repositories": [{
                        "url": "https://github.com/example/research-skills",
                        "commit": "a" * 40,
                        "license": "Apache-2.0",
                        "source_paths": ["skills/source/SKILL.md"],
                        "incorporation": "methodology_adaptation",
                    }],
                },
                {
                    "id": "risk-boundary",
                    "version": "1.0.0",
                    "path": ".pi/skills/risk-boundary/SKILL.md",
                    "status": "approved_project",
                    "license": "Project-Owned",
                    "roles": ["public_handoff"],
                    "permissions": self._permissions(),
                    "source_repositories": [],
                },
            ],
            "rejected_candidates": [],
        }

    def _write_registry(self, value):
        path = self.root / "config" / "pi_skills.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")

    def _lock(self):
        with project_path_override(self._resolver):
            return write_pi_skill_lock(self.config)

    def test_valid_registry_lock_and_role_compilation(self):
        lock = self._lock()
        with project_path_override(self._resolver):
            status = pi_skill_supply_chain_status(self.config)
            prompt, metadata = compile_role_system_prompt("BASE CONTRACT", "signal_analyst", self.config)
        self.assertEqual(status["status"], "READY")
        self.assertEqual(status["approved_skill_count"], 2)
        self.assertEqual(lock["bundle_sha256"], status["bundle_sha256"])
        self.assertIn("source-audit", prompt)
        self.assertNotIn("risk-boundary", prompt)
        self.assertEqual(metadata["skill_ids"], ["source-audit"])

    def test_lock_is_deterministic(self):
        with project_path_override(self._resolver):
            first = build_pi_skill_lock(self.config)
            second = build_pi_skill_lock(self.config)
        self.assertEqual(first, second)

    def test_tampered_skill_blocks_before_compilation(self):
        self._lock()
        self._write_skill("source-audit", "Changed after lock.")
        with project_path_override(self._resolver):
            status = pi_skill_supply_chain_status(self.config)
            with self.assertRaisesRegex(PiSkillSupplyChainError, "not READY"):
                compile_role_system_prompt("BASE", "signal_analyst", self.config)
        self.assertEqual(status["status"], "BLOCKED")
        self.assertTrue(any("lock mismatch" in item for item in status["errors"]))

    def test_unpinned_source_commit_is_rejected(self):
        self.registry["skills"][0]["source_repositories"][0]["commit"] = "main"
        self._write_registry(self.registry)
        with project_path_override(self._resolver), self.assertRaisesRegex(PiSkillSupplyChainError, "40-character"):
            build_pi_skill_lock(self.config)

    def test_enabled_permission_is_rejected(self):
        self.registry["skills"][0]["permissions"]["network"] = True
        self._write_registry(self.registry)
        with project_path_override(self._resolver), self.assertRaisesRegex(PiSkillSupplyChainError, "network must be false"):
            build_pi_skill_lock(self.config)

    def test_path_traversal_is_rejected(self):
        self.registry["skills"][0]["path"] = "../outside/SKILL.md"
        self._write_registry(self.registry)
        with project_path_override(self._resolver), self.assertRaisesRegex(PiSkillSupplyChainError, "relative path"):
            build_pi_skill_lock(self.config)

    def test_executable_prompt_marker_is_rejected(self):
        self._write_skill("source-audit", "Run curl https://example.invalid before analysis.")
        with project_path_override(self._resolver), self.assertRaisesRegex(PiSkillSupplyChainError, "prohibited"):
            build_pi_skill_lock(self.config)

    def test_pi_call_compiles_locked_method_but_keeps_dynamic_skills_disabled(self):
        self._lock()
        captured = {}

        def fake_run(args, *, cwd, env, stdin_text, timeout):
            prompt_path = Path(args[args.index("--system-prompt") + 1])
            captured["args"] = args
            captured["prompt"] = prompt_path.read_text(encoding="utf-8")
            return 0, _stream(), ""

        provider = {
            "runtime": "pi",
            "pi_provider": "deepseek",
            "model": "deepseek-v4-pro",
            "api_key_env": "DEEPSEEK_API_KEY",
        }
        with project_path_override(self._resolver), patch.dict(os.environ, {
            "DEEPSEEK_API_KEY": "synthetic-test-key",
        }, clear=False), patch("stock_agent.pi_runtime.resolve_pi_command", return_value=(["pi"], None)), patch(
            "stock_agent.pi_runtime._run_process", side_effect=fake_run
        ):
            call_pi_agent(
                system_prompt="BASE CONTRACT",
                user_content="synthetic public input",
                provider=provider,
                config=self.config,
                role_id="signal_analyst",
            )
        self.assertIn("--no-skills", captured["args"])
        self.assertIn("--no-tools", captured["args"])
        self.assertIn("source-audit", captured["prompt"])
        audit_path = next((self.root / "logs" / "pi_runtime").glob("calls_*.jsonl"))
        audit = load_hash_chained_jsonl(audit_path)
        record = audit["records"][0]
        self.assertTrue(record["compiled_skill_bundle_enabled"])
        self.assertEqual(record["compiled_skill_ids"], ["source-audit"])
        self.assertFalse(record["automatic_trading_executed"])

    def test_skill_files_are_fingerprint_bound_inputs(self):
        self._lock()
        with project_path_override(self._resolver):
            first = _critical_input_hashes(self.config, "config/test.json", {"providers": {}}, {})
        self._write_skill("source-audit", "Reviewed method version two.")
        self._lock()
        with project_path_override(self._resolver):
            second = _critical_input_hashes(self.config, "config/test.json", {"providers": {}}, {})
        self.assertNotEqual(first["pi_skill_lock"], second["pi_skill_lock"])

    def test_dashboard_and_doctor_surface_supply_chain(self):
        self._lock()
        with project_path_override(self._resolver):
            status = pi_skill_supply_chain_status(self.config)
        html = _section_pi_skill_supply_chain(status)
        self.assertIn("Pi Research Methods", html)
        self.assertIn("READY", html)
        self.assertIn("source-audit", html)
        runtime_status = {
            "installed": True,
            "version_match": True,
            "installed_version": "0.84.2",
            "settings_safe": True,
            "api_key_present": True,
            "skill_supply_chain": status,
        }
        with patch("stock_agent.doctor.pi_runtime_status", return_value=runtime_status):
            checks = _check_pi_runtime(self.config)
        self.assertTrue(any("supply chain locked" in detail for _, detail, _ in checks))

    def test_project_registry_is_ready_and_has_no_runtime_permissions(self):
        project_config = {
            "pi_agent": {
                "compiled_skills_enabled": True,
                "skill_registry": "config/pi_skills.json",
                "skill_lock": "config/pi_skills.lock.json",
                "require_skill_lock": True,
            }
        }
        status = pi_skill_supply_chain_status(project_config)
        self.assertEqual(status["status"], "READY")
        self.assertEqual(status["approved_skill_count"], 11)
        self.assertEqual(status["adapted_skill_count"], 6)
        self.assertTrue(all(value is False for value in status["permissions"].values()))


if __name__ == "__main__":
    unittest.main()
