#!/usr/bin/env python
"""R23 observability drill driver (KONM-96) — synthetic case, zero providers.

Runs inside the candidate container (app bind-mounted at /app) or from a
checkout. Builds ONE frozen synthetic case through the real store API and
exercises the R23 projections against it:

    seed     write the drill config and the frozen case into the task store
             and the query ledger (attempt retry / known failure / unknown
             reconciled and unknown open / platform replay / blocked tasks
             carrying synthetic credential-shaped and prompt-body markers)
    report   read-only: build the report N times, compare every view number
             with the authoritative ledgers, prove zero side effects, scan
             the serialized output for the synthetic secrets and body marker,
             check alert candidates, then show the business chain still
             works with sampling off and the audit rows are untouched
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

import io
import json
import os
import sys
import threading
import urllib.error
import urllib.request
from pathlib import Path

DB_REL = "data/task_runtime/tasks.sqlite3"
LEDGER_REL = "data/r23_drill/query_ledger.jsonl"
CONFIG_REL = "data/config/watchlist.r23.json"

CASE_ID = "case-r23-frozen-001"
INTENT_ID = "intent-r23-frozen-001"
DIFY_RUN_1 = "dify-run-synthetic-001"
DIFY_RUN_2 = "dify-run-synthetic-002-replay"

# Synthetic, never valid anywhere. The report must not contain any of them.
SECRETS = {
    "api_key": "sk-SYNTHETICR23KEY0000000000",
    "bearer": "Bearer SYNTHETICR23TOKEN000000000000",
    "assigned": "api_key=SYNTHETICR23ASSIGNED0000000",
    "body": "PROMPT_BODY_MARKER_R23_DO_NOT_LEAK",
}
FORBIDDEN = tuple(SECRETS.values())

DRILL_CONFIG = {
    "storage": {"sqlite_path": "data/stock_agent.sqlite3"},
    "task_runtime": {"db_path": DB_REL},
    "kimi_sources": {"ledger_path": LEDGER_REL},
    "dify": {"enabled": False},
    "observability": {
        "sampling": {"enabled": True},
        "alerts": {"enabled": True, "thresholds": {"oldest_queued_age_seconds": 600}},
    },
}


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


# --------------------------------------------------------------------------- #
# seed: the frozen synthetic case, through the real store API
# --------------------------------------------------------------------------- #

def seed_case(store, root: Path, config: dict) -> dict:
    from stock_agent.task_runtime import TaskStore
    from stock_agent.task_runtime.store import (
        ATTEMPT_FAILED_KNOWN, ATTEMPT_OUTCOME_UNKNOWN, ATTEMPT_SUCCEEDED)

    def spec(question):
        return {"research_question": question, "symbols": ["600519"],
                "execution_mode": "replay", "data_classification": "public",
                "prompt_body": SECRETS["body"]}

    run1 = {"platform": "dify", "run_id": DIFY_RUN_1, "workflow_version": "wf-r23-synthetic",
            "actor": "dify-synthetic"}
    # The permitted Dify entry for a synthetic case: business intent + first platform run.
    task, _ = store.create_task(
        title="R23 冻结合成案例", input_payload=spec(SECRETS["body"]), case_id=CASE_ID,
        idempotency_key="intent::{0}".format(INTENT_ID), business_intent_id=INTENT_ID,
        execution_mode="replay", platform_run=run1)
    task_id = task["task_id"]
    # A replayed platform run re-drives the SAME intent: association, not a new task.
    store.attach_platform_run(task_id, run={
        "platform": "dify", "run_id": DIFY_RUN_2, "workflow_version": "wf-r23-synthetic",
        "actor": "dify-synthetic"})

    claimed = store.claim_task(task_id, "w-r23", ttl_seconds=120)
    token = claimed["fencing_token"]
    store.mark_running(task_id, worker_id="w-r23", fencing_token=token,
                       expected_version=claimed["version"])

    def attempt(round_index, close=None, error="", provider="synthetic-replay"):
        record = store.prepare_invocation_attempt(
            task_id, worker_id="w-r23", fencing_token=token, round_index=round_index,
            provider=provider, request_model="synthetic-model", prompt_sha256="b" * 64)
        store.mark_invocation_dispatched(record["attempt_id"], worker_id="w-r23", fencing_token=token)
        if close:
            store.complete_invocation_attempt(
                record["attempt_id"], worker_id="w-r23", fencing_token=token, state=close,
                sent="unknown" if close == ATTEMPT_OUTCOME_UNKNOWN else "yes", error=error)
        return record

    failed = attempt(1, close=ATTEMPT_FAILED_KNOWN,
                     error="provider rejected: {0}".format(SECRETS["assigned"]))
    unknown = attempt(2, close=ATTEMPT_OUTCOME_UNKNOWN, error="timeout after send")
    store.reconcile_invocation_attempt(
        unknown["attempt_id"], actor="ops-synthetic", resolution="confirmed_not_charged",
        reason="合成回执无计费记录")
    open_unknown = attempt(3, close=ATTEMPT_OUTCOME_UNKNOWN, error="connection reset")
    succeeded = attempt(4, close=ATTEMPT_SUCCEEDED)
    store.complete(task_id, worker_id="w-r23", fencing_token=token, output={"summary": "synthetic"})

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

    # Query ledger: one settled query, one outcome-unknown, one intent without result.
    ledger_path = root / LEDGER_REL
    ledger_path.parent.mkdir(parents=True, exist_ok=True)
    stamp = "2026-11-24T00:00:00+00:00"
    rows = [
        {"record_type": "query_intent", "created_at": stamp, "request_id": "req-r23-ok",
         "idempotency_key": "k1", "source_id": "kimi_synthetic", "api_id": "api-1",
         "data_kind": "financing", "purpose": "r23", "task_id": task_id,
         "contract_hash": "c" * 64, "normalized_params": {"ticker": "600519"},
         "secret": SECRETS["api_key"]},
        {"record_type": "query_result", "created_at": stamp, "request_id": "req-r23-ok",
         "outcome": "ok", "source_id": "kimi_synthetic", "data_kind": "financing",
         "cost_status": "free", "attempts": 1, "row_count": 5, "http_status": 200,
         "artifact_plans": [{"remote_name_record": "x.parquet", "remote_name_rejected": True}]},
        {"record_type": "query_intent", "created_at": stamp, "request_id": "req-r23-unknown",
         "idempotency_key": "k2", "source_id": "kimi_synthetic", "api_id": "api-2",
         "data_kind": "financing", "purpose": "r23", "task_id": task_id,
         "contract_hash": "c" * 64, "normalized_params": {}},
        {"record_type": "query_result", "created_at": stamp, "request_id": "req-r23-unknown",
         "outcome": "outcome_unknown", "source_id": "kimi_synthetic", "data_kind": "financing",
         "cost_status": "unknown", "attempts": 1, "row_count": None, "http_status": None,
         "artifact_plans": []},
        {"record_type": "query_intent", "created_at": stamp, "request_id": "req-r23-open",
         "idempotency_key": "k3", "source_id": "kimi_synthetic", "api_id": "api-3",
         "data_kind": "financing", "purpose": "r23", "task_id": "other-task",
         "contract_hash": "c" * 64, "normalized_params": {}},
    ]
    ledger_path.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows),
                           encoding="utf-8")
    assert isinstance(store, TaskStore) and failed and open_unknown and succeeded
    return {"task_id": task_id, "case_id": CASE_ID, "business_intent_id": INTENT_ID,
            "platform_runs": [DIFY_RUN_1, DIFY_RUN_2],
            "attempt_ids": {"failed_known": failed["attempt_id"],
                            "unknown_reconciled": unknown["attempt_id"],
                            "unknown_open": open_unknown["attempt_id"],
                            "succeeded": succeeded["attempt_id"]},
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


# --------------------------------------------------------------------------- #
# report: assertions over the read-only projections
# --------------------------------------------------------------------------- #

def verify_report(store, config: dict, task_id: str) -> dict:
    from stock_agent.observability import (
        build_observability_report, build_trace, emit_observability_log)

    before = ledger_counts(store)
    checks = {}

    def check(name, expected, actual):
        checks[name] = {"expected": expected, "actual": actual, "ok": expected == actual}
        return expected == actual

    report = build_observability_report(store, config=config, task_id=task_id)
    recon = report["reconciliation"]
    technical, business = recon["technical"], recon["business"]

    # --- trace: one chain from case / intent to platform run, query, attempt
    trace = build_trace(store, task_id, config=config)
    check("trace.case", CASE_ID, trace["business_identity"]["case_id"])
    check("trace.intent", INTENT_ID, trace["business_identity"]["business_intent_id"])
    check("trace.platform_runs", [DIFY_RUN_1, DIFY_RUN_2],
          [r["run_id"] for r in trace["platform_runs"]])
    check("trace.intent_not_a_platform_id", True,
          all(r["run_id"] != INTENT_ID for r in trace["platform_runs"]))
    check("trace.attempts", 4, len(trace["attempts"]))
    check("trace.attempt_rounds", [1, 2, 3, 4], [a["round_index"] for a in trace["attempts"]])
    check("trace.attempt_states", ["failed_known", "reconciled", "outcome_unknown",
                                   "succeeded"], [a["state"] for a in trace["attempts"]])
    check("trace.reconciled_flags", [False, True, False, False],
          [a["reconciled"] for a in trace["attempts"]])
    check("trace.reconciliation", "confirmed_not_charged", trace["attempts"][1]["reconciliation"])
    check("trace.queries", ["req-r23-ok", "req-r23-unknown"],
          sorted(q["request_id"] for q in trace["queries"]))
    check("trace.coverage", ["reconciled", "retry", "success", "unknown"],
          trace["coverage"]["outcomes"])
    check("trace.events_nonempty", True, len(trace["events"]) > 0)
    check("report.trace_same_task", task_id, report["trace"]["task"]["task_id"])

    # --- layering: machine health and business gates side by side
    # One live lease exists in the store but it is expired: healthy in-flight work is 0.
    check("layer.healthy_inflight", 0,
          report["status"]["technical"]["inflight_leases"] - technical["stale_leases"])
    check("layer.blocked", 4, business["blocked_tasks"])
    check("layer.gates", {"budget": 1, "evidence_quality": 1, "governance": 1,
                          "invocation_outcome_unknown": 1, "other": 0}, business["gate_counts"])
    check("layer.no_error_in_technical", False, "error" in technical)

    # --- reconciliation against the authoritative ledgers
    counts = before
    check("recon.queue_depth", 1, recon["queue"]["depth"]["value"])
    check("recon.stale_leases", 1, recon["queue"]["stale_leases"]["value"])
    check("recon.oldest_age_years", True, recon["queue"]["oldest_age_seconds"]["value"] > 6 * 365 * 86400)
    check("recon.attempts", counts["attempts"], sum(technical["invocation_attempts"].values()))
    check("recon.known_failures", 1, recon["invocations"]["known_failures"])
    check("recon.unknown_open", 1, recon["invocations"]["unknown_open"])
    check("recon.unknown_reconciled", 1, recon["invocations"]["unknown_reconciled"])
    check("recon.waiting", 1, recon["invocations"]["reconciliations_waiting"])
    check("recon.outbox", counts["outbox_events"], recon["authoritative"]["outbox_events"])
    check("recon.event_scan_complete", True, recon["authoritative"]["event_scan"]["complete"])
    check("recon.status_total", counts["tasks"],
          sum(recon["authoritative"]["task_status_counts"].values()))
    check("recon.query_outcomes", {"ok": 1, "outcome_unknown": 1}, recon["queries"]["outcome_counts"])
    check("recon.query_intents_without_result", 1, recon["queries"]["intents_without_result"])
    check("recon.artifact_rejections", 1, recon["rejections"]["query_artifact_rejections"])
    check("recon.missing_evidence", 1, recon["evidence"]["missing_evidence_blocked_tasks"])
    check("recon.reservations", counts["reservations"],
          recon["usage"]["ledger"]["model_calls_today"])
    check("recon.replay_tasks", counts["tasks"], recon["usage"]["replay"]["tasks_in_replay_mode"])
    check("recon.real_supplier_unknown", "unknown", recon["usage"]["real_supplier"]["status"])
    check("recon.estimated_unknown", "unknown", recon["usage"]["estimated"]["status"])
    check("recon.unknown_items_visible", True,
          {"estimated_usage", "real_supplier_usage"} <= set(recon["unknown_items"]))
    check("recon.read_only_store", True, recon["resources"]["store_read_only"])

    # --- metrics: every value tagged with its evidence class
    metrics = report["metrics"]
    check("metrics.evidence_tags", True, all(m["evidence"] in ("ledger", "replay", "unknown")
                                             for m in metrics))
    check("metrics.unknown_usage", ["unknown", "unknown"],
          [m["evidence"] for m in metrics if m["name"].startswith("stock_agent_usage_")])
    check("metrics.replay_tagged", True, any(
        m["name"] == "stock_agent_tasks_replay_mode" and m["evidence"] == "replay" for m in metrics))

    # --- alert candidates: technical anomalies page, business gates never do
    alerts = report["alert_candidates"]
    triggered = {c["rule"]: c["layer"] for c in alerts["candidates"] if c["candidate"]}
    check("alerts.enabled", True, alerts["enabled"])
    check("alerts.not_dispatched", False, alerts["dispatched"])
    check("alerts.dispatch", "NOT_RUN", alerts["dispatch"])
    check("alerts.technical", ["query_outcome_unknown", "queue_oldest_age",
                               "reconciliations_waiting", "stale_leases"],
          sorted(r for r, layer in triggered.items() if layer == "technical"))
    check("alerts.business_not_technical", [], [r for r, layer in triggered.items()
                                                 if layer == "technical" and "blocked" in r])
    check("alerts.technical_anomalies_only", True,
          all(layer == "business" for r, layer in triggered.items() if "blocked" in r))
    check("alerts.business_severity", "info",
          next(c["severity"] for c in alerts["candidates"] if c["rule"] == "blocked_tasks"))

    # --- structured log: redacted, and silent when sampling is off
    sink = io.StringIO()
    record = emit_observability_log(
        "r23_drill", {"token": SECRETS["api_key"], "header": SECRETS["bearer"],
                      "body": SECRETS["body"][:10] + "…", "task_id": task_id},
        config=config, sink=sink)
    check("log.emitted", True, record is not None and len(sink.getvalue().splitlines()) == 1)
    off_sink = io.StringIO()
    check("log.off_silent", None, emit_observability_log(
        "r23_drill", {"x": 1}, config=drill_config(sampling=False), sink=off_sink))
    check("log.off_no_output", "", off_sink.getvalue())

    # --- redaction: serialized output carries no secret / body marker
    blob = json.dumps([report, trace, sink.getvalue()], ensure_ascii=False, default=str)
    leaked = [name for name, value in SECRETS.items() if value in blob]
    check("redaction.no_secret", [], leaked)
    check("redaction.marked", True, "[REDACTED]" in blob)

    # --- zero side effects: N repeated reads leave every ledger untouched
    for _ in range(5):
        build_observability_report(store, config=config, task_id=task_id)
    after = ledger_counts(store)
    check("zero_side_effects.counts", before, after)
    return {"ok": all(c["ok"] for c in checks.values()), "checks": checks,
            "counts_before": before, "counts_after": after}


def business_with_sampling_off(store, config_off: dict) -> dict:
    """Rollback path: observation off, the business chain and its audit rows stay intact."""
    from stock_agent.observability import emit_observability_log

    before = ledger_counts(store)
    history = {e["event_id"] for e in store.events(limit=100000)}
    sink = io.StringIO()
    emitted = emit_observability_log("business_step", {"note": "x"}, config=config_off, sink=sink)
    task, _ = store.create_task(title="R23 关闭采样后的业务任务",
                                input_payload={"research_question": "合成", "execution_mode": "replay",
                                               "data_classification": "public"},
                                case_id=CASE_ID, execution_mode="replay")
    got = store.claim_task(task["task_id"], "w-r23-off", ttl_seconds=120)
    store.mark_running(task["task_id"], worker_id="w-r23-off", fencing_token=got["fencing_token"],
                       expected_version=got["version"])
    store.complete(task["task_id"], worker_id="w-r23-off", fencing_token=got["fencing_token"],
                   output={"summary": "synthetic"})
    after = ledger_counts(store)
    now = {e["event_id"] for e in store.events(limit=100000)}
    task_events = [e["event_type"] for e in store.events(task_id=task["task_id"], limit=100)]
    ok = (emitted is None and sink.getvalue() == "" and history <= now
          and after["tasks"] == before["tasks"] + 1
          and after["outbox_events"] == before["outbox_events"] + len(task_events)
          and task_events == ["task_created", "task_claimed", "task_running", "task_succeeded"])
    return {"ok": ok, "log_emitted": emitted, "log_output": sink.getvalue(),
            "audit_history_preserved": history <= now,
            "new_task_events": task_events,
            "counts_before": before, "counts_after": after}


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
        finally:
            store.close()
    emit({"phase": "seed", "ok": True, "root": str(root), "config": CONFIG_REL,
          "seeded": seeded, "counts": counts})
    return 0


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
            result = verify_report(store, config, tasks[0]["task_id"])
        finally:
            store.close()
        writable = TaskStore(root / DB_REL)
        try:
            off = business_with_sampling_off(writable, drill_config(sampling=False))
        finally:
            writable.close()
    failed = [name for name, c in result["checks"].items() if not c["ok"]]
    emit({"phase": "report", "ok": result["ok"] and off["ok"],
          "checks_total": len(result["checks"]), "checks_failed": failed,
          "checks": result["checks"], "zero_side_effects": {
              "before": result["counts_before"], "after": result["counts_after"]},
          "business_with_sampling_off": off})
    return 0 if result["ok"] and off["ok"] else 13


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
