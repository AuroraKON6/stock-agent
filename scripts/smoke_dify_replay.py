"""Provision or exercise one synthetic R16 case in a disposable Dify project."""

import argparse
import base64
import hashlib
import json
import secrets
import subprocess
import time
from collections import Counter
from datetime import datetime, timezone
from http.cookiejar import CookieJar
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import HTTPCookieProcessor, ProxyHandler, Request, build_opener


ROOT = Path(__file__).resolve().parents[1]
DEPLOY = ROOT / "deploy/dify"
FLOW = ROOT / "config/candidates/dify/flows/single-research-replay.yml"
PROJECT = "stock-agent-r16-konm13"
CONSOLE = "http://127.0.0.1:18080/console/api"
EMAIL = "r16-replay@example.com"
CASE_ID = "case-r16-container-smoke"
QUESTION = '合成公告的影响？"引号"\n下一行 + & %'


def prepare(directory: Path) -> None:
    directory.mkdir(mode=0o700)
    version = json.loads(FLOW.with_suffix(".manifest.json").read_bytes())["workflow_version"]
    token = secrets.token_hex(32)
    registry = directory / "replay-credentials.json"
    registry.write_text(json.dumps({
        "schema_version": "1.0",
        "credentials": [{
            "credential_id": "r16-container-smoke",
            "token_sha256": hashlib.sha256(token.encode()).hexdigest(),
            "scopes": ["create_research", "read_research"],
            "case_scope": [CASE_ID],
            "active": True,
            "created_at": datetime.now(timezone.utc).isoformat(),
        }],
    }), encoding="utf-8")
    registry.chmod(0o644)
    values = {
        line.split("=", 1)[0]: secrets.token_hex(24)
        for line in (DEPLOY / "compose.example.env").read_text().splitlines()
        if line.startswith("R16_")
    }
    values.update(
        R16_AGENT_SERVER_KEY=secrets.token_urlsafe(32),
        R16_APPROVED_WORKFLOW_VERSION=version,
        R16_REPLAY_CREDENTIALS_FILE=str(registry),
    )
    (directory / "compose.env").write_text(
        "".join(f"{key}={value}\n" for key, value in values.items()), encoding="utf-8"
    )
    (directory / "console.json").write_text(json.dumps({
        "workflow_version": version, "replay_token": token,
        "password": "R16-Test-" + secrets.token_hex(12),
    }), encoding="utf-8")
    for name in ("compose.env", "console.json"):
        (directory / name).chmod(0o600)


class Console:
    def __init__(self):
        self.cookies = CookieJar()
        self.opener = build_opener(ProxyHandler({}), HTTPCookieProcessor(self.cookies))

    def request(self, path: str, payload: dict | None = None):
        body = json.dumps(payload).encode() if payload is not None else None
        request = Request(CONSOLE + path, body, {
            "Content-Type": "application/json", "Accept": "application/json, text/event-stream",
        })
        for cookie in self.cookies:
            if cookie.name in {"csrf_token", "__Host-csrf_token"}:
                request.add_header("X-CSRF-Token", cookie.value)
        try:
            return self.opener.open(request, timeout=30)
        except HTTPError as exc:
            exc.close()
            raise RuntimeError(f"{path}: HTTP {exc.code}") from None

    def read(self, path: str, payload: dict | None = None):
        with self.request(path, payload) as response:
            return json.load(response)

    def run_case(self, app: str) -> None:
        inputs = {
            "title": "Synthetic corporate announcement",
            "case_id": CASE_ID,
            "research_question": QUESTION,
            "input_version": 1,
            "business_intent_id": "r16-container-smoke",
        }
        nodes = Counter()
        terminal = {}
        receipt_id = ""
        started = time.monotonic()
        with self.request(app + "/workflows/draft/run", {"inputs": inputs}) as response:
            if response.headers.get_content_type() != "text/event-stream":
                raise RuntimeError("workflow response is not SSE")
            for line in response:
                if time.monotonic() - started > 60:
                    raise TimeoutError("workflow SSE exceeded 60 seconds")
                if not line.startswith(b"data:"):
                    continue
                event = json.loads(line[5:])
                data = event.get("data") or {}
                if event.get("event") == "node_finished":
                    node_type = data.get("node_type")
                    nodes[node_type] += 1
                    if node_type == "http-request" and data.get("node_id") == "16003":
                        outputs = data.get("outputs") or {}
                        if outputs.get("status_code") == 200:
                            receipt = json.loads(outputs.get("body") or "{}")
                            if receipt.get("ok") is True and receipt["data"]["case_id"] == CASE_ID:
                                receipt_id = receipt["data"]["task_id"]
                if event.get("event") == "workflow_finished":
                    terminal = data
                    break
        outputs = terminal.get("outputs") or {}
        print(json.dumps({
            "workflow_status": terminal.get("status"), "workflow_run_id": terminal.get("id"),
            "nodes": nodes, "receipt_task_id": receipt_id,
            "outputs": {key: outputs.get(key) for key in ("status", "task_id", "case_id", "case_path")},
            "elapsed_seconds": round(time.monotonic() - started, 2),
        }), flush=True)
        if (terminal.get("status") != "succeeded" or not receipt_id
                or nodes["http-request"] != 2 or nodes["code"]
                or outputs.get("status") != "queued" or outputs.get("case_id") != CASE_ID
                or outputs.get("task_id") != receipt_id or outputs.get("case_path") != "/?task=" + receipt_id):
            raise RuntimeError("workflow did not return a confirmed single-case replay receipt")


