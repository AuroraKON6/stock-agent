"""Exercise R17 (A76–A79) on a disposable Dify project — one rerunnable entry.

    python3.12 -B scripts/r17_role_drill.py all <evidence-dir> --port 18080

``all`` = prepare → up → run → down. Each step is also selectable on its own
(``prepare``, ``up``, ``run``, ``down``) so a reviewer can inspect the stack
between steps. Every step prints what it does; ``run`` writes one JSON file
per scenario under ``<evidence-dir>/``, plus ``summary.json`` with one
PASS/FAIL line per acceptance item, and exits non-zero on any FAIL.

Metering source: the Dify canvas and the controlled entry are REAL (pinned
images, isolated networks, restricted egress); model answers are RECORDED
replays — every count below is O/C-layer, no provider is called.

Secrets stay in ``<evidence-dir>/private`` (0700) and are redacted from
every saved file. The only resources touched are this project's containers,
networks and volumes; nothing else on the host is stopped or removed.
"""

import argparse
import base64
import hashlib
import json
import os
import socket
import secrets
import shutil
import subprocess
import sys
import time
from collections import Counter
from http.cookiejar import CookieJar
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import HTTPCookieProcessor, ProxyHandler, Request, build_opener

ROOT = Path(__file__).resolve().parents[1]
DEPLOY = ROOT / "deploy/dify"
FLOWS = ROOT / "config/candidates/dify/flows"
FLOW = FLOWS / "event-role-flow.yml"
COMPOSE = [str(DEPLOY / "compose.yaml"), str(DEPLOY / "compose.r17.yaml")]
EMAIL = "r17-role@example.com"

ROLES = ("event_analyst", "counter_evidence", "synthesis_drafter")
STEP_NODES = {"17031": "event_analyst", "17041": "counter_evidence", "17051": "synthesis_drafter"}
# node -> the canvas route it calls
ROUTES = {"17011": "create-research", "17021": "role-bind", "17031": "role-step",
          "17041": "role-step", "17051": "role-step", "17061": "role-finish"}
QUESTION = "合成事件：样例公司公告拟以自有资金回购股份，并终止一项在建项目，对本期业绩和后续经营有何影响？"
TITLE = "Synthetic corporate announcement (frozen input)"

MAIN, RETRY, EXHAUST, QUALITY = "r17-main", "r17-retry", "r17-exhaust", "r17-quality"
CHAIN, CHAIN_QUALITY, PROBE, ROLLBACK = "r17-chain", "r17-chain-quality", "r17-probe", "r17-rollback"
ROLLBACK_PENDING = "r17-rollback-pending"
CASES = (MAIN, RETRY, EXHAUST, QUALITY, CHAIN, CHAIN_QUALITY, PROBE, ROLLBACK, ROLLBACK_PENDING)

ANSWERS = {
    "event_analyst": "事件分析：样例公司公告回购股份并终止在建项目（合成回放）。",
    "counter_evidence": "反证：未发现与公告表述相反的证据（合成回放）。",
    "synthesis_drafter": "综合草稿：事件影响待人工确认（合成回放）。",
    "chain": "整链草稿：事件影响待人工确认（合成回放）。",
}


def answer(summary):
    return {"schema_version": "1.0", "task_id": "placeholder", "summary": summary, "key_findings": [],
            "unknowns": [], "evidence_refs": [], "human_questions": [], "model_self_assessed_confidence": 0.5}


# ---------------------------------------------------------------------- #
# plumbing                                                               #
# ---------------------------------------------------------------------- #

def say(*parts):
    print(*parts, flush=True)


def project(directory: Path) -> str:
    return "stock-agent-r17-" + hashlib.sha256(str(directory).encode()).hexdigest()[:12]


def compose(directory: Path, *args, env=None, check=True, stdin=None, timeout=400):
    command = ["docker", "compose", "--parallel", "2", "-p", project(directory),
               "--env-file", str(directory / "private/compose.env")]
    for file in COMPOSE:
        command += ["-f", file]
    runtime_env = {key: value for key, value in os.environ.items()
                   if not key.startswith(("R16_", "R17_"))}
    runtime_env.update(env or {})
    return subprocess.run(command + list(args), check=check, capture_output=True, text=True,
                          input=stdin, timeout=timeout, env=runtime_env)


def drill(directory: Path, command: str, stdin=None) -> dict:
    """Run one in-container probe; tokens travel on stdin, the reply is one JSON object."""
    result = compose(directory, "exec", "-T", "research_api", "python", "-B", "-m", "r17_drill", command,
                     stdin=json.dumps(stdin or {}), check=False)
    lines = result.stdout.strip().splitlines()
    if result.returncode != 0 or not lines:
        raise RuntimeError("probe {0} failed (exit {1}): {2}".format(
            command, result.returncode, result.stderr.strip()[-600:]))
    payload = json.loads(lines[-1])
    if "probe_error" in payload:
        raise RuntimeError("probe {0} failed: {1}".format(command, payload))
    return payload


class Secrets:
    """Redact every generated secret from text before it is written as evidence."""

    def __init__(self, directory: Path):
        self.values = []
        for line in (directory / "private/compose.env").read_text().splitlines():
            value = line.split("=", 1)[-1].strip()
            if value and not value.startswith("sha256:") and len(value) >= 12:
                self.values.append(value)
        config = json.loads((directory / "private/console.json").read_text())
        self.values += [config[key] for key in ("replay_token", "limited_token", "password", "password_b64")]

    def clean(self, text: str) -> str:
        for value in sorted(self.values, key=len, reverse=True):
            text = text.replace(value, "[REDACTED]")
        return text


def save(directory: Path, name: str, payload, secrets=None):
    text = json.dumps(payload, ensure_ascii=False, indent=2, default=str)
    if secrets:
        text = secrets.clean(text)
    (directory / name).write_text(text + "\n", encoding="utf-8")


# ---------------------------------------------------------------------- #
# Dify console                                                           #
# ---------------------------------------------------------------------- #

class Console:
    def __init__(self, port):
        self.origin = "http://127.0.0.1:{0}".format(port)
        self.cookies = CookieJar()
        self.opener = build_opener(ProxyHandler({}), HTTPCookieProcessor(self.cookies))

    def request(self, path, payload=None, timeout=90):
        body = json.dumps(payload).encode() if payload is not None else None
        request = Request(self.origin + "/console/api" + path, body, {
            "Content-Type": "application/json", "Accept": "application/json, text/event-stream"})
        for cookie in self.cookies:
            if cookie.name in {"csrf_token", "__Host-csrf_token"}:
                request.add_header("X-CSRF-Token", cookie.value)
        try:
            return self.opener.open(request, timeout=timeout)
        except HTTPError as exc:
            detail = exc.read().decode(errors="replace")[:500]
            exc.close()
            raise RuntimeError("{0}: HTTP {1} {2}".format(path, exc.code, detail)) from None

    def read(self, path, payload=None):
        with self.request(path, payload) as response:
            return json.load(response)


