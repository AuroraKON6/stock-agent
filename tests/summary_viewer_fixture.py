"""Browser-only temporary fixture; stdout is private test IPC, not a user log."""
import json
from pathlib import Path
import sys
import threading
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from scripts.run_isolated_tests import install_guard
install_guard(ROOT)

from _summary_fixture import SummaryFixture, REVIEWER, SID_R
from stock_agent.summary_viewer import create_summary_view_server


class Fixture(SummaryFixture, unittest.TestCase):
    pass


def main():
    fixture = Fixture()
    server = thread = None
    try:
        fixture.setUp()
        draft = fixture.submit(text='Synthetic research draft. Revenue decreased. Literal markup: <img src=x onerror="window.__injected=1">',
                               kind="human_judgment")
        config_path = fixture.root / "config.json"
        config_path.write_text(json.dumps(fixture.config), encoding="utf-8")
        with patch("stock_agent.local_identity.current_windows_subject", return_value=SID_R):
            server = create_summary_view_server(str(config_path), fixture.task_id, REVIEWER, draft["draft_id"],
                                                fixture.version(), lifetime_seconds=int(sys.argv[1]))
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        print(json.dumps({"url": server.origin + "/#" + server.access._bootstrap}), flush=True)
        for line in sys.stdin:
            command = json.loads(line)
            if command == "stop":
                break
            if command == "revoke":
                fixture.roster["principals"][1]["active"] = False
                fixture.save_roster()
            elif command == "change":
                fixture.store.record_human_response(fixture.task_id, actor="synthetic-feedback", decision="acknowledge",
                                                    expected_version=fixture.version(), command_id="browser-feedback")
            else:
                raise ValueError("unknown fixture command")
            print(json.dumps({"done": command}), flush=True)
    finally:
        if server:
            server.shutdown()
            server.server_close()
        if thread:
            thread.join(timeout=5)
        fixture.doCleanups()


if __name__ == "__main__":
    main()
