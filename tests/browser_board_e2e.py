"""Real HTTP + SSE end-to-end browser check for the task board.

Standalone (no project imports). It launches the REAL board server and CLI
under the locked interpreter as subprocesses, seeds a replay research task
that lands in awaiting_human, then drives real Chromium against the live
HTTP page: typing a note, submitting the feedback form (real POST with the
page CSRF), and watching the SSE-driven refresh update the row to succeeded
with the dirty state cleared.

Usage: python tests/browser_board_e2e.py <project_root>
"""

from __future__ import annotations

import json
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(sys.argv[1] if len(sys.argv) > 1 else ".").resolve()
PY = ROOT / ".venv312" / "Scripts" / "python.exe"

CHECKS = []


def check(name, condition, detail=""):
    CHECKS.append((name, bool(condition), detail))
    print("{0} {1}{2}".format("PASS" if condition else "FAIL", name, (" :: " + detail) if detail and not condition else ""))


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def main() -> int:
    from playwright.sync_api import sync_playwright

    port = free_port()
    with tempfile.TemporaryDirectory() as tmp:
        # Resolve every default business path in the disposable workspace, in
        # every CLI/server process. Never seed replay tasks in the real queue.
        config_path = Path(tmp) / "config.json"
        config = json.loads((ROOT / "config/watchlist.example.json").read_text(encoding="utf-8"))
        config.setdefault("research_operations", {})["enforce_roster"] = False
        config.setdefault("llm_governance", {}).update({"enabled": True, "enforce": True})
        restrictions = Path(tmp) / "restrictions.json"
        restrictions.write_text('{"entries":[]}', encoding="utf-8")
        config["research_operations"]["restrictions_path"] = str(restrictions)
        config.setdefault("task_runtime", {})["db_path"] = str(Path(tmp) / "tasks.sqlite3")
        config_path.write_text(json.dumps(config), encoding="utf-8")
        CONFIG = str(config_path)
        bootstrap = (
            "import sys,runpy; from pathlib import Path; "
            "sys.path.insert(0,sys.argv.pop(1)); "
            "from scripts.run_isolated_tests import install_guard; "
            "import stock_agent.config as c; install_guard(c.PROJECT_ROOT); "
            "c.PROJECT_ROOT=Path(sys.argv.pop(1)); "
            "runpy.run_module('stock_agent',run_name='__main__')"
        )
        command = [str(PY), "-B", "-c", bootstrap, str(ROOT), tmp]
        spec = Path(tmp) / "e2e_spec.json"
        spec.write_text(json.dumps({
            "research_question": "浏览器端到端验收：公开事件研究",
            "data_classification": "public",
            "public_context": {},
            "replay_response": {
                "schema_version": "1.0", "task_id": "placeholder",
                "summary": "E2E REPLAY 草稿：等待人工签收。",
                "key_findings": [], "unknowns": [], "evidence_refs": [],
                "human_questions": [{"question": "是否接受该结论？", "why_needed": "e2e", "options": []}],
                "model_self_assessed_confidence": 0.3,
            },
        }, ensure_ascii=False), encoding="utf-8")

        create = subprocess.run(command + ["task-create", "--config", CONFIG,
                                 "--title", "浏览器E2E任务", "--spec", str(spec),
                                 "--case-id", "browser-e2e-{0}".format(int(time.time())), "--execution-mode", "replay"],
                                cwd=ROOT, capture_output=True, text=True, timeout=120)
        check("cli_task_create", create.returncode == 0, create.stdout + create.stderr)
        if create.returncode:
            return 1
        run = subprocess.run(command + ["task-run-next", "--config", CONFIG,
                              "--worker-id", "browser-e2e"], cwd=ROOT, capture_output=True, text=True, timeout=180)
        check("cli_task_awaits_human", "awaiting_human" in run.stdout, run.stdout + run.stderr)
        if run.returncode or "awaiting_human" not in run.stdout:
            return 1
        import re

        match = re.search(r"task_[0-9a-f]+", create.stdout)
        task_id = match.group(0) if match else ""

        server = subprocess.Popen(command + ["task-board", "--config", CONFIG,
                                   "--port", str(port)], cwd=ROOT, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            deadline = time.monotonic() + 15
            while True:
                try:
                    with socket.create_connection(("127.0.0.1", port), timeout=0.5):
                        break
                except OSError:
                    if server.poll() is not None or time.monotonic() >= deadline:
                        raise RuntimeError("isolated board server did not start")
                    time.sleep(0.1)
            with sync_playwright() as p:
                browser = p.chromium.launch(headless=True)
                page = browser.new_page()
                page.goto("http://127.0.0.1:{0}/".format(port), wait_until="domcontentloaded")
                page.wait_for_selector('tr[data-task="{0}"]'.format(task_id), timeout=15000)
                check("live_http_page_shows_task", True)

                notes = page.locator('form[data-task="{0}"] input[name="notes"]'.format(task_id))
                notes.fill("端到端人工备注")
                page.locator('form[data-task="{0}"] input[name="actor"]'.format(task_id)).fill("browser-e2e-user")
                page.locator('form[data-task="{0}"] button[type="submit"]'.format(task_id)).click()

                # The submit handler records, clears dirty and refreshes; SSE
                # events drive the same refresh for other viewers.
                page.wait_for_function("document.getElementById('msg').textContent.indexOf('recorded') !== -1", timeout=15000)
                page.wait_for_selector('tr[data-task="{0}"] td.status-succeeded'.format(task_id), timeout=15000)
                status_cell = page.locator('tr[data-task="{0}"] td.status-succeeded'.format(task_id))
                check("real_post_and_sse_refresh_to_succeeded", status_cell.count() == 1)
                msg = page.locator("#msg").inner_text()
                check("submit_feedback_recorded", "recorded" in msg.lower(), msg)
                conn = page.locator("#conn").inner_text()
                check("sse_connection_live", conn == "LIVE", conn)
                browser.close()
        finally:
            server.terminate()
            try:
                server.wait(timeout=10)
            except subprocess.TimeoutExpired:
                server.kill()
                server.wait(timeout=10)

    failed = [name for name, ok, _ in CHECKS if not ok]
    print("\n{0}/{1} checks passed".format(len(CHECKS) - len(failed), len(CHECKS)))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
