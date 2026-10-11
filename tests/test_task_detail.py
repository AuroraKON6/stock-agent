"""GLM-06 minimal case detail: one read-only projection per research task."""
import json
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from stock_agent.config import load_config, project_path_override
from stock_agent.task_detail import task_detail
from stock_agent.task_runtime import TaskStore
from stock_agent.task_supply import build_task_spec

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _vault_fixture import import_document, vault_config

NOW = datetime.now(timezone.utc)


def _output(task_id, summary="阶段草稿"):
    return {
        "schema_version": "1.0", "task_id": task_id, "summary": summary,
        "key_findings": ["finding-1"], "unknowns": ["unknown-1"],
        "evidence_refs": [], "human_questions": [], "model_self_assessed_confidence": 0.5,
    }


class TaskDetailTestCase(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.enterContext(project_path_override(lambda value: self.root / value))
        self.config = vault_config(self.root, {})
        self.config["task_runtime"] = {"db_path": str(self.root / "tasks.sqlite3")}
        self.store = TaskStore(self.config["task_runtime"]["db_path"])
        self.addCleanup(self.store.close)

    def make_task(self, *, execution_mode="replay", with_rounds=False):
        spec = build_task_spec(research_question="评估该公告", symbols=["600519"],
                               execution_mode=execution_mode, config=self.config)
        spec["bound_signal_ids"] = []
        spec["origin"] = {"kind": "question", "symbols": ["600519"]}
        task, _ = self.store.create_task(title="详情任务", input_payload=spec,
                                         execution_mode=execution_mode)
        if with_rounds:
            claimed = self.store.claim_next("w1")
            self.store.mark_running(task["task_id"], worker_id="w1",
                                    fencing_token=claimed["fencing_token"],
                                    expected_version=claimed["version"])
            self.store.record_research_round(
                task["task_id"], worker_id="w1", fencing_token=claimed["fencing_token"],
                record={"round": 1, "final": False, "actual_model": "deepseek-flash",
                        "intent_results": [{"status": "ok", "action": "read_public_quote_snapshot"}]})
        return task

    def test_detail_shows_identity_status_versions_and_question(self):
        task = self.make_task()
        detail = task_detail(self.store, task["task_id"], config=self.config)
        self.assertEqual(detail["task_id"], task["task_id"])
        self.assertEqual(detail["status"], "queued")
        self.assertEqual(detail["input_version"], 1)
        self.assertEqual(detail["row_version"], task["version"])
        self.assertEqual(detail["origin"]["kind"], "question")
        self.assertEqual(detail["research_question"], "评估该公告")
        self.assertEqual(detail["scope_note"], "personal_research_only_not_institutional")

    def test_detail_shows_output_questions_and_errors(self):
        task = self.make_task()
        claimed = self.store.claim_next("w1")
        self.store.mark_running(task["task_id"], worker_id="w1",
                                fencing_token=claimed["fencing_token"],
                                expected_version=claimed["version"])
        self.store.request_awaiting_human(
            task["task_id"], worker_id="w1", fencing_token=claimed["fencing_token"],
            human_request={"question": "需要补充哪季报？", "why_needed": "缺上下文",
                           "options": ["Q3", "年报"]},
            output=_output(task["task_id"]),
            model_call={"mode": "replay", "called": False, "rounds": [],
                        "context_delivery": {"delivered_quotes": ["600519"], "dropped_evidence": []}})
        detail = task_detail(self.store, task["task_id"], config=self.config)
        self.assertEqual(detail["status"], "awaiting_human")
        self.assertEqual(detail["output"]["summary"], "阶段草稿")
        self.assertEqual(detail["pending_human_questions"][0]["question"], "需要补充哪季报？")
        self.assertEqual(detail["evidence_delivery"]["delivered_quotes"], ["600519"])

    def test_detail_shows_research_rounds_and_model_facts(self):
        task = self.make_task(with_rounds=True)
        detail = task_detail(self.store, task["task_id"], config=self.config)
        self.assertEqual(len(detail["research_rounds"]), 1)
        self.assertEqual(detail["research_rounds"][0]["actual_model"], "deepseek-flash")
        self.assertEqual(detail["research_rounds"][0]["intent_results"][0]["action"],
                         "read_public_quote_snapshot")

    def test_detail_read_only_and_unknown_task_rejected(self):
        from stock_agent.task_runtime import TaskRuntimeError

        task = self.make_task()
        before = json.dumps(self.store.get_task(task["task_id"]), sort_keys=True, default=str)
        with self.assertRaises(TaskRuntimeError):
            task_detail(self.store, "task_does_not_exist", config=self.config)
        after = json.dumps(self.store.get_task(task["task_id"]), sort_keys=True, default=str)
        self.assertEqual(before, after)
        self.assertEqual([e["event_type"] for e in self.store.events(task["task_id"])],
                         ["task_created"])

    def test_board_http_serves_detail_json(self):
        import threading
        import urllib.request
        from stock_agent.task_board import create_task_board_server

        task = self.make_task()
        server = create_task_board_server(self.store, host="127.0.0.1", port=0, config=self.config)
        port = server.server_address[1]
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.shutdown)
        self.addCleanup(server.server_close)
        with urllib.request.urlopen(
                "http://127.0.0.1:{0}/api/v1/tasks/{1}".format(port, task["task_id"]),
                timeout=5) as response:
            payload = json.loads(response.read().decode("utf-8"))
        self.assertEqual(payload["task_id"], task["task_id"])
        self.assertIn("research_rounds", payload)
        with self.assertRaises(urllib.error.HTTPError) as raised:
            urllib.request.urlopen(
                "http://127.0.0.1:{0}/api/v1/tasks/task_missing".format(port), timeout=5)
        self.assertEqual(raised.exception.code, 404)

    def test_cli_task_detail_prints_json(self):
        import contextlib
        import io
        from unittest.mock import patch as argv_patch

        from stock_agent.cli import main

        task = self.make_task()
        config_file = self.root / "config.json"
        config_file.write_text(json.dumps(self.config), encoding="utf-8")
        with argv_patch.object(sys, "argv", ["stock_agent", "task-detail", "--config", str(config_file),
                                             "--task-id", task["task_id"]]), \
                contextlib.redirect_stdout(io.StringIO()) as captured:
            main()
        payload = json.loads(captured.getvalue().split("consumption_ledger:")[0])
        self.assertEqual(payload["task_id"], task["task_id"])


if __name__ == "__main__":
    unittest.main()
