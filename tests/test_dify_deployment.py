"""DF-01: Dify deployment-candidate config — offline validation, zero secrets."""
import json
import sys
import tempfile
import unittest
from pathlib import Path

from stock_agent.config import project_path_override
from stock_agent.dify_deployment import (
    DEPLOYMENT_CANDIDATE_VERSION,
    deployment_candidate,
    load_deployment_candidate,
    validate_deployment_candidate,
)

sys.path.insert(0, str(Path(__file__).resolve().parent))


class DeploymentCandidateTests(unittest.TestCase):
    def test_candidate_shape_and_no_secrets(self):
        candidate = deployment_candidate()
        self.assertEqual(candidate["dify_version"], DEPLOYMENT_CANDIDATE_VERSION)
        for section in ("deployment", "capabilities_required", "python_boundary",
                        "open_questions", "blocked_items"):
            self.assertIn(section, candidate)
        # No credential fields anywhere in the candidate.
        text = json.dumps(candidate, ensure_ascii=False)
        self.assertNotIn("api_key", text.lower())
        self.assertNotIn("password", text.lower())
        self.assertNotIn("secret", text.lower())

    def test_capabilities_required_lists_the_phase_one_needs(self):
        candidate = deployment_candidate()
        caps = candidate["capabilities_required"]
        for cap in ("workflow_api_trigger", "human_input_node",
                    "service_credential_scoping", "workflow_version_export"):
            self.assertIn(cap, [item["capability"] for item in caps])

    def test_python_boundary_forbids_direct_db_and_dual_executor(self):
        boundary = deployment_candidate()["python_boundary"]
        joined = " ".join(boundary["python_owns"])
        self.assertIn("TaskStore", joined)
        forbidden = " ".join(boundary["dify_must_not"])
        self.assertIn("数据库", forbidden)
        self.assertIn("双执行", forbidden)

    def test_blocked_items_are_explicit(self):
        blocked = " ".join(deployment_candidate()["blocked_items"])
        for marker in ("安装", "公网", "凭据", "启动服务"):
            self.assertIn(marker, blocked)

    def test_validate_accepts_shipped_example(self):
        example = Path(__file__).resolve().parents[1] / "config" / "dify.deployment.example.json"
        document = json.loads(example.read_text(encoding="utf-8"))
        validate_deployment_candidate(document)
        self.assertEqual(document["dify_version"], DEPLOYMENT_CANDIDATE_VERSION)

    def test_validate_rejects_unknown_fields_and_secret_keys(self):
        base = deployment_candidate()
        with self.assertRaises(ValueError):
            validate_deployment_candidate({**base, "surprise": 1})
        poisoned = json.loads(json.dumps(base))
        poisoned["deployment"]["compose_profile"] = {"api_key": "sk-live"}
        with self.assertRaises(ValueError):
            validate_deployment_candidate(poisoned)

    def test_load_missing_file_is_not_configured_not_crash(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with project_path_override(lambda value: root / value):
                result = load_deployment_candidate({"dify": {"config_path": "none.json"}})
        self.assertFalse(result["configured"])
        self.assertIn("blocked", " ".join(result["status"]))


class InterfaceInventoryTests(unittest.TestCase):
    def test_inventory_maps_existing_endpoints_to_contract_roles(self):
        from stock_agent.dify_deployment import interface_inventory

        inventory = interface_inventory()
        roles = {item["contract_role"] for item in inventory["interfaces"]}
        for needed in ("create_research", "research_status", "human_feedback",
                       "evidence_snapshot", "model_governance"):
            self.assertIn(needed, roles)
        for item in inventory["interfaces"]:
            self.assertTrue(item["exists"], "inventory maps only what exists today")

    def test_gaps_are_honest(self):
        from stock_agent.dify_deployment import interface_inventory

        gaps = interface_inventory()["gaps"]
        self.assertTrue(gaps, "phase-one gaps must be listed, not implied complete")


if __name__ == "__main__":
    unittest.main()
