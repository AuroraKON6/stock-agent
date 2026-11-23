#!/usr/bin/env python
"""R25 precheck drill driver — runs INSIDE the candidate image container.

KONM-27 (R25 前置验收): produces the container-layer (C/N) run evidence for
the A41–A45 targets defined in docs/implementation/REFACTOR_R09_20261022.md,
plus the R23 run-record check and the R24 quiescent-snapshot/restore
comparison, against a synthetic replay store. Zero real providers, zero
external network (the drill compose network is `internal: true`).

Every subcommand prints one JSON object to stdout and exits 0 on success /
non-zero on a failed assertion, so the host-side runner can record real
container commands, exit codes and before/after states verbatim.

Usage (inside the container, state rooted at /app/data on a temp volume):
    python /drills/r25_precheck_drill.py <subcommand> [args]

Subcommands:
    seed [count]        write drill config, create the three SQLite sources,
                        seed `count` synthetic replay tasks (unique questions)
    status              queue depth + per-status counts + event totals
    heartbeat           A42: claim one task, wrap a synthetic 12s "call" in
                        lease_heartbeat, sample the lease mid-call from a
                        separate connection, then persist the terminal state
    crash               A43: claim one task, persist a claim marker, then
                        SIGKILL itself (no cleanup path can run)
    inspect_crash       A43: task still leased, lease row intact, no new
                        events after the crash, expired-lease visibility (R23)
    recover             A43: store.recover_abandoned requeues the expired task
    fencing_reject      A43: the dead worker's late completion with the stale
                        fencing token is rejected
    dbbusy_holder secs  hold BEGIN EXCLUSIVE on the task DB, marker when held
    wait_marker path    poll until a marker file exists (host-side sequencing)
    probe_server        A45: serve /livez + /readyz on 0.0.0.0:8770 (disabled
                        gateway, injected empty credentials)
    probe_client url    A45: probes return 200, task/outbox counts unchanged
    observability       R23: build_observability_status over the drilled store
    integrity           PRAGMA integrity_check + status counts on the task DB
    diskfull            A44: isolated tmpfs store — seed, fill the volume,
                        run worker-service as a child, report the failure mode
    compare live restored   R24: seven-table + task-row + outbox comparison
"""
from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

def _project_root() -> Path:
    # Inside the drill container the script is bind-mounted at /drills while
    # the application lives at /app; when run from a checkout the parent of
    # this file is the project root.
    for candidate in (Path("/app"), Path(__file__).resolve().parents[1]):
        if (candidate / "stock_agent").is_dir():
            return candidate
    return Path.cwd()


PROJECT_ROOT = _project_root()
sys.path.insert(0, str(PROJECT_ROOT))

DATA = Path("/app/data")
CONFIG_REL = "data/config/watchlist.drill.json"
TASKS_DB = DATA / "task_runtime/tasks.sqlite3"
DRILL_CONFIG = {
    "storage": {"sqlite_path": "data/stock_agent.sqlite3"},
    "official_evidence_vault": {
        "enabled": True,
        "events_path": "data/vault/events.jsonl",
        "index_path": "data/vault/index.json",
        "objects_dir": "data/vault/objects",
        "quarantine_dir": "data/vault/quarantine",
        "report_dir": "data/vault/reports",
        "allow_network_fetch": False,
        "automatic_approval": False,
    },
    "research_operations": {
        "enforce_roster": False,
        "roster_path": "data/vault/roster.json",
        "restrictions_path": "data/vault/restrictions.json",
    },
    "task_runtime": {"db_path": "data/task_runtime/tasks.sqlite3"},
    "kimi_sources": {"query_store_path": "data/kimi/query_intake.sqlite3"},
    "backup_restore": {
        "enabled": True,
        "bundle_root": "data/backup_restore/bundles",
        "drill_root": "drill-restore/drills",
        "event_path": "data/backup_restore/events.jsonl",
        "report_dir": "data/backup_restore/reports",
    },
    "dify": {"enabled": False},
}


def emit(payload: dict) -> None:
    print(json.dumps(payload, ensure_ascii=False, sort_keys=True), flush=True)


