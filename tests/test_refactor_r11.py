"""R11: governed queries feeding the task supply (offline file provider).

A51: a fixture query enters the MAIN supply with the full
query → result → consumed chain bound in one authority.
A52: no_data / permission_denied / suspended are distinct supply statuses —
never collapsed into one "empty success".
A53: replaying the same api+params never re-reserves, re-bills or
double-binds a consumption.
A54: announcement metadata joins the context as URL LEADS only — no
evidence_summaries entry appears, the original stays honestly missing.
"""
import json
import sys
import tempfile
import unittest
from pathlib import Path

from stock_agent.config import project_path_override
from stock_agent.connector_runtime import ConnectorQueryStore
from stock_agent.query_supply import (
    FileFixtureTransport,
    QuerySupplyError,
    attach_query_to_spec,
)

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_kimi_sources_v1 import registry_entry, registry_with  # noqa: F401 (reuse fixture)

PRICE_CSV = "日期,开盘价,收盘价\n2026-10-09,1690.0,1700.0\n"
ANNOUNCEMENT_ROWS = [{"title": "甲公司年报", "公告链接": "https://www.cninfo.com.cn/x/1"}]


def _envelope_files(content: str):
    return [{"name": "fixture.csv", "content": content}]


def _body(success=True, user_text="", files=None):
    body: dict = {"is_success": success}
    result = {}
    if user_text:
        result["user"] = [{"type": "text", "text": user_text}]
    if result:
        body["result"] = result
    if files is not None:
        body["files"] = files
    return body


FIXTURES = {
    "schema_version": "1.0",
    "apis": {
        "file_get_price": {"outcome": "ok", "body": _body(files=_envelope_files(PRICE_CSV))},
        "file_empty": {"outcome": "empty_result", "body": _body()},
        "file_suspended": {"outcome": "suspended", "body": _body(success=False,
                                                                 user_text="停牌")},
        "file_denied": {"outcome": "permission_denied"},
        "file_announcements": {
            "outcome": "ok",
            "body": _body(files=_envelope_files(
                "标题,公告链接\n" + "\n".join(
                    "{0},{1}".format(row["title"], row["公告链接"])
                    for row in ANNOUNCEMENT_ROWS) + "\n")),
        },
    },
}