def wait_worker(directory: Path):
    """Compose reports the worker running before Celery consumes workflows."""
    worker = compose(directory, "ps", "-q", "worker").stdout.strip()
    if not worker:
        raise RuntimeError("the disposable Dify worker was not started")
    started = subprocess.run(["docker", "inspect", "--format", "{{.State.StartedAt}}", worker],
                             check=True, capture_output=True, text=True, timeout=20).stdout.strip()
    deadline = time.monotonic() + 420
    while time.monotonic() < deadline:
        try:
            logs = subprocess.run(["docker", "logs", "--since", started, "--tail", "200", worker], capture_output=True,
                                  text=True, timeout=20).stdout
        except subprocess.TimeoutExpired:
            continue
        if any("celery@" in line and line.rstrip().endswith(" ready.") for line in logs.splitlines()):
            return
        time.sleep(2)
    raise TimeoutError("Dify Celery worker did not become ready in 420 seconds")


def restart_stack(directory: Path, console):
    """Restart the role gateway AND the Dify engine, then wait until Dify serves again."""
    compose(directory, "restart", "research_api", "api", "api_websocket", "worker", "nginx")
    deadline = time.monotonic() + 420
    while time.monotonic() < deadline:
        try:
            with build_opener(ProxyHandler({})).open(console.origin + "/console/api/setup", timeout=20) as response:
                if response.status == 200:
                    say("  restarted research_api, api, api_websocket, worker and nginx; Dify serving again")
                    return
        except OSError:
            pass
        time.sleep(3)
    raise TimeoutError("the Dify console did not come back within 420 seconds of the restart")


def import_flow(directory: Path, config):
    console = Console(config["port"])
    if console.read("/setup")["step"] != "finished":
        console.read("/setup", {"email": EMAIL, "name": "R17 role drill", "password": config["password"],
                                "language": "en-US"})
    if console.read("/login", {"email": EMAIL, "password": config["password_b64"],
                               "language": "en-US", "remember_me": False})["result"] != "success":
        raise RuntimeError("Dify login failed")
    imported = console.read("/apps/imports", {"mode": "yaml-content", "yaml_content": FLOW.read_text()})
    if imported["status"] == "pending":
        imported = console.read("/apps/imports/{0}/confirm".format(imported["id"]), {})
    say("DSL import:", imported["status"])
    app = "/apps/" + imported["app_id"]
    draft = console.read(app + "/workflows/draft")
    values = {"STOCK_AGENT_SERVICE_SECRET": config["replay_token"],
              "STOCK_AGENT_WORKFLOW_VERSION": config["workflow_version"],
              "STOCK_AGENT_ROLE_FLOW_SHA256": config["flow_sha256"]}
    variables = [dict(variable, value=values.get(variable["name"], variable.get("value")))
                 for variable in draft["environment_variables"]]
    console.read(app + "/workflows/draft", {
        "hash": draft["hash"], "graph": draft["graph"], "features": draft["features"],
        "environment_variable_patch": {"environment_variables": variables},
        "conversation_variables": draft["conversation_variables"]})
    console.read(app + "/workflows/publish", {"marked_name": "R17 role flow drill"})
    published = console.read(app + "/workflows/publish")
    if published["graph"] != draft["graph"]:
        raise RuntimeError("published Dify graph differs from the imported canvas")
    app_key = console.read(app + "/api-keys", {})["token"]
    return console, app_key, {"app_id": imported["app_id"], "import_status": imported["status"],
                              "published_id": published["id"], "dify_version": published["version"],
                              "workflow_version": config["workflow_version"]}


def http_node(data):
    outputs = data.get("outputs") or {}
    headers = {str(key).lower(): value for key, value in (outputs.get("headers") or {}).items()}
    try:
        body = json.loads(outputs.get("body") or "{}")
    except ValueError:
        body = {}
    record = (body.get("data") or {}) if body.get("ok") else {}
    return {
        "node": data.get("node_id"), "route": ROUTES.get(data.get("node_id")),
        "status_code": outputs.get("status_code"), "node_status": data.get("status"),
        "node_error": data.get("error"), "error": body.get("error"),
        "headers": {key: value for key, value in headers.items() if key.startswith("r1")},
        "executed": record.get("executed"), "step_status": record.get("step_status"),
        "outcome": record.get("outcome"), "bind_result": record.get("bind_result"),
        "already_complete": record.get("already_complete"),
    }


def run_flow(console, app_key, publication, case_id, intent):
    """Run the pinned Dify workflow and correlate SSE with its stored node executions."""
    inputs = {"title": TITLE, "case_id": case_id, "research_question": QUESTION, "input_version": 1,
              "business_intent_id": intent}
    sse_calls, nodes, terminal = [], Counter(), {}
    started = time.monotonic()
    request = Request(console.origin + "/v1/workflows/{0}/run".format(publication["published_id"]), json.dumps(
        {"inputs": inputs, "response_mode": "streaming", "user": "r17-role-drill"}).encode(),
        {"Authorization": "Bearer " + app_key, "Content-Type": "application/json", "Accept": "text/event-stream"})
    with console.opener.open(request, timeout=400) as response:
        if response.headers.get_content_type() != "text/event-stream":
            raise RuntimeError("workflow response is not SSE")
        for line in response:
            if time.monotonic() - started > 380:
                raise TimeoutError("workflow SSE exceeded 380 seconds")
            if not line.startswith(b"data:"):
                continue
            event = json.loads(line[5:])
            data = event.get("data") or {}
            if event.get("event") == "node_finished":
                nodes[data.get("node_type")] += 1
                if data.get("node_type") == "http-request":
                    sse_calls.append(http_node(data))
            if event.get("event") == "workflow_finished":
                terminal = data
                break
    if not terminal.get("id"):
        raise RuntimeError("Dify SSE finished without a workflow result")
    path = "/apps/{0}/workflow-runs/{1}/node-executions".format(publication["app_id"], terminal["id"])
    entries = sorted(console.read(path)["data"], key=lambda item: item["index"])
    calls = [http_node(entry) for entry in entries if entry["node_type"] == "http-request"]
    stored = {call["node"]: call for call in calls}
    for observed in sse_calls:
        entry = stored.get(observed["node"])
        if entry is None or any(observed[key] != entry[key] for key in ("status_code", "executed", "step_status")):
            raise RuntimeError("Dify SSE disagrees with the stored result for node " + observed["node"])
    outputs = terminal.get("outputs") or {}
    return {"case_id": case_id, "workflow_status": terminal.get("status"),
            "workflow_run_id": terminal["id"], "published_id": terminal.get("workflow_id"),
            "error": terminal.get("error"), "nodes": dict(nodes), "sse_calls": sse_calls, "calls": calls,
            "node_execution_count": len(entries),
            "outputs": {key: outputs.get(key) for key in ("status", "task_id", "case_id", "case_path")},
            "elapsed_seconds": round(time.monotonic() - started, 2)}


# ---------------------------------------------------------------------- #
# assertions                                                             #
# ---------------------------------------------------------------------- #

class Report:
    def __init__(self):
        self.items = []

    def check(self, item, ok, **evidence):
        self.items.append({"item": item, "result": "PASS" if ok else "FAIL", **evidence})
        say(("PASS " if ok else "FAIL ") + item)
        return ok

    def failed(self):
        return [entry for entry in self.items if entry["result"] == "FAIL"]