def load_drill_config() -> dict:
    from stock_agent.config import load_config

    return load_config(CONFIG_REL)


def open_store(*, read_only: bool = False):
    from stock_agent.task_runtime import TaskStore

    return TaskStore(TASKS_DB, read_only=read_only)


def status_counts(store) -> dict:
    counts: dict[str, int] = {}
    for task in store.list_tasks():
        status = str(task.get("status") or "?")
        counts[status] = counts.get(status, 0) + 1
    return counts


# --------------------------------------------------------------------------- #
# seed / status
# --------------------------------------------------------------------------- #
def cmd_seed(count: int) -> int:
    from stock_agent.task_supply import build_task_spec
    from stock_agent.task_runtime import TaskStore

    DATA.joinpath("config").mkdir(parents=True, exist_ok=True)
    DATA.joinpath("task_runtime").mkdir(parents=True, exist_ok=True)
    DATA.joinpath("kimi").mkdir(parents=True, exist_ok=True)
    vault = DATA / "vault"
    vault.mkdir(parents=True, exist_ok=True)
    (vault / "restrictions.json").write_text(
        json.dumps({"version": "empty-drill-restrictions", "restricted": []}),
        encoding="utf-8")
    (vault / "roster.json").write_text(
        json.dumps({"version": "empty-drill-roster", "members": []}),
        encoding="utf-8")
    config_path = DATA / "config" / "watchlist.drill.json"
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(
        json.dumps(DRILL_CONFIG, ensure_ascii=False, indent=2), encoding="utf-8")
    # The three default R24 SQLite sources must exist or backup-create fails.
    for relative in ("stock_agent.sqlite3", "task_runtime/tasks.sqlite3",
                     "kimi/query_intake.sqlite3"):
        target = DATA / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        if not target.exists():
            sqlite3.connect(str(target)).close()

    config = load_drill_config()
    store = TaskStore(TASKS_DB)
    seeded = []
    try:
        for index in range(1, count + 1):
            spec = build_task_spec(
                research_question="R25 前置检查合成问题 {0}-{1}".format(
                    index, os.urandom(4).hex()),
                symbols=["600519"], execution_mode="replay", config=config)
            spec["bound_signal_ids"] = []
            spec["public_context"]["evidence_summaries"] = {}
            spec["replay_response"] = {
                "schema_version": "1.0", "task_id": "placeholder",
                # Generic summary only: the evidence-quality gate blocks replay
                # answers whose findings carry uncited specifics/numbers.
                "summary": "结论",
                "key_findings": [], "unknowns": [], "evidence_refs": [],
                "human_questions": [], "model_self_assessed_confidence": 0.3}
            task, _dedup = store.create_task(
                title="R25-drill-{0}".format(index),
                input_payload=spec, execution_mode="replay")
            seeded.append(str(task.get("task_id") or ""))
    finally:
        store.close()
    probe = open_store(read_only=True)
    try:
        statuses = status_counts(probe)
    finally:
        probe.close()
    emit({"phase": "seed", "seeded": len(seeded), "task_ids": seeded,
          "statuses": statuses,
          "db_bytes": TASKS_DB.stat().st_size})
    return 0


def cmd_status() -> int:
    store = open_store(read_only=True)
    try:
        tasks = store.list_tasks()
        queued = [t for t in tasks if t.get("status") == "queued"]
        emit({"phase": "status", "total": len(tasks),
              "queue_depth": len(queued),
              "statuses": status_counts(store),
              "events_total": len(store.events(limit=1000000))})
    finally:
        store.close()
    return 0


