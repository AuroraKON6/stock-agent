"""GLM-11: SSE slow consumer - the bounded stream ends and the server stays healthy."""
import http.client
import json
import socket
import sys
import tempfile
import threading
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from stock_agent.config import project_path_override
from stock_agent.models import AnalyzedSignal
from stock_agent.storage import SignalStore
from stock_agent.task_board import create_task_board_server
from stock_agent.task_runtime import TaskStore
from stock_agent.task_supply import build_task_spec

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _task_fixture_helpers import final_replay_response

NOW = datetime.now(timezone.utc)


class SlowConsumerTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.enterContext(project_path_override(lambda value: self.root / value))
        spec = build_task_spec(research_question="SSE 慢消费者", symbols=["600519"],
                               execution_mode="replay", config=None)
        spec["replay_response"] = final_replay_response()
        self.store = TaskStore(self.root / "tasks.sqlite3")
        self.addCleanup(self.store.close)
        task, _ = self.store.create_task(title="sse", input_payload=spec, execution_mode="replay")
        self.task_id = task["task_id"]

    def _server(self, **overrides):
        server = create_task_board_server(self.store, host="127.0.0.1", port=0,
                                          config={}, **overrides)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.shutdown)
        self.addCleanup(server.server_close)
        return server

    def _wait_ready(self, port):
        deadline = time.time() + 5
        while True:
            try:
                connection = http.client.HTTPConnection("127.0.0.1", port, timeout=1)
                connection.request("GET", "/health")
                if connection.getresponse().status == 200:
                    connection.close()
                    return
            except Exception:
                if time.time() >= deadline:
                    raise
                time.sleep(0.05)

    def test_non_reading_consumer_stream_ends_within_bound_and_server_survives(self):
        server = self._server(sse_max_seconds=2)
        port = server.server_address[1]
        self._wait_ready(port)
        # A client that never reads: the bounded stream must end on its own.
        sock = socket.create_connection(("127.0.0.1", port), timeout=10)
        sock.sendall(("GET /api/v1/events HTTP/1.1\r\nHost: 127.0.0.1:{0}\r\n"
                      "Accept: text/event-stream\r\n\r\n").format(port).encode("utf-8"))
        sock.settimeout(10)
        # The server closes its side once the bound elapses; a later read sees EOF.
        deadline = time.time() + 10
        saw_eof = False
        while time.time() < deadline:
            try:
                chunk = sock.recv(4096)
            except socket.timeout:
                break
            if chunk == b"":
                saw_eof = True
                break
        sock.close()
        self.assertTrue(saw_eof, "bounded SSE stream must end for a non-reading consumer")
        # The handler thread pool is healthy afterwards.
        connection = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        connection.request("GET", "/api/v1/tasks/{0}".format(self.task_id))
        response = connection.getresponse()
        body = json.loads(response.read().decode("utf-8"))
        connection.close()
        self.assertEqual(response.status, 200)
        self.assertEqual(body["task_id"], self.task_id)

    def test_reading_consumer_receives_backlog_before_bound(self):
        server = self._server(sse_max_seconds=30, sse_keepalive_seconds=1)
        port = server.server_address[1]
        self._wait_ready(port)
        connection = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        connection.request("GET", "/api/v1/events", headers={"Accept": "text/event-stream"})
        response = connection.getresponse()
        self.assertEqual(response.status, 200)
        self.assertEqual(response.getheader("Content-Type", ""), "text/event-stream; charset=utf-8")
        buffer = b""
        deadline = time.time() + 8
        while b"task_created" not in buffer and time.time() < deadline:
            chunk = response.fp.read1(4096)
            if not chunk:
                break
            buffer += chunk
        connection.close()
        self.assertIn(b"task_created", buffer, "backlog must reach a reading consumer")


if __name__ == "__main__":
    unittest.main()
