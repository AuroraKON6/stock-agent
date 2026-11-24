#!/usr/bin/env python
"""R23 observability drill driver (KONM-96) — synthetic case, zero providers.

Runs inside the candidate container (app bind-mounted at /app) or from a
checkout. Builds ONE frozen synthetic case through the REAL entries — the Dify HTTP
entry (first run + replayed run), one governed query through the R11 offline
file provider, and the real worker with an in-memory model stub (known
failure, then unknown, then reconciled) — and exercises the R23 projections:

    seed     write the drill config, credentials and file fixture; build the
             case; assert both HTTP calls were 200, the query bound to the
             task, and the worker emitted one redacted line per run (the stub
             error carries a synthetic body marker; the log sink must not)
    report   read-only: build the report N times, compare every view number
             with the authoritative ledgers, prove zero side effects (ledger
             counts + query files), scan the output for synthetic secrets and
             body markers, check alert candidates, malformed config, query
             ledger availability (missing / unreadable / readable-empty),
             then run the real worker with sampling OFF on a COPY of the
             store (business continues, audit rows kept) and the 5,001-task
             no-sampling-cap check on its own store
    serve    /livez + /readyz (Dify API server) and /api/v1/ops +
             /api/v1/queries-view (task board) over the drilled store
    probe    loopback: repeated probes + ops/queries reads with before/after
             ledger counts, permission (non-loopback Host -> 403) and failure
             degradation (503 carries the exception type only)
    cross base_url_serve base_url_board
             from ANOTHER container: the same reads must be refused (403)

Every subcommand prints one JSON object and exits 0 on success, 13 on a
failed assertion. No real supplier, no external send: the compose network is
`internal: true`.
"""
from __future__ import annotations

import contextlib
import io
import sqlite3
import json
import os
import sys
import threading
import urllib.error
import urllib.request
from pathlib import Path

# --------------------------------------------------------------------------- #
# seed: the frozen synthetic case through the REAL entries
#   Dify HTTP entry (first run + replayed run)  -> task + platform runs
#   R11 file-fixture governed query             -> intake binding + JSONL ledger
#   real worker with an in-memory model stub    -> known failure, unknown
# --------------------------------------------------------------------------- #

DB_REL = "data/task_runtime/tasks.sqlite3"
SCALE_DB_REL = "data/r23_drill/scale.sqlite3"
OFF_DB_REL = "data/r23_drill/off.sqlite3"
LEDGER_REL = "data/r23_drill/query_ledger.jsonl"
INTAKE_REL = "data/r23_drill/query_intake.sqlite3"
FIXTURE_REL = "data/r23_drill/file_fixtures.json"
CONFIG_REL = "data/config/watchlist.r23.json"
CREDS_REL = "data/r23_drill/service_credentials.json"

CASE_ID = "case-r23-frozen-001"
INTENT_ID = "intent-r23-frozen-001"
DIFY_RUN_1 = "dify-run-synthetic-001"
DIFY_RUN_2 = "dify-run-synthetic-002-replay"

# Synthetic, never valid anywhere. No view, log or projection output may
# contain any of them. ``token`` has no recognisable prefix on purpose.
SECRETS = {
    "api_key": "sk-SYNTHETICR23KEY0000000000",
    "bearer": "Bearer SYNTHETICR23TOKEN000000000000",
    "assigned": "api_key=SYNTHETICR23ASSIGNED0000000",
    "token": "zcodeSyntheticOpaqueR23Value00001124",
    "body": "PROMPT_BODY_MARKER_R23_DO_NOT_LEAK",
}
FORBIDDEN = tuple(SECRETS.values())

DRILL_CONFIG = {
    "storage": {"sqlite_path": "data/stock_agent.sqlite3"},
    "task_runtime": {"db_path": DB_REL},
    "dify": {"enabled": True, "service_credentials_path": CREDS_REL,
             "approved_workflow_versions": ["wf-demo-1"]},
    "kimi_integration": {"enabled": True},
    "kimi_sources": {"ledger_path": LEDGER_REL, "query_store_path": INTAKE_REL,
                     "file_fixture_path": FIXTURE_REL, "daily_data_call_limit": 10},
    "observability": {
        "sampling": {"enabled": True},
        "alerts": {"enabled": True, "thresholds": {"oldest_queued_age_seconds": 600}},
    },
}

PRICE_CSV = "日期,开盘价,收盘价\n2026-10-09,1690.0,1700.0\n"
FILE_FIXTURES = {"schema_version": "1.0", "apis": {
    "file_get_price": {"outcome": "ok", "body": {"is_success": True, "result": {}, "files": [
        {"name": "fixture.csv", "content": PRICE_CSV}]}},
}}


def emit(payload: dict) -> None:
    print(json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str), flush=True)


def project_root() -> Path:
    for candidate in (Path("/app"), Path(__file__).resolve().parents[1]):
        if (candidate / "stock_agent").is_dir():
            return candidate
    return Path.cwd()


# The checkout root is not always the interpreter's cwd / script directory
# (the script is bind-mounted at /drills in the container).
if str(project_root()) not in sys.path:
    sys.path.insert(0, str(project_root()))


def drill_config(*, sampling: bool = True) -> dict:
    config = json.loads(json.dumps(DRILL_CONFIG))
    config["observability"]["sampling"]["enabled"] = sampling
    return config


