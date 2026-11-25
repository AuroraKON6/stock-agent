#!/usr/bin/env python3
"""Container-side fixture and read-only reconciliation for the R24 drill."""
from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import sqlite3
from contextlib import closing
from datetime import datetime
from pathlib import Path

import r25_precheck_drill as r25

from stock_agent.backup_restore import _open_payload_sqlite
from stock_agent.budget_guard import may_start_model_call, reserve_model_attempt
from stock_agent.dify_api import DifyGateway
from stock_agent.dify_deployment import DeploymentCandidateError, validate_replay_workflow
from stock_agent.runtime_layout import business_date

DATA = r25.DATA
ARCHIVE = Path("/app/drill-archive")
RESTORE = Path("/app/drill-restore")
NEGATIVES = Path("/app/drill-negatives")
DB = Path("task_runtime/tasks.sqlite3")
WORKFLOW_DIR = Path("workflow_artifacts")
FLOW_MANIFEST = "single-research-replay.manifest.json"
FLOW_DSL = "single-research-replay.yml"
CASE = "r24-synthetic-case"
ACTOR = "r24-fixture"
LIMIT = 8
RECEIPT = {"basis": "synthetic_fixture", "receipt_id": "r24-receipt-1",
           "amount": "0.012345", "currency": "CNY"}
USAGE = {"prompt_tokens": 120, "completion_tokens": 37, "total_tokens": 157}
STATES = {"prepared": "no", "succeeded": "yes",
          "reconciled": "unknown", "outcome_unknown": "unknown"}
# What each seeded attempt must look like: sent, its invocation events in
# order, and the task status. R24 keeps the prepared fixture running for its
# static restore; the R25 path (KONM-112) settles it before the snapshot.
PREPARED = ("prepared", "no", [], "running")
SETTLED_NOT_SENT = ("failed_known", "no", ["invocation_failed_known"], "failed")
TABLE_KEYS = {
    "research_tasks": ("task_id",),
    "task_outbox": ("sequence",),
    "task_input_snapshots": ("task_id", "input_version"),
    "task_platform_runs": ("run_key",),
    "task_invocation_attempts": ("attempt_id",),
    "principal_verifications": ("verification_id",),
    "workflow_dispatches": ("dispatch_id",),
    "task_input_bindings": ("task_id",),
    "idempotency_receipts": ("command_id",),
}


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def read_json(path: Path) -> dict:
    return json.loads(path.read_bytes())


def require(condition: bool, message: str) -> None:
    if not condition:
        raise RuntimeError(message)


def seed_attempt(store, config: dict, state: str) -> dict:
    task, _ = store.create_task(
        title=f"R24 fixture {state}", case_id=CASE, execution_mode="replay",
        business_intent_id=f"{CASE}-{state}",
        input_payload={"data_classification": "public",
                       "research_question": f"Synthetic budget fixture {state}"})
    task_id = task["task_id"]
    claimed = store.claim_task(task_id, ACTOR)
    worker = {"worker_id": ACTOR, "fencing_token": claimed["fencing_token"]}
    store.mark_running(task_id, expected_version=claimed["version"], **worker)
    request = {"round": 1, "provider": "synthetic-local", "request_model": "r24-fixture",
               "prompt_sha256": hashlib.sha256(state.encode()).hexdigest(),
               "started_at": datetime.now().astimezone().isoformat(timespec="seconds")}
    reservation = reserve_model_attempt(store, config, task_id, attempt=request, **worker)
    require(reservation["reserved"], f"Budget refused fixture {state}: {reservation}")
    row = store.prepare_invocation_attempt(
        task_id, round_index=request["round"], provider=request["provider"],
        request_model=request["request_model"], prompt_sha256=request["prompt_sha256"],
        budget_event_id=reservation["event_id"], deadline_seconds=30, **worker)
    attempt_id = row["attempt_id"]
    if state == "prepared":
        return row

    store.mark_invocation_dispatched(attempt_id, **worker)
    if state == "succeeded":
        row = store.complete_invocation_attempt(attempt_id, state=state, sent="yes", **worker)
        completed = store.complete(
            task_id, output={"summary": "Synthetic successful model round"},
            model_call={"called": True, "rounds": [{
                **request, "attempt_id": attempt_id, "request_id": "r24-local-stub-receipt",
                "usage": USAGE, "final": True,
                "budget_reservation": {key: reservation[key] for key in ("calls_today", "limit")},
            }]}, **worker)
        require(completed["status"] == "succeeded", "Synthetic success did not commit")
        return row

    error = "Synthetic send outcome unknown"
    row = store.complete_invocation_attempt(
        attempt_id, state="outcome_unknown", sent="unknown", error=error, **worker)
    store.mark_blocked(task_id, error=error, **worker)
    if state == "reconciled":
        row = store.reconcile_invocation_attempt(
            attempt_id, actor=ACTOR, resolution="confirmed_charged",
            reason=json.dumps(RECEIPT, sort_keys=True), requeue_if_blocked=False)
    return row


