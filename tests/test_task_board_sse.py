"""SSE endpoint tests for the task board (ADR-001 stage one).

Deterministic backlog replay, live push, and Last-Event-ID resume — all
against the explicit 127.0.0.1 loopback via http.client with fixed paths.
"""

from __future__ import annotations

import http.client
import json
import tempfile
import threading
import unittest
from pathlib import Path

from stock_agent.task_board import create_task_board_server
from stock_agent.task_runtime import TaskStore


def _spec() -> dict:
    return {
        "research_question": "q",
        "data_classification": "public",
        "public_context": {},
    }


def _read_event(response) -> dict:
    """Read one SSE event (id/event/data lines until the blank separator)."""

    event = {"id": "", "event": "", "data": ""}
    while True:
        line = response.readline()
        if not line:
            raise AssertionError("SSE stream closed before an event arrived")
        text = line.decode("utf-8").rstrip("\n\r")
        if text == "":
            if event["id"] or event["data"]:
                event["data"] = json.loads(event["data"] or "{}")
                return event
            continue  # skip leading blank / keepalive comment lines
        if text.startswith(":"):
            continue
        if text.startswith("id:"):
            event["id"] = text[3:].strip()
        elif text.startswith("event:"):
            event["event"] = text[6:].strip()
        elif text.startswith("data:"):
            event["data"] = text[5:].strip()


class TaskBoardSseTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = TaskStore(Path(self.tmp.name) / "tasks.sqlite3")
        self.server = create_task_board_server(self.store, host="127.0.0.1", port=0)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.port = self.server.server_address[1]

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.store.close()
        self.tmp.cleanup()

    def _connect(self, last_event_id: int = 0):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        headers = {"Host": "127.0.0.1"}
        if last_event_id:
            headers["Last-Event-ID"] = str(last_event_id)
        conn.request("GET", "/api/v1/events", headers=headers)
        response = conn.getresponse()
        self.assertEqual(response.status, 200)
        self.assertIn("text/event-stream", response.headers.get("Content-Type", ""))
        return conn, response

    def test_backlog_replayed_from_cursor_zero(self):
        self.store.create_task(title="积压任务", input_payload=_spec(), case_id="sse-backlog")
        conn, response = self._connect(last_event_id=0)
        try:
            event = _read_event(response)
            self.assertEqual(event["data"]["event_type"], "task_created")
            self.assertTrue(int(event["id"]) >= 1)
            self.assertIn("sequence", event["data"])
        finally:
            conn.close()

    def test_live_push_and_resume_after_disconnect(self):
        first, _ = self.store.create_task(title="断线前任务", input_payload=_spec(), case_id="sse-live-1")
        last_sequence = max(item["sequence"] for item in self.store.events(limit=500))

        conn, response = self._connect(last_event_id=last_sequence)
        second = None
        try:
            # No backlog expected: create a task while the stream is open and
            # require it to arrive as a live push.
            self.store.create_task(title="断线中任务", input_payload=_spec(), case_id="sse-live-2")
            event = _read_event(response)
            self.assertEqual(event["data"]["event_type"], "task_created")
            resumed_from = int(event["id"])
        finally:
            conn.close()

        # Events land while disconnected...
        third, _ = self.store.create_task(title="断线后任务", input_payload=_spec(), case_id="sse-live-3")

        conn2, response2 = self._connect(last_event_id=resumed_from)
        try:
            missed = _read_event(response2)
            self.assertEqual(missed["data"]["event_type"], "task_created")
            self.assertEqual(missed["data"]["task_id"], third["task_id"])
            self.assertGreater(int(missed["id"]), resumed_from)
        finally:
            conn2.close()


class BoardPageWiringTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = TaskStore(Path(self.tmp.name) / "tasks.sqlite3")
        self.server = create_task_board_server(self.store, host="127.0.0.1", port=0)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.port = self.server.server_address[1]

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.store.close()
        self.tmp.cleanup()

    def test_page_contains_live_wiring(self):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        try:
            conn.request("GET", "/", headers={"Host": "127.0.0.1"})
            response = conn.getresponse()
            html = response.read().decode("utf-8")
        finally:
            conn.close()
        self.assertEqual(response.status, 200)
        # Live refresh wiring: SSE stream, snapshot refetch, async feedback.
        self.assertIn("EventSource('/api/v1/events')", html)
        self.assertIn("fetch('/api/v1/tasks')", html)
        self.assertIn("'/api/v1/human-responses'", html)
        self.assertIn("X-Task-Board-CSRF", html)
        self.assertIn('id="conn"', html)
        self.assertIn("if (!r.ok)", html)
        self.assertIn("!Array.isArray(state.tasks)", html)
        self.assertIn("conn.className = 'live'; refresh();", html)


if __name__ == "__main__":
    unittest.main()