def _write_inputs(root: Path) -> None:
    import hashlib

    # Only the query inputs are reset here; the TASK store is owned by the
    # caller (it is already open) and must never be unlinked underneath it.
    for relative in (LEDGER_REL, INTAKE_REL, FIXTURE_REL, CREDS_REL):
        target = root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.suffix in (".sqlite3", ".jsonl") and target.exists():
            target.unlink()
    (root / FIXTURE_REL).write_text(json.dumps(FILE_FIXTURES, ensure_ascii=False), encoding="utf-8")
    (root / LEDGER_REL).write_text("", encoding="utf-8")
    (root / CREDS_REL).write_text(json.dumps({
        "schema_version": "1.0",
        "credentials": [{
            "credential_id": "cred-r23-synthetic",
            "token_sha256": hashlib.sha256(SECRETS["token"].encode("utf-8")).hexdigest(),
            "scopes": ["create_research", "read_research", "read_fact_bundle",
                       "submit_result", "submit_feedback"],
            "case_scope": [CASE_ID],
            "active": True, "created_at": "2026-11-24T00:00:00+08:00", "expires_at": "",
        }]}, ensure_ascii=False), encoding="utf-8")


def _dify_http_entry(store, root: Path) -> dict:
    """The permitted Dify HTTP entry: one intent, two platform runs."""
    import urllib.request

    from stock_agent.dify_api import DifyGateway
    from stock_agent.dify_api_server import create_dify_api_server

    gateway = DifyGateway(store, drill_config())
    server = create_dify_api_server(gateway, host="127.0.0.1", port=0)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    form = {"title": "R23 冻结合成案例", "case_id": CASE_ID, "input_version": 1,
            "research_question": "Synthetic R23 frozen case?", "dify_run_id": DIFY_RUN_1,
            "workflow_version": "wf-demo-1", "business_intent_id": INTENT_ID,
            "execution_mode": "live"}
    codes = []
    try:
        for run_id in (DIFY_RUN_1, DIFY_RUN_2):
            request = urllib.request.Request(
                "http://127.0.0.1:{0}/api/dify/v1/create-research".format(server.server_address[1]),
                data=json.dumps({**form, "dify_run_id": run_id}).encode("utf-8"),
                headers={"Content-Type": "application/json",
                         "Authorization": "Bearer {0}".format(SECRETS["token"])}, method="POST")
            with urllib.request.urlopen(request, timeout=10) as response:
                body = json.loads(response.read().decode("utf-8"))
                codes.append(response.status)
            assert body.get("ok"), body
            if run_id == DIFY_RUN_1:
                task_id = body["data"]["task_id"]
            else:
                assert body["data"]["task_id"] == task_id, "a replayed run must reuse the task"
    finally:
        server.shutdown()
        server.server_close()
    assert root  # (inputs are written by the caller; kept explicit)
    return {"task_id": task_id, "http_status": codes}


def _governed_query(task_id: str, root: Path) -> dict:
    """One governed query through the R11 offline file provider (A51 chain)."""
    import stock_agent.query_supply as supply
    import stock_agent.sources.registry as registry_module
    from stock_agent.query_supply import attach_query_to_spec

    entry = {
        "source_id": "file_price", "kind": "price", "upstream": "ifind",
        "purpose": "r23 drill", "purposes": ["research supply"],
        "approval": {"state": "approved", "approved_by": "合成负责人（非真实）",
                     "approved_at": "2026-11-24T00:00:00"},
        "body_retention_allowed": True, "llm_external_send_allowed": False,
        "timeliness": {"expected_max_delay_minutes": 1440, "staleness_policy": "flag"},
        "rate_limit": {"max_requests_per_minute": 60, "max_requests_per_day": 10},
        "expires_at": "2099-12-31",
    }
    registry = {"schema_version": "1.0", "entries": {"file_price": entry}, "path": "test"}
    real_loader = registry_module.load_source_registry
    registry_module.load_source_registry = lambda config: registry
    try:
        receipt = attach_query_to_spec(
            drill_config(), {"data_classification": "public", "research_question": "合成",
                             "public_context": {}},
            source_id="file_price", api_id="file_get_price",
            params={"ticker": "600519.SH", "start_date": "2026-10-01",
                    "end_date": "2026-10-09", "adjust": "forward"},
            purpose="research supply", task_id=task_id, data_kind="daily_quote", allow_cache=False)
    finally:
        registry_module.load_source_registry = real_loader
    assert supply  # (the governed path stays the production one)
    return receipt


class _ModelStub:
    """In-memory stand-in for the provider: no network, no real model."""

    def __init__(self, message, *, stage, sent_hint):
        from stock_agent.pi_runtime import PiRuntimeError

        def fail(*args, **kwargs):
            self.calls += 1
            error = PiRuntimeError(message)
            error.failure_stage = stage
            error.sent_hint = sent_hint
            raise error

        self.fail = fail
        self.calls = 0


def _run_worker(store, task_id: str, config: dict, stub: _ModelStub) -> dict:
    """The real worker entry with only the provider call stubbed."""
    from unittest.mock import patch

    from stock_agent.research_worker import run_research_task

    log = io.StringIO()
    with patch("stock_agent.pi_runtime.call_pi_agent", side_effect=stub.fail), \
            contextlib.redirect_stderr(log):
        result = run_research_task(store, task_id, worker_id="w-r23", config=config)
    return {"result": result, "log": log.getvalue(), "calls": stub.calls}