def seed_personal_evidence(config: dict) -> None:
    from stock_agent.evidence_excerpts import _document_text, create_excerpt, excerpt_selector
    from stock_agent.evidence_vault import link_evidence_to_signal
    from stock_agent.models import AnalyzedSignal, now_iso
    from stock_agent.personal_evidence import import_personal_document, personal_vault_view
    from stock_agent.personal_workspace import create_personal_research
    from stock_agent.research_worker import run_research_task
    from stock_agent.storage import SignalStore

    owner = r25.EVIDENCE_OWNER
    url = "https://www.cninfo.com.cn/x?r24-synthetic-fixture"
    signal_id = "r24-synthetic-filing"
    with closing(SignalStore(DATA / "stock_agent.sqlite3")) as signals:
        signals.upsert_many([AnalyzedSignal(
            signal_id=signal_id, source="manual", source_type="filing", title="R24 合成公告信号",
            summary="Synthetic filing fixture", url=url, published_at=now_iso(), collected_at=now_iso(),
            symbols=["600519"], payload={"data_classification": "public"})])
    incoming = DATA / "incoming" / "r24_synthetic_filing.html"
    incoming.parent.mkdir()
    incoming.write_text("<html><body><p>合成公告（非真实披露）：营业收入为 1,234.56 万元。</p></body></html>",
                        encoding="utf-8")
    document = import_personal_document(
        config, owner, str(incoming), source_url=url, title="R24 合成公告（非真实披露）",
        published_at=now_iso())["document"]
    view = personal_vault_view(config, owner)
    link_evidence_to_signal(view, document_id=document["document_id"], signal_id=signal_id,
                            actor=owner, actor_role="information_analyst", notes="R24 synthetic association")
    body, _ = _document_text(view, document)
    start = body.index("营业收入为")
    selector = excerpt_selector(create_excerpt(
        view, document["document_id"], start, body.index("。", start) + 1))
    with closing(r25.open_store()) as store:
        result = create_personal_research(
            store, actor=owner, question="核对 R24 合成公告", symbols=["600519"],
            config=config, fact_selectors=[selector])
        task_id = result["task"]["task_id"]
        store.bump_input_version(
            task_id, actor=owner, reason="Use synthetic replay response",
            payload_overrides={"replay_response": {
                "schema_version": "1.0", "task_id": task_id, "summary": "结论",
                "key_findings": [], "unknowns": [], "evidence_refs": [], "human_questions": [],
                "model_self_assessed_confidence": 0.3}})
        require(bool(store.claim_task(task_id, ACTOR)), "Unable to claim personal evidence task")
        outcome = run_research_task(store, task_id, worker_id=ACTOR, config=config)
        require(outcome["status"] == "succeeded", f"Personal evidence replay failed: {outcome}")
    evidence = r25._evidence_checks(str(DATA / DB), DATA, connect=sealed_connection)
    require(evidence.get("ok"), f"Personal evidence reference check failed: {evidence}")
    r25.emit({"phase": "seed_personal_evidence", **evidence})


