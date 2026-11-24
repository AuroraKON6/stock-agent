"""In-container probes for the isolated R17 role-flow drill.

Run inside the ``research_api`` container (same network namespace and task
store as the role gateway):

    docker compose ... exec -T research_api python -B -m r17_drill <command>

Every command prints ONE JSON object on stdout. Secrets (service tokens)
arrive on stdin, never on a command line. ``snapshot`` opens the store
read-only; ``probe-a76`` and ``chain`` are the only commands that write, and
they write through the production claim/worker paths on purpose.
"""

import hashlib
import json
import sys
from pathlib import Path
from urllib.parse import urlencode
from urllib.request import Request, build_opener

from stock_agent.config import project_path_override
from stock_agent.task_runtime import TaskStore
from stock_agent.task_runtime.store import TERMINAL_STATUSES

ROOT = Path("/srv/r16")
GATEWAY = "http://127.0.0.1:8770"


def post(token, path, fields):
    """One call to the controlled entry, through its nginx listener."""
    request = Request(GATEWAY + path, urlencode(fields).encode(), {
        "Content-Type": "application/x-www-form-urlencoded",
        "Authorization": "Bearer " + token,
    })
    opener = build_opener()
    try:
        with opener.open(request, timeout=150) as response:
            return response.status, {k.lower(): v for k, v in response.headers.items()}, json.load(response)
    except Exception as exc:  # HTTPError carries the refusal body
        body = {}
        status = 0
        headers = {}
        read = getattr(exc, "read", None)
        if read is not None:
            status = exc.code
            headers = {k.lower(): v for k, v in exc.headers.items()}
            try:
                body = json.loads(read())
            except ValueError:
                body = {}
        else:
            body = {"error": type(exc).__name__, "detail": str(exc)[:200]}
        return status, headers, body


def summary(call):
    if not isinstance(call, dict):
        return {}
    rounds = call.get("rounds")
    return {"mode": call.get("mode"), "called": bool(call.get("called")),
            "model_rounds": len(rounds) if isinstance(rounds, list) else 0,
            "usage": call.get("usage") or {}, "error": str(call.get("error") or "")[:300],
            "gates": [str(call["governance"]["gate"])] if isinstance(call.get("governance"), dict)
            and call["governance"].get("gate") else [],
            "steps": len(call.get("steps") or []) if isinstance(call.get("steps"), list) else 0,
            "role_prompt_sha256": call.get("role_prompt_sha256")}


def input_evidence(task):
    from stock_agent.research_worker import build_safe_research_context

    payload = task.get("input_payload") or {}
    with project_path_override(ROOT.joinpath):
        context, _ = build_safe_research_context(payload, config={})
    return {
        "allowed_references": sorted((context.get("evidence_summaries") or {}).keys()),
        "frozen_question_sha256": hashlib.sha256(str(payload.get("research_question") or "").encode()).hexdigest(),
    }


def snapshot(_stdin):
    store = TaskStore(ROOT / "data" / "tasks.sqlite3", read_only=True)
    try:
        tasks = [{
            "task_id": task["task_id"], "case_id": task.get("case_id"), **input_evidence(task),
            "parent_task_id": task.get("parent_task_id") or "", "role_id": task.get("role_id") or "",
            "status": task["status"], "attempt_count": int(task.get("attempt_count") or 0),
            "max_attempts": int(task.get("max_attempts") or 0), "input_version": int(task.get("input_version") or 1),
            "version": int(task.get("version") or 0), "execution_mode": task.get("execution_mode"),
            "terminal": task["status"] in TERMINAL_STATUSES,
            "role_bound": bool((task.get("input_payload") or {}).get("role_flow")),
            "role_step": (task.get("input_payload") or {}).get("role_step") or None,
            "origin": ((task.get("input_payload") or {}).get("origin") or {}).get("kind"),
            "summary": str((task.get("output") or {}).get("summary") or "")[:160],
            "output_keys": sorted((task.get("output") or {}).keys()),
            # Same canonical form as role_execution's provenance digest.
            "output_sha256": hashlib.sha256(json.dumps(
                task.get("output") or {}, ensure_ascii=False, sort_keys=True,
                separators=(",", ":"), default=str).encode()).hexdigest(),
            "last_error": str(task.get("last_error") or "")[:200], "model_call": summary(task.get("model_call")),
            "platform_runs": [run["run_id"] for run in store.list_platform_runs(task["task_id"])],
        } for task in store.list_tasks()]
        events = {}
        for task in store.list_tasks():
            for event in store.events(task["task_id"], limit=500):
                key = "{0}:{1}".format(task["task_id"], event["event_type"])
                events[key] = events.get(key, 0) + 1
        attempts = {}
        for task in store.list_tasks():
            rows = store.invocation_attempts(task["task_id"])
            if rows:
                attempts[task["task_id"]] = len(rows)
    finally:
        store.close()
    ledgers = {}
    with project_path_override(ROOT.joinpath):
        from stock_agent.dify_api import (
            LEDGER_ACCESS_LOG, LEDGER_CANDIDATE_RESULTS, LEDGER_ROLE_FLOW, _ledger_records)

        for name in (LEDGER_ROLE_FLOW, LEDGER_ACCESS_LOG, LEDGER_CANDIDATE_RESULTS):
            ledgers[name.rsplit("/", 1)[-1]] = _ledger_records({}, name)
    return {"tasks": tasks, "events": events, "invocation_attempt_rows": attempts, "ledgers": ledgers}


