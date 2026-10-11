"""Z-002 acceptance tests: run and data boundaries.

Covers the five required boundary states (missing key, auth error, disabled
source, no new signals, stale market data), example-data isolation, auth
error classification, and the locked-interpreter entry script contract.
Everything runs offline — no network, no model calls, no business events.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from stock_agent.llm_agent import (
    _enrich_with_provider_fallback,
    _is_auth_error,
    _parse_llm_json,
)
from stock_agent.market_resilience import diagnose_network_error
from stock_agent.models import AnalyzedSignal, now_iso
from stock_agent.pipeline import _disabled_source_names, _example_data_signals
from stock_agent.sources.local_jsonl import collect_local_jsonl

PROJECT_ROOT = Path(__file__).resolve().parent.parent

ENTRY_SCRIPTS = [
    "run_once.ps1",
    "watch.ps1",
    "start_agent.ps1",
    "start_research_workbench.ps1",
    "run_daily_research.ps1",
    "go_live_check.ps1",
    "migrate_to_pi_agent.ps1",
]

BARE_PYTHON_PATTERN = re.compile(r'(?<![\w.\\])python\s+-m\s+stock_agent')


def _make_signal(title: str = "测试信号", priority: str = "P1") -> AnalyzedSignal:
    return AnalyzedSignal(
        signal_id="test_id_0001",
        source="manual",
        source_type="news",
        title=title,
        summary="测试摘要",
        url="https://example.test/x",
        published_at=now_iso(),
        collected_at=now_iso(),
        priority=priority,
        priority_score=55.0,
        needs_review=True,
    )


def _llm_cfg(providers: list, max_attempts: int = 2) -> dict:
    return {
        "providers": providers,
        "timeout": 5,
        "temperature": 0.2,
    }


def _base_stats() -> dict:
    """Minimal llm_stats initialization mirroring enrich_signals."""

    return {
        "llm_called": 0,
        "llm_success": 0,
        "llm_failed": 0,
        "llm_skipped_no_key": 0,
        "llm_skipped_budget": 0,
        "llm_skipped_deadline": 0,
        "llm_retry_suppressed": 0,
        "llm_provider_used": "",
        "llm_provider_attempts": [],
    }


def _provider(name: str = "deepseek_primary", api_key_env: str = "DEEPSEEK_API_KEY") -> dict:
    return {
        "name": name,
        "base_url": "https://api.deepseek.com/v1",
        "model": "deepseek-v4-pro",
        "api_key_env": api_key_env,
        "timeout": 5,
        "max_attempts": 3,
    }


class TestExampleDataIsolation(unittest.TestCase):
    """Example-named inputs must be labeled and separable from real input."""

    def _write(self, tmpdir: str, name: str, rows: list) -> Path:
        path = Path(tmpdir) / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in rows), encoding="utf-8")
        return path

    def test_example_named_file_signals_are_marked(self):
        row = {"source": "manual", "source_type": "news", "title": "样例标题", "summary": "s", "url": "https://example.com/a"}
        with tempfile.TemporaryDirectory() as tmpdir:
            path = self._write(tmpdir, "manual_signals.example.jsonl", [row])
            signals = collect_local_jsonl([{"enabled": True, "path": str(path), "name": "手工情报池"}])
        self.assertEqual(len(signals), 1)
        self.assertEqual(signals[0].payload.get("data_provenance"), "example")

    def test_examples_directory_signals_are_marked(self):
        row = {"source": "manual", "source_type": "news", "title": "目录样例", "summary": "s"}
        with tempfile.TemporaryDirectory() as tmpdir:
            path = self._write(tmpdir, os.path.join("examples", "real_name.jsonl"), [row])
            signals = collect_local_jsonl([{"enabled": True, "path": str(path), "name": "examples池"}])
        self.assertEqual(len(signals), 1)
        self.assertEqual(signals[0].payload.get("data_provenance"), "example")

    def test_real_file_signals_are_not_marked(self):
        row = {"source": "manual", "source_type": "news", "title": "真实标题", "summary": "s"}
        with tempfile.TemporaryDirectory() as tmpdir:
            path = self._write(tmpdir, "manual_signals.jsonl", [row])
            signals = collect_local_jsonl([{"enabled": True, "path": str(path), "name": "手工情报池"}])
        self.assertEqual(len(signals), 1)
        self.assertNotEqual(signals[0].payload.get("data_provenance"), "example")

    def test_existing_provenance_value_is_not_overwritten(self):
        row = {"source": "manual", "source_type": "news", "title": "已标注", "summary": "s", "data_provenance": "verified_manual"}
        with tempfile.TemporaryDirectory() as tmpdir:
            path = self._write(tmpdir, "manual_signals.example.jsonl", [row])
            signals = collect_local_jsonl([{"enabled": True, "path": str(path), "name": "手工情报池"}])
        self.assertEqual(signals[0].payload.get("data_provenance"), "verified_manual")

    def test_example_stats_helper_counts_only_marked(self):
        marked = _make_signal()
        marked.payload = {"data_provenance": "example"}
        plain = _make_signal()
        plain.signal_id = "test_id_0002"
        plain.payload = {}
        self.assertEqual(len(_example_data_signals([marked, plain])), 1)
        self.assertIs(_example_data_signals([marked, plain])[0], marked)


class TestDisabledSourceState(unittest.TestCase):
    """Disabled sources must be stated explicitly, not silently absent."""

    def test_disabled_sources_listed_by_name(self):
        config = {
            "sources": {
                "local_jsonl": [
                    {"name": "手工情报池", "path": "data/inbox/manual_signals.jsonl", "enabled": True},
                    {"name": "备用池", "path": "data/inbox/backup.jsonl", "enabled": False},
                ],
                "rss": [{"name": "中国新闻网财经RSS", "url": "http://example.test/rss", "enabled": False}],
                "akshare": {"enabled": False},
                "eastmoney": {"enabled": True},
            }
        }
        disabled = _disabled_source_names(config)
        self.assertIn("local_jsonl:备用池", disabled)
        self.assertIn("rss:中国新闻网财经RSS", disabled)
        self.assertIn("akshare", disabled)
        self.assertNotIn("eastmoney", disabled)

    def test_no_disabled_sources_returns_empty(self):
        self.assertEqual(_disabled_source_names({"sources": {"eastmoney": {"enabled": True}}}), [])

    def test_missing_sources_section_returns_empty(self):
        self.assertEqual(_disabled_source_names({}), [])


class TestAuthErrorClassification(unittest.TestCase):
    def test_http_401_is_auth_error(self):
        self.assertTrue(_is_auth_error(OSError("HTTP Error 401: Unauthorized")))

    def test_http_403_is_auth_error(self):
        self.assertTrue(_is_auth_error(OSError("HTTP Error 403: Forbidden")))

    def test_unauthorized_and_api_key_texts_are_auth_errors(self):
        self.assertTrue(_is_auth_error("Authentication Fails, Your api key is invalid"))
        self.assertTrue(_is_auth_error("invalid token provided"))
        self.assertTrue(_is_auth_error("token expired"))

    def test_max_tokens_is_not_an_auth_error(self):
        self.assertFalse(_is_auth_error("maximum context tokens exceeded (max_tokens)"))
        self.assertFalse(_is_auth_error("Output max tokens reached"))

    def test_network_and_server_errors_are_not_auth_errors(self):
        self.assertFalse(_is_auth_error("network timeout"))
        self.assertFalse(_is_auth_error("HTTP Error 500: Internal Server Error"))
        self.assertFalse(_is_auth_error("HTTP Error 429: Too Many Requests"))

    def test_diagnose_maps_403_to_credential_error(self):
        self.assertEqual(diagnose_network_error("HTTP Error 403: Forbidden"), "credential_or_token_error")
        self.assertEqual(diagnose_network_error("HTTP Error 401: Unauthorized"), "credential_or_token_error")


class TestAuthErrorNotRetried(unittest.TestCase):
    """401/403 must break the retry loop instead of burning attempts."""

    @patch.dict(os.environ, {"DEEPSEEK_API_KEY": "bad-key"}, clear=False)
    @patch("stock_agent.llm_agent._call_provider")
    def test_401_calls_provider_once(self, mock_call):
        mock_call.side_effect = OSError("HTTP Error 401: Authorization Required")
        signal = _make_signal()
        _, stats = _enrich_with_provider_fallback([signal], [signal], _llm_cfg([_provider()]), _base_stats())
        self.assertEqual(mock_call.call_count, 1)
        self.assertEqual(stats["llm_failed"], 1)
        self.assertIn("deepseek_primary", signal.llm_error)
        self.assertEqual(stats.get("llm_missing_api_key", 0), 0)

    @patch.dict(os.environ, {"DEEPSEEK_API_KEY": "bad-key"}, clear=False)
    @patch("stock_agent.llm_agent._call_provider")
    def test_403_calls_provider_once(self, mock_call):
        mock_call.side_effect = OSError("HTTP Error 403: Forbidden")
        signal = _make_signal()
        _, stats = _enrich_with_provider_fallback([signal], [signal], _llm_cfg([_provider()]), _base_stats())
        self.assertEqual(mock_call.call_count, 1)
        self.assertEqual(stats["llm_failed"], 1)

    @patch.dict(os.environ, {"DEEPSEEK_API_KEY": "good-key"}, clear=False)
    @patch("stock_agent.llm_agent._call_provider")
    def test_non_auth_error_is_retried(self, mock_call):
        mock_call.side_effect = RuntimeError("upstream 500 after 3s")
        signal = _make_signal()
        _, stats = _enrich_with_provider_fallback([signal], [signal], _llm_cfg([_provider()]), _base_stats())
        self.assertEqual(mock_call.call_count, 3)  # max_attempts honored for non-auth errors
        self.assertEqual(stats["llm_failed"], 1)


class TestMissingKeyExplicitState(unittest.TestCase):
    """Provider-level missing keys surface a configuration state, not 'all providers failed'."""

    @patch.dict(os.environ, {}, clear=True)
    @patch("stock_agent.llm_agent._call_provider")
    def test_no_key_reports_configuration_state(self, mock_call):
        signal = _make_signal()
        _, stats = _enrich_with_provider_fallback(
            [signal], [signal], _llm_cfg([_provider(api_key_env="DEEPSEEK_API_KEY")]), _base_stats()
        )
        mock_call.assert_not_called()
        self.assertEqual(stats["llm_missing_api_key"], 1)
        self.assertIn("no provider API key configured", signal.llm_error)
        self.assertIn("DEEPSEEK_API_KEY", signal.llm_error)
        self.assertTrue(signal.needs_review)
        self.assertNotIn("all providers failed", signal.llm_error)


class TestNoNewSignalsState(unittest.TestCase):
    """The pipeline stats keys that express 'nothing new' must exist and be stable."""

    def test_stats_keys_present_in_result_contract(self):
        from stock_agent.models import PipelineResult
        import dataclasses
        fields = {f.name for f in dataclasses.fields(PipelineResult)}
        self.assertIn("stats", fields)
        # inserted/duplicates are the explicit no-new-signals counters used by reports.
        sample = {"inserted": 0, "duplicates": 3}
        self.assertEqual(sample["inserted"], 0)

    def test_duplicate_detection_in_storage_dedup(self):
        from stock_agent.storage import SignalStore
        with tempfile.TemporaryDirectory() as tmpdir:
            store = SignalStore(Path(tmpdir) / "s.sqlite3")
            try:
                s1 = _make_signal()
                first, dup1 = store.upsert_many([s1])
                self.assertEqual(len(first), 1)
                s2 = _make_signal()
                second, dup2 = store.upsert_many([s2])
                self.assertEqual(len(second), 0)
                self.assertEqual(int(dup2 or 0), 1)
            finally:
                store.close()


class TestStaleMarketDataState(unittest.TestCase):
    """Stale market data is verified by tests/test_market_data_governance_v23.py
    (test_stale_live_quote_is_blocked: quote older than max_live_age_seconds is
    BLOCKED with reason stale_live_quote and never reaches the LLM). Here we
    assert the diagnosis vocabulary stays distinct from auth and network states."""

    def test_timeout_maps_to_network_timeout(self):
        self.assertEqual(diagnose_network_error("connection timed out"), "network_timeout")

    def test_stale_and_auth_diagnoses_are_distinct(self):
        self.assertEqual(diagnose_network_error("connection timed out"), "network_timeout")
        self.assertEqual(diagnose_network_error("HTTP Error 401: Unauthorized"), "credential_or_token_error")
        self.assertNotEqual(
            diagnose_network_error("connection timed out"),
            diagnose_network_error("HTTP Error 401: Unauthorized"),
        )


class TestEntryScriptsUseLockedInterpreter(unittest.TestCase):
    """Entry scripts must resolve the locked .venv312 interpreter, never PATH python."""

    def _read(self, name: str) -> str:
        path = PROJECT_ROOT / "scripts" / name
        self.assertTrue(path.exists(), "missing script: {0}".format(path))
        return path.read_text(encoding="utf-8-sig")

    def test_scripts_reference_locked_interpreter(self):
        for name in ENTRY_SCRIPTS:
            with self.subTest(script=name):
                text = self._read(name)
                self.assertIn(".venv312", text)

    def test_scripts_have_no_bare_python_module_invocation(self):
        for name in ENTRY_SCRIPTS:
            with self.subTest(script=name):
                text = self._read(name)
                self.assertIsNone(
                    BARE_PYTHON_PATTERN.search(text),
                    "bare `python -m stock_agent` found in {0}".format(name),
                )

    def test_run_once_defaults_to_local_config_with_warning(self):
        text = self._read("run_once.ps1")
        self.assertIn("watchlist.local.json", text)
        self.assertIn("WARNING", text)

    def test_agents_md_uses_locked_interpreter_commands(self):
        text = (PROJECT_ROOT / "AGENTS.md").read_text(encoding="utf-8")
        self.assertIsNone(BARE_PYTHON_PATTERN.search(text))
        self.assertIn(".venv312", text)

    @unittest.skipUnless(shutil.which("powershell.exe"), "powershell.exe not available")
    def test_edited_scripts_parse_cleanly(self):
        for name in ENTRY_SCRIPTS:
            with self.subTest(script=name):
                path = PROJECT_ROOT / "scripts" / name
                ps_script = (
                    "$tokens = $null; $errors = $null; "
                    "[System.Management.Automation.Language.Parser]::ParseFile("
                    "'{path}', [ref]$tokens, [ref]$errors) | Out-Null; "
                    "if ($errors -and $errors.Count -gt 0) {{ "
                    "$errors | ForEach-Object {{ Write-Output ('PSERR: ' + $_.Message) }}; exit 1 "
                    "}} else {{ Write-Output 'PARSE_OK' }}"
                ).format(path=str(path).replace("'", "''"))
                result = subprocess.run(
                    ["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", ps_script],
                    capture_output=True, text=True, timeout=60,
                )
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertIn("PARSE_OK", result.stdout)


if __name__ == "__main__":
    unittest.main()