def seed() -> int:
    require(not r25.TASKS_DB.exists(), "R24 requires a fresh source volume")
    require(r25.cmd_seed(0) == 0, "R25 synthetic source seed failed")
    seed_personal_evidence(r25.load_drill_config())

    original = Path("/fixtures") / FLOW_MANIFEST
    version = validate_replay_workflow(original)
    frozen = DATA / WORKFLOW_DIR / version.removeprefix("sha256:")
    frozen.mkdir(parents=True)
    for filename in (FLOW_MANIFEST, FLOW_DSL):
        shutil.copyfile(original.with_name(filename), frozen / filename)
    validate_replay_workflow(frozen / FLOW_MANIFEST, approved_version=version)

    config_path = Path("/app") / r25.CONFIG_REL
    config = read_json(config_path)
    config["task_runtime"]["budget"] = {"daily_model_call_limit": LIMIT}
    config["dify"]["approved_workflow_versions"] = [version]
    config["backup_restore"].update({
        "bundle_root": str(ARCHIVE / "bundles"),
        "event_path": str(ARCHIVE / "events.jsonl"),
        "report_dir": str(ARCHIVE / "reports"),
        "drill_root": str(RESTORE / "drills"),
    })
    config_path.write_text(json.dumps(config), encoding="utf-8")
    config = r25.load_drill_config()
    with closing(r25.open_store()) as store:
        attempts = {state: seed_attempt(store, config, state) for state in STATES}
        credential = "r24-local-fixture"
        gateway = DifyGateway(
            store, {**config, "dify": {**config["dify"], "enabled": True}},
            credentials={"credentials": [{
                "credential_id": ACTOR, "active": True,
                "token_sha256": hashlib.sha256(credential.encode()).hexdigest(),
                "scopes": ["create_research"], "case_scope": [CASE],
            }]},
        )
        task = gateway.create_research(
            credential, title="R24 frozen replay workflow", case_id=CASE, input_version=1,
            research_question="Synthetic workflow reference",
            dify_run_id="r24-local-fixture-run", workflow_version=version, execution_mode="replay")
        r25.emit({"phase": "r24_seed", "workflow": task, "attempts": attempts,
                  "budget": may_start_model_call(store, config), "provider_requests": 0})
    return 0


def sealed_connection(db_path: str | Path) -> sqlite3.Connection:
    db = Path(db_path)
    wal = db.with_name(db.name + "-wal")
    require(not wal.exists() or not wal.stat().st_size, f"Uncheckpointed database: {db}")
    return _open_payload_sqlite(db)


def snapshot(data: Path) -> dict:
    with closing(sealed_connection(data / DB)) as connection:
        connection.row_factory = sqlite3.Row
        return {
            table: [dict(row) for row in connection.execute(
                f"SELECT * FROM {table} ORDER BY {', '.join(keys)}")]
            for table, keys in TABLE_KEYS.items()
        }


def row_diffs(source: dict, restored: dict) -> list:
    differences = []
    for table, keys in TABLE_KEYS.items():
        left, right = [{tuple(row[key] for key in keys): row for row in rows[table]}
                       for rows in (source, restored)]
        for key in sorted(left.keys() | right.keys()):
            if left.get(key) != right.get(key):
                columns = (sorted(col for col in left[key] if left[key][col] != right[key][col])
                           if key in left and key in right else None)
                differences.append({"table": table, "key": dict(zip(keys, key)),
                                    "changed_columns": columns,
                                    "present_in_source": key in left,
                                    "present_in_restore": key in right})
    return differences