def seed_case(store, root: Path, config: dict) -> dict:
    from stock_agent.task_runtime.store import ATTEMPT_OUTCOME_UNKNOWN

    _write_inputs(root)
    entered = _dify_http_entry(store, root)
    task_id = entered["task_id"]
    receipt = _governed_query(task_id, root)

    # Worker path 1: a known before-send failure (its message carries the
    # restricted body marker — the marker must never reach a view or a log).
    first = _run_worker(store, store.claim_next("w-r23")["task_id"], drill_config(),
                        _ModelStub("synthetic before-send failure " + SECRETS["body"],
                                   stage="preflight", sent_hint="no"))
    assert first["result"]["task_id"] if "task_id" in first["result"] else True
    # Worker path 2: sent, then the outcome became unknown (timeout).
    claimed = store.claim_next("w-r23")
    assert claimed["task_id"] == task_id
    second = _run_worker(store, task_id, drill_config(),
                         _ModelStub("synthetic process timeout", stage="process",
                                    sent_hint="unknown"))
    unknown = [a for a in store.invocation_attempts(task_id)
               if a["state"] == ATTEMPT_OUTCOME_UNKNOWN]
    assert len(unknown) == 1, unknown
    store.reconcile_invocation_attempt(
        unknown[0]["attempt_id"], actor="ops-synthetic", resolution="confirmed_not_charged",
        reason="合成回执无计费记录")

    # Business-gate states (policy outcomes, created through the store API).
    def spec(question):
        return {"research_question": question, "symbols": ["600519"],
                "execution_mode": "replay", "data_classification": "public",
                "prompt_body": SECRETS["body"]}

    def blocked(question, error):
        row, _ = store.create_task(title=question, input_payload=spec(question),
                                   case_id=CASE_ID, execution_mode="replay")
        got = store.claim_task(row["task_id"], "w-r23", ttl_seconds=120)
        store.mark_running(row["task_id"], worker_id="w-r23", fencing_token=got["fencing_token"],
                           expected_version=got["version"])
        store.mark_blocked(row["task_id"], worker_id="w-r23", fencing_token=got["fencing_token"],
                           error=error)
        return row

    blocked("R23 预算阻断", "daily model-call budget exhausted: 2/2 attempts")
    blocked("R23 缺证据阻断", "specific findings carry numbers without any citation; "
                             "the draft is saved as an evidence gap")
    blocked("R23 治理阻断", "task input classification is not public; {0}".format(SECRETS["bearer"]))
    blocked("R23 未知外发阻断", "model invocation outcome unknown; manual reconciliation required")

    old, _ = store.create_task(title="R23 老排队任务", input_payload=spec("排队"),
                               case_id=CASE_ID, execution_mode="replay")
    stale, _ = store.create_task(title="R23 过期租约任务", input_payload=spec("租约"),
                                 case_id=CASE_ID, execution_mode="replay")
    got = store.claim_task(stale["task_id"], "w-r23-dead", ttl_seconds=120)
    store.mark_running(stale["task_id"], worker_id="w-r23-dead", fencing_token=got["fencing_token"],
                       expected_version=got["version"])
    store._conn.execute(
        "UPDATE research_tasks SET lease_expires_at = ? WHERE task_id = ?",
        ("2020-01-01T00:00:00+00:00", stale["task_id"]))
    store._conn.execute(
        "UPDATE research_tasks SET created_at = ? WHERE task_id = ?",
        ("2020-01-01T00:00:00+00:00", old["task_id"]))
    store._conn.commit()
    return {"task_id": task_id, "case_id": CASE_ID, "business_intent_id": INTENT_ID,
            "dify_http_status": entered["http_status"],
            "platform_runs": [DIFY_RUN_1, DIFY_RUN_2],
            "governed_query": {"supply_status": receipt.get("supply_status"),
                               "consumption_recorded": receipt.get("consumption_recorded")},
            "worker": {"first_calls": first["calls"], "second_calls": second["calls"],
                       "first_log_lines": len(first["log"].splitlines()),
                       "second_log_lines": len(second["log"].splitlines()),
                       "logs_contain_marker": any(m in first["log"] + second["log"]
                                                  for m in FORBIDDEN)},
            "unknown_attempt_id": unknown[0]["attempt_id"],
            "old_queued_task_id": old["task_id"], "stale_lease_task_id": stale["task_id"]}


def ledger_counts(store) -> dict:
    """Every counter a probe must leave untouched (authoritative ledgers)."""
    conn = store._conn
    one = lambda sql: int(conn.execute(sql).fetchone()[0])  # noqa: E731
    return {
        "tasks": one("SELECT COUNT(*) FROM research_tasks"),
        "outbox_events": one("SELECT COUNT(*) FROM task_outbox"),
        "attempts": one("SELECT COUNT(*) FROM task_invocation_attempts"),
        "platform_runs": one("SELECT COUNT(*) FROM task_platform_runs"),
        "input_snapshots": one("SELECT COUNT(*) FROM task_input_snapshots"),
        "consumptions": one("SELECT COUNT(*) FROM signal_consumptions"),
        "idempotency_receipts": one("SELECT COUNT(*) FROM idempotency_receipts"),
        "reservations": one("SELECT COUNT(*) FROM task_outbox WHERE event_type = "
                            "'research_attempt_started'"),
        "external_send_marks": one("SELECT COUNT(*) FROM task_invocation_attempts "
                                   "WHERE sent = 'yes'"),
        "max_outbox_sequence": int(conn.execute("SELECT COALESCE(MAX(sequence), 0) "
                                                "FROM task_outbox").fetchone()[0]),
    }


def file_fingerprints(root: Path) -> dict:
    out = {}
    for relative in (LEDGER_REL, INTAKE_REL):
        path = root / relative
        stat = path.stat() if path.exists() else None
        out[relative] = (stat.st_size, stat.st_mtime_ns) if stat else None
    return out


