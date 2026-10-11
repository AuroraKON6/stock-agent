import unittest
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]


class ScheduledWorkspaceOpsTests(unittest.TestCase):
    def test_daily_runner_refreshes_workspace_before_dashboard_and_summary(self):
        script = (PROJECT_ROOT / "scripts" / "run_daily_research.ps1").read_text(encoding="utf-8")

        self.assertIn('Run-Step -Name "workspace-sync"', script)
        self.assertIn("& '$PythonExe' -m stock_agent workspace-sync", script)
        self.assertIn('Run-Step -Name "market-cache-seed"', script)
        self.assertIn("& '$PythonExe' -m stock_agent market-cache-seed --config $Config", script)
        self.assertIn('Run-Step -Name "evidence-vault-status"', script)
        self.assertIn("& '$PythonExe' -m stock_agent evidence-vault-status --config $Config --require-ready", script)
        self.assertIn("workspace_latest_path", script)
        self.assertIn("workspace_latest_json_path", script)
        self.assertIn('[ValidateSet("daily", "review", "dashboard", "pilot")]', script)
        self.assertIn('[ValidateSet("manual", "scheduled")]', script)
        self.assertIn("trigger_source = $TriggerSource", script)
        self.assertIn('Run-Step -Name "pi-handoff-run"', script)
        self.assertIn("agent_handoff_public_signal_count", script)
        self.assertIn("pi_model_call_count", script)
        self.assertNotIn("openclaw agent", script.lower())
        self.assertNotIn('Run-Step -Name "export-review"', script)

        cache_idx = script.index('Run-Step -Name "market-cache-seed"')
        agent_idx = script.index('Run-Step -Name "agent-run"')
        evidence_idx = script.index('Run-Step -Name "evidence-vault-status"')
        handoff_idx = script.index('Run-Step -Name "pi-handoff-run"')
        workspace_idx = script.index('Run-Step -Name "workspace-sync"')
        dashboard_idx = script.index('Run-Step -Name "dashboard"')
        alerts_idx = script.index('Run-Step -Name "alerts"')
        pilot_idx = script.index('Run-Step -Name "production-pilot-sync"')
        self.assertLess(cache_idx, agent_idx)
        self.assertLess(agent_idx, evidence_idx)
        self.assertLess(evidence_idx, handoff_idx)
        self.assertLess(workspace_idx, dashboard_idx)
        self.assertLess(workspace_idx, pilot_idx)
        self.assertLess(pilot_idx, dashboard_idx)
        self.assertLess(workspace_idx, alerts_idx)

    def test_scheduler_maps_requested_proxy_spelling_without_hardcoding_endpoint(self):
        script = (PROJECT_ROOT / "scripts" / "run_daily_research.ps1").read_text(encoding="utf-8")
        self.assertIn("$env:HTTPS_PROXY = $env:HTPPS_PROXY", script)
        self.assertIn("proxy_compatibility_applied", script)
        self.assertNotIn('$env:HTTPS_PROXY = "http://127.0.0.1:7890"', script)

    def test_dashboard_task_does_not_rerun_daily_llm_pipeline(self):
        installer = (PROJECT_ROOT / "scripts" / "install_scheduled_tasks.ps1").read_text(encoding="utf-8")
        self.assertIn('Args = "-Mode dashboard -TriggerSource scheduled -SkipPi', installer)
        self.assertIn('Name = "${TaskPrefix}PilotReconcile"', installer)
        self.assertIn('Args = "-Mode pilot -TriggerSource scheduled -SkipPi', installer)
        self.assertIn('Args = "-Mode daily -TriggerSource scheduled', installer)

    def test_go_live_check_generates_and_reports_workspace_artifacts(self):
        script = (PROJECT_ROOT / "scripts" / "go_live_check.ps1").read_text(encoding="utf-8")

        self.assertIn('Run-Step -Name "workspace_sync"', script)
        self.assertIn('Run-Step -Name "production_pilot_sync"', script)
        self.assertIn('Run-Step -Name "production_pilot_status"', script)
        self.assertIn('Run-Step -Name "final_doctor"', script)
        self.assertIn("& '$PythonExe' -m stock_agent workspace-sync", script)
        self.assertIn("workspace_inbox", script)
        self.assertIn("reports\\workspace\\latest.md", script)
        self.assertIn("reports\\workspace\\latest.json", script)
        self.assertNotIn("python -m stock_agent agent-run", script)
        self.assertNotIn("& '$PythonExe' -m stock_agent agent-run", script)
        self.assertNotIn("Invoke-Expression", script)
        self.assertGreater(script.index('Run-Step -Name "final_doctor"'), script.index('Run-Step -Name "alerts"'))
        self.assertIn('$PilotDecision -ne "GO"', script)
        self.assertIn('$ExitCode = 3', script)

    def test_go_live_check_records_installed_scheduled_tasks(self):
        script = (PROJECT_ROOT / "scripts" / "go_live_check.ps1").read_text(encoding="utf-8")

        self.assertIn("Get-ScheduledTask -TaskName StockAgent_*", script)
        self.assertIn("scheduled_tasks_installed", script)
        self.assertIn("scheduled_tasks", script)
        self.assertIn("$ScheduledTasksInstalled", script)
        self.assertIn("StockAgent_PilotReconcile", script)
        self.assertIn("Technical readiness:", script)
        self.assertIn("Business decision:", script)
        self.assertIn("Technical health does not authorize business production use.", script)


if __name__ == "__main__":
    unittest.main()