def budget_projection(rows: dict, config: dict, *, drained: bool = False) -> tuple[list, list]:
    profile = {
        PREPARED[0]: PREPARED,
        "succeeded": ("succeeded", "yes", ["invocation_dispatched", "invocation_succeeded"], "succeeded"),
        "reconciled": ("reconciled", "unknown",
                       ["invocation_dispatched", "invocation_outcome_unknown", "invocation_reconciled"], "blocked"),
        "outcome_unknown": ("outcome_unknown", "unknown",
                            ["invocation_dispatched", "invocation_outcome_unknown"], "blocked"),
    }
    if drained:
        del profile[PREPARED[0]]
        profile[SETTLED_NOT_SENT[0]] = SETTLED_NOT_SENT
    errors = []
    projection = []
    attempts = rows["task_invocation_attempts"]
    tasks = {row["task_id"]: row for row in rows["research_tasks"]}
    outbox = rows["task_outbox"]
    reservations = {row["event_id"]: row for row in outbox
                    if row["event_type"] == "research_attempt_started"}
    limit = config["task_runtime"]["budget"]["daily_model_call_limit"]
    dates = {event_id: business_date(datetime.fromisoformat(event["created_at"]))
             for event_id, event in reservations.items()}
    if (len(attempts) != len(profile) or {row["state"] for row in attempts} != profile.keys()
            or len(reservations) != len(profile)
            or {row["budget_event_id"] for row in attempts} != reservations.keys() or limit != LIMIT):
        errors.append(f"Expected the four attempt states {sorted(profile)}, each linked to one reservation, "
                     "and an 8-call cap")

    for row in attempts:
        task_id, attempt_id, state = (row[key] for key in ("task_id", "attempt_id", "state"))
        event_id = row["budget_event_id"]
        task = tasks[task_id]
        label = f"{task_id}/{attempt_id}/{event_id}"
        reservation = reservations.get(event_id)
        if reservation is None:
            errors.append(f"{label}: reservation missing")
            continue
        events = [event for event in outbox if event["task_id"] == task_id]
        invocation_events = [event for event in events if event["event_type"].startswith("invocation_")]
        payload = json.loads(reservation["payload"])
        if (reservation["task_id"] != task_id or task["case_id"] != CASE
                or payload.get("round") != row["round_index"] or any(
                    payload.get(key) != row[key] for key in ("provider", "request_model", "prompt_sha256"))):
            errors.append(f"{label}: reservation/task/round binding disagrees")

        _, sent, events, status = profile.get(state, (state, None, None, None))
        if (row["sent"] != sent or
                [event["event_type"] for event in invocation_events] != events or
                any(json.loads(event["payload"]).get("attempt_id") != attempt_id
                    or event["sequence"] <= reservation["sequence"] for event in invocation_events)):
            errors.append(f"{label}: invocation event sequence/state disagrees")
        if task["status"] != status or any(
                event["event_type"] in {"task_requeued", "task_retry_scheduled"} for event in events):
            errors.append(f"{label}: task status or retry event disagrees")

        receipt = json.loads(row["reconciliation_reason"]) if row["reconciliation_reason"] else None
        if state == "reconciled":
            final_event = json.loads(invocation_events[-1]["payload"]) if invocation_events else {}
            if (receipt != RECEIPT or row["reconciliation"] != "confirmed_charged"
                    or not row["reconciled_at"] or row["reconciled_by"] != ACTOR
                    or final_event.get("reason") != row["reconciliation_reason"]
                    or final_event.get("resolution") != row["reconciliation"]):
                errors.append(f"{label}: synthetic receipt/state differs from reconciliation event")
        elif receipt or any(row[key] for key in ("reconciled_at", "reconciled_by", "reconciliation")):
            errors.append(f"{label}: unexpected reconciliation")

        rounds = json.loads(task["model_call"]).get("rounds", [])
        usage = rounds[0].get("usage") if rounds else None
        calls_at_reservation = sum(
            dates[other_id] == dates[event_id] and other["sequence"] <= reservation["sequence"]
            for other_id, other in reservations.items())
        if state == "succeeded" and (usage != USAGE or rounds[0].get("attempt_id") != attempt_id
                or rounds[0].get("budget_reservation") != {"calls_today": calls_at_reservation, "limit": limit}):
            errors.append(f"{label}: persisted successful consumption or reservation count disagrees")
        projection.append({
            "task_id": task_id, "attempt_id": attempt_id, "budget_event_id": event_id,
            "business_date_asia_shanghai": str(dates[event_id]), "calls_at_reservation": calls_at_reservation,
            "reservation_sequence": reservation["sequence"], "reserved_calls": 1, "daily_call_limit": limit,
            "state": state, "sent": row["sent"], "task_status": task["status"],
            "usage_tokens": usage, "reconciliation": row["reconciliation"] or None,
            "amount": receipt.get("amount") if receipt else None,
            "currency": receipt.get("currency") if receipt else None,
            "invocation_sequences": [event["sequence"] for event in invocation_events],
        })
    return projection, errors