def run(directory: Path) -> None:
    compose = ["docker", "compose", "-p", PROJECT, "--env-file", str(directory / "compose.env"),
               "-f", str(DEPLOY / "compose.yaml")]
    if not subprocess.check_output(compose + ["ps", "-q", "--status", "running", "nginx"]).strip():
        raise RuntimeError(f"start the disposable {PROJECT} project before exercising its console")
    # Compose can report the worker as running before Celery consumes workflows.
    deadline = time.monotonic() + 180
    while True:
        logs = subprocess.check_output(compose + ["logs", "--no-color", "--tail", "80", "worker"], timeout=20)
        if any(b"celery@" in line and line.rstrip().endswith(b" ready.") for line in logs.splitlines()):
            break
        if time.monotonic() >= deadline:
            raise TimeoutError("Dify Celery worker did not become ready in 180 seconds")
        time.sleep(2)
    print("Dify Celery worker ready", flush=True)
    config = json.loads((directory / "console.json").read_bytes())
    console = Console()
    if console.read("/setup")["step"] != "finished":
        console.read("/setup", {
            "email": EMAIL, "name": "R16 replay smoke", "password": config["password"], "language": "en-US",
        })
    if console.read("/login", {
        "email": EMAIL, "password": base64.b64encode(config["password"].encode()).decode(),
        "language": "en-US", "remember_me": False,
    })["result"] != "success":
        raise RuntimeError("Dify login failed")
    imported = console.read("/apps/imports", {"mode": "yaml-content", "yaml_content": FLOW.read_text()})
    if imported["status"] == "pending":
        imported = console.read(f"/apps/imports/{imported['id']}/confirm", {})
    print("DSL import:", imported["status"], flush=True)
    app = "/apps/" + imported["app_id"]
    draft = console.read(app + "/workflows/draft")
    values = {"STOCK_AGENT_SERVICE_SECRET": config["replay_token"],
              "STOCK_AGENT_WORKFLOW_VERSION": config["workflow_version"]}
    variables = [
        dict(variable, value=values.get(variable["name"], variable.get("value")))
        for variable in draft["environment_variables"]
    ]
    console.read(app + "/workflows/draft", {
        "hash": draft["hash"], "graph": draft["graph"], "features": draft["features"],
        "environment_variable_patch": {"environment_variables": variables},
        "conversation_variables": draft["conversation_variables"],
    })
    console.read(app + "/workflows/publish", {"marked_name": "R16 synthetic replay"})
    console.run_case(app)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("step", choices=("prepare", "run"))
    parser.add_argument("directory", type=Path)
    args = parser.parse_args()
    (prepare if args.step == "prepare" else run)(args.directory.resolve())


if __name__ == "__main__":
    main()