class R11SupplyTestCase(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.enterContext(project_path_override(lambda value: self.root / value))
        fixture_path = self.root / "config/candidates/sources/file_fixtures.json"
        fixture_path.parent.mkdir(parents=True, exist_ok=True)
        fixture_path.write_text(json.dumps(FIXTURES, ensure_ascii=False), encoding="utf-8")
        self.config = {
            "kimi_integration": {"enabled": True},
            "kimi_sources": {"file_fixture_path": str(fixture_path),
                             "daily_data_call_limit": 10,
                             "query_store_path": "data/kimi/query_intake.sqlite3"},
            "source_registry": {"path": "test"},
        }
        self.registry = registry_with(
            registry_entry("file_price", "price", purposes=("research supply",)),
            registry_entry("file_news", "filing", purposes=("research supply",),
                           kind=None) if False else
            registry_entry("file_news", "news", purposes=("research supply",)),
        )
        self.spec: dict = {"data_classification": "public",
                           "research_question": "供给接线？",
                           "public_context": {}}

    def attach(self, api_id="file_get_price", *, params=None, purpose="research supply",
               task_id="task-a51", source_id="file_price", data_kind="daily_quote",
               config=None, transport=None, allow_cache=True):
        return attach_query_to_spec(
            config or self.config, self.spec, source_id=source_id, api_id=api_id,
            transport=transport, allow_cache=allow_cache,
            params=params or {"ticker": "600519.SH", "start_date": "2026-10-01",
                              "end_date": "2026-10-09", "adjust": "forward"},
            purpose=purpose, task_id=task_id, data_kind=data_kind)

    def intake(self):
        return ConnectorQueryStore(
            self.root / "data/kimi/query_intake.sqlite3")

    def attach_with_registry(self, *args, **kwargs):
        config = dict(kwargs.pop("config", None) or self.config)
        config["source_registry"] = {"path": "test"}
        # governed path loads the registry from config; inject ours via a
        # temporary registry file is overkill — monkeypatch load instead.
        import stock_agent.query_supply as supply
        original = supply.load_source_registry if hasattr(supply, "load_source_registry") else None
        import stock_agent.sources.registry as registry_module
        real_loader = registry_module.load_source_registry
        registry_module.load_source_registry = lambda config: self.registry
        try:
            return self.attach(*args, **kwargs)
        finally:
            registry_module.load_source_registry = real_loader


class A51ChainTests(R11SupplyTestCase):
    def test_query_result_consumed_chain_in_one_authority(self):
        receipt = self.attach_with_registry()
        self.assertEqual(receipt["supply_status"], "data_delivered", receipt)
        self.assertTrue(receipt["consumption_recorded"])
        # Result joined the SPEC (main supply), normalized.
        normalized = self.spec["query_results"]["file_get_price"]
        self.assertEqual(normalized["data_kind"], "daily_quote")
        self.assertEqual(normalized["row_count"], 1)
        self.assertIn("600519.SH", self.spec["public_context"]["quote_snapshots"])
        self.assertEqual(self.spec["public_context"]["quote_snapshots"]["600519.SH"]["price"],
                         1700.0)
        # Receipt rides the spec (auditable without reopening the store).
        self.assertEqual(self.spec["query_receipts"][0]["supply_status"],
                         "data_delivered")
        # Consumed binding lives in the SAME store as the reservation.
        with self.intake() as intake:
            consumption = intake.find_query_consumption(receipt["intent_id"])
        self.assertEqual(consumption["task_id"], "task-a51")
        self.assertEqual(consumption["supply_status"], "data_delivered")


class A52StatusTests(R11SupplyTestCase):
    def test_no_data_permission_suspension_distinct(self):
        no_data = self.attach_with_registry(api_id="file_empty", task_id="t-nd")
        suspended = self.attach_with_registry(api_id="file_suspended", task_id="t-sus")
        denied = self.attach_with_registry(api_id="file_denied", task_id="t-deny")
        self.assertEqual(no_data["supply_status"], "no_data")
        self.assertEqual(suspended["supply_status"], "suspended")
        self.assertEqual(denied["supply_status"], "permission_denied")
        # None of the failures masquerade as delivered context.
        self.assertNotIn("file_empty", self.spec.get("query_results", {}))
        self.assertNotIn("file_suspended", self.spec.get("query_results", {}))
        self.assertNotIn("file_denied", self.spec.get("query_results", {}))
        # Yet each executed query still binds its (honest) consumption.
        for receipt, task in ((no_data, "t-nd"), (suspended, "t-sus"), (denied, "t-deny")):
            self.assertTrue(receipt["consumption_recorded"])
            with self.intake() as intake:
                self.assertEqual(
                    intake.find_query_consumption(receipt["intent_id"])["task_id"], task)

    def test_gate_refusal_reported_without_execution(self):
        receipt = self.attach_with_registry(purpose="undisclosed purpose")
        self.assertEqual(receipt["supply_status"], "gate_refused")
        self.assertFalse(receipt.get("intent_id"),
                         "a refused purpose never reserves or consumes")


class A53ReplayTests(R11SupplyTestCase):
    def test_same_params_replay_never_reserves_or_double_binds(self):
        first = self.attach_with_registry()
        transport = FileFixtureTransport(
            self.root / "config/candidates/sources/file_fixtures.json")
        second = self.attach_with_registry(task_id="task-second", transport=transport, allow_cache=False)
        self.assertEqual(second["supply_status"], "replay")
        self.assertEqual(second["intent_id"], first["intent_id"])
        self.assertEqual(transport.requests, [],
                         "a replay never reaches the provider")
        with self.intake() as intake:
            consumptions = intake.consumptions_for_task("task-second")
            self.assertEqual(consumptions, [],
                             "the replay binds no second consumption")
            # And the original binding is untouched.
            original = intake.find_query_consumption(first["intent_id"])
            self.assertEqual(original["task_id"], "task-a51")


class A54MetadataLeadsTests(R11SupplyTestCase):
    def test_announcement_metadata_is_leads_not_evidence(self):
        receipt = self.attach_with_registry(
            api_id="file_announcements", source_id="file_news",
            params={"query": "甲公司 年报"}, task_id="t-a54",
            data_kind="announcement_metadata")
        self.assertEqual(receipt["supply_status"], "data_delivered")
        context = self.spec["public_context"]
        self.assertIn("announcement_leads", context)
        self.assertEqual(context["announcement_leads"][0]["标题"], "甲公司年报")
        # A54: the lead NEVER becomes an evidence summary — the original is
        # still missing until an explicit vault import happens.
        self.assertEqual(context.get("evidence_summaries", {}), {})
        self.assertNotIn("甲公司年报", json.dumps(
            context.get("evidence_summaries", {}), ensure_ascii=False))


class FailureModeTests(R11SupplyTestCase):
    def test_missing_fixture_file_refused(self):
        config = dict(self.config)
        config["kimi_sources"] = dict(self.config["kimi_sources"],
                                      file_fixture_path="config/candidates/sources/absent.json")
        with self.assertRaises(QuerySupplyError):
            self.attach(config=config)


if __name__ == "__main__":
    unittest.main()