def workflow_projection(data: Path, rows: dict, manifest: dict, config: dict) -> tuple[dict, list]:
    errors = []
    runs = rows["task_platform_runs"]
    if len(runs) != 1:
        return {}, ["Expected one workflow platform-run row"]
    run = runs[0]
    task_id, version = run["task_id"], run["workflow_version"]
    label = f"{run['run_key']}/{task_id}/{version}"
    task = next((row for row in rows["research_tasks"] if row["task_id"] == task_id), {})
    inputs = [row for row in rows["task_input_snapshots"]
              if row["task_id"] == task_id and row["input_version"] == task.get("input_version")]
    if (len(inputs) != 1 or task.get("case_id") != CASE or run["platform"] != "dify"
            or run["business_intent_id"] != task.get("business_intent_id")):
        errors.append(f"{label}: task/run/input identity disagrees")
    else:
        payload = json.loads(inputs[0]["payload"])
        origin = payload.get("origin", {})
        snapshot_digest = hashlib.sha256(inputs[0]["payload"].encode()).hexdigest()
        if (snapshot_digest != inputs[0]["payload_sha256"] or
                payload != json.loads(task["input_payload"]) or origin != {"kind": "dify", "dify_run_id": run["run_id"], "workflow_version": version}):
            errors.append(f"{label}: input snapshot origin disagrees")

    approved = config["dify"]["approved_workflow_versions"]
    digest = version.removeprefix("sha256:")
    if version not in approved or version != f"sha256:{digest}" or len(digest) != 64 or any(
            char not in "0123456789abcdef" for char in digest):
        return {}, errors + [f"{label}: unapproved or invalid workflow version"]
    relative = WORKFLOW_DIR / digest
    entries = {entry["path"]: entry for entry in manifest["files"]}
    files = {}
    for filename, kind in ((FLOW_MANIFEST, "workflow_manifest"), (FLOW_DSL, "workflow_dsl")):
        path = data / relative / filename
        key = (Path("data") / relative / filename).as_posix()
        entry = entries.get(key, {})
        if not path.is_file():
            errors.append(f"{label}: missing workflow artifact {key}")
            continue
        digest_found = sha256(path)
        files[key] = {"sha256": digest_found, "bytes": path.stat().st_size}
        if (entry.get("sha256") != digest_found or entry.get("kind") != kind
                or entry.get("bytes") != path.stat().st_size):
            errors.append(f"{label}: manifest hash/size/kind mismatch for {key}")
    if len(files) == 2:
        try:
            validate_replay_workflow(data / relative / FLOW_MANIFEST, approved_version=version)
        except DeploymentCandidateError as exc:
            errors.append(f"{label}: {exc}")
    return {"run": run, "input_snapshot": inputs[0] if inputs else None, "files": files}, errors


def compare(bundle: Path, restored: Path, *, raw: bool = False, drained: bool = False) -> int:
    manifest_path = bundle / "manifest.json"
    manifest = read_json(manifest_path)
    digest = sha256(manifest_path)
    require((bundle / "manifest.sha256").read_text().split()[0] == digest,
            "Backup manifest SHA-256 disagrees with its checksum")
    source_config = read_json(Path("/app") / r25.CONFIG_REL)
    restored_config = read_json(restored / "config/watchlist.restore.json")
    source, recovered = snapshot(DATA), snapshot(restored / "data")
    differences = row_diffs(source, recovered)
    budget_source, source_errors = budget_projection(source, source_config, drained=drained)
    budget_restore, restore_errors = budget_projection(recovered, restored_config, drained=drained)
    workflow_source, workflow_source_errors = workflow_projection(DATA, source, manifest, source_config)
    workflow_restore, workflow_restore_errors = workflow_projection(
        restored / "data", recovered, manifest, restored_config)
    errors = source_errors + restore_errors + workflow_source_errors + workflow_restore_errors
    r25_exit = r25.cmd_compare(str(DATA / DB), str(restored), connect=sealed_connection)
    ok = (r25_exit == 0 and not differences and not errors and budget_source == budget_restore
          and workflow_source == workflow_restore)
    report = {
        "phase": "r24_compare", "ok": ok, "restored_root": str(restored),
        "manifest_id": manifest["manifest_id"], "manifest_sha256": digest,
        "table_counts": {table: {"source": len(source[table]), "restore": len(recovered[table])}
                         for table in TABLE_KEYS},
        "row_differences": differences, "errors": errors, "r25_compare_exit": r25_exit,
        "drained": bool(drained),
        "budget_source": budget_source, "budget_restore": budget_restore,
        "workflow_source": workflow_source, "workflow_restore": workflow_restore,
    }
    if raw:
        report["rows_source"] = source
        report["rows_restore"] = recovered
    r25.emit(report)
    return 0 if ok else 13


