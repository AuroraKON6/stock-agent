import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from stock_agent.reports import write_outputs


class ReportOutputIsolationTests(unittest.TestCase):
    def test_custom_report_directories_isolate_uat_artifacts(self):
        config = {
            "report": {
                "daily_dir": "reports/uat/governance/daily",
                "json_dir": "reports/uat/governance/json",
                "alert_dir": "reports/uat/governance/alerts",
            },
            "priority_rules": {"min_priority_for_alert": "P1"},
        }
        with tempfile.TemporaryDirectory() as tmpdir, patch(
            "stock_agent.reports.project_path", side_effect=lambda value: Path(tmpdir) / value
        ):
            report_path, json_path, alert_path, alerts = write_outputs([], config, {})

        self.assertIn(str(Path("reports/uat/governance/daily")), report_path)
        self.assertIn(str(Path("reports/uat/governance/json")), json_path)
        self.assertIn(str(Path("reports/uat/governance/alerts")), alert_path)
        self.assertEqual(alerts, [])


if __name__ == "__main__":
    unittest.main()