def intake_counts(root: Path) -> dict:
    import sqlite3

    path = root / INTAKE_REL
    if not path.is_file():
        return {}
    conn = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)
    try:
        return {"intents": conn.execute("SELECT COUNT(*) FROM query_intents").fetchone()[0],
                "consumptions": conn.execute("SELECT COUNT(*) FROM query_consumptions").fetchone()[0]}
    finally:
        conn.close()


# --------------------------------------------------------------------------- #
# HTTP: probes, ops/queries projection, permission and degradation
# --------------------------------------------------------------------------- #

def http(url: str, *, host: str = "") -> dict:
    request = urllib.request.Request(url, headers={"Host": host} if host else {})
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            return {"status": response.status, "body": response.read().decode("utf-8")}
    except urllib.error.HTTPError as exc:
        return {"status": exc.code, "body": exc.read().decode("utf-8", "replace")}


def start_servers(config: dict):
    from stock_agent.dify_api import DifyGateway
    from stock_agent.dify_api_server import create_dify_api_server
    from stock_agent.task_board import create_task_board_server
    from stock_agent.task_runtime import TaskStore

    from stock_agent.config import project_path

    store = TaskStore(project_path(DB_REL))
    gateway = DifyGateway(store, {"dify": {"enabled": False}},
                          credentials={"schema_version": "1.0", "credentials": []})
    dify = create_dify_api_server(gateway, host="0.0.0.0", port=8770)
    # Bound on every interface INSIDE the internal drill network only, so the
    # cross-container check exercises the application's own Host refusal.
    board = create_task_board_server(store, host="0.0.0.0", port=8769, config=config)
    for server in (dify, board):
        threading.Thread(target=server.serve_forever, daemon=True).start()
    return store, dify, board


def read_projection(base: str, path: str, *, host: str = "") -> dict:
    result = http(base.rstrip("/") + path, host=host)
    body = ""
    try:
        body = json.loads(result["body"])
    except ValueError:
        pass
    return {"path": path, "host_header": host or "(default)", "status": result["status"],
            "body": result["body"], "json": body}


def probe_loop(base_dify: str, base_board: str, rounds: int = 3) -> dict:
    from stock_agent.config import project_path
    from stock_agent.task_runtime import TaskStore

    def counts():
        store = TaskStore(project_path(DB_REL), read_only=True)
        try:
            return ledger_counts(store)
        finally:
            store.close()

    before = counts()
    calls = []
    ok = True
    for _ in range(rounds):
        for path in ("/livez", "/readyz"):
            result = read_projection(base_dify, path)
            calls.append(result)
            ok = ok and result["status"] == 200
        for path in ("/api/v1/ops", "/api/v1/queries-view"):
            result = read_projection(base_board, path)
            calls.append(result)
            ok = ok and result["status"] == 200
    after = counts()
    ok = ok and before == after
    leaked = [c["path"] for c in calls if any(marker in c["body"] for marker in FORBIDDEN)]
    return {"ok": ok and not leaked, "calls": [{k: c[k] for k in ("path", "status")}
                                                for c in calls],
            "counts_before": before, "counts_after": after,
            "secret_leaks": leaked}


def probe_permission(base_dify: str, base_board: str, *, host: str) -> dict:
    """A caller that is not on loopback must be refused on every read."""
    results = [read_projection(base_dify, p, host=host) for p in ("/livez", "/readyz")]
    results += [read_projection(base_board, p, host=host)
                for p in ("/api/v1/ops", "/api/v1/queries-view")]
    return {"ok": all(r["status"] == 403 for r in results),
            "results": [{"path": r["path"], "status": r["status"]} for r in results]}


def probe_degrade(config: dict) -> dict:
    """A failing projection answers 503 with the exception TYPE only.

    Needs its own in-process board server: the patched projection functions
    live in this process, not in the ``serve`` one."""
    import stock_agent.ops_view as ops_view
    import stock_agent.queries_view as queries_view
    from stock_agent.task_board import create_task_board_server
    from stock_agent.task_runtime import TaskStore

    from stock_agent.config import project_path

    store = TaskStore(project_path(DB_REL), read_only=True)
    board = create_task_board_server(store, host="127.0.0.1", port=0, config=config)
    threading.Thread(target=board.serve_forever, daemon=True).start()
    base = "http://127.0.0.1:{0}".format(board.server_address[1])
    saved = (ops_view.build_ops_view, queries_view.build_queries_view)

    def explode(*args, **kwargs):
        raise RuntimeError("secret detail {0}".format(SECRETS["api_key"]))

    ops_view.build_ops_view = explode
    queries_view.build_queries_view = explode
    try:
        results = [read_projection(base, p) for p in ("/api/v1/ops", "/api/v1/queries-view")]
    finally:
        ops_view.build_ops_view, queries_view.build_queries_view = saved
        board.shutdown()
        board.server_close()
        store.close()
    leaked = [r["path"] for r in results if SECRETS["api_key"] in r["body"]]
    types_only = all(set(r["json"]) == {"error"} and "RuntimeError" in r["json"]["error"]
                     for r in results)
    return {"ok": all(r["status"] == 503 for r in results) and not leaked and types_only,
            "results": [{"path": r["path"], "status": r["status"], "json": r["json"]}
                        for r in results],
            "exception_type_only": types_only, "secret_leaks": leaked}


# --------------------------------------------------------------------------- #
# report: assertions over the read-only projections
# --------------------------------------------------------------------------- #