def corrupt(restored: Path, mode: str) -> int:
    destination = NEGATIVES / mode
    if mode in {"outbox-payload", "missing-object"}:
        return r25.cmd_corrupt_copy(str(restored), str(destination), mode)

    shutil.copytree(restored, destination)
    db = destination / "data" / DB
    if mode == "missing-workflow":
        run, = snapshot(destination / "data")["task_platform_runs"]
        digest = run["workflow_version"].removeprefix("sha256:")
        path = destination / "data" / WORKFLOW_DIR / digest / FLOW_DSL
        path.unlink()
        mutation = {"path": str(path)}
    else:
        with closing(sqlite3.connect(db)) as connection, connection:
            attempt_id, reason = connection.execute(
                "SELECT attempt_id, reconciliation_reason FROM task_invocation_attempts WHERE state = 'reconciled'"
            ).fetchone()
            if mode == "amount":
                value = json.dumps({**json.loads(reason), "amount": "0.012346"}, sort_keys=True)
                connection.execute("UPDATE task_invocation_attempts SET reconciliation_reason = ? WHERE attempt_id = ?",
                                   (value, attempt_id))
            else:
                connection.execute("UPDATE task_invocation_attempts SET state = 'outcome_unknown' WHERE attempt_id = ?",
                                   (attempt_id,))
        mutation = {"attempt_id": attempt_id, "table": "task_invocation_attempts"}
    r25.emit({"phase": "r24_corrupt", "mode": mode, "path": str(destination), "mutation": mutation})
    return 0


def inventory() -> int:
    r25.emit({path.relative_to(DATA).as_posix(): {"bytes": path.stat().st_size, "sha256": sha256(path)}
              for path in sorted(DATA.rglob("*")) if path.is_file()})
    return 0


def collect() -> int:
    backup = read_json(ARCHIVE / "reports/backup_latest.json")
    drill = read_json(ARCHIVE / "reports/drill_latest.json")
    bundle = Path("/app") / backup["bundle_path"]
    r25.emit({"backup": backup, "drill": drill, "manifest": read_json(bundle / "manifest.json"),
              "manifest_sha256_file": (bundle / "manifest.sha256").read_text(),
              "bundle": str(bundle), "restored": str(Path("/app") / drill["restored_root"])})
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("seed", "inventory", "collect"):
        commands.add_parser(name)
    check = commands.add_parser("compare")
    check.add_argument("bundle", type=Path)
    check.add_argument("restored", type=Path)
    check.add_argument("--raw", action="store_true")
    check.add_argument("--drained", action="store_true",
                       help="R25 path: the reserved-but-unsent fixture was settled before the snapshot")
    copy = commands.add_parser("corrupt")
    copy.add_argument("restored", type=Path)
    copy.add_argument("mode", choices=("amount", "state", "missing-workflow",
                                       "outbox-payload", "missing-object"))
    args = parser.parse_args()
    if args.command == "compare":
        return compare(args.bundle, args.restored, raw=args.raw, drained=args.drained)
    if args.command == "corrupt":
        return corrupt(args.restored, args.mode)
    return {"seed": seed, "inventory": inventory, "collect": collect}[args.command]()


if __name__ == "__main__":
    raise SystemExit(main())
