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
    seed_evidence       import one synthetic personal-evidence announcement,
                        create a task whose approved_facts reference a located
                        excerpt of it, and run that task through the real
                        worker entry with the personal vault view
    status              queue depth + per-status counts + event totals
    heartbeat           A42: claim one task, wrap a synthetic 12s "call" in
                        lease_heartbeat, sample the lease mid-call from a
                        separate connection, then persist the terminal state
    crash               A43: claim one task, persist a claim marker, then
                        sleep holding the lease until the HOST sends SIGKILL
                        (a same-namespace SIGKILL to PID 1 is dropped)
    inspect_crash       A43: task still leased, lease row intact, no new
                        events after the crash, expired-lease visibility (R23)
    recover             A43: store.recover_abandoned requeues the expired task
                        (or expires it when attempts are exhausted)
    fencing_reject      A43: the dead worker's late completion with the stale
                        fencing token is rejected
    dbbusy_holder secs  hold BEGIN EXCLUSIVE on the task DB, marker when held
    wait_marker path    poll until a marker file exists (host-side sequencing)
    probe_server        A45: serve /livez + /readyz on 0.0.0.0:8770 (disabled
                        gateway, injected empty credentials)
    probe_client url    A45: probes return 200, task/outbox counts unchanged
    observability       R23: build_observability_status over the drilled store
    integrity           PRAGMA integrity_check + status counts on the task DB
    diskfull            A44: REFUSES to run unless /app/data is a drill-bounded
                        tmpfs mount (zero writes on any other mount); then
                        seed, fill up to a hard byte cap, run worker-service
                        as a child, report the failure mode
    compare live_db restored_root
                        R24 reconciliation: seven-table counts, FULL-row
                        research_tasks and task_outbox comparison (every
                        column incl. payload), and the evidence chain — the
                        evidence task's selector must resolve, in the given
                        root, to a personal-evidence object whose bytes hash
                        to the referenced digest and whose delivered fact
                        content hashes to the excerpt digest
    corrupt_copy src_root dst_root outbox-payload|missing-object
                        R24 negative factory: copy a restored tree and mutate
                        exactly one thing (one outbox row's payload, or delete
                        the referenced evidence object)
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


EVIDENCE_OWNER = "lead-analyst"  # a real-human style owner; automated
# identities are refused ownership of personal evidence.