# --------------------------------------------------------------------------- #
# A42: in-container heartbeat around a synthetic long call
# --------------------------------------------------------------------------- #
def cmd_heartbeat() -> int:
    import threading

    from stock_agent.invocation_runtime import lease_heartbeat
    from stock_agent.research_worker import run_research_task

    config = load_drill_config()
    store = open_store()
    ttl = 15
    samples: dict[str, str] = {}
    try:
        task = store.claim_next("w-a42-heartbeat", ttl_seconds=ttl)
        if task is None:
            emit({"phase": "heartbeat", "ok": False, "error": "queue empty"})
            return 3
        task_id = str(task["task_id"])
        samples["lease_expires_at_claim"] = str(task.get("lease_expires_at") or "")

        def sampler() -> None:
            time.sleep(6)
            probe = open_store(read_only=True)
            try:
                fresh = probe.get_task(task_id)
                samples["lease_expires_at_midcall"] = str(
                    fresh.get("lease_expires_at") or "")
                samples["status_midcall"] = str(fresh.get("status") or "")
            finally:
                probe.close()

        watcher = threading.Thread(target=sampler)
        watcher.start()
        with lease_heartbeat(store.db_path, task_id,
                             worker_id="w-a42-heartbeat",
                             fencing_token=int(task["fencing_token"]),
                             ttl_seconds=ttl) as runner:
            time.sleep(12)  # the synthetic long "call": zero network, zero model
            beats = runner.beats
            healthy = runner.healthy
        watcher.join()
        # Persist the terminal state through the normal worker entry.
        outcome = run_research_task(store, task_id,
                                    worker_id="w-a42-heartbeat",
                                    config=config, ttl_seconds=ttl)
        final = store.get_task(task_id)
        ok = (beats >= 2 and healthy
              and samples["lease_expires_at_midcall"] > samples["lease_expires_at_claim"]
              and str(outcome.get("status")) == "succeeded"
              and str(final.get("status")) == "succeeded")
        emit({"phase": "heartbeat", "ok": ok, "task_id": task_id,
              "beats": beats, "healthy": healthy,
              "lease_extended_midcall": samples["lease_expires_at_midcall"]
              > samples["lease_expires_at_claim"],
              "samples": samples,
              "outcome_status": str(outcome.get("status")),
              "final_status": str(final.get("status"))})
        return 0 if ok else 4
    finally:
        store.close()


# --------------------------------------------------------------------------- #
# A43: crash holding a lease, inspect, recover, reject late write
# --------------------------------------------------------------------------- #
def cmd_crash() -> int:
    marker = DATA / "r25_crash.marker.json"
    store = open_store()
    try:
        task = store.claim_next("w-a43-crash", ttl_seconds=8)
        if task is None:
            emit({"phase": "crash", "ok": False, "error": "queue empty"})
            return 3
        events = store.events(task_id=str(task["task_id"]), limit=1000000)
        marker.write_text(json.dumps({
            "task_id": str(task["task_id"]),
            "fencing_token": int(task["fencing_token"]),
            "lease_expires_at": str(task.get("lease_expires_at") or ""),
            "events_at_claim": len(events),
        }, ensure_ascii=False), encoding="utf-8")
        print(json.dumps({"phase": "crash", "claimed": str(task["task_id"]),
                          "lease_expires_at": str(task.get("lease_expires_at") or "")},
                         ensure_ascii=False), flush=True)
    finally:
        store.close()
    # The container runs this process as PID 1: a same-namespace SIGKILL to
    # init is dropped by the kernel, so the REAL forced kill must come from
    # the host (`docker kill`). Sleep here holding the lease until it lands.
    time.sleep(120)
    return 0


def cmd_inspect_crash() -> int:
    from stock_agent.observability import build_observability_status

    marker = json.loads((DATA / "r25_crash.marker.json").read_text(encoding="utf-8"))
    task_id = marker["task_id"]
    store = open_store(read_only=True)
    try:
        task = store.get_task(task_id)
        events_after = store.events(task_id=task_id, limit=1000000)
        status = build_observability_status(store)
    finally:
        store.close()
    not_auto_recovered = str(task.get("status")) in ("leased", "running")
    no_new_events = len(events_after) == marker["events_at_claim"]
    expired = str(task.get("lease_expires_at") or "")
    technical = status.get("technical") or {}
    ok = not_auto_recovered and no_new_events
    emit({"phase": "inspect_crash", "ok": ok, "task_id": task_id,
          "status": str(task.get("status")),
          "worker_id": str(task.get("worker_id")),
          "lease_expires_at": expired,
          "not_auto_recovered": not_auto_recovered,
          "events_at_claim": marker["events_at_claim"],
          "events_now": len(events_after),
          "no_new_events_after_crash": no_new_events,
          "r23_stale_leases": technical.get("stale_leases"),
          "r23_inflight_leases": technical.get("inflight_leases")})
    return 0 if ok else 5


