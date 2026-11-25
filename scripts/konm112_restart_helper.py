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
from pathlib import Path

sys.dont_write_bytecode = True

import r24_restore_drill as r24  # noqa: E402

r25 = r24.r25
PAGE = 100


def event_digest(event: dict) -> str:
    canonical = json.dumps({
        "event_id": event["event_id"], "task_id": event["task_id"], "event_type": event["event_type"],
        "actor": event["actor"], "command_id": event["command_id"], "created_at": event["created_at"],
        "payload": event["payload"]}, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _data_root(value: str) -> Path:
    return Path(value) if value.startswith("/") else r24.DATA / value


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
    budget, budget_errors = r24.budget_projection(rows, config)
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
    if args.command == "resubmit":
        return resubmit()
    if args.command == "read":
        return read(args.data, args.cursor, args.limit)
    if args.command == "consume":
        return consume(args.data, args.receipt_db, args.label, args.cursor)
    return receipts(args.receipt_db)


if __name__ == "__main__":
    raise SystemExit(main())