def http(stdin):
    status, headers, body = post(stdin["token"], stdin["path"], stdin["fields"])
    return {"http_status": status, "headers": {k: v for k, v in headers.items() if k.startswith("r1")},
            "ok": body.get("ok"), "error": body.get("error"), "detail": body.get("detail"),
            "data": body.get("data")}


def probe_a76(stdin):
    """The Python full chain against a role-bound root, through its own claim path."""
    store = TaskStore(ROOT / "data" / "tasks.sqlite3")
    try:
        before = store.get_task(stdin["task_id"])
        queued_before = sorted(task["task_id"] for task in store.list_tasks("queued"))
        skipped = store.claim_next("full-chain-probe", exclude_role_bound=True)
        blind = store.claim_next("full-chain-probe")
        refusal = None
        if blind is not None and blind["task_id"] == stdin["task_id"]:
            from stock_agent.research_worker import run_research_task

            with project_path_override(ROOT.joinpath):
                refusal = run_research_task(
                    store, stdin["task_id"], worker_id="full-chain-probe",
                    config={"task_runtime": {"db_path": str(ROOT / "data" / "tasks.sqlite3")}})
        elif blind is not None:
            store.fail(blind["task_id"], worker_id="full-chain-probe",
                       fencing_token=int(blind["fencing_token"]), error="probe: not the target", retryable=True)
        after = store.get_task(stdin["task_id"])
        return {
            "task_id": stdin["task_id"], "queued_before": queued_before,
            "exclude_claim": skipped["task_id"] if skipped else None,
            "default_claim": blind["task_id"] if blind else None,
            "worker_status": (refusal or {}).get("status"),
            "model_called": ((refusal or {}).get("model_call") or {}).get("called"),
            "model_rounds": len(((refusal or {}).get("model_call") or {}).get("rounds") or []),
            "status_before": before["status"], "status_after": after["status"],
            "output_empty": not after.get("output"),
            "attempt_count": int(after.get("attempt_count") or 0),
        }
    finally:
        store.close()


def chain(stdin):
    """An UNBOUND task through the production full-chain claim path (replay)."""
    store = TaskStore(ROOT / "data" / "tasks.sqlite3")
    try:
        status, headers, body = post(stdin["token"], "/api/dify/v1/create-research", {
            "title": stdin["title"], "case_id": stdin["case_id"], "input_version": "1",
            "research_question": stdin["research_question"], "business_intent_id": stdin["intent"],
            "dify_run_id": stdin["dify_run_id"], "workflow_version": stdin["workflow_version"],
            "execution_mode": "replay"})
        task_id = (body.get("data") or {}).get("task_id")
        if status != 200 or not task_id:
            return {"created": False, "http_status": status, "error": body.get("error")}
        # Harness step: the recorded answer rides the task input (audited bump).
        store.bump_input_version(task_id, payload_overrides={"replay_response": stdin["replay_response"]},
                                 actor="r17-drill", reason="drill: recorded replay answer")
        from stock_agent.research_worker import run_research_task

        with project_path_override(ROOT.joinpath):
            claimed = store.claim_next("python-full-chain", exclude_role_bound=True)
            if claimed is None or claimed["task_id"] != task_id:
                return {"created": True, "task_id": task_id, "claimed": dict(claimed or {}), "ran": False}
            result = run_research_task(
                store, task_id, worker_id="python-full-chain",
                config={"task_runtime": {"db_path": str(ROOT / "data" / "tasks.sqlite3")}})
        task = store.get_task(task_id)
        return {"created": True, "task_id": task_id, "claimed_task_id": claimed["task_id"], "ran": True,
                **input_evidence(task),
                "worker_status": result["status"], "model_call": summary(result.get("model_call")),
                "status": task["status"], "attempt_count": int(task.get("attempt_count") or 0),
                "output_keys": sorted((task.get("output") or {}).keys()),
                "summary": str((task.get("output") or {}).get("summary") or "")[:200],
                "references": (task.get("output") or {}).get("references"),
                "evidence_gap_draft": bool((task.get("model_call") or {}).get("evidence_gap_draft"))}
    finally:
        store.close()


COMMANDS = {"http": http, "snapshot": snapshot, "probe-a76": probe_a76, "chain": chain}


def main():
    name = sys.argv[1]
    stdin = json.loads(sys.stdin.read() or "{}")
    try:
        print(json.dumps(COMMANDS[name](stdin), ensure_ascii=False, default=str))
    except Exception as exc:  # the drill reports, never hides, a probe failure
        print(json.dumps({"probe_error": type(exc).__name__, "detail": str(exc)[:500]}))


if __name__ == "__main__":
    main()
