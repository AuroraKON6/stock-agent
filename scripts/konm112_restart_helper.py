#!/usr/bin/env python3
"""KONM-112 cross-restart ledger helper — runs INSIDE the fixed candidate image.

Extends the R24 fixture (scripts/r24_restore_drill.py, same /drills mount) with
the reads a restart needs. Everything here is synthetic and local: no provider,
no network, no production volume.

Subcommands (each prints one JSON object):
    ledger                        full-row ledger of one store: nine tables,
                                  per-attempt budget projection, workflow
                                  projection, outbox sequence/ID/hash, the
                                  evidence reference chain and the open
                                  outcome_unknown attempts
    resubmit                      submit the same five business intents again
                                  (four attempt fixtures + the workflow task)
                                  and report task ID / dedup verdict each
    settle-unsent                 drain step: a task that only RESERVED and
                                  prepared (nothing dispatched: every attempt
                                  still prepared, sent='no') is closed through
                                  the public transitions as not sent — the
                                  attempt as failed_known/sent='no', the task
                                  as failed without retry. The budget
                                  reservation is kept, nothing is consumed.
    inject <running|live-lease>   negative control for the drain gate. running:
                                  one task left running after its lease has
                                  expired, with no open attempt. live-lease:
                                  one awaiting_human task whose original
                                  fencing token can still complete, and
                                  queued/leased/running/open attempts are all
                                  zero. Neither control prepares an attempt.
    drain-gate                    one SQLite read-only snapshot: task counts,
                                  leases and open attempts are read inside a
                                  single deferred read transaction. Exit 0
                                  only when queued, leased, running, live
                                  leases, still-writable fencing leases
                                  (leased/running/awaiting_human) and open
                                  prepared/dispatched attempts are all zero.
    repro-drain-gaps              re-run the two missed cases on temporary
                                  databases: interleaved commits during the
                                  read, and an awaiting_human token that can
                                  still complete. Exit 0 only when both gates
                                  refuse.
    hold-writes                   hold BEGIN IMMEDIATE on the task database
                                  until /drill-receipts releases it, so no
                                  other connection can start a write
    wait-barrier                  wait until that write barrier is held
    probe-writes                  exit 0 only when a second write transaction
                                  is refused while the barrier is held
    release-writes                ask the barrier holder to roll back and exit
    read <data-root> <cursor> <limit>
                                  cursor read through TaskStore.events_after
    consume <data-root> <receipt-db> <label> <cursor>
                                  isolated idempotent consumer: deliver the
                                  events after <cursor> once per event ID,
                                  persisting receipts in its OWN sqlite file
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
import sys
from contextlib import closing
import time
from pathlib import Path

sys.dont_write_bytecode = True

import r24_restore_drill as r24  # noqa: E402

r25 = r24.r25
PAGE = 100
BARRIER_MARKER = Path("/drill-receipts/konm112_write_barrier.json")
BARRIER_RELEASE = Path("/drill-receipts/konm112_write_barrier.release")


def event_digest(event: dict) -> str:
    canonical = json.dumps({
        "event_id": event["event_id"], "task_id": event["task_id"], "event_type": event["event_type"],
        "actor": event["actor"], "command_id": event["command_id"], "created_at": event["created_at"],
        "payload": event["payload"]}, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _data_root(value: str) -> Path:
    """The live store is named "" or "data"; anything else is an absolute path
    (the independent restore's data tree)."""
    value = value.strip()
    if value in {"", "data"}:
        return r24.DATA
    return Path(value)


def _store(data: Path):
    from stock_agent.task_runtime import TaskStore

    return TaskStore(data / r24.DB, read_only=True)


def _live_manifest(data: Path) -> dict:
    """The manifest file entries the R24 workflow projection checks, rebuilt
    from the live artifacts' own bytes (no bundle needs to exist)."""
    files = []
    for folder in sorted(path for path in (data / r24.WORKFLOW_DIR).iterdir() if path.is_dir()):
        for filename, kind in ((r24.FLOW_MANIFEST, "workflow_manifest"), (r24.FLOW_DSL, "workflow_dsl")):
            path = folder / filename
            files.append({"path": path.relative_to(data.parent).as_posix() if data.name == "data"
                          else (Path("data") / path.relative_to(data)).as_posix(),
                         "sha256": r24.sha256(path), "bytes": path.stat().st_size, "kind": kind})
    return {"files": files}


def ledger(data_value: str = "") -> int:
    from stock_agent.budget_guard import may_start_model_call

    data = _data_root(data_value)
    config = r24.read_json(Path("/app") / r25.CONFIG_REL)
    rows = r24.snapshot(data)
    budget, budget_errors = r24.budget_projection(rows, config, drained=True)
    workflow, workflow_errors = r24.workflow_projection(data, rows, _live_manifest(data), config)
    tokens = 0
    for task in rows["research_tasks"]:
        for round_ in (json.loads(task["model_call"]).get("rounds") or []):
            usage = round_.get("usage") or {}
            tokens += int(usage.get("total_tokens") or 0)
    intents = sorted({row["business_intent_id"] for row in rows["research_tasks"]} - {""})
    per_intent = []
    for intent in intents:
        task_ids = [row["task_id"] for row in rows["research_tasks"] if row["business_intent_id"] == intent]
        per_intent.append({
            "intent": intent, "task_ids": task_ids,
            "attempt_rows": sum(row["task_id"] in task_ids for row in rows["task_invocation_attempts"]),
            "reservations": sum(row["task_id"] in task_ids and row["event_type"] == "research_attempt_started"
                                for row in rows["task_outbox"]),
            "outbox_events": sum(row["task_id"] in task_ids for row in rows["task_outbox"])})
    outbox, cursor = [], 0
    with closing(_store(data)) as store:
        unknown = [{key: row[key] for key in ("attempt_id", "task_id", "state", "sent", "budget_event_id")}
                   for row in store.outstanding_unknown_attempts()]
        summary = store.summary()
        gate = may_start_model_call(store, config)
        while True:
            page = store.events_after(cursor, limit=PAGE)
            if not page:
                break
            outbox.extend({"sequence": e["sequence"], "event_id": e["event_id"], "task_id": e["task_id"],
                           "event_type": e["event_type"], "sha256": event_digest(e)} for e in page)
            cursor = page[-1]["sequence"]
    evidence = r25._evidence_checks(str(data / r24.DB), data, connect=r24.sealed_connection)
    errors = list(budget_errors) + list(workflow_errors)
    r25.emit({
        "phase": "konm112_ledger", "data_root": data.as_posix(),
        "table_counts": {table: len(items) for table, items in rows.items()},
        "tasks": [{key: row[key] for key in ("task_id", "status", "case_id", "business_intent_id",
                                              "attempt_count", "updated_at")}
                  for row in rows["research_tasks"]],
        "attempts": budget, "per_intent": per_intent, "outcome_unknown_open": unknown,
        "workflow": workflow, "outbox": outbox, "outbox_summary": summary, "budget_gate": gate,
        "total_tokens_all_tasks": tokens, "evidence_chain_ok": bool(evidence.get("ok")),
        "errors": errors,
    })
    return 0 if not errors and evidence.get("ok") else 7


def _status_constants() -> tuple:
    from stock_agent.task_runtime.store import STATUS_LEASED, STATUS_RUNNING

    return STATUS_LEASED, STATUS_RUNNING


def _settled(item: dict) -> bool:
    return (item["status"] == "failed" and item["attempt_states"] == ["failed_known"]
            and item["attempt_sent"] == ["no"])


def settle_unsent() -> int:
    """Close what the seed left reserved-but-never-dispatched, before the snapshot.

    Only a task whose every attempt is still prepared / sent='no' is provably
    unsent; anything dispatched is left alone for the gate to refuse.
    """
    from stock_agent.task_runtime.store import ATTEMPT_DISPATCHED, ATTEMPT_PREPARED

    settled = []
    with closing(r25.open_store()) as store:
        active = set(_status_constants())
        for task in store.list_tasks():
            if task["status"] not in active:
                continue
            attempts = store.invocation_attempts(task["task_id"])
            if not attempts or any(a["state"] != ATTEMPT_PREPARED or a["sent"] != "no" for a in attempts):
                continue
            worker = {"worker_id": task["worker_id"], "fencing_token": int(task["fencing_token"])}
            closed = store.recover_stale_invocation_attempts(
                task["task_id"], error="drain: reserved and prepared, confirmed not sent")
            failed = store.fail(task["task_id"], error="drain: invocation never sent; settled before the snapshot",
                                retryable=False, **worker)
            attempts = store.invocation_attempts(task["task_id"])
            settled.append({"task_id": task["task_id"], "intent": task["business_intent_id"],
                            "closed": closed, "status": failed["status"],
                            "attempt_states": [a["state"] for a in attempts],
                            "attempt_sent": [a["sent"] for a in attempts]})
    ok = bool(settled) and all(_settled(item) for item in settled)
    r25.emit({"phase": "konm112_settle_unsent", "settled": settled, "ok": ok})
    return 0 if ok else 8


def inject(mode: str) -> int:
    """Leave one task the gate must refuse for that reason alone.

    Neither mode prepares an invocation attempt. ``running`` stays running
    after the lease timestamp has passed. ``live-lease`` is ``awaiting_human``
    with the claim's fencing token still able to complete, while queued,
    leased, running and open attempts are all zero.
    """
    label = "r25-negative-" + mode
    with closing(r25.open_store()) as store:
        task, _ = store.create_task(
            title=f"KONM-112 negative {mode}", case_id=r24.CASE, execution_mode="replay",
            business_intent_id=f"{r24.CASE}-{label}",
            input_payload={"data_classification": "public", "research_question": f"Synthetic {label}"})
        task_id = task["task_id"]
        claimed = store.claim_task(task_id, r24.ACTOR, ttl_seconds=1 if mode == "running" else 300)
        worker = {"worker_id": r24.ACTOR, "fencing_token": int(claimed["fencing_token"])}
        store.mark_running(task_id, expected_version=claimed["version"], **worker)
        if mode == "live-lease":
            store.request_awaiting_human(
                task_id, human_request={"question": "Synthetic handoff before the snapshot?"}, **worker)
        elif mode == "running":
            while r24.datetime.fromisoformat(store.get_task(task_id)["lease_expires_at"]) > \
                    r24.datetime.now().astimezone():
                time.sleep(1)
        final = store.get_task(task_id)
        attempts = store.invocation_attempts(task_id)
    now = r24.datetime.now().astimezone()
    try:
        lease_live = r24.datetime.fromisoformat(str(final["lease_expires_at"])) > now
    except ValueError:
        lease_live = False
    r25.emit({"phase": "konm112_inject", "mode": mode, "task_id": task_id, "status": final["status"],
              "lease_expires_at": final["lease_expires_at"], "lease_live": lease_live,
              "fencing_token_unchanged": int(final["fencing_token"]) == int(claimed["fencing_token"]),
              "open_attempts": len(attempts)})
    return 0


def _lease_still_valid(expires: str, read_at) -> bool:
    if not expires:
        return False
    try:
        return r24.datetime.fromisoformat(expires) > read_at
    except ValueError:
        return False


def drain_gate() -> int:
    """One read-only SQLite snapshot of what could still be written or dispatched.

    Task counts, lease rows and open attempts share one deferred read
    transaction on the read-only connection. A commit from another connection
    cannot appear in only part of this report. ``store.summary()`` is called
    inside that transaction so a hook placed between the old separate reads
    still runs against the same snapshot; the numbers below come from the
    snapshot queries, not from that return value.
    """
    from stock_agent.task_runtime.store import (
        ATTEMPT_DISPATCHED, ATTEMPT_PREPARED, STATUS_AWAITING_HUMAN, STATUS_LEASED, STATUS_RUNNING,
    )

    # _assert_worker still accepts the current fencing token in these statuses,
    # including after lease_expires_at and including awaiting_human.
    writable_statuses = {STATUS_LEASED, STATUS_RUNNING, STATUS_AWAITING_HUMAN}
    read_at = r24.datetime.now().astimezone()
    live_leases, writable_leases, historical = [], [], []
    with closing(_store(r24.DATA)) as store:
        connection = store._conn
        connection.execute("PRAGMA busy_timeout=30000")
        connection.execute("PRAGMA query_only=ON")
        connection.execute("BEGIN")
        try:
            data_version = int(connection.execute("PRAGMA data_version").fetchone()[0])
            # Establishes the snapshot, then gives an interleaving hook the
            # same transaction instead of a later autocommit read.
            store.summary()
            count_rows = connection.execute(
                "SELECT status, COUNT(*) AS n FROM research_tasks GROUP BY status").fetchall()
            task_rows = connection.execute(
                "SELECT task_id, status, fencing_token, lease_expires_at FROM research_tasks "
                "ORDER BY created_at").fetchall()
            open_attempts = connection.execute(
                "SELECT state, COUNT(*) AS n FROM task_invocation_attempts "
                "WHERE state IN (?, ?) GROUP BY state",
                (ATTEMPT_PREPARED, ATTEMPT_DISPATCHED)).fetchall()
            data_version_after = int(connection.execute("PRAGMA data_version").fetchone()[0])
        finally:
            connection.execute("ROLLBACK")
    counts = {row["status"]: int(row["n"]) for row in count_rows}
    for task in task_rows:
        expires = str(task["lease_expires_at"] or "")
        valid = _lease_still_valid(expires, read_at)
        if task["status"] in writable_statuses:
            detail = {"task_id": task["task_id"], "status": task["status"],
                      "lease_expires_at": expires, "lease_live": valid,
                      "fencing_token_present": int(task["fencing_token"] or 0) > 0}
            writable_leases.append(detail)
            if valid:
                live_leases.append({"task_id": detail["task_id"], "status": detail["status"],
                                    "lease_expires_at": expires})
        elif expires:
            historical.append({"task_id": task["task_id"], "status": task["status"]})
    open_by_state = {row["state"]: int(row["n"]) for row in open_attempts}
    report = {
        "phase": "konm112_drain_gate", "read_at": read_at.isoformat(), "read_only": True,
        "queued": int(counts.get("queued", 0)), "leased": int(counts.get("leased", 0)),
        "running": int(counts.get("running", 0)), "live_leases": len(live_leases),
        "live_lease_details": live_leases, "writable_leases": len(writable_leases),
        "writable_lease_details": writable_leases,
        "open_prepared": int(open_by_state.get(ATTEMPT_PREPARED, 0)),
        "open_dispatched": int(open_by_state.get(ATTEMPT_DISPATCHED, 0)),
        "terminal_tasks_with_past_lease_timestamp": len(historical),
        "task_counts": counts,
        "snapshot": {"isolation": "deferred_read_transaction", "query_only": True,
                     "data_version": data_version,
                     "data_version_stable": data_version == data_version_after},
    }
    report["drained"] = bool(report["snapshot"]["data_version_stable"]) and not any(
        report[key] for key in (
            "queued", "leased", "running", "live_leases", "writable_leases",
            "open_prepared", "open_dispatched"))
    r25.emit(report)
    return 0 if report["drained"] else 9


def repro_drain_gaps() -> int:
    """The two reads sol showed could pass. Both must now refuse."""
    from tempfile import TemporaryDirectory
    from unittest.mock import patch

    from stock_agent.task_runtime import TaskStore

    def create_fixture(store, title):
        task, verdict = store.create_task(
            title=title, execution_mode="replay", business_intent_id=title,
            input_payload={"data_classification": "public", "research_question": title})
        if verdict != "RECORDED":
            raise RuntimeError(f"fixture was not recorded: {verdict}")
        return task

    def run_gate(path: Path):
        from io import StringIO
        from contextlib import redirect_stdout

        output = StringIO()
        with patch.object(r24, "DATA", path), redirect_stdout(output):
            code = drain_gate()
        return code, json.loads(output.getvalue())

    results = []
    with TemporaryDirectory() as folder:
        path = Path(folder)
        with closing(TaskStore(path / r24.DB)) as writer:
            task = create_fixture(writer, "Synthetic attempt pending cleanup")
            task_id = task["task_id"]
            claim = writer.claim_task(task_id, "fixture-worker", ttl_seconds=300)
            worker = {"worker_id": "fixture-worker", "fencing_token": claim["fencing_token"]}
            writer.mark_running(task_id, expected_version=claim["version"], **worker)
            writer.prepare_invocation_attempt(
                task_id, round_index=1, provider="local-stub", request_model="fixture",
                prompt_sha256="fixture", **worker)
            writer.fail(task_id, error="Synthetic interruption", retryable=False, **worker)
            before, before_gate = run_gate(path)
            original_summary = TaskStore.summary

            def summary_then_queue(reader):
                summary = original_summary(reader)
                create_fixture(writer, "Synthetic queued during drain check")
                writer.recover_stale_invocation_attempts(task_id, error="Synthetic cleanup")
                return summary

            with patch.object(TaskStore, "summary", summary_then_queue):
                code, report = run_gate(path)
            after, after_gate = run_gate(path)
        consistent_blocker = code == 9 and (
            int(report["open_prepared"]) > 0 or int(report["queued"]) > 0
            or int(report["writable_leases"]) > 0)
        torn_clear = (int(report["queued"]) == 0 and int(report["open_prepared"]) == 0
                      and int(report["writable_leases"]) == 0 and report["drained"])
        results.append({
            "scenario": "queued then open attempt closed between SELECTs",
            "exit": code, "gate": report, "before_exit": before, "before_gate": before_gate,
            "after_exit": after, "after_gate": after_gate,
            "consistent_blocker": consistent_blocker, "torn_clear": torn_clear,
        })

    with TemporaryDirectory() as folder:
        path = Path(folder)
        with closing(TaskStore(path / r24.DB)) as writer:
            task = create_fixture(writer, "Synthetic human handoff")
            task_id = task["task_id"]
            claim = writer.claim_task(task_id, "fixture-worker", ttl_seconds=300)
            worker = {"worker_id": "fixture-worker", "fencing_token": claim["fencing_token"]}
            writer.mark_running(task_id, expected_version=claim["version"], **worker)
            task = writer.request_awaiting_human(
                task_id, human_request={"question": "Synthetic?"}, **worker)
            code, report = run_gate(path)
            lease_live = r24.datetime.fromisoformat(task["lease_expires_at"]) > r24.datetime.fromisoformat(
                report["read_at"])
            completed = writer.complete(task_id, output={"synthetic": True}, **worker)
        counters_clear = all(int(report[key]) == 0 for key in (
            "queued", "leased", "running", "open_prepared", "open_dispatched"))
        results.append({
            "scenario": "awaiting_human and worker token still valid",
            "exit": code, "gate": report, "lease_expires_at": task["lease_expires_at"],
            "lease_live": lease_live, "counters_clear": counters_clear,
            "status_after_same_worker_complete": completed["status"],
        })

    first, second = results
    ok = (first["before_exit"] == 9 and first["after_exit"] == 9 and first["consistent_blocker"]
          and not first["torn_clear"] and first["gate"]["snapshot"]["data_version_stable"]
          and second["exit"] == 9 and second["counters_clear"] and second["lease_live"]
          and int(second["gate"]["writable_leases"]) >= 1
          and second["status_after_same_worker_complete"] == "succeeded")
    r25.emit({"phase": "konm112_repro_drain_gaps", "ok": ok, "results": results})
    return 0 if ok else 9


def hold_writes() -> int:
    """Keep a write transaction open so no other connection can start one."""
    database = r24.DATA / r24.DB
    BARRIER_MARKER.parent.mkdir(parents=True, exist_ok=True)
    if BARRIER_RELEASE.exists():
        BARRIER_RELEASE.unlink()
    connection = sqlite3.connect(str(database), timeout=30, isolation_level=None)
    try:
        connection.execute("PRAGMA busy_timeout=30000")
        connection.execute("BEGIN IMMEDIATE")
        BARRIER_MARKER.write_text(json.dumps({
            "held_at": r24.datetime.now().astimezone().isoformat(),
            "database": database.as_posix(),
        }), encoding="utf-8")
        while not BARRIER_RELEASE.exists():
            time.sleep(0.2)
        connection.execute("ROLLBACK")
    finally:
        connection.close()
        if BARRIER_MARKER.exists():
            BARRIER_MARKER.unlink()
    r25.emit({"phase": "konm112_hold_writes", "released": True})
    return 0


def wait_barrier() -> int:
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        if BARRIER_MARKER.is_file():
            r25.emit({"phase": "konm112_wait_barrier", "present": True,
                      "marker": json.loads(BARRIER_MARKER.read_text(encoding="utf-8"))})
            return 0
        time.sleep(0.2)
    r25.emit({"phase": "konm112_wait_barrier", "present": False})
    return 8


def probe_writes() -> int:
    """A second BEGIN IMMEDIATE must fail while the barrier holder is alive."""
    database = r24.DATA / r24.DB
    held = BARRIER_MARKER.is_file()
    refused, error = False, ""
    connection = sqlite3.connect(str(database), timeout=0.2, isolation_level=None)
    try:
        connection.execute("PRAGMA busy_timeout=200")
        connection.execute("BEGIN IMMEDIATE")
        connection.execute("ROLLBACK")
    except sqlite3.OperationalError as exc:
        error = str(exc)
        refused = "locked" in error.lower() or "busy" in error.lower()
    finally:
        connection.close()
    ok = held and refused
    r25.emit({"phase": "konm112_probe_writes", "held": held, "refused": refused,
              "error": error, "ok": ok})
    return 0 if ok else 9


def release_writes() -> int:
    BARRIER_MARKER.parent.mkdir(parents=True, exist_ok=True)
    BARRIER_RELEASE.write_text("release\n", encoding="utf-8")
    r25.emit({"phase": "konm112_release_writes", "ok": True})
    return 0


def resubmit() -> int:
    """Same business content, same intents, after the restart."""
    from stock_agent.dify_api import DifyGateway

    config = r25.load_drill_config()
    results = []
    with closing(r25.open_store()) as store:
        for state in r24.STATES:
            task, dedup = store.create_task(
                title=f"R24 fixture {state}", case_id=r24.CASE, execution_mode="replay",
                business_intent_id=f"{r24.CASE}-{state}",
                input_payload={"data_classification": "public",
                               "research_question": f"Synthetic budget fixture {state}"})
            results.append({"intent": f"{r24.CASE}-{state}", "task_id": task["task_id"], "verdict": dedup})
        credential = "r24-local-fixture"
        gateway = DifyGateway(
            store, {**config, "dify": {**config["dify"], "enabled": True}},
            credentials={"credentials": [{
                "credential_id": r24.ACTOR, "active": True,
                "token_sha256": hashlib.sha256(credential.encode()).hexdigest(),
                "scopes": ["create_research"], "case_scope": [r24.CASE],
            }]})
        task = gateway.create_research(
            credential, title="R24 frozen replay workflow", case_id=r24.CASE, input_version=1,
            research_question="Synthetic workflow reference",
            dify_run_id="r24-local-fixture-run",
            workflow_version=config["dify"]["approved_workflow_versions"][0], execution_mode="replay")
        results.append({"intent": task["business_intent_id"], "task_id": task["task_id"],
                        "verdict": task["outcome"], "platform_run": task["platform_run"]})
    r25.emit({"phase": "konm112_resubmit", "provider_requests": 0, "results": results})
    return 0


def read(data_value: str, cursor: int, limit: int) -> int:
    with closing(_store(_data_root(data_value))) as store:
        events = store.events_after(int(cursor), limit=int(limit))
    r25.emit({"phase": "konm112_read", "cursor": int(cursor), "limit": int(limit),
              "sequences": [e["sequence"] for e in events],
              "events": [{"sequence": e["sequence"], "event_id": e["event_id"], "sha256": event_digest(e)}
                         for e in events]})
    return 0


def _receipts(path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(str(path), isolation_level=None)
    connection.execute("""CREATE TABLE IF NOT EXISTS receipts (
        event_id TEXT PRIMARY KEY, sequence INTEGER NOT NULL, sha256 TEXT NOT NULL,
        first_label TEXT NOT NULL, delivered_at TEXT NOT NULL)""")
    return connection


def consume(data_value: str, receipt_db: str, label: str, cursor: int) -> int:
    """Read-side cursor + deliver-side idempotency. An event whose ID already has
    a receipt is counted as a duplicate and delivered nothing new."""
    data = _data_root(data_value)
    target = Path(receipt_db)
    target.parent.mkdir(parents=True, exist_ok=True)
    result = {"phase": "konm112_consume", "label": label, "data_root": data.as_posix(),
              "receipt_db": target.as_posix(), "cursor_start": int(cursor)}
    read, delivered, duplicates, sequence = 0, 0, 0, int(cursor)
    with closing(_store(data)) as store, closing(_receipts(target)) as receipts:
        # One transaction for the whole pass: a receipt and its "delivery" are
        # committed together, per event.
        receipts.execute("BEGIN IMMEDIATE")
        while True:
            page = store.events_after(sequence, limit=PAGE)
            if not page:
                break
            for event in page:
                sequence = event["sequence"]
                read += 1
                inserted = receipts.execute(
                    "INSERT OR IGNORE INTO receipts VALUES (?,?,?,?,?)",
                    (event["event_id"], sequence, event_digest(event), label, r24.datetime.now().isoformat()))
                if inserted.rowcount:
                    delivered += 1          # the delivery itself (a local stub)
                else:
                    duplicates += 1
        receipts.execute("COMMIT")
        result.update(read=read, delivered=delivered, duplicates=duplicates, cursor_end=sequence,
                      receipts_total=int(receipts.execute("SELECT COUNT(*) FROM receipts").fetchone()[0]))
    r25.emit(result)
    return 0


def receipts(receipt_db: str) -> int:
    with closing(sqlite3.connect(str(Path(receipt_db)))) as connection:
        rows = connection.execute(
            "SELECT event_id, sequence, sha256, first_label, delivered_at FROM receipts ORDER BY sequence").fetchall()
    r25.emit({"phase": "konm112_receipts", "count": len(rows), "receipts": [list(row) for row in rows]})
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("ledger").add_argument("data", nargs="?", default="")
    commands.add_parser("settle-unsent")
    commands.add_parser("inject").add_argument("mode", choices=("running", "live-lease"))
    commands.add_parser("drain-gate")
    commands.add_parser("repro-drain-gaps")
    commands.add_parser("hold-writes")
    commands.add_parser("wait-barrier")
    commands.add_parser("probe-writes")
    commands.add_parser("release-writes")
    commands.add_parser("resubmit")
    read_parser = commands.add_parser("read")
    read_parser.add_argument("data")
    read_parser.add_argument("cursor", type=int)
    read_parser.add_argument("limit", type=int)
    consume_parser = commands.add_parser("consume")
    consume_parser.add_argument("data")
    consume_parser.add_argument("receipt_db")
    consume_parser.add_argument("label")
    consume_parser.add_argument("cursor", type=int)
    commands.add_parser("receipts").add_argument("receipt_db")
    args = parser.parse_args()
    if args.command == "ledger":
        return ledger(args.data)
    if args.command == "settle-unsent":
        return settle_unsent()
    if args.command == "inject":
        return inject(args.mode)
    if args.command == "drain-gate":
        return drain_gate()
    if args.command == "repro-drain-gaps":
        return repro_drain_gaps()
    if args.command == "hold-writes":
        return hold_writes()
    if args.command == "wait-barrier":
        return wait_barrier()
    if args.command == "probe-writes":
        return probe_writes()
    if args.command == "release-writes":
        return release_writes()
    if args.command == "resubmit":
        return resubmit()
    if args.command == "read":
        return read(args.data, args.cursor, args.limit)
    if args.command == "consume":
        return consume(args.data, args.receipt_db, args.label, args.cursor)
    return receipts(args.receipt_db)


if __name__ == "__main__":
    raise SystemExit(main())