def cmd_recover() -> int:
    marker = json.loads((DATA / "r25_crash.marker.json").read_text(encoding="utf-8"))
    store = open_store()
    try:
        recovered = store.recover_abandoned(ttl_grace_seconds=0)
        task = store.get_task(marker["task_id"])
        status = str(task.get("status"))
        # Valid recovery outcomes: requeued (attempts left) or expired
        # (attempts exhausted). Either way the dead worker's lease is gone.
        ok = (status in ("queued", "expired")
              and not str(task.get("worker_id") or "")
              and not str(task.get("lease_expires_at") or ""))
        emit({"phase": "recover", "ok": ok, "recovered": recovered,
              "task_id": marker["task_id"], "status": status,
              "worker_id_cleared": not str(task.get("worker_id") or ""),
              "fencing_token": int(task.get("fencing_token") or 0),
              "note": "fencing_token bumps on the NEXT claim, not on requeue;"
                      " a twice-crashed task legitimately expires"})
        return 0 if ok else 6
    finally:
        store.close()


def cmd_fencing_reject() -> int:
    from stock_agent.task_runtime.store import TaskRuntimeError

    marker = json.loads((DATA / "r25_crash.marker.json").read_text(encoding="utf-8"))
    store = open_store()
    try:
        rejected = False
        error = ""
        kind = ""
        before = store.get_task(marker["task_id"])
        try:
            store.complete(marker["task_id"], worker_id="w-a43-crash",
                           fencing_token=marker["fencing_token"],
                           output={"schema_version": "1.0", "summary": "迟到结果"},
                           model_call={"mode": "replay", "called": False})
        except TaskRuntimeError as exc:
            rejected = True
            error = str(exc)
            kind = type(exc).__name__
        after = store.get_task(marker["task_id"])
        unchanged = (str(after.get("status")) == str(before.get("status"))
                     and int(after.get("version") or 0) == int(before.get("version") or 0))
        ok = rejected and unchanged
        emit({"phase": "fencing_reject", "ok": ok, "task_id": marker["task_id"],
              "rejected": rejected, "error_kind": kind, "error": error[:200],
              "status_unchanged": unchanged,
              "status": str(after.get("status"))})
        return 0 if ok else 7
    finally:
        store.close()


# --------------------------------------------------------------------------- #
# A44: storage faults (DB busy here; disk full via the `diskfull` subcommand)
# --------------------------------------------------------------------------- #
def cmd_dbbusy_holder(seconds: float) -> int:
    connection = sqlite3.connect(str(TASKS_DB), timeout=5, isolation_level=None)
    try:
        connection.execute("BEGIN EXCLUSIVE")
        (DATA / "r25_dbbusy.marker").write_text("held", encoding="utf-8")
        print(json.dumps({"phase": "dbbusy_holder", "held": True,
                          "seconds": seconds}), flush=True)
        time.sleep(seconds)
        connection.execute("ROLLBACK")
    finally:
        connection.close()
    return 0


def cmd_wait_marker(path: str, timeout: float = 30.0) -> int:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if Path(path).exists():
            emit({"phase": "wait_marker", "path": path, "present": True})
            return 0
        time.sleep(0.25)
    emit({"phase": "wait_marker", "path": path, "present": False})
    return 8


def cmd_integrity() -> int:
    connection = sqlite3.connect(str(TASKS_DB))
    try:
        integrity = connection.execute("PRAGMA integrity_check").fetchone()[0]
    finally:
        connection.close()
    store = open_store(read_only=True)
    try:
        statuses = status_counts(store)
    finally:
        store.close()
    ok = integrity == "ok"
    emit({"phase": "integrity", "ok": ok, "integrity_check": integrity,
          "statuses": statuses})
    return 0 if ok else 9


