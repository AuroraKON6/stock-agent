import json
import unittest
from pathlib import Path


class ConfigDefaultsTests(unittest.TestCase):
    def test_local_config_uses_approved_deepseek_model_as_primary_llm(self):
        data = json.loads(Path("config/watchlist.local.json").read_text(encoding="utf-8"))
        llm = data.get("llm_agent", {})
        self.assertTrue(llm.get("enabled"))
        self.assertEqual(llm.get("base_url"), "https://api.deepseek.com/v1")
        # Operator-approved default since 2026-09-18: deepseek-flash
        # (pricing for it is intentionally absent from model_operations until
        # real per-token prices are supplied).
        self.assertEqual(llm.get("model"), "deepseek-flash")

    def test_example_config_documents_deepseek_v4_pro_without_api_key(self):
        data = json.loads(Path("config/watchlist.example.json").read_text(encoding="utf-8"))
        text = json.dumps(data, ensure_ascii=False)
        self.assertNotIn("your-key", text.lower())
        self.assertNotIn("sk-", text.lower())
        self.assertEqual(data["llm_agent"]["base_url"], "https://api.deepseek.com/v1")
        self.assertEqual(data["llm_agent"]["model"], "deepseek-v4-pro")

    def test_llm_provider_fallback_is_documented_without_secrets(self):
        for path in ["config/watchlist.example.json", "config/watchlist.local.json"]:
            with self.subTest(path=path):
                data = json.loads(Path(path).read_text(encoding="utf-8"))
                providers = data["llm_agent"].get("providers", [])
                self.assertGreaterEqual(len(providers), 2)
                self.assertEqual(providers[0]["name"], "deepseek_primary")
                self.assertEqual(providers[0]["api_key_env"], "DEEPSEEK_API_KEY")
                self.assertEqual(providers[1]["name"], "fallback_openai_compatible")
                self.assertEqual(providers[1]["api_key_env"], "LLM_FALLBACK_API_KEY")
                self.assertNotIn("api_key", providers[0])
                self.assertNotIn("api_key", providers[1])

    def test_external_market_sources_are_warn_only_for_transient_source_errors(self):
        for path in ["config/watchlist.example.json", "config/watchlist.local.json"]:
            with self.subTest(path=path):
                data = json.loads(Path(path).read_text(encoding="utf-8"))
                warn_only = set(data["source_health"].get("warn_only_source_errors", []))
                self.assertIn("eastmoney", warn_only)
                self.assertIn("eastmoney_filing", warn_only)

    def test_filing_jsonl_source_is_documented_but_disabled_by_default(self):
        for path in ["config/watchlist.example.json", "config/watchlist.local.json"]:
            with self.subTest(path=path):
                data = json.loads(Path(path).read_text(encoding="utf-8"))
                filing_sources = data["sources"].get("filing_jsonl", [])
                self.assertGreaterEqual(len(filing_sources), 1)
                self.assertFalse(filing_sources[0]["enabled"])
                self.assertIn("filing_evidence.example.jsonl", filing_sources[0]["path"])

    def test_eastmoney_filing_source_is_documented_and_local_enabled(self):
        example = json.loads(Path("config/watchlist.example.json").read_text(encoding="utf-8"))
        local = json.loads(Path("config/watchlist.local.json").read_text(encoding="utf-8"))
        self.assertFalse(example["sources"]["eastmoney_filing"]["enabled"])
        self.assertTrue(local["sources"]["eastmoney_filing"]["enabled"])
        for source in [example["sources"]["eastmoney_filing"], local["sources"]["eastmoney_filing"]]:
            self.assertTrue(source["write_jsonl"])
            self.assertEqual(source["output_dir"], "data/inbox/filings")
            self.assertGreaterEqual(len(source["symbols"]), 1)

    def test_filing_importance_rules_are_configurable(self):
        for path in ["config/watchlist.example.json", "config/watchlist.local.json"]:
            with self.subTest(path=path):
                data = json.loads(Path(path).read_text(encoding="utf-8"))
                rules = data["priority_rules"]["filing_importance_rules"]
                self.assertTrue(rules["enabled"])
                self.assertIn("performance_forecast", rules["high_types"])
                self.assertIn("governance", rules["low_types"])
                self.assertGreater(rules["high_score"], rules["low_score"])

    def test_research_git_llm_handoff_is_configurable(self):
        example = json.loads(Path("config/watchlist.example.json").read_text(encoding="utf-8"))
        local = json.loads(Path("config/watchlist.local.json").read_text(encoding="utf-8"))
        self.assertFalse(example["research_git"]["enabled"])
        self.assertTrue(local["research_git"]["enabled"])
        for data in [example, local]:
            cfg = data["research_git"]
            self.assertTrue(cfg["auto_stage_from_llm"])
            self.assertEqual(cfg["min_priority"], "P2")
            self.assertGreaterEqual(cfg["min_confidence"], 0.7)
            self.assertTrue(cfg["require_human_decision_needed"])

    def test_research_case_llm_budget_is_documented(self):
        for path in ["config/watchlist.example.json", "config/watchlist.local.json"]:
            with self.subTest(path=path):
                data = json.loads(Path(path).read_text(encoding="utf-8"))
                cfg = data["research_cases"]
                self.assertFalse(cfg["llm_enabled"])
                self.assertEqual(cfg["llm_timeout"], 30)
                self.assertEqual(cfg["llm_max_attempts"], 1)

    def test_daily_runtime_collect_threshold_matches_live_provider_latency(self):
        for path in ["config/watchlist.example.json", "config/watchlist.local.json"]:
            with self.subTest(path=path):
                data = json.loads(Path(path).read_text(encoding="utf-8"))
                self.assertEqual(data["run_ops"]["max_run_seconds"], 420)
                self.assertEqual(data["run_ops"]["warn_run_seconds"], 360)
                self.assertEqual(data["run_ops"]["stage_warn_seconds"]["collect"], 210)

    def test_multi_role_llm_budget_allows_one_retry_within_runtime_budget(self):
        for path in ["config/watchlist.example.json", "config/watchlist.local.json"]:
            with self.subTest(path=path):
                data = json.loads(Path(path).read_text(encoding="utf-8"))
                role = data["multi_role_agent"]
                signal_llm = data["llm_agent"]
                run_ops = data["run_ops"]
                self.assertEqual(role["timeout"], 60)
                self.assertEqual(role["max_attempts"], 2)
                self.assertLessEqual(signal_llm["timeout"] + role["timeout"] * role["max_attempts"], run_ops["llm_max_seconds"])
                self.assertLessEqual(role["max_signals_per_run"], 3)


if __name__ == "__main__":
    unittest.main()
