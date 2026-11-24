"""Start the loopback façade for the isolated R17 role-flow drill.

Same boundary as ``replay_api`` (replay tasks only, status_only reads), plus
the controlled role entry bound to ONE reviewed canvas digest and the frozen
flow artifacts mounted read-only. No provider credential exists here: the
model boundary stays with the managed Python deployment.
"""

import hashlib
import json
import os
from pathlib import Path

from stock_agent.config import project_path_override
from stock_agent.dify_api import DifyGateway
from stock_agent.dify_api_server import serve_dify_api
from stock_agent.role_execution import ArtifactFlowRegistry
from stock_agent.task_runtime import TaskStore


class RoleGateway(DifyGateway):
    def create_research(self, token, *, execution_mode="", **request):
        if execution_mode != "replay":
            raise ValueError("R17 accepts replay tasks only")
        return super().create_research(token, execution_mode=execution_mode, **request)

    def research_status(self, token, *, task_id, status_only=False):
        if status_only is not True:
            raise ValueError("R17 requires status_only")
        return super().research_status(token, task_id=task_id, status_only=True)


def _digest(name):
    value = os.environ[name]
    import re
    if not re.fullmatch(r"sha256:[a-f0-9]{64}", value):
        raise ValueError(f"{name} must be a SHA-256 digest")
    return value


def main():
    root = Path("/srv/r16")
    flows = Path("/srv/r17/flows")
    role_version = _digest("R17_APPROVED_WORKFLOW_VERSION")
    manifest = json.loads((flows / "event-role-flow.manifest.json").read_bytes())
    canvas_version = "sha256:" + hashlib.sha256((flows / "event-role-flow.yml").read_bytes()).hexdigest()
    if role_version != manifest["workflow_version"] or role_version != canvas_version:
        raise ValueError("mounted R17 canvas does not match its approved digest")
    role_flows = ArtifactFlowRegistry.from_directory(flows)
    flow_sha256 = role_flows.by_digest(manifest["flow_sha256"])["flow_sha256"]
    config = {"dify": {
        "enabled": True,
        "approved_workflow_versions": [_digest("R16_APPROVED_WORKFLOW_VERSION"), role_version],
        "service_credentials_path": "/etc/stock-agent/replay-credentials.json",
    }}
    store = TaskStore(root / "data" / "tasks.sqlite3")
    try:
        # DifyGateway resolves its ledger and credential paths at construction.
        with project_path_override(root.joinpath):
            gateway = RoleGateway(
                store, config, role_flows=role_flows,
                role_replay_dir="/srv/r17/replay", role_workflow_version=role_version,
                role_flow_sha256=flow_sha256, role_bind_enabled=os.environ.get("R17_ROLE_ENTRY") == "enabled")
        serve_dify_api(gateway, host="127.0.0.1", port=8771, replay_forms=True)
    finally:
        store.close()


if __name__ == "__main__":
    main()