def cmd_diskfull() -> int:
    # Isolated tmpfs-backed store: this subcommand manages its own /app/data.
    cmd_seed(3)
    free_before = os.statvfs(DATA).f_bavail * os.statvfs(DATA).f_frsize
    fill = DATA / "fill.bin"
    leave_free = 48 * 1024
    with open(fill, "wb") as handle:
        chunk = b"x" * 65536
        written = 0
        try:
            while True:
                free = os.statvfs(DATA).f_bavail * os.statvfs(DATA).f_frsize
                if free <= leave_free:
                    break
                handle.write(chunk)
                written += len(chunk)
        except OSError:
            pass  # ENOSPC reached
    free_filled = os.statvfs(DATA).f_bavail * os.statvfs(DATA).f_frsize
    worker = subprocess.run(
        [sys.executable, "-m", "stock_agent", "worker-service",
         "--config", CONFIG_REL, "--worker-id", "w-a44-diskfull",
         "--ttl-seconds", "30", "--idle-sleep-seconds", "0.2", "--max-tasks", "1"],
        capture_output=True, text=True, timeout=120)
    connection = sqlite3.connect(str(TASKS_DB))
    try:
        integrity = connection.execute("PRAGMA integrity_check").fetchone()[0]
    finally:
        connection.close()
    store = open_store(read_only=True)
    try:
        statuses = status_counts(store)
    finally:
        store.close()
    half_states = statuses.get("succeeded", 0)  # disk full must not "succeed" a task
    ok = worker.returncode != 0 and integrity == "ok" and half_states == 0
    emit({"phase": "diskfull", "ok": ok,
          "free_bytes_before": free_before, "free_bytes_filled": free_filled,
          "fill_bytes": written,
          "worker_returncode": worker.returncode,
          "worker_stdout_tail": (worker.stdout or "")[-400:],
          "worker_stderr_tail": (worker.stderr or "")[-600:],
          "integrity_check": integrity, "statuses": statuses})
    return 0 if ok else 10


# --------------------------------------------------------------------------- #
# A45: zero-side-effect probes
# --------------------------------------------------------------------------- #
def cmd_probe_server() -> int:
    from stock_agent.dify_api import DifyGateway
    from stock_agent.dify_api_server import create_dify_api_server

    store = open_store()
    gateway = DifyGateway(
        store, {"dify": {"enabled": False}},
        credentials={"schema_version": "1.0", "credentials": []})
    server = create_dify_api_server(gateway, host="0.0.0.0", port=8770)
    print(json.dumps({"phase": "probe_server", "listening": "0.0.0.0:8770"}),
          flush=True)
    server.serve_forever()
    return 0


def cmd_probe_client(base_url: str) -> int:
    import urllib.error

    def count() -> dict:
        store = open_store(read_only=True)
        try:
            return {"tasks": len(store.list_tasks()),
                    "events": len(store.events(limit=1000000))}
        finally:
            store.close()

    before = count()
    results = []
    ok = True
    for path in ("/livez", "/readyz", "/livez", "/readyz", "/livez", "/readyz"):
        try:
            with urllib.request.urlopen(base_url.rstrip("/") + path, timeout=10) as response:
                body = json.loads(response.read().decode("utf-8"))
                results.append({"path": path, "status": response.status,
                                "data": body.get("data") or {}})
                if response.status != 200:
                    ok = False
        except urllib.error.HTTPError as exc:
            ok = False
            results.append({"path": path, "status": exc.code})
    after = count()
    unchanged = before == after
    readyz = [r for r in results if r["path"] == "/readyz"]
    store_read_only_open = all(
        (r.get("data") or {}).get("store_read_only_open") for r in readyz)
    ok = ok and unchanged and store_read_only_open
    emit({"phase": "probe_client", "ok": ok, "base_url": base_url,
          "probes": results, "counts_before": before, "counts_after": after,
          "counts_unchanged": unchanged,
          "store_read_only_open": store_read_only_open})
    return 0 if ok else 11