def cmd_seed_evidence() -> int:
    """One synthetic announcement -> located excerpt -> task that references
    it -> the task RUNS through the real worker entry with the personal vault
    view, so the restored-state reconciliation has a REAL reference chain."""
    import hashlib as _hashlib

    from stock_agent.evidence_excerpts import (
        _document_text, create_excerpt, excerpt_selector)
    from stock_agent.personal_evidence import (
        import_personal_document, personal_vault_view)
    from stock_agent.research_worker import run_research_task
    from stock_agent.task_supply import build_task_spec

    config = load_drill_config()
    incoming = DATA / "incoming"
    incoming.mkdir(parents=True, exist_ok=True)
    announcement = incoming / "r25_announcement.html"
    announcement.write_text(
        "<html><body><p>合成公告（非真实披露）：营业收入为 1,234.56 万元。</p></body></html>",
        encoding="utf-8")
    imported = import_personal_document(
        config, EVIDENCE_OWNER, str(announcement),
        source_url="https://www.cninfo.com.cn/x?r25-precheck-fixture",
        title="R25 前置检查合成公告（非真实披露）",
        published_at="2026-11-23T09:00:00+08:00")
    document = imported["document"]
    view = personal_vault_view(config, EVIDENCE_OWNER)
    text, _version = _document_text(view, document)
    start = text.index("营业收入为")
    end = text.index("。", start) + 1
    excerpt = create_excerpt(view, document["document_id"], start, end)
    selector = excerpt_selector(excerpt)

    spec = build_task_spec(
        research_question="R25 证据引用合成问题 {0}".format(os.urandom(4).hex()),
        symbols=["600519"], execution_mode="replay", config=config)
    spec["bound_signal_ids"] = []
    spec["public_context"]["evidence_summaries"] = {
        document["document_id"]: {
            "summary": "R25 前置检查合成公告正文", "sha256": document["sha256"]}}
    spec["approved_facts"] = [selector]
    spec["replay_response"] = {
        "schema_version": "1.0", "task_id": "placeholder",
        "summary": "结论",
        "key_findings": [], "unknowns": [], "evidence_refs": [],
        "human_questions": [], "model_self_assessed_confidence": 0.3}

    store = open_store()
    try:
        task, _dedup = store.create_task(
            title="R25-drill-evidence", input_payload=spec,
            execution_mode="replay")
        task_id = str(task["task_id"])
        # The worker entry runs one ALREADY-CLAIMED task: claim through the
        # store's real claim path first (run this before seeding other tasks
        # so the evidence task is the oldest queued one).
        claimed = store.claim_next("w-r24-evidence", ttl_seconds=60)
        if not claimed or str(claimed["task_id"]) != task_id:
            emit({"phase": "seed_evidence", "ok": False,
                  "error": "claim_next did not return the evidence task",
                  "claimed": str((claimed or {}).get("task_id") or "")})
            return 17
        outcome = run_research_task(store, task_id,
                                    worker_id="w-r24-evidence",
                                    config=view, ttl_seconds=60)
        final = store.get_task(task_id)
        model_call = final.get("model_call") or {}
        facts = ((model_call.get("outbound_context") or {})
                 .get("verified_facts") or [])
        fact = facts[0] if facts else {}
        fact_content_sha = _hashlib.sha256(
            str(fact.get("content") or "").encode("utf-8")).hexdigest()
        ok = (str(outcome.get("status")) == "succeeded"
              and str(final.get("status")) == "succeeded"
              and str(fact.get("document_sha256") or "") == document["sha256"]
              and fact_content_sha == selector["excerpt_sha256"])
        emit({"phase": "seed_evidence", "ok": ok, "task_id": task_id,
              "owner": EVIDENCE_OWNER,
              "document_id": document["document_id"],
              "document_sha256": document["sha256"],
              "excerpt_sha256": selector["excerpt_sha256"],
              "excerpt_text": excerpt["text"],
              "delivered_fact_document_sha256": str(fact.get("document_sha256") or ""),
              "delivered_fact_content_sha256": fact_content_sha,
              "outcome_status": str(outcome.get("status")),
              "final_status": str(final.get("status"))})
        return 0 if ok else 15
    finally:
        store.close()


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
        # Valid recovery outcomes: requeued (attempts left — dead worker's
        # lease fully cleared) or expired (terminal; the store intentionally
        # keeps the last worker attribution on the expired row).
        if status == "queued":
            ok = (not str(task.get("worker_id") or "")
                  and not str(task.get("lease_expires_at") or ""))
        elif status == "expired":
            ok = True
        else:
            ok = False
        emit({"phase": "recover", "ok": ok, "recovered": recovered,
              "task_id": marker["task_id"], "status": status,
              "worker_id_kept_on_expired": str(task.get("worker_id") or ""),
              "lease_cleared": not str(task.get("lease_expires_at") or ""),
              "fencing_token": int(task.get("fencing_token") or 0),
              "note": "fencing_token bumps on the NEXT claim, not on requeue;"
                      " a twice-crashed task legitimately expires with the"
                      " last worker attribution kept on the terminal row"})
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


def _data_mount_info() -> dict:
    """Longest-prefix mount info for /app/data from /proc/self/mounts."""
    target = str(DATA)
    best = {"mountpoint": "", "fstype": "", "source": ""}
    try:
        with open("/proc/self/mounts", encoding="utf-8") as handle:
            for line in handle:
                fields = line.split()
                if len(fields) < 3:
                    continue
                mountpoint = fields[1].replace("\\040", " ")
                if (target == mountpoint
                        or (target.startswith(mountpoint.rstrip("/") + "/")
                            and len(mountpoint) > len(best["mountpoint"]))):
                    best = {"mountpoint": mountpoint, "fstype": fields[2],
                            "source": fields[0]}
    except OSError:
        pass
    return best


# The fill phase never writes more than this, whatever the mount reports.
DISKFULL_MAX_FILL_BYTES = 32 * 1024 * 1024