def steps_of(snapshot, case_id):
    roots = [task for task in snapshot["tasks"]
             if task["case_id"] == case_id and not task["parent_task_id"]]
    steps = [task for task in snapshot["tasks"] if roots and task["parent_task_id"] == roots[0]["task_id"]]
    return (roots[0] if roots else None), sorted(steps, key=lambda task: task["task_id"])


def by_role(steps):
    return {task["role_id"]: task for task in steps}


# ---------------------------------------------------------------------- #
# steps                                                                  #
# ---------------------------------------------------------------------- #

def prepare(directory: Path, port: int) -> None:
    directory = directory.resolve()
    directory.mkdir(mode=0o700)
    private = directory / "private"
    private.mkdir(mode=0o700)
    manifest = json.loads(FLOW.with_suffix(".manifest.json").read_bytes())
    if manifest["workflow_version"] != "sha256:" + hashlib.sha256(FLOW.read_bytes()).hexdigest():
        raise ValueError("canvas changed since its workflow manifest was generated")
    flow = json.loads((FLOWS / manifest["flow_artifact"]).read_text(encoding="utf-8"))["flow"]
    registry = private / "replay-credentials.json"

    def entry(credential_id, token, scopes):
        return {"credential_id": credential_id, "token_sha256": hashlib.sha256(token.encode()).hexdigest(),
                "scopes": list(scopes), "case_scope": list(CASES), "active": True,
                "created_at": "2026-11-24T00:00:00+00:00"}

    replay_token, limited_token = secrets.token_hex(32), secrets.token_hex(32)
    registry.write_text(json.dumps({"schema_version": "1.0", "credentials": [
        entry("r17-role-orchestrator", replay_token, ["create_research", "run_role_flow"]),
        entry("r17-create-only", limited_token, ["create_research"]),
    ]}), encoding="utf-8")
    registry.chmod(0o644)
    values = {line.split("=", 1)[0]: secrets.token_hex(24)
              for line in (DEPLOY / "compose.r17.example.env").read_text().splitlines()
              if line.startswith("R16_") and "VERSION" not in line and "FILE" not in line}
    replay_dir = private / "replay"
    values.update(
        R16_AGENT_SERVER_KEY=secrets.token_urlsafe(32),
        R16_APPROVED_WORKFLOW_VERSION=json.loads(
            (FLOWS / "single-research-replay.manifest.json").read_bytes())["workflow_version"],
        R16_REPLAY_CREDENTIALS_FILE=str(registry),
        R17_APPROVED_WORKFLOW_VERSION=manifest["workflow_version"],
        R17_ROLE_ENTRY="enabled", R17_UI_PORT=str(port), R17_REPLAY_DIR=str(replay_dir))
    (private / "compose.env").write_text(
        "".join("{0}={1}\n".format(key, value) for key, value in values.items()), encoding="utf-8")
    password = "R17-Test-" + secrets.token_hex(12)
    (private / "console.json").write_text(json.dumps({
        "workflow_version": manifest["workflow_version"], "flow_sha256": flow["flow_sha256"],
        "port": port, "replay_token": replay_token, "limited_token": limited_token, "password": password,
        "password_b64": base64.b64encode(password.encode()).decode()}), encoding="utf-8")
    for name in ("compose.env", "console.json"):
        (private / name).chmod(0o600)

    fixtures = {
        MAIN: {role: answer(ANSWERS[role]) for role in ROLES},
        RETRY: {"event_analyst": answer(ANSWERS["event_analyst"]),
                "counter_evidence.attempt-1": None,
                "counter_evidence.attempt-2": answer(ANSWERS["counter_evidence"]),
                "synthesis_drafter": answer(ANSWERS["synthesis_drafter"])},
        # both attempts of the synthesis step are withheld -> the attempt budget is exhausted
        EXHAUST: {"event_analyst": answer(ANSWERS["event_analyst"]),
                  "counter_evidence": answer(ANSWERS["counter_evidence"]),
                  "synthesis_drafter.attempt-1": None, "synthesis_drafter.attempt-2": None},
        # a specific number with no citation: the R06 quality gate must block this step
        QUALITY: {"event_analyst": answer(ANSWERS["event_analyst"]),
                  "counter_evidence": answer("公告称回购金额为 3 亿元。")},
    }
    fixtures[ROLLBACK_PENDING] = fixtures[RETRY]
    for case_id, files in fixtures.items():
        (replay_dir / case_id).mkdir(parents=True)
        for role, document in files.items():
            (replay_dir / case_id / (role + ".json")).write_text(
                json.dumps(document, ensure_ascii=False), encoding="utf-8")
    for path in replay_dir.rglob("*"):
        path.chmod(0o755 if path.is_dir() else 0o644)
    say("prepared", directory, "| workflow", manifest["workflow_version"][:19], "| flow", flow["flow_sha256"][:19])


GATEWAY_EGRESS_PROBE = (
    "import socket\n"
    "for host, port in (('1.1.1.1', 443), ('db_postgres', 5432)):\n"
    "    try:\n"
    "        socket.create_connection((host, port), 3).close(); print(host, 'OPEN')\n"
    "    except OSError as exc:\n"
    "        print(host, 'DENIED')\n")


def leftovers(directory: Path):
    """Containers, networks and volumes still carrying this project's label."""
    label = "com.docker.compose.project=" + project(directory)
    found = {}
    for kind, args in (("containers", ["docker", "ps", "-aq", "--filter", "label=" + label]),
                       ("networks", ["docker", "network", "ls", "-q", "--filter", "label=" + label]),
                       ("volumes", ["docker", "volume", "ls", "-q", "--filter", "label=" + label])):
        found[kind] = subprocess.run(args, check=True, capture_output=True, text=True).stdout.split()
    return found


def up(directory: Path) -> None:
    if any(leftovers(directory).values()):
        raise RuntimeError("project {0} already has resources".format(project(directory)))
    port = json.loads((directory / "private/console.json").read_text())["port"]
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", port))
    (directory / "private/started").touch()
    say("Compose project:", project(directory), "| loopback UI:", port)
    compose(directory, "config", "--quiet")
    images = sorted(set(compose(directory, "config", "--images").stdout.split()))
    services = compose(directory, "config", "--services").stdout.split()
    say("config --quiet exit 0 | config --images exit 0 |", len(images), "pinned images |", len(services), "services")
    save(directory, "compose-inventory.json", {"images": images, "services": services, "project": project(directory)})
    if len(images) != 13 or len(services) != 19 or any("@sha256:" not in image for image in images):
        raise RuntimeError("rendered stack differs from the pinned R16 inventory")
    compose(directory, "pull", "--policy", "missing")
    # Docker Desktop can hold a stale view of an evidence directory that was
    # removed and recreated under the same path ("bind source path does not
    # exist"), while `docker run -v` on the very path refreshes it. Warm the
    # shared-file view with a read-only mount of each bind source first, then
    # still retry `up` a few times in case the view lags behind.
    busybox = next(image for image in images if image.startswith("busybox:"))
    for source in (directory / "private/replay", directory / "private/replay-credentials.json"):
        subprocess.run(["docker", "run", "--rm", "-v", "{0}:/probe:ro".format(source), busybox, "true"],
                       check=False, capture_output=True, text=True, timeout=120)
    for attempt in range(8):
        started = compose(directory, "up", "-d", "--pull", "never", "--wait", "--wait-timeout", "300",
                          check=False)
        if started.returncode == 0:
            break
        if "bind source path does not exist" not in started.stderr:
            raise RuntimeError("`up` failed: " + started.stderr.strip()[-800:])
        say("  waiting for the shared-file view of the evidence directory ...")
        time.sleep(5)
    else:
        raise RuntimeError("`up` failed: the evidence directory never became visible to Docker")
    wait_worker(directory)
    say("up -d --pull never --wait: exit 0 | Celery worker ready")