def verify_report(store, config: dict, task_id: str, root: Path) -> dict:
    from stock_agent.observability import (
        build_observability_report,
        build_trace,
        emit_observability_log,
        observability_settings,
        project_log_fields,
        read_query_ledgers)

    before = ledger_counts(store)
    files_before = file_fingerprints(root)
    checks = {}

    def check(name, expected, actual):
        checks[name] = {"expected": expected, "actual": actual, "ok": expected == actual}
        return expected == actual

    report = build_observability_report(store, config=config, task_id=task_id)
    recon = report["reconciliation"]
    technical, business = recon["technical"], recon["business"]
    trace = report["trace"]

    # --- trace: case / intent -> task -> platform runs -> query -> attempts
    check("trace.case", CASE_ID, trace["business_identity"]["case_id"])
    check("trace.intent", INTENT_ID, trace["business_identity"]["business_intent_id"])
    check("trace.platform_runs", [DIFY_RUN_1, DIFY_RUN_2],
          [r["run_id"] for r in trace["platform_runs"]])
    check("trace.intent_not_a_platform_id", True,
          all(r["run_id"] != INTENT_ID for r in trace["platform_runs"]))
    # Each worker run is its own claim, so both attempts are round 1.
    check("trace.attempt_rounds", [1, 1], [a["round_index"] for a in trace["attempts"]])
    check("trace.attempt_states", ["failed_known", "reconciled"],
          [a["state"] for a in trace["attempts"]])
    check("trace.reconciled_flags", [False, True], [a["reconciled"] for a in trace["attempts"]])
    check("trace.coverage", ["reconciled", "retry"], trace["coverage"]["outcomes"])
    check("trace.events_nonempty", True, len(trace["events"]) > 0)
    read = read_query_ledgers(config)
    check("trace.query_states", ["readable", "readable"],
          [trace["coverage"]["query_ledger_state"], trace["coverage"]["query_intake_state"]])
    check("trace.query_unknown_items", [], trace["coverage"]["unknown_items"])
    # The query -> task link comes from the intake consumption binding written
    # by the governed query path (the JSONL intent itself has task_id=None).
    check("trace.queries", 1, len(trace["queries"]))
    link = (trace["queries"] or [{}])[0]
    check("trace.query_link", "consumption_binding", link.get("link"))
    check("trace.query_outcome", "ok", link.get("outcome"))
    check("trace.query_supply_status", "data_delivered", link.get("supply_status"))
    check("trace.jsonl_intent_has_no_task", True,
          all(not r.get("task_id") for r in read["records"] if r.get("record_type") == "query_intent"))
    check("trace.no_text_fields", [],
          [key for a in trace["attempts"] + [trace["task"]] for key in a
           if key == "error" or key.endswith(("_excerpt", "_text", "_message"))])
    # The frozen task itself sits blocked on the reconciled unknown attempt.
    check("trace.task_state", ("blocked", "invocation_outcome_unknown"),
          (trace["task"]["status"], trace["task"]["last_error_code"]))
    check("report.trace_same_task", task_id, trace["task"]["task_id"])

    # --- layering: machine health and business gates side by side
    check("layer.healthy_inflight", 0,
          report["status"]["technical"]["inflight_leases"] - technical["stale_leases"])
    check("layer.blocked", 5, business["blocked_tasks"])
    check("layer.gates", {"budget": 1, "evidence_quality": 1, "governance": 1,
                          "invocation_outcome_unknown": 2, "other": 0}, business["gate_counts"])
    check("layer.no_error_in_technical", False, "error" in technical)
    check("layer.blocked_sample_codes", ["budget_exhausted", "evidence_quality", "governance",
                                         "invocation_outcome_unknown", "invocation_outcome_unknown"],
          sorted(t["error_code"] for t in business["blocked_sample"]))
    check("layer.blocked_sample_keys", [],
          [k for t in business["blocked_sample"] for k in t
           if k == "error" or k.endswith(("_excerpt", "_text", "_message"))])

    # --- reconciliation against the authoritative ledgers
    counts = before
    check("recon.queue_depth", 1, recon["queue"]["depth"]["value"])
    check("recon.stale_leases", 1, recon["queue"]["stale_leases"]["value"])
    check("recon.oldest_age_years", True, recon["queue"]["oldest_age_seconds"]["value"] > 6 * 365 * 86400)
    check("recon.attempts", counts["attempts"], sum(technical["invocation_attempts"].values()))
    check("recon.known_failures", 1, recon["invocations"]["known_failures"])
    check("recon.unknown_open", 0, recon["invocations"]["unknown_open"])
    check("recon.unknown_reconciled", 1, recon["invocations"]["unknown_reconciled"])
    check("recon.waiting", 0, recon["invocations"]["reconciliations_waiting"])
    check("recon.scope_complete", True, recon["invocations"]["scope"]["complete"])
    check("recon.outbox", counts["outbox_events"], recon["authoritative"]["outbox_events"])
    check("recon.event_scan_complete", True, recon["authoritative"]["event_scan"]["complete"])
    check("recon.status_total", counts["tasks"],
          sum(recon["authoritative"]["task_status_counts"].values()))
    check("recon.query_outcomes", {"ok": 1}, recon["queries"]["outcome_counts"])
    check("recon.query_intents_without_result", 0, recon["queries"]["intents_without_result"])
    check("recon.query_consumptions_bound", 1, recon["queries"]["consumptions_bound"])
    check("recon.budget_blocked", 1, recon["rejections"]["budget_blocked_tasks"])
    check("recon.missing_evidence", 1, recon["evidence"]["missing_evidence_blocked_tasks"])
    check("recon.reservations", counts["reservations"], recon["usage"]["ledger"]["model_calls_today"])
    check("recon.replay_tasks", counts["tasks"] - 1,
          recon["usage"]["replay"]["tasks_in_replay_mode"])
    check("recon.real_supplier_unknown", "unknown", recon["usage"]["real_supplier"]["status"])
    check("recon.estimated_unknown", "unknown", recon["usage"]["estimated"]["status"])
    check("recon.unknown_items", ["estimated_usage", "real_supplier_usage"], recon["unknown_items"])
    check("recon.read_only_store", True, recon["resources"]["store_read_only"])

    # --- metrics: every value tagged with its evidence class
    metrics = report["metrics"]
    check("metrics.evidence_tags", True, all(m["evidence"] in ("ledger", "replay", "unknown")
                                             for m in metrics))
    check("metrics.unknown_usage", ["unknown", "unknown"],
          [m["evidence"] for m in metrics if m["name"].startswith("stock_agent_usage_")])

    # --- alert candidates: technical anomalies page, business gates never do
    alerts = report["alert_candidates"]
    triggered = {c["rule"]: c["layer"] for c in alerts["candidates"] if c["candidate"]}
    check("alerts.enabled", True, alerts["enabled"])
    check("alerts.not_dispatched", False, alerts["dispatched"])
    check("alerts.dispatch", "NOT_RUN", alerts["dispatch"])
    check("alerts.technical", ["queue_oldest_age", "stale_leases"],
          sorted(r for r, layer in triggered.items() if layer == "technical"))
    check("alerts.business_severity", "info",
          next(c["severity"] for c in alerts["candidates"] if c["rule"] == "blocked_tasks"))
    check("alerts.business_not_technical", [],
          [r for r, layer in triggered.items() if layer == "technical" and "blocked" in r])

    # --- structured log: allow-listed fields only, silent when sampling is off
    sink = io.StringIO()
    record = emit_observability_log(
        "r23_drill", {"task_id": task_id, "status": "queued",
                      "prompt": SECRETS["body"], "access_token": SECRETS["token"],
                      "token": SECRETS["api_key"], "header": SECRETS["bearer"]},
        config=config, sink=sink)
    kept = (record or {}).get("fields") or {}
    check("log.emitted", True, record is not None and len(sink.getvalue().splitlines()) == 1)
    check("log.kept_fields", {"task_id", "status"}, set(kept))
    check("log.dropped_count", 4, (record or {}).get("dropped_fields"))
    # A body under an allow-listed NAME is still dropped: values must be
    # identifier-shaped and credential-free.
    check("log.body_under_allowed_name", ({}, 1),
          project_log_fields({"task_id": "x " + SECRETS["body"] + " y"}))
    off_sink = io.StringIO()
    check("log.off_silent", None, emit_observability_log(
        "r23_drill", {"task_id": task_id}, config=drill_config(sampling=False), sink=off_sink))
    check("log.off_no_output", "", off_sink.getvalue())
    malformed = {"observability": {"sampling": [], "alerts": {"enabled": True, "thresholds": []}}}
    check("config.malformed", (False, False, False), (lambda st: (
        st["config_valid"], st["sampling_enabled"], st["alerts_enabled"]))(
            observability_settings(malformed)))
    check("config.alerts_only_malformed", (False, True, False), (lambda st: (
        st["config_valid"], st["sampling_enabled"], st["alerts_enabled"]))(
        observability_settings({"observability": {"sampling": {"enabled": True},
                                                  "alerts": {"enabled": True,
                                                             "thresholds": []}}})))

    # --- query-ledger availability: missing / unreadable / readable-empty
    import tempfile

    from stock_agent.config import project_path_override
    from stock_agent.task_runtime import TaskStore

    def availability(make):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            (base / "led.jsonl").parent.mkdir(parents=True, exist_ok=True)
            make(base)
            probe_config = {"kimi_sources": {"ledger_path": "led.jsonl",
                                             "query_store_path": "intake.sqlite3"}}
            with project_path_override(lambda value: base / value):
                read = read_query_ledgers(probe_config)
                store = TaskStore(base / "t.sqlite3")
                try:
                    task, _ = store.create_task(title="合成", input_payload={
                        "research_question": "合成", "data_classification": "public"},
                        execution_mode="replay")
                    rep = build_observability_report(store, config=probe_config,
                                                     task_id=task["task_id"])
                finally:
                    store.close()
                created = sorted(p.name for p in base.iterdir())
            return (read["state"], read["intake_state"],
                    rep["reconciliation"]["queries"]["ledger_available"],
                    rep["trace"]["coverage"]["query_ledger_available"],
                    "data_query_ledger" in rep["reconciliation"]["unknown_items"],
                    rep["reconciliation"]["queries"]["outcome_counts"],
                    sorted(n for n in created if "intake" in n))

    def make_missing(base):
        pass

    def make_unreadable(base):
        (base / "led.jsonl").write_bytes(b"\xff\xfe\x00garbage\n")

    def make_empty(base):
        (base / "led.jsonl").write_text("", encoding="utf-8")

    def make_readable(base):
        (base / "led.jsonl").write_text(json.dumps({
            "record_type": "query_result", "request_id": "r1", "outcome": "ok"}) + "\n",
            encoding="utf-8")

    # (ledger state, intake state, report available, trace available,
    #  data_query_ledger unknown, outcome counts, files named *intake* created)
    check("ledger.missing", ("missing", "missing", False, False, True, {}, []),
          availability(make_missing))
    check("ledger.unreadable", ("unreadable", "missing", False, False, True, {}, []),
          availability(make_unreadable))
    # Readable but empty is a real answer: available, no unknown item.
    check("ledger.readable_empty", ("readable_empty", "missing", True, True, False, {}, []),
          availability(make_empty))
    check("ledger.readable", ("readable", "missing", True, True, False, {"ok": 1}, []),
          availability(make_readable))
    check("ledger.read_never_creates_intake", True, True)  # see the last tuple slot above

    # --- redaction / no-body: serialized output carries no secret or body marker
    blob = json.dumps([report, trace, sink.getvalue()], ensure_ascii=False, default=str)
    leaked = [name for name, value in SECRETS.items() if value in blob]
    check("redaction.no_secret", [], leaked)

    # --- zero side effects: N repeated reads leave every ledger untouched
    for _ in range(5):
        build_observability_report(store, config=config, task_id=task_id)
        build_trace(store, task_id, config=config)
    after = ledger_counts(store)
    check("zero_side_effects.counts", before, after)
    check("zero_side_effects.files", files_before, file_fingerprints(root))
    return {"ok": all(c["ok"] for c in checks.values()), "checks": checks,
            "counts_before": before, "counts_after": after}


