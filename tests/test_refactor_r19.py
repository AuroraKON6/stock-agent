"""R19: inbound Research MCP — A86—A89 over the shared service layer.

REST and MCP over the same intent produce the same task and audit
semantics; scope/case walls hold; the tool table is scope-filtered;
candidate notes never flip protected state.
"""
import hashlib
import json
import sys
import tempfile
import unittest
from pathlib import Path

from stock_agent.config import project_path_override
from stock_agent.research_mcp import (
    TOOL_TABLE,
    ResearchMCPError,
    ResearchMCPServer,
)
from stock_agent.task_intake import ensure_question_task
from stock_agent.task_runtime import TaskStore
from stock_agent.task_supply import build_task_spec

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _vault_fixture import vault_config

READ_TOKEN = "mcp-reader"
WRITE_TOKEN = "mcp-writer"


def _credentials():
    def entry(credential_id, token, scopes, case_scope):
        return {
            "credential_id": credential_id,
            "token_sha256": hashlib.sha256(token.encode("utf-8")).hexdigest(),
            "scopes": list(scopes), "case_scope": list(case_scope),
            "active": True, "created_at": "2026-10-23T00:00:00+08:00",
            "expires_at": "",
        }
    return {"schema_version": "1.0", "credentials": [
        entry("reader", READ_TOKEN, ["read_research"], ["case-r19"]),
        entry("writer", WRITE_TOKEN,
              ["read_research", "create_research", "submit_result"], ["*"]),
    ]}


class R19TestCase(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.enterContext(project_path_override(lambda value: self.root / value))
        self.config = vault_config(self.root, {})
        self.db_path = self.root / "tasks.sqlite3"
        self.server = ResearchMCPServer(
            credentials=_credentials(),
            store_factory=lambda: TaskStore(self.db_path),
            config=self.config)

    def _task(self, question, case_id="case-r19", actor="operator"):
        spec = build_task_spec(research_question=question, symbols=["600519"],
                               execution_mode="replay", config=self.config)
        spec["bound_signal_ids"] = []
        spec["public_context"]["evidence_summaries"] = {}
        spec["replay_response"] = {
            "schema_version": "1.0", "task_id": "placeholder",
            "summary": "s", "key_findings": [], "unknowns": [],
            "evidence_refs": [], "human_questions": [],
            "model_self_assessed_confidence": 0.3}
        store = TaskStore(self.db_path)
        try:
            task, _ = store.create_task(title=question[:40], input_payload=spec,
                                        case_id=case_id, execution_mode="replay")
        finally:
            store.close()
        return task


class ToolTableTests(R19TestCase):
    def test_a88_tools_list_scope_filtered(self):
        reader_tools = self.server.tools_list(READ_TOKEN)
        names = [t["name"] for t in reader_tools]
        self.assertNotIn("research.submit_intent", names,
                         "a read-only credential never sees write tools exist")
        self.assertNotIn("research.submit_candidate_note", names)
        writer_tools = self.server.tools_list(WRITE_TOKEN)
        self.assertIn("research.submit_intent", [t["name"] for t in writer_tools])
        self.assertEqual(len(reader_tools) + 2, len(writer_tools))

    def test_unknown_tool_is_unknown_even_with_scopes(self):
        with self.assertRaises(ResearchMCPError) as caught:
            self.server.tools_call(WRITE_TOKEN, "research.delete_everything", {})
        self.assertIn("unknown_tool", str(caught.exception))
        self.assertNotIn("research.delete_everything", TOOL_TABLE)


class A86SameSemanticsTests(R19TestCase):
    def test_rest_and_mcp_same_intent_one_task_one_audit_trail(self):
        intent = "qi-r19-shared"
        # REST side: the question intake service.
        store = TaskStore(self.db_path)
        try:
            rest = ensure_question_task(
                store, research_question="同意图问题？", symbols=["600519"],
                config=self.config, execution_mode="replay", intent_id=intent)
        finally:
            store.close()
        # MCP side: the same intent through the inbound face.
        mcp = self.server.tools_call(WRITE_TOKEN, "research.submit_intent", {
            "question": "同意图问题？", "symbols": ["600519"], "intent_id": intent})
        self.assertEqual(mcp["outcome"], "DUPLICATE",
                         "the MCP call found the SAME task — no second task")
        self.assertEqual(mcp["task_id"], rest["task"]["task_id"])
        store = TaskStore(self.db_path)
        try:
            self.assertEqual(len(store.list_tasks()), 1)
            events = store.events(rest["task"]["task_id"])
        finally:
            store.close()
        self.assertEqual([e["event_type"] for e in events], ["task_created"],
                         "one creation event — the audit trail is shared, not doubled")

    def test_submit_intent_requires_write_scope(self):
        with self.assertRaises(ResearchMCPError) as caught:
            self.server.tools_call(READ_TOKEN, "research.submit_intent", {
                "question": "q", "symbols": ["600519"]})
        self.assertIn("scope_not_granted", str(caught.exception))


class A87WallTests(R19TestCase):
    def test_case_wall_holds_for_reads_and_unknown_shares_one_reason(self):
        task = self._task("越权案件问题？", case_id="case-secret")
        for arguments in ({"task_id": task["task_id"]},
                          {"task_id": "task-does-not-exist"}):
            with self.subTest(arguments=arguments):
                with self.assertRaises(ResearchMCPError) as caught:
                    self.server.tools_call(READ_TOKEN, "research.get_case", arguments)
                self.assertIn("case_out_of_scope", str(caught.exception))
        # The same reader CAN read an in-scope case.
        in_scope = self._task("范围内案件问题？", case_id="case-r19")
        result = self.server.tools_call(READ_TOKEN, "research.get_case",
                                        {"task_id": in_scope["task_id"]})
        self.assertEqual(result["task_id"], in_scope["task_id"])

    def test_search_filters_to_case_scope(self):
        self._task("范围内搜索词", case_id="case-r19")
        self._task("范围外搜索词", case_id="case-other")
        result = self.server.tools_call(READ_TOKEN, "research.search_cases",
                                        {"query": "搜索词"})
        self.assertEqual(len(result["matches"]), 1)
        self.assertEqual(result["matches"][0]["case_id"], "case-r19")


class A88NoteTests(R19TestCase):
    def test_candidate_note_is_data_not_approval(self):
        task = self._task("候选备注案件", case_id="case-r19")
        result = self.server.tools_call(WRITE_TOKEN, "research.submit_candidate_note", {
            "task_id": task["task_id"], "note": "外部观察：需要复核口径"})
        self.assertTrue(result["recorded"])
        self.assertEqual(result["disposition"], "candidate_note_not_approval")
        self.assertFalse(result["protected_state_changed"])
        store = TaskStore(self.db_path)
        try:
            fresh = store.get_task(task["task_id"])
        finally:
            store.close()
        self.assertEqual(fresh["status"], "queued",
                         "the note changed no protected state")
        self.assertNotIn("personal_acknowledgement", fresh.get("output") or {})
        for forbidden in ("institutional_approval", "quality_approval"):
            self.assertNotIn(forbidden, fresh.get("output") or {})


class JobStatusTests(R19TestCase):
    def test_job_status_projection(self):
        task = self._task("状态查询案件", case_id="case-r19")
        result = self.server.tools_call(READ_TOKEN, "research.get_job_status",
                                        {"task_id": task["task_id"]})
        self.assertEqual(result["status"], "queued")
        self.assertFalse(result["terminal"])


if __name__ == "__main__":
    unittest.main()