def exercise(directory: Path, config, redactor, report, runs):
    token, workflow = config["replay_token"], config["workflow_version"]

    def save_run(name, payload):
        save(directory, name + ".json", payload, redactor)

    def snap(name):
        snapshot = drill(directory, "snapshot")
        save_run("snapshot-" + name, snapshot)
        return snapshot

    def flow_run(label, case_id):
        result = run_flow(console, app_key, publication, case_id, "intent-" + case_id)
        result["label"] = label
        runs.append(result)
        save_run("run-" + label, result)
        say("  Dify run", label, result["workflow_status"],
            [(call["node"], call["status_code"], call["error"] or call["step_status"] or call["outcome"]) for call in result["calls"]])
        return result

    def call(label, path, fields, use=None):
        reply = drill(directory, "http", {"token": use or token, "path": "/api/dify/v1/" + path, "fields": fields})
        save_run("call-" + label, {"path": path, "fields": fields, "reply": reply})
        return reply

    def form(case_id, run_id, **extra):
        """The create-research form (the canvas's fixed creation fields)."""
        return {"case_id": case_id, "dify_run_id": run_id, "workflow_version": workflow, **extra}

    def role(case_id, run_id, **extra):
        """A role-entry form: the create fields plus the approved flow digest."""
        return {**form(case_id, run_id), "flow_sha256": config["flow_sha256"],
                "input_version": "2", **extra}

    def shape(snapshot, case_id):
        """(root status, {role: (status, attempts)}) — the reconciliation view."""
        root, steps = steps_of(snapshot, case_id)
        return ((root or {}).get("status"), {task["role_id"]: (task["status"], task["attempt_count"]) for task in steps})

    console, app_key, publication = import_flow(directory, config)
    redactor.values.append(app_key)
    save_run("dify-publication", publication)

    # ---- A76: a role-bound root is closed to the Python full chain ---- #
    created = call("probe-create", "create-research", form(
        PROBE, "probe-run-1", title=TITLE, input_version="1", research_question=QUESTION,
        business_intent_id="intent-" + PROBE, execution_mode="replay"))
    probe_id = created["data"]["task_id"]
    bound = call("probe-bind", "role-bind", role(
        PROBE, "probe-run-1", task_id=probe_id, input_version=str(created["data"]["input_version"])))
    report.check("A76 binding is an audited input version (1 -> 2) on a Dify-created task",
                 bound["ok"] is True and bound["data"]["bind_result"] == "bound"
                 and bound["data"]["input_version"] == 2, reply=bound["data"])
    a76 = drill(directory, "probe-a76", {"task_id": probe_id})
    save_run("a76-full-chain-probe", a76)
    report.check("A76 the full-chain claim path skips the role-bound root",
                 a76["queued_before"] == [probe_id] and a76["exclude_claim"] is None, probe=a76)
    report.check("A76 the full-chain worker cannot advance it: refused, zero model calls, no output",
                 a76["worker_status"] == "refused_role_flow" and a76["model_called"] is False
                 and a76["model_rounds"] == 0 and a76["status_after"] == "leased" and a76["output_empty"],
                 probe=a76)

    # ---- A78: one real Dify run, bind -> 3 role steps -> finish ------- #
    first = flow_run("01-main", MAIN)
    executed = [call_["route"] for call_ in first["calls"]]
    report.check("A76 real Dify run triggers analysis, counter-evidence, synthesis in order, then finish",
                 first["workflow_status"] == "succeeded" and executed == [
                     "create-research", "role-bind", "role-step", "role-step", "role-step", "role-finish"]
                 and all(call_["status_code"] == 200 for call_ in first["calls"])
                 and all(call_["executed"] is True for call_ in first["calls"][1:])
                 and first["outputs"]["status"] == "role_flow_complete"
                 and first["outputs"]["case_id"] == MAIN
                 and first["outputs"]["task_id"]
                 and first["outputs"]["case_path"] == "/?task=" + first["outputs"]["task_id"], calls=first["calls"])
    happy = snap("01-main")
    root, steps = steps_of(happy, MAIN)
    if not root or not root["platform_runs"] or root["status"] != "succeeded":
        raise RuntimeError("main workflow did not finish: inspect run-01-main.json and snapshot-01-main.json")
    report.check("A78 root completes from 3 step tasks with one attempt each; root itself calls no model",
                 root["status"] == "succeeded" and root["model_call"]["called"] is False
                 and root["model_call"]["steps"] == 3 and len(steps) == 3
                 and [task["attempt_count"] for task in steps] == [1, 1, 1]
                 and set(root["output_keys"]) == {"summary", "role_outputs", "role_flow"},
                 root={key: root[key] for key in ("status", "input_version", "output_keys", "model_call", "summary")},
                 steps=[{key: task[key] for key in ("role_id", "status", "attempt_count", "max_attempts")} for task in steps])
    report.check("A76 the SSE run and root reference the same case and task",
                 bool(first["workflow_run_id"]) and root["platform_runs"] == [first["workflow_run_id"]]
                 and first["published_id"] == publication["published_id"]
                 and root["task_id"] == first["outputs"]["task_id"]
                 and all(task["case_id"] == MAIN and task["parent_task_id"] == root["task_id"] for task in steps))
    role_outputs = drill(directory, "http", {"token": token, "path": "/api/dify/v1/role-finish", "fields": role(
        MAIN, root["platform_runs"][0], task_id=root["task_id"], input_version=str(root["input_version"]))})
    save_run("role-outputs", role_outputs)
    steps_by_role = by_role(steps)
    report.check("A76 root result keeps role provenance (role -> step task, summary, output digest)",
                 {role: item["task_id"] for role, item in role_outputs["data"]["role_outputs"].items()}
                 == {role: steps_by_role[role]["task_id"] for role in ROLES}
                 and all(item["output_sha256"] == steps_by_role[role]["output_sha256"]
                         for role, item in role_outputs["data"]["role_outputs"].items()),
                 role_outputs=role_outputs["data"]["role_outputs"])

    # ---- A79: restart an in-flight task, then continue it ------------- #
    retry1 = flow_run("02-retry-attempt-1", RETRY)
    mid = snap("02-retry-in-flight")
    report.check("A78 a withheld answer costs one bounded attempt: step requeued, canvas halted",
                 retry1["workflow_status"] == "succeeded" and retry1["outputs"]["status"] == "step_halted"
                 and retry1["calls"][-1]["node"] == "17041" and retry1["calls"][-1]["step_status"] == "queued"
                 and shape(mid, RETRY) == ("queued", {"event_analyst": ("succeeded", 1), "counter_evidence": ("queued", 1)}),
                 calls=[(c["node"], c["step_status"]) for c in retry1["calls"]], shape=shape(mid, RETRY))
    mid_steps = steps_of(mid, RETRY)[1]
    restart_stack(directory, console)
    wait_worker(directory)
    retry2 = flow_run("03-retry-continue", RETRY)
    done = snap("03-retry-done")
    done_steps = steps_of(done, RETRY)[1]
    report.check("A79 a new Dify run continues the in-flight root after the restart and completes it",
                 retry2["workflow_status"] == "succeeded" and retry2["outputs"]["status"] == "role_flow_complete"
                 and [c["executed"] for c in retry2["calls"][1:]] == [False, False, True, True, True],
                 calls=[(c["node"], c["executed"], c["step_status"]) for c in retry2["calls"]])
    report.check("A79/A78 nothing paid is redone: analyst keeps 1 attempt, counter 2, synthesis 1, same step tasks",
                 shape(done, RETRY) == ("succeeded", {"event_analyst": ("succeeded", 1),
                                                      "counter_evidence": ("succeeded", 2),
                                                      "synthesis_drafter": ("succeeded", 1)})
                 and {task["task_id"] for task in mid_steps} <= {task["task_id"] for task in done_steps}
                 and len(done_steps) == 3, before=shape(mid, RETRY), after=shape(done, RETRY))

    # ---- A78: a replayed orchestration run re-executes nothing -------- #
    before_replay = snap("04-before-replay")
    replay = flow_run("05-main-replay", MAIN)
    after_replay = snap("05-main-replay")
    fingerprint = lambda snapshot: sorted(  # noqa: E731
        (task["task_id"], task["status"], task["attempt_count"], task["output_sha256"]) for task in snapshot["tasks"])
    report.check("A78/A79 a replayed run re-executes nothing: bind, all three steps and finish report executed=false",
                 replay["workflow_status"] == "succeeded" and len(replay["calls"]) == 6
                 and [c["executed"] for c in replay["calls"][1:]] == [False] * 5
                 and replay["calls"][-1]["already_complete"] is True
                 and replay["workflow_run_id"] != first["workflow_run_id"], calls=[(c["node"], c["executed"]) for c in replay["calls"]])
    report.check("A78 the replay leaves every task, attempt count and output digest untouched",
                 fingerprint(after_replay) == fingerprint(before_replay), tasks=len(after_replay["tasks"]))

    # ---- A78: the attempt budget is finite ---------------------------- #
    exhaust = [flow_run("06-exhaust-attempt-1", EXHAUST), flow_run("07-exhaust-attempt-2", EXHAUST),
               flow_run("08-exhaust-replay", EXHAUST)]
    exhausted = snap("08-exhaust")
    synthesis = by_role(steps_of(exhausted, EXHAUST)[1]).get("synthesis_drafter") or {}
    report.check("A78 attempts stop at the step budget (2), then the step fails terminally — one step task only",
                 synthesis.get("status") == "failed" and synthesis["attempt_count"] == 2 == synthesis["max_attempts"]
                 and len(steps_of(exhausted, EXHAUST)[1]) == 3
                 and exhaust[0]["calls"][-1]["step_status"] == "queued"
                 and exhaust[1]["calls"][-1]["step_status"] == "failed",
                 runs=[[(c["node"], c["step_status"]) for c in run["calls"][2:]] for run in exhaust[:2]],
                 step={k: synthesis.get(k) for k in ("task_id", "status", "attempt_count", "max_attempts", "last_error")})
    again = snap("09-exhaust-replay")
    # A refused node surfaces in the canvas as its default value (status 0);
    # the RECORDED reason is on the gateway ledger for that run.
    refused = [entry for entry in again["ledgers"]["role_flow.jsonl"]
               if entry.get("dify_run_id") == exhaust[2]["workflow_run_id"] and entry["operation"] == "role-step"]
    report.check("A78 after exhaustion another run is refused and spends nothing (decision required)",
                 exhaust[2]["calls"][-1]["node"] == "17051" and exhaust[2]["calls"][-1]["status_code"] == 0
                 and exhaust[2]["outputs"]["status"] == "step_halted"
                 and [entry["reason"] for entry in refused][-1:] == ["step_failed_needs_decision"]
                 and all(entry["executed"] is not True for entry in refused)
                 and fingerprint(again) == fingerprint(exhausted),
                 ledger=[(entry["role_id"], entry["reason"], entry["executed"]) for entry in refused])

    # ---- A78: quality gate --------------------------------------------- #
    quality = flow_run("10-quality", QUALITY)
    blocked = by_role(steps_of(snap("10-quality"), QUALITY)[1])
    report.check("A78 the R06 quality gate blocks an uncited-number role step; synthesis never starts",
                 quality["outputs"]["status"] == "step_halted" and quality["calls"][-1]["node"] == "17041"
                 and blocked["counter_evidence"]["status"] == "blocked"
                 and blocked["counter_evidence"]["model_call"]["gates"] == ["evidence_quality"]
                 and "synthesis_drafter" not in blocked,
                 steps=[(t["role_id"], t["status"], t["model_call"]["gates"]) for t in (blocked["event_analyst"], blocked["counter_evidence"])])

    # ---- A77: the approved artifact is what actually ran -------------- #
    ledger = again["ledgers"]["role_flow.jsonl"]
    spent = [entry for entry in ledger if entry.get("executed") is True]
    report.check("A77 every executed call is ledgered under the approved flow digest and approver",
                 bool(spent) and {entry["flow_sha256"] for entry in spent} == {config["flow_sha256"]}
                 and {entry["flow_id"] for entry in spent} == {"event_draft_flow"}
                 and {entry["approved_by"] for entry in spent} == {"r17-candidate-review"},
                 executed_calls=len(spent), operations=dict(Counter(entry["operation"] for entry in spent)))

    registered = json.loads((FLOWS / "event-role-flow.flow.json").read_text())["flow"]["prompts"]
    report.check("A77 the worker verified each reviewed prompt before running its step",
                 all(task["role_step"]["role_prompt_sha256"] == registered[task["role_id"]]["sha256"]
                     == task["model_call"]["role_prompt_sha256"] for task in steps),
                 steps=[(task["task_id"], task["role_id"], task["model_call"].get("role_prompt_sha256"))
                        for task in steps])

    # ---- A76: the controlled entry refuses what the canvas must not do - #
    before_refusals = snap("11-before-refusals")
    refusals = [
        ("step order (synthesis first)", "role-step", role(PROBE, "probe-run-1", task_id=probe_id, role_id="synthesis_drafter", input_version="2"), 409, "group_not_current"),
        ("role outside the flow", "role-step", role(PROBE, "probe-run-1", task_id=probe_id, role_id="quality_check"), 409, "role_not_in_flow"),
        ("case mismatch", "role-step", role(PROBE, "probe-run-1", task_id=probe_id, role_id="event_analyst") | {"case_id": RETRY}, 403, "task_not_found_or_out_of_scope"),
        ("Dify run not recorded on the task", "role-step", role(PROBE, "run-never-recorded", task_id=probe_id, role_id="event_analyst"), 403, "task_not_found_or_out_of_scope"),
        ("stale input version", "role-step", role(PROBE, "probe-run-1", task_id=probe_id, role_id="event_analyst", input_version="1"), 409, "input_version_mismatch"),
        ("edited prompt (new digest)", "role-step", role(PROBE, "probe-run-1", task_id=probe_id, role_id="event_analyst") | {"flow_sha256": "e" * 64}, 409, "flow_not_registered"),
        ("edited canvas (new digest)", "role-step", role(PROBE, "probe-run-1", task_id=probe_id, role_id="event_analyst") | {"workflow_version": "sha256:" + "0" * 64}, 403, "workflow_version_not_approved"),
        ("credential without the role scope", "role-step", role(PROBE, "probe-run-1", task_id=probe_id, role_id="event_analyst"), 403, "scope_not_granted"),
    ]
    for index, (label, path, fields, status, reason) in enumerate(refusals):
        reply = call("refusal-{0}".format(index + 1), path, fields,
                     config["limited_token"] if label == refusals[-1][0] else None)
        report.check("A76/A77 entry refuses: " + label, reply["http_status"] == status and reply["error"] == reason,
                     http_status=reply["http_status"], error=reply["error"])
    after_refusals = snap("11-after-refusals")
    report.check("A76 refusals execute nothing", fingerprint(after_refusals) == fingerprint(before_refusals))

    # ---- A78: Python full chain on the same frozen input --------------- #
    chains = {}
    for case_id, text in ((CHAIN, ANSWERS["chain"]), (CHAIN_QUALITY, "公告称回购金额为 3 亿元。")):
        chains[case_id] = drill(directory, "chain", {
            "token": token, "case_id": case_id, "title": TITLE, "research_question": QUESTION,
            "intent": "intent-" + case_id, "dify_run_id": "chain-run-" + case_id, "workflow_version": workflow,
            "replay_response": answer(text)})
        save_run("chain-" + case_id, chains[case_id])
    report.check("A76 an unbound task still runs on the Python full chain through its production claim path",
                 chains[CHAIN]["ran"] is True and chains[CHAIN]["worker_status"] == "succeeded"
                 and chains[CHAIN]["claimed_task_id"] == chains[CHAIN]["task_id"], chain=chains[CHAIN])
    report.check("A78 the same quality gate governs the full chain (uncited number -> blocked)",
                 chains[CHAIN_QUALITY]["worker_status"] == "blocked"
                 and chains[CHAIN_QUALITY]["model_call"]["gates"] == ["evidence_quality"], chain=chains[CHAIN_QUALITY])
    report.check("A78 same frozen input, expected report schemas, same citation allowlist, 1 vs 3 replay answers",
                 chains[CHAIN]["output_keys"] == steps_by_role["synthesis_drafter"]["output_keys"]
                 and root["allowed_references"] == chains[CHAIN]["allowed_references"]
                 and all(task["allowed_references"] == root["allowed_references"] for task in steps)
                 and chains[CHAIN]["frozen_question_sha256"] == root["frozen_question_sha256"]
                 and chains[CHAIN]["model_call"]["called"] is False
                 and all(t["model_call"]["called"] is False for t in steps)
                 and chains[CHAIN]["attempt_count"] == 1
                 and sum(t["attempt_count"] for t in steps) == 3,
                 frozen_question_sha256=root["frozen_question_sha256"], allowed_references=root["allowed_references"],
                 full_chain={"output_keys": chains[CHAIN]["output_keys"], "replay_answers": chains[CHAIN]["attempt_count"]},
                 role_mode={"step_output_keys": steps_by_role["synthesis_drafter"]["output_keys"],
                            "replay_answers": sum(t["attempt_count"] for t in steps), "root_output_keys": root["output_keys"]})

    # ---- A78/A79: ledger reconciliation ------------------------------- #
    pending = flow_run("12-rollback-pending", ROLLBACK_PENDING)
    final = snap("12-before-rollback")
    pending_root, pending_steps = steps_of(final, ROLLBACK_PENDING)
    report.check("A79 the in-flight task is reconciled before changing the routing flag",
                 pending["outputs"]["status"] == "step_halted" and shape(final, ROLLBACK_PENDING) ==
                 ("queued", {"event_analyst": ("succeeded", 1), "counter_evidence": ("queued", 1)}),
                 root=pending_root, steps=pending_steps)
    executed_steps_ = [entry for entry in final["ledgers"]["role_flow.jsonl"]
                       if entry["operation"] == "role-step" and entry["executed"] is True]
    step_attempts = Counter({task["task_id"]: task["attempt_count"] for task in final["tasks"] if task["role_step"]})
    report.check("A78 ledger reconciles per step: executed step calls == attempts on the step tasks",
                 Counter(entry["step_task_id"] for entry in executed_steps_) == step_attempts
                 and sum(step_attempts.values()) == len(executed_steps_)
                 and final["invocation_attempt_rows"] == {}, executed_step_calls=len(executed_steps_),
                 attempts=dict(step_attempts))

    # ---- A79: rollback ------------------------------------------------- #
    compose(directory, "up", "-d", "--pull", "never", "--wait", "--wait-timeout", "120", "research_api",
            env={"R17_ROLE_ENTRY": "disabled"})
    say("  rolled back: role entry disabled")
    rolled_back = flow_run("13-rollback-new-case", ROLLBACK)
    after_rollback = snap("13-rollback")
    report.check("A79 after rollback a new case cannot enter role mode: task stays unbound, zero steps",
                 rolled_back["calls"][-1]["node"] == "17021" and rolled_back["outputs"]["status"] == "bind_refused"
                 and shape(after_rollback, ROLLBACK) == ("queued", {}), calls=[(c["node"], c["status_code"]) for c in rolled_back["calls"]])
    replay_rollback = flow_run("14-rollback-main-replay", MAIN)
    after_replay_rollback = snap("14-rollback-main-replay")
    kept = {task["task_id"]: task for task in after_replay_rollback["tasks"]}
    report.check("A79 in-flight and finished role tasks are reconciled, not redone; replayed callbacks execute nothing",
                 all(kept[task["task_id"]]["attempt_count"] == task["attempt_count"]
                     and kept[task["task_id"]]["output_sha256"] == task["output_sha256"] for task in final["tasks"])
                 and replay_rollback["outputs"]["status"] == "role_flow_complete"
                 and len(replay_rollback["calls"]) == 6
                 and all(c["executed"] is not True for c in replay_rollback["calls"]))
    chain_after = drill(directory, "chain", {
        "token": token, "case_id": ROLLBACK, "title": TITLE, "research_question": QUESTION,
        "intent": "intent-" + ROLLBACK, "dify_run_id": rolled_back["workflow_run_id"],
        "workflow_version": workflow, "replay_response": answer(ANSWERS["chain"])})
    save_run("chain-after-rollback", chain_after)
    report.check("A79 the new Dify root falls back to the Python full chain and completes once",
                 chain_after.get("ran") is True and chain_after["worker_status"] == "succeeded"
                 and chain_after["task_id"] == rolled_back["outputs"]["task_id"]
                 and chain_after["attempt_count"] == 1, chain=chain_after)
    continued = flow_run("15-rollback-pending-continues", ROLLBACK_PENDING)
    reconciled = snap("15-rollback-reconciled")
    resumed_root, resumed_steps = steps_of(reconciled, ROLLBACK_PENDING)
    prior_analyst = by_role(pending_steps).get("event_analyst")
    resumed_analyst = by_role(resumed_steps).get("event_analyst")
    report.check("A79 the in-flight bound root continues after rollback without redoing its successful step",
                 continued["outputs"]["status"] == "role_flow_complete"
                 and pending_root and resumed_root["task_id"] == pending_root["task_id"]
                 and [c["executed"] for c in continued["calls"][1:]] == [False, False, True, True, True]
                 and prior_analyst == resumed_analyst,
                 before=shape(final, ROLLBACK_PENDING), after=shape(reconciled, ROLLBACK_PENDING))
    executed_after = Counter(entry["step_task_id"] for entry in reconciled["ledgers"]["role_flow.jsonl"]
                             if entry["operation"] == "role-step" and entry["executed"] is True)
    report.check("A79 replay accounting still reconciles after the rollback and continuation",
                 executed_after == Counter({task["task_id"]: task["attempt_count"]
                                            for task in reconciled["tasks"] if task["role_step"]})
                 and reconciled["invocation_attempt_rows"] == {},
                 role_attempts=sum(executed_after.values()), live_invocations=0)

    # ---- isolation evidence -------------------------------------------- #
    isolation = {}
    probes = (
        ("api_direct_to_gateway", "api", ["curl", "-s", "-o", "/dev/null", "-w", "%{http_code}", "--noproxy", "*",
                                          "-m", "5", "http://stock-agent-r16-gateway:8770/health"]),
        ("proxy_unlisted_route", "api", ["curl", "-s", "-o", "/dev/null", "-w", "%{http_code}",
                                         "--proxy", "http://ssrf_proxy:3128", "--noproxy", "", "-m", "10",
                                         "-X", "POST", "http://stock-agent-r16-gateway:8770/health"]),
        ("gateway_egress", "research_api", ["python", "-B", "-c", GATEWAY_EGRESS_PROBE]),
        ("sandbox_network", "sandbox", ["printenv", "ENABLE_NETWORK"]),
        ("dify_health", "api", ["curl", "-fsS", "http://127.0.0.1:5001/health"]),
    )
    for name, service, command in probes:
        result = compose(directory, "exec", "-T", service, *command, check=False)
        isolation[name] = {"exit": result.returncode, "stdout": result.stdout.strip()[:300],
                           "stderr": result.stderr.strip()[:300]}
    save_run("isolation", isolation)
    report.check("A76 the Dify api cannot reach the role gateway except through its restricted proxy",
                 isolation["api_direct_to_gateway"]["stdout"] == "000", probe=isolation["api_direct_to_gateway"])
    report.check("A76 the proxy denies an unlisted route to the role gateway",
                 isolation["proxy_unlisted_route"]["stdout"] == "403", probe=isolation["proxy_unlisted_route"])
    report.check("A76 the role gateway has no route to the internet or to Dify's database",
                 isolation["gateway_egress"]["stdout"] == "1.1.1.1 DENIED\ndb_postgres DENIED", probe=isolation["gateway_egress"])
    report.check("the Dify code sandbox has networking disabled", isolation["sandbox_network"]["stdout"] == "false")
    report.check("Dify api healthy after the restart and the rollback",
                 isolation["dify_health"]["exit"] == 0 and '"status":"ok"' in isolation["dify_health"]["stdout"].replace(" ", ""),
                 probe=isolation["dify_health"])

    access = compose(directory, "exec", "-T", "ssrf_proxy", "awk",
                     "/stock-agent-r16-gateway:8770/ {print $6, $7}", "/var/log/squid/access.log", check=False)
    squid_posts = sum(1 for line in access.stdout.splitlines() if line.startswith("POST ")
                      and line.split()[-1].rsplit("/", 1)[-1] in {"create-research", "research-status", "role-bind", "role-step", "role-finish"})
    (directory / "logs-squid-access.txt").write_text(redactor.clean(access.stdout), encoding="utf-8")
    report.check("A76 every canvas call crossed the restricted proxy (one Squid POST per HTTP node executed)",
                 squid_posts == sum(len(run["calls"]) for run in runs), squid_posts=squid_posts,
                 http_nodes=sum(len(run["calls"]) for run in runs),
                 sse_http_nodes=sum(len(run["sse_calls"]) for run in runs))

    # ---- R22: personal review, rule candidates, bounded graph (live store) --
    identity = {"credential_id": "r22-review", "actor_id": "r22-reviewer",
                "scopes": ["read_research"], "case_scope": list(CASES) + ["r22-graph"]}
    review_payload = drill(directory, "review", {
        "identity": identity, "task_ids": [root["task_id"]],
        "denied": [
            {"name": "foreign_case", "task_ids": [root["task_id"]],
             "identity": {**identity, "case_scope": ["r22-nowhere"]}},
            {"name": "no_read_scope", "task_ids": [root["task_id"]],
             "identity": {**identity, "scopes": ["create_research"]}},
        ]})
    save_run("r22-review", review_payload)
    projection = review_payload["projection"]
    reviewed = {row["task_id"]: row for row in projection["tasks"]}
    root_row = reviewed.get(root["task_id"]) or {}
    step_rows = [reviewed.get(task["task_id"]) or {} for task in steps]
    report.check("R22 review projects the live three-role run: relations, versions, rule digest, citations, metering",
                 len(projection["tasks"]) == 4
                 and sorted(root_row.get("relations", {}).get("children") or [])
                 == sorted(steps_by_role[role]["task_id"] for role in ROLES)
                 and root_row.get("input", {}).get("rule", {}).get("flow_sha256") == config["flow_sha256"]
                 and [item["input_version"] for item in root_row.get("input", {}).get("snapshots") or []]
                 == [1, 2]
                 and all(row.get("input", {}).get("step_prompt_sha256")
                         == registered[row["role_id"]]["sha256"] for row in step_rows)
                 and all(row.get("sources", {}).get("citations_outside_allowlist") == []
                         for row in projection["tasks"])
                 and all(row.get("metering", {}).get("attempts") == 1 for row in step_rows),
                 root={key: root_row.get(key) for key in ("input", "metering", "relations")},
                 steps=[{key: row.get(key) for key in ("role_id", "status", "metering")} for row in step_rows])
    report.check("R22 attribution keeps model roles, evidence and human sign-off apart",
                 len(projection["attribution"]["model_roles"]) == 3
                 and projection["attribution"]["human_signoffs"] == []
                 and projection["attribution"]["independent_evidence_count"]
                 == len({ref for row in projection["tasks"]
                         for ref in row["sources"]["citations"]})
                 and projection["attribution"]["rules"]["model_roles_are_not_human"] is True,
                 attribution=projection["attribution"])
    report.check("R22 review refuses a foreign case and a credential without read scope",
                 [item["reason"] for item in review_payload["denied"]]
                 == ["case_out_of_scope", "scope_not_granted"], denied=review_payload["denied"])
    report.check("R22 the review projection wrote nothing to the live store",
                 review_payload["read_only"]["tasks_before"] == review_payload["read_only"]["tasks_after"]
                 and review_payload["read_only"]["events_before"] == review_payload["read_only"]["events_after"],
                 read_only=review_payload["read_only"])

    rule = drill(directory, "r22-rule", {})
    save_run("r22-rule", rule)
    report.check("R22 a rule change is only a pending candidate and never alters the approved version",
                 rule["candidate"]["status"] == "pending_review"
                 and rule["candidate"]["applies_to_approved"] is False
                 and rule["approved_version_unchanged"] == rule["approved_digest"]
                 and rule["candidate"]["digest"] != rule["approved_digest"]
                 and rule["candidate_not_registered"] == "flow_not_registered"
                 and rule["approved_still_valid"] is True, rule=rule)
    report.check("R22 only a human approves a rule candidate (model role refused)",
                 rule["model_reviewer"] == "reviewer_not_human"
                 and rule["human_approval"]["status"] == "approved"
                 and rule["human_approval"]["approved_by"] == "r22-human-reviewer",
                 rule=rule)

    graph = drill(directory, "r22-graph", {})
    save_run("r22-graph", graph)
    refusals = {"scope_widened": "scope_widened", "cross_case": "cross_case",
                "unknown_role": "role_not_allowed", "self_dependency": "dependency_invalid",
                "dependency_cycle": "dependency_invalid",
                "unknown_dependency": "dependency_invalid",
                "fanout_exceeded": "fanout_exceeded", "depth_exceeded": "depth_exceeded",
                "budget_exceeded": "budget_exceeded"}
    report.check("R22 self/cycle/scope/case/depth/fan-out/dependency refusals create zero tasks",
                 all(graph.get(name, {}).get("status") == "refused"
                     and graph.get(name, {}).get("tasks_added") == 0
                     and graph.get(name, {}).get("reason") == reason
                     for name, reason in refusals.items()),
                 refusals={name: graph.get(name) for name in refusals})
    report.check("R22 a legal proposal is created through the reviewed entry with dependency edges",
                 graph["legal_with_dependency"]["created"] == 3
                 and all(len(edge["depends_on"]) == (2 if edge["role"] == "synthesis_drafter" else 0)
                         for edge in graph["legal_with_dependency"]["edges"]),
                 legal=graph["legal_with_dependency"])
    report.check("R22 a duplicate batch after the parent budget is spent adds and meters nothing",
                 graph["duplicate_batch"]["created"] == 0
                 and graph["duplicate_batch"]["duplicates"] == 3
                 and graph["duplicate_batch"]["tasks_added"] == 0
                 and graph["budget_ok"]["created"] == 1
                 and graph["budget_exceeded"]["tasks_added"] == 0,
                 duplicate=graph["duplicate_batch"], budget_exceeded=graph["budget_exceeded"])
    report.check("R22 parent cancel reaches every non-terminal descendant with its own event",
                 graph["cancel"]["count"] == 4
                 and set(graph["cancel"]["statuses"].values()) == {"cancelled"}
                 and list(graph["cancel"]["cancel_events"].values()) == [1, 1, 1, 1],
                 cancel=graph["cancel"])
    report.check("R22 personal review is owner-only and case/scope bound on the live store",
                 graph["personal_access"]["owner"] == "allowed"
                 and graph["personal_access"]["foreign_actor"] == "personal_owner_mismatch"
                 and graph["personal_access"]["anonymous"] == "personal_owner_required"
                 and graph["personal_access"]["wrong_case"] == "case_out_of_scope"
                 and graph["personal_access"]["no_read_scope"] == "scope_not_granted"
                 and graph["read_only"]["tasks_before_review"] == graph["read_only"]["tasks_after_review"],
                 personal=graph["personal_access"], read_only=graph["read_only"])