def business_with_sampling_off(store, root: Path, config_off: dict) -> dict:
    """Rollback path: sampling off, the real worker still runs and the audit
    rows (old and new) stay intact."""
    from stock_agent.research_worker import run_research_task

    before = ledger_counts(store)
    history = {e["event_id"] for e in store.events(limit=100000)}
    sink = io.StringIO()
    # Same input shape and execution mode as the frozen case, so this run
    # takes the same worker path (and reaches the model stub).
    frozen = [t for t in store.list_tasks() if t.get("business_intent_id") == INTENT_ID][0]
    payload = dict(frozen.get("input_payload") or {})
    task, _ = store.create_task(title="R23 关闭采样后的业务任务", input_payload=payload,
                                case_id=CASE_ID, execution_mode="live")
    task_id = task["task_id"]
    # Claim THIS task: claim_next would take the oldest queued one instead.
    got = store.claim_task(task_id, "w-r23-off", ttl_seconds=120)
    stub = _ModelStub("synthetic before-send failure " + SECRETS["body"],
                      stage="preflight", sent_hint="no")
    from unittest.mock import patch

    with patch("stock_agent.pi_runtime.call_pi_agent", side_effect=stub.fail), \
            contextlib.redirect_stderr(sink):
        result = run_research_task(store, task_id, worker_id="w-r23-off", config=config_off)
    after = ledger_counts(store)
    now = {e["event_id"] for e in store.events(limit=100000)}
    task_events = [e["event_type"] for e in store.events(task_id=task_id, limit=100)]
    state = str(store.get_task(task_id)["status"])
    ok = (stub.calls == 1 and sink.getvalue() == "" and history <= now
          and after["tasks"] == before["tasks"] + 1
          and after["outbox_events"] == before["outbox_events"] + len(task_events)
          and after["attempts"] == before["attempts"] + 1
          and state == "queued")
    assert root  # (state lives in the task store; root kept for symmetry)
    return {"ok": ok, "model_stub_calls": stub.calls, "log_output": sink.getvalue(),
            "task_status": state, "result_status": str(result.get("status") or ""),
            "audit_history_preserved": history <= now, "new_task_events": task_events,
            "counts_before": before, "counts_after": after}


