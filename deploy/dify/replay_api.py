"""Start the loopback façade for the isolated R16 Compose project."""

import os
import re
from pathlib import Path

from stock_agent.config import project_path_override
from stock_agent.dify_api import DifyGateway
from stock_agent.dify_api_server import serve_dify_api
from stock_agent.task_runtime import TaskStore


class ReplayGateway(DifyGateway):
    def create_research(self, token, *, execution_mode="", **request):
        if execution_mode != "replay":
            raise ValueError("R16 accepts replay tasks only")
        return super().create_research(token, execution_mode=execution_mode, **request)

    def research_status(self, token, *, task_id, status_only=False):
        if status_only is not True:
            raise ValueError("R16 requires status_only")
        return super().research_status(token, task_id=task_id, status_only=True)


def main():
    root = Path("/srv/r16")
    approved = os.environ["R16_APPROVED_WORKFLOW_VERSION"]
    if not re.fullmatch(r"sha256:[a-f0-9]{64}", approved):
        raise ValueError("R16_APPROVED_WORKFLOW_VERSION must be a SHA-256 digest")
    config = {"dify": {
        "enabled": True,
        "approved_workflow_versions": [approved],
        "service_credentials_path": "/etc/stock-agent/replay-credentials.json",
    }}
    store = TaskStore(root / "data" / "tasks.sqlite3")
    try:
        # DifyGateway resolves its ledger and credential paths at construction.
        with project_path_override(root.joinpath):
            gateway = ReplayGateway(store, config)
        serve_dify_api(gateway, host="127.0.0.1", port=8771, replay_forms=True)
    finally:
        store.close()


if __name__ == "__main__":
    main()
