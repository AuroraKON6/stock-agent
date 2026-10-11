import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from stock_agent.pipeline import run_pipeline


class PipelineArtifactIsolationTests(unittest.TestCase):
    @patch("stock_agent.pipeline._sync_brokerage_architecture")
    @patch("stock_agent.pipeline._sync_workspace_inbox")
    @patch("stock_agent.pipeline.write_run_summary")
    @patch("stock_agent.pipeline.write_decision_summary")
    @patch("stock_agent.pipeline.write_team_ops_artifacts")
    @patch("stock_agent.pipeline.write_source_health_summary")
    @patch("stock_agent.pipeline.collect_sources", return_value=[])
    def test_isolated_profile_skips_shared_production_artifacts(
        self,
        _collect,
        source_health_writer,
        team_writer,
        decision_writer,
        run_writer,
        workspace_sync,
        architecture_sync,
    ):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            config = {
                "run_profile": {"name": "uat", "isolated": True},
                "storage": {"sqlite_path": "data/uat.sqlite3"},
                "report": {
                    "daily_dir": "reports/uat/daily",
                    "json_dir": "reports/uat/json",
                    "alert_dir": "reports/uat/alerts",
                },
                "priority_rules": {"min_priority_for_alert": "P1"},
                "research_cases": {"enabled": False},
                "research_runtime": {"enabled": False, "auto_sync": False},
                "research_team": {"enabled": False, "write_artifacts": False},
                "research_framework": {"enabled": False},
                "run_ops": {"enabled": False},
            }
            with patch("stock_agent.pipeline.project_path", side_effect=lambda value: root / value), patch(
                "stock_agent.reports.project_path", side_effect=lambda value: root / value
            ), patch("stock_agent.pipeline.ensure_directories"):
                result = run_pipeline(config)

        self.assertIn(str(Path("reports/uat/json")), result.json_path)
        source_health_writer.assert_not_called()
        team_writer.assert_not_called()
        decision_writer.assert_not_called()
        run_writer.assert_not_called()
        workspace_sync.assert_not_called()
        architecture_sync.assert_not_called()


if __name__ == "__main__":
    unittest.main()
