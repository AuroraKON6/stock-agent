import json
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
PROJECT = ROOT / "openclaw" / "stock_agent_project"


class OpenClawModelUpdateV23Tests(unittest.TestCase):
    def test_project_export_uses_v4_pro_only_for_stock_agent_definitions(self):
        registry = json.loads((PROJECT / "registry_export.json").read_text(encoding="utf-8"))
        agents = registry["agents_to_register"]
        self.assertEqual(9, len(agents))
        self.assertTrue(all(agent["id"].startswith("stock_agent_") for agent in agents))
        self.assertTrue(all(agent["model"]["primary"] == "deepseek/deepseek-v4-pro" for agent in agents))
        self.assertTrue(all(agent["model"]["fallbacks"] == ["deepseek/deepseek-v4-flash"] for agent in agents))

    def test_registry_update_script_is_scoped_and_backed_up(self):
        script = (ROOT / "scripts" / "update_openclaw_stock_agent_models.ps1").read_text(encoding="utf-8")
        self.assertEqual(9, script.count('    "stock_agent_'))
        self.assertIn("Copy-Item -LiteralPath $RegistryPath -Destination $BackupPath", script)
        self.assertIn("Where-Object { $_.id -in $ExpectedIds }", script)
        self.assertNotIn("agents.defaults.model.primary =", script)


if __name__ == "__main__":
    unittest.main()