def cmd_diskfull() -> int:
    # Boundary: the injection may ONLY touch a drill-bounded tmpfs. On any
    # other mount (named volume, bind, overlay) refuse BEFORE a single byte
    # is written — not even the seed/config may land there.
    mount = _data_mount_info()
    if mount["fstype"] != "tmpfs":
        emit({"phase": "diskfull", "ok": False, "refused": True,
              "reason": "/app/data is not a tmpfs drill mount",
              "mount": mount, "fill_bytes": 0})
        return 14
    # Isolated tmpfs-backed store: this subcommand manages its own /app/data.
    cmd_seed(3)
    free_before = os.statvfs(DATA).f_bavail * os.statvfs(DATA).f_frsize
    fill = DATA / "fill.bin"
    leave_free = 48 * 1024
    with open(fill, "wb") as handle:
        chunk = b"x" * 65536
        written = 0
        try:
            while written < DISKFULL_MAX_FILL_BYTES:
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
    emit({"phase": "diskfull", "ok": ok, "refused": False,
          "mount": mount, "max_fill_bytes": DISKFULL_MAX_FILL_BYTES,
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
# R24: full reconciliation — counts, FULL rows (incl. outbox payload), and
# the personal-evidence reference chain, live vs restored
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


def _ro_connection(db_path: str) -> sqlite3.Connection:
    return sqlite3.connect(
        "file:{0}?mode=ro".format(Path(db_path).resolve()), uri=True)


def _evidence_checks(db_path: str, data_root: Path) -> dict:
    """Verify the evidence reference chain inside ONE root.

    Requires at least one task whose approved_facts carry an excerpt
    selector; resolves that reference through the root's own bytes: the
    owner vault's capture event (identity), the content-addressed object
    (content hash) and the task's own delivered fact (association)."""
    import hashlib as _hashlib

    result: dict = {"present": False}
    connection = _ro_connection(db_path)
    try:
        rows = connection.execute(
            "SELECT task_id, input_payload, model_call FROM research_tasks"
        ).fetchall()
    finally:
        connection.close()
    for task_id, payload_text, model_call_text in rows:
        try:
            payload = json.loads(payload_text or "{}")
            selectors = payload.get("approved_facts") or []
            selector = selectors[0] if selectors and isinstance(
                selectors[0], dict) and "excerpt_sha256" in selectors[0] else None
            if selector is None:
                continue
            model_call = json.loads(model_call_text or "{}")
        except ValueError:
            continue
        document_id = str(selector.get("document_id") or "")
        document_sha = str(selector.get("document_sha256") or "")
        excerpt_sha = str(selector.get("excerpt_sha256") or "")
        result = {"present": True, "task_id": task_id,
                  "document_id": document_id, "document_sha256": document_sha,
                  "excerpt_sha256": excerpt_sha}
        # Identity: the owner vault's own capture event names the document.
        # The namespace sits at <root>/personal_evidence when the root IS the
        # live data root (/app/data), and at <root>/data/personal_evidence in
        # a restored tree (manifest paths are project-relative).
        owner_root = None
        capture_sha = ""
        events_seen = 0
        bases = [data_root / "personal_evidence",
                 data_root / "data" / "personal_evidence"]
        for base in bases:
            if not base.is_dir():
                continue
            for index_dir in sorted(base.glob("*/")):
                events_path = index_dir / "events.jsonl"
                if not events_path.is_file():
                    continue
                for line in events_path.read_text(encoding="utf-8").splitlines():
                    if not line.strip():
                        continue
                    events_seen += 1
                    try:
                        record = json.loads(line)
                    except ValueError:
                        continue
                    if str(record.get("document_id") or "") == document_id:
                        owner_root = index_dir
                        capture_sha = str(
                            (record.get("payload") or {}).get("sha256") or "")
                        break
                if owner_root is not None:
                    break
            if owner_root is not None:
                break
        result["vault_events_scanned"] = events_seen
        result["capture_event_found"] = owner_root is not None
        result["capture_sha256_matches_selector"] = capture_sha == document_sha
        # Content hash: the referenced object's BYTES must hash to the digest.
        object_path = (owner_root / "objects" / "sha256"
                       / document_sha[:2] / document_sha) if owner_root else None
        result["object_path"] = str(object_path or "")
        result["object_exists"] = bool(object_path and object_path.is_file())
        object_sha = (_hashlib.sha256(object_path.read_bytes()).hexdigest()
                      if result["object_exists"] else "")
        result["object_bytes_sha256_matches"] = object_sha == document_sha
        # Association: this task's own run record delivered the fact.
        facts = ((model_call.get("outbound_context") or {})
                 .get("verified_facts") or [])
        fact = facts[0] if facts else {}
        fact_content_sha = _hashlib.sha256(
            str(fact.get("content") or "").encode("utf-8")).hexdigest()
        result["delivered_fact_document_sha256"] = str(
            fact.get("document_sha256") or "")
        result["delivered_fact_matches_document"] = (
            str(fact.get("document_sha256") or "") == document_sha)
        result["delivered_fact_content_sha256_matches_excerpt"] = (
            fact_content_sha == excerpt_sha)
        result["ok"] = (result["capture_event_found"]
                        and result["capture_sha256_matches_selector"]
                        and result["object_exists"]
                        and result["object_bytes_sha256_matches"]
                        and result["delivered_fact_matches_document"]
                        and result["delivered_fact_content_sha256_matches_excerpt"])
        return result
    return result


def cmd_compare(live_db: str, restored_root: str) -> int:
    restored_root_path = Path(restored_root)
    restored_db = str(restored_root_path / "data/task_runtime/tasks.sqlite3")
    live_root = Path(live_db).resolve().parents[1]

    def snapshot(db_path: str) -> dict:
        connection = _ro_connection(db_path)
        try:
            counts = {}
            for table, _ in TABLES:
                try:
                    counts[table] = connection.execute(
                        "SELECT COUNT(*) FROM {0}".format(table)).fetchone()[0]
                except sqlite3.OperationalError:
                    counts[table] = None  # table absent on both sides = old schema
            tasks = connection.execute(
                "SELECT * FROM research_tasks ORDER BY task_id").fetchall()
            outbox = connection.execute(
                "SELECT * FROM task_outbox ORDER BY sequence").fetchall()
            return {"counts": counts, "tasks": tasks, "outbox": outbox}
        finally:
            connection.close()

    live = snapshot(live_db)
    restored = snapshot(restored_db)
    counts_equal = live["counts"] == restored["counts"]
    tasks_equal = live["tasks"] == restored["tasks"]
    outbox_equal = live["outbox"] == restored["outbox"]
    drift = []
    if not outbox_equal and len(live["outbox"]) == len(restored["outbox"]):
        for live_row, restored_row in zip(live["outbox"], restored["outbox"]):
            if live_row != restored_row:
                differing = [live_row[index] != restored_row[index]
                             for index in range(len(live_row))]
                drift.append({"sequence": live_row[0], "event_id": live_row[1],
                              "differing_columns": sum(differing)})
                if len(drift) >= 3:
                    break
    live_evidence = _evidence_checks(live_db, live_root)
    restored_evidence = _evidence_checks(restored_db, restored_root_path)
    evidence_ok = bool(live_evidence.get("ok")) and bool(restored_evidence.get("ok"))
    ok = counts_equal and tasks_equal and outbox_equal and evidence_ok
    emit({"phase": "compare", "ok": ok,
          "live_db": live_db, "restored_db": restored_db,
          "counts_live": live["counts"], "counts_restored": restored["counts"],
          "counts_equal": counts_equal,
          "task_rows": len(live["tasks"]),
          "tasks_equal_full_rows": tasks_equal,
          "outbox_rows": len(live["outbox"]),
          "outbox_equal_full_rows_incl_payload": outbox_equal,
          "outbox_drift_sample": drift,
          "evidence_live": live_evidence,
          "evidence_restored": restored_evidence})
    return 0 if ok else 13


def cmd_corrupt_copy(source_root: str, destination_root: str, mode: str) -> int:
    import shutil as _shutil

    source = Path(source_root)
    destination = Path(destination_root)
    if destination.exists():
        emit({"phase": "corrupt_copy", "ok": False,
              "error": "destination already exists"})
        return 16
    _shutil.copytree(source, destination)
    mutated = ""
    if mode == "outbox-payload":
        connection = sqlite3.connect(
            str(destination / "data/task_runtime/tasks.sqlite3"))
        try:
            row = connection.execute(
                "SELECT sequence FROM task_outbox ORDER BY sequence DESC"
                " LIMIT 1").fetchone()
            if row is None:
                emit({"phase": "corrupt_copy", "ok": False,
                      "error": "no outbox rows to corrupt"})
                return 16
            connection.execute(
                "UPDATE task_outbox SET payload = payload || ? WHERE sequence = ?",
                ('"r25-tamper"', row[0]))
            connection.commit()
            mutated = "outbox sequence {0} payload".format(row[0])
        finally:
            connection.close()
    elif mode == "missing-object":
        evidence = _evidence_checks(
            str(destination / "data/task_runtime/tasks.sqlite3"), destination)
        target = Path(evidence.get("object_path") or "")
        if not evidence.get("present") or not target.is_file():
            emit({"phase": "corrupt_copy", "ok": False,
                  "error": "no referenced evidence object found to delete"})
            return 16
        target.unlink()
        mutated = str(target)
    else:
        emit({"phase": "corrupt_copy", "ok": False,
              "error": "unknown mode: {0}".format(mode)})
        return 2
    emit({"phase": "corrupt_copy", "ok": True, "mode": mode,
          "source_root": source_root, "destination_root": destination_root,
          "mutated": mutated})
    return 0


COMMANDS = {
    "seed": lambda args: cmd_seed(int(args[0]) if args else 40),
    "seed_evidence": lambda args: cmd_seed_evidence(),
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
    "corrupt_copy": lambda args: cmd_corrupt_copy(args[0], args[1], args[2]),
}


def main() -> int:
    if len(sys.argv) < 2 or sys.argv[1] not in COMMANDS:
        print(__doc__, file=sys.stderr)
        return 2
    os.chdir(PROJECT_ROOT)  # relative data/... paths resolve against /app
    return COMMANDS[sys.argv[1]](sys.argv[2:])


if __name__ == "__main__":
    raise SystemExit(main())