def scale_check(root: Path) -> dict:
    """An attempt on the 5,001st task must be counted (no sampling cap)."""
    from stock_agent.observability import build_observability_report
    from stock_agent.task_runtime import TaskStore

    path = root / SCALE_DB_REL
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        path.unlink()
    store = TaskStore(path)
    try:
        older, newer = "2026-11-24T01:00:00+00:00", "2026-11-24T02:00:00+00:00"
        for number in range(5001):
            task, _ = store.create_task(
                title="R23 scale {0}".format(number),
                input_payload={"research_question": "Synthetic case {0}?".format(number),
                               "data_classification": "public"}, execution_mode="replay")
        task_id = task["task_id"]
        claimed = store.claim_task(task_id, "w-r23-scale", ttl_seconds=120)
        token = claimed["fencing_token"]
        store.mark_running(task_id, worker_id="w-r23-scale", fencing_token=token,
                           expected_version=claimed["version"])
        store.prepare_invocation_attempt(
            task_id, worker_id="w-r23-scale", fencing_token=token, round_index=1,
            provider="synthetic", request_model="synthetic", prompt_sha256="b" * 64)
        store._conn.execute(
            "UPDATE research_tasks SET created_at = CASE WHEN task_id = ? THEN ? ELSE ? END",
            (task_id, newer, older))
        store._conn.commit()
        physical = int(store._conn.execute(
            "SELECT COUNT(*) FROM task_invocation_attempts").fetchone()[0])
        report = build_observability_report(store, task_id=task_id)
    finally:
        store.close()
    invocation = report["reconciliation"]["invocations"]
    view = sum(invocation["states"].values())
    path.unlink()
    return {"ok": physical == view == len(report["trace"]["attempts"]) == 1
                   and invocation["by_provider"] == {"synthetic": 1},
            "tasks": 5001, "sqlite_attempts": physical, "reconciliation_states_total": view,
            "provider_counts": invocation["by_provider"],
            "trace_attempts": len(report["trace"]["attempts"]),
            "scope": invocation["scope"]}


