"""Dump a real board page + state fixtures for the standalone browser check.

Runs under the LOCKED interpreter. Discovery writes only temporary files.
Set BOARD_TEST_OUTPUT_DIR to an external temporary directory to retain the
page and states for tests/browser_board_check.py (use the same variable).
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

from stock_agent.task_board import _render_html

HERE = Path(__file__).resolve().parent
TID = "task_browser_0001"


def _state(status, version, extra_tasks=()):
    tasks = [{
        "task_id": TID, "is_replay": False, "status": status, "title": "浏览器验收任务",
        "input_version": version, "version": version,
        "model_call": {"error": ""}, "last_error": "",
        "human_request": {"question": "是否接受该研究结论？"} if status == "awaiting_human" else {},
    }]
    for i, tid in enumerate(extra_tasks):
        tasks.append({
            "task_id": tid, "is_replay": True, "status": "queued", "title": "其他任务 {0}".format(i),
            "input_version": 1, "version": 1, "model_call": {}, "last_error": "", "human_request": {},
        })
    return {"schema_version": "1.0", "generated_at": "2026-09-21T12:00:00+08:00", "summary": {}, "tasks": tasks}


class DumpBoardPage(unittest.TestCase):
    def test_dump_page_and_states(self):
        with tempfile.TemporaryDirectory() as tmp:
            self._dump(Path(os.environ.get("BOARD_TEST_OUTPUT_DIR") or tmp))

    def _dump(self, output):
        output.mkdir(parents=True, exist_ok=True)
        v1 = _state("awaiting_human", 1)
        page = _render_html(v1, "csrf-token-for-browser-check")
        (output / "board_page.html").write_text(page, encoding="utf-8")
        states = {
            "tid": TID,
            "v1": v1,
            "v2": _state("awaiting_human", 2),
            "v3": _state("awaiting_human", 3),
            # REAL reorder: the focused task moves from first to LAST.
            "reordered": {
                "schema_version": "1.0", "generated_at": "2026-09-21T12:00:00+08:00", "summary": {},
                "tasks": [
                    {"task_id": "task_other_a", "is_replay": True, "status": "queued", "title": "其他任务 A",
                     "input_version": 1, "version": 1, "model_call": {}, "last_error": "", "human_request": {}},
                    {"task_id": "task_other_b", "is_replay": True, "status": "queued", "title": "其他任务 B",
                     "input_version": 1, "version": 1, "model_call": {}, "last_error": "", "human_request": {}},
                    v1["tasks"][0],
                ],
            },
        }
        (output / "board_states.json").write_text(json.dumps(states, ensure_ascii=False), encoding="utf-8")
        self.assertIn(TID, page)


if __name__ == "__main__":
    unittest.main()
