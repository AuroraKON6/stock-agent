"""K05: queries view, kimi-data-status layers, and board wiring (offline).

The board route test runs a real loopback HTTP server (allowed by the
isolation guard) against a TemporaryDirectory ledger. The status command
must never contact the network: its layers report NOT_RUN/BLOCKED honestly
instead of probing.
"""
from __future__ import annotations

import http.client
import json
import tempfile
import threading
import unittest
from pathlib import Path
from typing import Any, Dict

from stock_agent.config import project_path_override
from stock_agent.integrations.kimi import client as kimi_client
from stock_agent.queries_view import build_kimi_data_status, build_queries_view
from stock_agent.sources import kimi_adapter
from stock_agent.task_board import create_task_board_server
from stock_agent.task_runtime import TaskStore


class QueriesViewTestCase(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.enterContext(project_path_override(lambda value: self.root / value))
        self.config: Dict[str, Any] = {"kimi_sources": {
            "ledger_path": "data/kimi/query_ledger.jsonl",
            "cache_dir": "data/kimi/cache"}}

    def _seed(self, records: list) -> None:
        path = self.root / "data/kimi/query_ledger.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a", encoding="utf-8") as handle:
            for record in records:
                handle.write(json.dumps(record, ensure_ascii=False) + "\n")

    def test_empty_ledger_projects_empty_view(self):
        view = build_queries_view(self.config)
        self.assertEqual(view["query_count"], 0)
        self.assertEqual(view["ledger_record_count"], 0)
        self.assertTrue(view["daily_data_call_budget"]["allowed"])

    def test_view_pairs_intent_with_result_and_sanitizes_params(self):
        self._seed([
            {"record_type": "query_intent", "created_at": "2026-10-12T01:00:00+00:00",
             "request_id": "kq_1", "idempotency_key": "idem1", "source_id": "kimi_ifind_price",
             "api_id": "ifind_get_price", "data_kind": "daily_quote", "purpose": "event background",
             "task_id": "t-1", "contract_hash": "a" * 64,
             "normalized_params": {"ticker": "600519.SH", "start_date": "2026-09-28",
                                   "end_date": "2026-10-09", "adjust": "forward",
                                   "file_path": "kimi_x.csv"}},
            {"record_type": "query_result", "created_at": "2026-10-12T01:00:05+00:00",
             "request_id": "kq_1", "outcome": "ok", "source_id": "kimi_ifind_price",
             "data_kind": "daily_quote", "cost_status": "unknown", "attempts": 1,
             "row_count": 9, "http_status": 200,
             "artifact_plans": [{"artifact_id": "ka_1", "remote_name_rejected": None,
                                 "remote_name_record": "prices.csv"}],
             "committed_artifacts": []},
            {"record_type": "consumed", "created_at": "2026-10-12T02:00:00+00:00",
             "artifact_id": "ka_1", "purpose": "research", "consumer": "supply"},
        ])
        view = build_queries_view(self.config)
        self.assertEqual(view["query_count"], 1)
        self.assertEqual(view["consumption_count"], 1)
        query = view["queries"][0]
        self.assertEqual(query["result"]["outcome"], "ok")
        self.assertEqual(query["result"]["outcome_label"], "成功（数据已回）")
        self.assertEqual(query["result"]["cost_status"], "unknown")
        # file_path is host-generated and must not surface in display params
        self.assertNotIn("file_path", query["params"])
        self.assertEqual(query["params"]["ticker"], "600519.SH")

    def test_distinct_outcomes_keep_distinct_labels(self):
        self._seed([
            {"record_type": "query_intent", "created_at": "2026-10-12T01:00:00+00:00",
             "request_id": "kq_a", "idempotency_key": "i1", "source_id": "kimi_dongcai_news",
             "api_id": "dongcai_mx_finance_search", "data_kind": "news_metadata",
             "purpose": "p", "task_id": "", "contract_hash": "b" * 64,
             "normalized_params": {"query": "茅台 研报"}},
            {"record_type": "query_result", "created_at": "2026-10-12T01:00:05+00:00",
             "request_id": "kq_a", "outcome": "outcome_unknown", "cost_status": "unknown",
             "attempts": 1, "artifact_plans": [], "committed_artifacts": []},
            {"record_type": "query_intent", "created_at": "2026-10-12T03:00:00+00:00",
             "request_id": "kq_b", "idempotency_key": "i2", "source_id": "kimi_ifind_price",
             "api_id": "ifind_get_price", "data_kind": "daily_quote", "purpose": "p",
             "task_id": "", "contract_hash": "b" * 64, "normalized_params": {}},
            {"record_type": "query_result", "created_at": "2026-10-12T03:00:05+00:00",
             "request_id": "kq_b", "outcome": "auth_rejected", "cost_status": "none",
             "attempts": 1, "artifact_plans": [], "committed_artifacts": []},
        ])
        view = build_queries_view(self.config)
        self.assertEqual(view["outcome_counts"], {"auth_rejected": 1, "outcome_unknown": 1})
        labels = {query["request_id"]: (query["result"] or {}).get("outcome_label")
                  for query in view["queries"]}
        self.assertIn("结果未知", labels["kq_a"])
        self.assertIn("认证被拒", labels["kq_b"])

    def test_purpose_filter(self):
        self._seed([
            {"record_type": "query_intent", "created_at": "2026-10-12T01:00:00+00:00",
             "request_id": "kq_1", "idempotency_key": "i1", "source_id": "s", "api_id": "a",
             "data_kind": "news_metadata", "purpose": "event background", "task_id": "",
             "contract_hash": "c" * 64, "normalized_params": {}},
        ])
        view = build_queries_view(self.config, purpose="event background")
        self.assertEqual(view["query_count"], 1)
        view = build_queries_view(self.config, purpose="other purpose")
        self.assertEqual(view["query_count"], 0)


class KimiDataStatusTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.enterContext(project_path_override(lambda value: self.root / value))
        self.config: Dict[str, Any] = {}

    def test_default_config_reports_blocked_layers_without_network(self):
        status = build_kimi_data_status(self.config)
        layers = status["layers"]
        self.assertFalse(layers["configured"])
        self.assertEqual(layers["contract_valid"]["status"], "NOT_CONFIGURED")
        self.assertEqual(layers["access_verified"]["status"], "NOT_RUN")
        self.assertEqual(layers["internally_approved"]["status"], "BLOCKED")
        self.assertEqual(layers["llm_eligible"]["status"], "BLOCKED")
        self.assertEqual(status["live_calls_made"], 0)

    def test_candidate_snapshot_validates_when_configured(self):
        snapshot_path = Path(__file__).resolve().parents[1] / "config/candidates/kimi/contracts.v1.json"
        if not snapshot_path.is_file():
            self.skipTest("candidate snapshot not present")
        # copy into the temp root so the view resolves it via project_path
        target = self.root / "config/candidates/kimi/contracts.v1.json"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(snapshot_path.read_bytes())
        config = {"kimi_sources": {"contract_snapshot": "config/candidates/kimi/contracts.v1.json"}}
        status = build_kimi_data_status(config)
        self.assertEqual(status["layers"]["contract_valid"]["status"], "VALID")
        self.assertEqual(status["layers"]["contract_valid"]["api_count"], 4)


class BoardQueriesRouteTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.enterContext(project_path_override(lambda value: self.root / value))
        ledger = self.root / "data/kimi/query_ledger.jsonl"
        ledger.parent.mkdir(parents=True, exist_ok=True)
        ledger.write_text(json.dumps({
            "record_type": "query_intent", "created_at": "2026-10-12T01:00:00+00:00",
            "request_id": "kq_1", "idempotency_key": "i1", "source_id": "kimi_ifind_price",
            "api_id": "ifind_get_price", "data_kind": "daily_quote", "purpose": "p",
            "task_id": "", "contract_hash": "d" * 64,
            "normalized_params": {"ticker": "600519.SH"}}) + "\n", encoding="utf-8")
        self.store = TaskStore(self.root / "tasks.sqlite3")
        # The server thread does not inherit this test's project_path_override
        # (contextvars are per-thread), so the board config pins the ledger by
        # absolute path — project_path returns absolute paths untouched.
        self.server = create_task_board_server(
            self.store, host="127.0.0.1", port=0,
            config={"kimi_sources": {"ledger_path": str(ledger)}})
        thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(self.server.shutdown)
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.store.close)
        self.port = self.server.server_address[1]

    def _get(self, path: str):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        conn.request("GET", path, headers={"Host": "127.0.0.1"})
        response = conn.getresponse()
        body = response.read().decode("utf-8")
        conn.close()
        return response.status, body

    def test_queries_route_serves_projection(self):
        status, body = self._get("/api/v1/queries-view")
        self.assertEqual(status, 200)
        payload = json.loads(body)
        self.assertEqual(payload["query_count"], 1)
        self.assertEqual(payload["queries"][0]["api_id"], "ifind_get_price")

    def test_home_page_contains_queries_section(self):
        status, body = self._get("/")
        self.assertEqual(status, 200)
        self.assertIn('id="kqlist"', body)
        self.assertIn("数据查询", body)


if __name__ == "__main__":
    unittest.main()