# --------------------------------------------------------------------------- #
# subcommands
# --------------------------------------------------------------------------- #

def cmd_seed() -> int:
    from stock_agent.config import project_path_override
    from stock_agent.task_runtime import TaskStore

    root = project_root()
    os.chdir(root)
    config_path = root / CONFIG_REL
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(json.dumps(drill_config(), ensure_ascii=False, indent=2), encoding="utf-8")
    with project_path_override(lambda value: root / value):
        db = root / DB_REL
        db.parent.mkdir(parents=True, exist_ok=True)
        if db.exists():
            db.unlink()
        store = TaskStore(db)
        try:
            seeded = seed_case(store, root, drill_config())
            counts = ledger_counts(store)
            intake = intake_counts(root)
        finally:
            store.close()
    worker = seeded["worker"]
    # Sampling ON + the real worker: one redacted line per run, no marker.
    worker_ok = (worker["first_log_lines"] == 1 and worker["second_log_lines"] == 1
                 and not worker["logs_contain_marker"] and worker["first_calls"] == 1
                 and worker["second_calls"] == 1)
    ok = (worker_ok and seeded["dify_http_status"] == [200, 200]
          and seeded["governed_query"] == {"supply_status": "data_delivered",
                                           "consumption_recorded": True}
          and intake == {"intents": 1, "consumptions": 1})
    emit({"phase": "seed", "ok": ok, "root": str(root), "config": CONFIG_REL,
          "seeded": seeded, "counts": counts, "intake": intake,
          "worker_sampling_on_ok": worker_ok})
    return 0 if ok else 13


def cmd_report() -> int:
    from stock_agent.config import project_path_override
    from stock_agent.task_runtime import TaskStore

    root = project_root()
    os.chdir(root)
    with project_path_override(lambda value: root / value):
        config = drill_config()
        store = TaskStore(root / DB_REL, read_only=True)
        try:
            tasks = [t for t in store.list_tasks() if t.get("business_intent_id") == INTENT_ID]
            result = verify_report(store, config, tasks[0]["task_id"], root)
        finally:
            store.close()
        # The rollback check RUNS the worker, so it writes. It does so on a
        # copy of the drilled store: the drilled store itself is never
        # touched by a report, and a second report sees the same ledgers.
        off_db = root / OFF_DB_REL
        if off_db.exists():
            off_db.unlink()
        source = TaskStore(root / DB_REL, read_only=True)
        try:
            target = sqlite3.connect(str(off_db))
            source._conn.backup(target)
            target.close()
        finally:
            source.close()
        writable = TaskStore(off_db)
        try:
            off = business_with_sampling_off(writable, root, drill_config(sampling=False))
        finally:
            writable.close()
            for leftover in sorted(off_db.parent.glob(off_db.name + "*")):
                leftover.unlink()
        scale = scale_check(root)
    failed = [name for name, c in result["checks"].items() if not c["ok"]]
    ok = result["ok"] and off["ok"] and scale["ok"]
    emit({"phase": "report", "ok": ok,
          "checks_total": len(result["checks"]), "checks_failed": failed,
          "checks": result["checks"], "zero_side_effects": {
              "before": result["counts_before"], "after": result["counts_after"]},
          "business_with_sampling_off": off, "scale": scale})
    return 0 if ok else 13


def cmd_serve() -> int:
    from stock_agent.config import project_path_override, load_config

    root = project_root()
    os.chdir(root)
    with project_path_override(lambda value: root / value):
        config = load_config(CONFIG_REL)
        store, dify, board = start_servers(config)
        emit({"phase": "serve", "listening": ["0.0.0.0:8770", "0.0.0.0:8769"]})
        try:
            threading.Event().wait()
        except KeyboardInterrupt:
            pass
        finally:
            dify.server_close()
            board.server_close()
            store.close()
    return 0


def cmd_probe() -> int:
    from stock_agent.config import load_config, project_path_override

    root = project_root()
    os.chdir(root)
    with project_path_override(lambda value: root / value):
        loop = probe_loop("http://127.0.0.1:8770", "http://127.0.0.1:8769")
        permission = probe_permission("http://127.0.0.1:8770", "http://127.0.0.1:8769",
                                      host="serve:8770")
        degrade = probe_degrade(load_config(CONFIG_REL))
    ok = loop["ok"] and permission["ok"] and degrade["ok"]
    emit({"phase": "probe", "ok": ok, "loop": loop, "permission": permission,
          "degrade": degrade})
    return 0 if ok else 13


def cmd_cross(base_dify: str, base_board: str) -> int:
    root = project_root()
    os.chdir(root)
    results = [read_projection(base_dify, p) for p in ("/livez", "/readyz")]
    results += [read_projection(base_board, p) for p in ("/api/v1/ops", "/api/v1/queries-view")]
    ok = all(r["status"] == 403 for r in results)
    emit({"phase": "cross", "ok": ok,
          "results": [{"path": r["path"], "status": r["status"]} for r in results]})
    return 0 if ok else 13


COMMANDS = {
    "seed": lambda args: cmd_seed(),
    "report": lambda args: cmd_report(),
    "serve": lambda args: cmd_serve(),
    "probe": lambda args: cmd_probe(),
    "cross": lambda args: cmd_cross(args[0], args[1]),
}


def main() -> int:
    if len(sys.argv) < 2 or sys.argv[1] not in COMMANDS:
        print(__doc__, file=sys.stderr)
        return 2
    return COMMANDS[sys.argv[1]](sys.argv[2:])


if __name__ == "__main__":
    raise SystemExit(main())