# --------------------------------------------------------------------------- #
# R23: run-record check over the drilled store
# --------------------------------------------------------------------------- #
def cmd_observability() -> int:
    from stock_agent.observability import build_observability_status

    store = open_store(read_only=True)
    try:
        status = build_observability_status(store)
        store_counts = status_counts(store)
    finally:
        store.close()
    technical = status.get("technical") or {}
    matches = (technical.get("queue_depth") == store_counts.get("queued", 0))
    ok = matches
    emit({"phase": "observability", "ok": ok,
          "technical": technical,
          "business": status.get("business") or {},
          "store_statuses": store_counts,
          "queue_depth_matches_store": matches})
    return 0 if ok else 12


# --------------------------------------------------------------------------- #
# R24: live-vs-restored comparison
# --------------------------------------------------------------------------- #
TABLES = (
    ("research_tasks", "tasks"),
    ("task_outbox", "outbox_events"),
    ("task_input_snapshots", "input_snapshots"),
    ("task_platform_runs", "platform_runs"),
    ("task_invocation_attempts", "invocation_attempts"),
    ("principal_verifications", "principal_verifications"),
    ("workflow_dispatches", "workflow_dispatches"),
)


def cmd_compare(live_db: str, restored_db: str) -> int:
    def snapshot(db_path: str) -> dict:
        connection = sqlite3.connect(
            "file:{0}?mode=ro".format(Path(db_path).resolve()), uri=True)
        try:
            counts = {}
            for table, _ in TABLES:
                try:
                    counts[table] = connection.execute(
                        "SELECT COUNT(*) FROM {0}".format(table)).fetchone()[0]
                except sqlite3.OperationalError:
                    counts[table] = None  # table absent on both sides = old schema
            tasks = connection.execute(
                "SELECT task_id, status, version, fencing_token, worker_id,"
                " attempt_count FROM research_tasks ORDER BY task_id").fetchall()
            outbox = connection.execute(
                "SELECT sequence, task_id, event_type FROM task_outbox"
                " ORDER BY sequence").fetchall()
            return {"counts": counts, "tasks": tasks, "outbox": outbox}
        finally:
            connection.close()

    live = snapshot(live_db)
    restored = snapshot(restored_db)
    counts_equal = live["counts"] == restored["counts"]
    tasks_equal = live["tasks"] == restored["tasks"]
    outbox_equal = live["outbox"] == restored["outbox"]
    ok = counts_equal and tasks_equal and outbox_equal
    emit({"phase": "compare", "ok": ok,
          "live_db": live_db, "restored_db": restored_db,
          "counts_live": live["counts"], "counts_restored": restored["counts"],
          "counts_equal": counts_equal,
          "task_rows": len(live["tasks"]),
          "tasks_equal_row_by_row": tasks_equal,
          "outbox_rows": len(live["outbox"]),
          "outbox_equal_row_by_row": outbox_equal})
    return 0 if ok else 13


COMMANDS = {
    "seed": lambda args: cmd_seed(int(args[0]) if args else 40),
    "status": lambda args: cmd_status(),
    "heartbeat": lambda args: cmd_heartbeat(),
    "crash": lambda args: cmd_crash(),
    "inspect_crash": lambda args: cmd_inspect_crash(),
    "recover": lambda args: cmd_recover(),
    "fencing_reject": lambda args: cmd_fencing_reject(),
    "dbbusy_holder": lambda args: cmd_dbbusy_holder(float(args[0])),
    "wait_marker": lambda args: cmd_wait_marker(args[0]),
    "probe_server": lambda args: cmd_probe_server(),
    "probe_client": lambda args: cmd_probe_client(args[0]),
    "observability": lambda args: cmd_observability(),
    "integrity": lambda args: cmd_integrity(),
    "diskfull": lambda args: cmd_diskfull(),
    "compare": lambda args: cmd_compare(args[0], args[1]),
}


def main() -> int:
    if len(sys.argv) < 2 or sys.argv[1] not in COMMANDS:
        print(__doc__, file=sys.stderr)
        return 2
    os.chdir(PROJECT_ROOT)  # relative data/... paths resolve against /app
    return COMMANDS[sys.argv[1]](sys.argv[2:])


if __name__ == "__main__":
    raise SystemExit(main())