def run(directory: Path) -> int:
    config = json.loads((directory / "private/console.json").read_text())
    redactor = Secrets(directory)
    report, runs = Report(), []
    error = None
    try:
        exercise(directory, config, redactor, report, runs)
    except Exception as exc:
        error = redactor.clean("{0}: {1}".format(type(exc).__name__, exc))
        report.check("drill completed all scenarios", False, error=error)
    for service in ("stock-agent-r16-gateway", "research_api"):
        logs = compose(directory, "logs", "--no-color", "--tail", "600", service, check=False)
        (directory / "logs-{0}.txt".format(service)).write_text(
            redactor.clean(logs.stdout + logs.stderr), encoding="utf-8")

    summary = {"project": project(directory), "metering_source": "O/C (real Dify + controlled entry on pinned images; "
                "recorded replay answers, no provider call)", "workflow_version": config["workflow_version"],
               "flow_sha256": config["flow_sha256"], "dify_runs": len(runs), "items": report.items, "error": error,
               "result": "PASS" if not report.failed() else "FAIL"}
    save(directory, "summary.json", summary)
    say("== R17 drill", summary["result"], "|", len(report.items) - len(report.failed()), "of", len(report.items), "items passed")
    return 0 if not report.failed() else 1


def down(directory: Path) -> None:
    if not (directory / "private/started").is_file():
        return
    compose(directory, "down", "--volumes", "--timeout", "5")
    left = leftovers(directory)
    say("down --volumes: exit 0 | left:", {name: len(ids) for name, ids in left.items()})
    if any(left.values()):
        raise RuntimeError("project resources survived `down`: " + str(left))
    config = json.loads((directory / "private/console.json").read_text())
    with socket.socket() as listener:
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listener.bind(("127.0.0.1", config["port"]))
    save(directory, "cleanup.json", {"compose_down_exit": 0, "remaining": left,
                                     "loopback_port_rebind": config["port"]})
    (directory / "private/started").unlink()

def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("step", choices=("prepare", "up", "run", "down", "all"))
    parser.add_argument("directory", type=Path, help="evidence directory (created by `prepare`)")
    parser.add_argument("--port", type=int, default=18080, help="unused loopback UI port for prepare/all")
    args = parser.parse_args()
    if not 1024 <= args.port <= 65535:
        parser.error("--port must be between 1024 and 65535")
    directory = args.directory.resolve()
    if args.step == "prepare":
        prepare(directory, args.port)
    elif args.step == "up":
        up(directory)
    elif args.step == "run":
        sys.exit(run(directory))
    elif args.step == "down":
        down(directory)
    else:
        prepare(directory, args.port)
        try:
            up(directory)
            code = run(directory)
        finally:
            down(directory)
            shutil.rmtree(directory / "private")
        sys.exit(code)


if __name__ == "__main__":
    main()
