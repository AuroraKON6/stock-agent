#!/usr/bin/env python
"""R25 cutover drill driver — runs INSIDE the accepted candidate image.

KONM-28 (R25 隔离切换演练): walks the taskbook's cutover — 排空 → 静止快照 →
隔离恢复 → 对照 → 唯一调度者切换 → 同时隙重放不重复 → 冻结回退 — against TWO
independent temp state roots (the legacy volume at /app/data and the new
volume at /app/r25-new), with synthetic incremental tasks only. Zero real
providers (the compose network is internal), zero real PowerShell tasks
touched: the legacy entry's disable is a clearly-marked simulated registry
record whose real-world effect belongs to
scripts/set_scheduled_tasks_enabled.ps1 in the real window.

Everything behavioral that the drill relies on (drain, store dedup by
business intent, backup/restore, worker entry) runs through the IMAGE's own
code; only this driver, the reviewed precheck driver (reused for the
full-column reconciliation), and stock_agent/scheduler_ownership.py (the new
R25 module, bind-mounted read-only) come from the checkout.

Every subcommand prints one JSON object to stdout and exits 0 on success /
non-zero on a failed assertion, so the host-side runner can record real
container commands, exit codes and before/after states verbatim.

State layout:
    /app/data                                  legacy temp state (source tree)
    /app/data/config/watchlist.drill.json      legacy drill config
    /app/data/scheduler_ownership/             unique-scheduler registry (source copy)
    /app/r25-new                               new temp state (volume)
    /app/r25-new/watchlist.cutover-new.json    working config of the NEW scheduler
    /app/r25-new/drills/drill_*/restored       restored tree (never over /app/data)

Subcommands: see COMMANDS at the bottom.
"""
from __future__ import annotations

import datetime as _datetime
import hashlib
import json
import os
import shutil
import sqlite3
import subprocess
import sys
from pathlib import Path

sys.dont_write_bytecode = True  # the mounted module dir is read-only

DRILLS_DIR = Path("/drills")
if DRILLS_DIR.is_dir() and str(DRILLS_DIR) not in sys.path:
    sys.path.insert(0, str(DRILLS_DIR))


def _project_root() -> Path:
    for candidate in (Path("/app"), Path(__file__).resolve().parents[1]):
        if (candidate / "stock_agent").is_dir():
            return candidate
    return Path.cwd()


PROJECT_ROOT = _project_root()
sys.path.insert(0, str(PROJECT_ROOT))

DATA = Path("/app/data")                      # legacy temp state root
NEW_ROOT = Path("/app/r25-new")               # new temp state root
LEGACY_CONFIG_REL = "data/config/watchlist.drill.json"
NEW_RESTORE_CONFIG = NEW_ROOT / "watchlist.cutover-restore.json"
NEW_WORK_CONFIG = NEW_ROOT / "watchlist.cutover-new.json"
LEGACY_TASKS_DB = DATA / "task_runtime/tasks.sqlite3"

SCHEDULE_ID = "legacy-daily-research"
# A fixed past slot: the legacy scheduler "already ran" this before cutover.
LEGACY_PLANNED_SLOT = "2026-11-20T08:30:00+08:00"
SCOPE_VERSION = "r25-cutover-drill-v1"
CUTOVER_ID = "cutover_20261123_r25"

EVIDENCE_OWNER = "lead-analyst"


def emit(payload: dict) -> None:
    print(json.dumps(payload, ensure_ascii=False, sort_keys=True), flush=True)


def _rel(path: Path) -> str:
    """Project-relative POSIX path for config entries (resolved against /app)."""
    return path.resolve().relative_to(PROJECT_ROOT).as_posix()


def load_config_at(relative: str) -> dict:
    from stock_agent.config import load_config

    return load_config(relative)


def legacy_config() -> dict:
    return load_config_at(LEGACY_CONFIG_REL)


def new_work_config() -> dict:
    if not NEW_WORK_CONFIG.is_file():
        raise SystemExit("new working config missing; run restore_new first")
    return load_config_at(_rel(NEW_WORK_CONFIG))


def open_store(db_path: Path, *, read_only: bool = False):
    from stock_agent.task_runtime import TaskStore

    return TaskStore(db_path, read_only=read_only)


def open_legacy_store(*, read_only: bool = False):
    return open_store(LEGACY_TASKS_DB, read_only=read_only)


def new_tasks_db() -> Path:
    cfg = new_work_config()
    from stock_agent.config import project_path

    return project_path(str((cfg.get("task_runtime") or {}).get("db_path") or ""))


def open_new_store(*, read_only: bool = False):
    return open_store(new_tasks_db(), read_only=read_only)


def registry_root(which: str) -> Path:
    if which == "legacy":
        return DATA / "scheduler_ownership"
    cfg = new_work_config()
    from stock_agent.config import project_path

    return project_path(str((cfg.get("scheduler_ownership") or {}).get("state_dir") or ""))


def status_counts(store) -> dict:
    counts: dict[str, int] = {}
    for task in store.list_tasks():
        status = str(task.get("status") or "?")
        counts[status] = counts.get(status, 0) + 1
    return counts


def slot_key(planned_slot: str) -> str:
    from stock_agent.worker_service import scheduled_slot_key

    return scheduled_slot_key(SCHEDULE_ID, planned_slot, scope_version=SCOPE_VERSION)


def slot_task_spec(config: dict, planned_slot: str) -> dict:
    """The scheduled request for ONE slot, built deterministically from the
    slot identity: a replay of the same slot (same schedule/scope) carries the
    same logical content, which is exactly what the store's business-intent
    dedup compares — a different request under the same intent is a conflict,
    not a duplicate."""
    from stock_agent.task_supply import build_task_spec

    spec = build_task_spec(
        research_question="R25 切换演练定时研究 {0} {1} {2}".format(
            SCHEDULE_ID, planned_slot, SCOPE_VERSION),
        symbols=["600519"], execution_mode="replay", config=config)
    spec["bound_signal_ids"] = []
    spec["public_context"]["evidence_summaries"] = {}
    spec["replay_response"] = {
        "schema_version": "1.0", "task_id": "placeholder",
        "summary": "结论", "key_findings": [], "unknowns": [],
        "evidence_refs": [], "human_questions": [],
        "model_self_assessed_confidence": 0.3}
    spec["scheduled_slot"] = {
        "schedule_id": SCHEDULE_ID, "planned_slot": planned_slot,
        "scope_version": SCOPE_VERSION}
    return spec


def _increment_spec(config: dict, index: int) -> dict:
    """A synthetic INCREMENTAL task (no scheduled intent): unique content, so
    each one is its own research request."""
    from stock_agent.task_supply import build_task_spec

    spec = build_task_spec(
        research_question="R25 切换演练增量问题 {0}-{1}".format(index, os.urandom(4).hex()),
        symbols=["600519"], execution_mode="replay", config=config)
    spec["bound_signal_ids"] = []
    spec["public_context"]["evidence_summaries"] = {}
    spec["replay_response"] = {
        "schema_version": "1.0", "task_id": "placeholder",
        "summary": "结论", "key_findings": [], "unknowns": [],
        "evidence_refs": [], "human_questions": [],
        "model_self_assessed_confidence": 0.3}
    return spec


def intent_accounting(db_path: Path, intent: str) -> dict:
    """Per-intent execution/billing/outbox accounting (read-only SQL)."""
    connection = sqlite3.connect("file:{0}?mode=ro".format(db_path.resolve()), uri=True)
    try:
        rows = connection.execute(
            "SELECT task_id, status, execution_mode, attempt_count, model_call"
            " FROM research_tasks WHERE business_intent_id = ?", (intent,)).fetchall()
        tasks = [{"task_id": r[0], "status": r[1], "execution_mode": r[2],
                  "attempt_count": r[3]} for r in rows]
        outbox_per_task = {}
        research_rounds = {}
        for row in rows:
            task_id = str(row[0])
            outbox_per_task[task_id] = int(connection.execute(
                "SELECT COUNT(*) FROM task_outbox WHERE task_id = ?", (task_id,)).fetchone()[0])
            research_rounds[task_id] = int(connection.execute(
                "SELECT COUNT(*) FROM task_outbox WHERE task_id = ? AND event_type = 'research_round'",
                (task_id,)).fetchone()[0])
        attempts = 0
        budget_events = 0
        for row in rows:
            task_id = str(row[0])
            attempts += int(connection.execute(
                "SELECT COUNT(*) FROM task_invocation_attempts WHERE task_id = ?",
                (task_id,)).fetchone()[0])
            budget_events += int(connection.execute(
                "SELECT COUNT(*) FROM task_invocation_attempts WHERE task_id = ?"
                " AND budget_event_id <> ''", (task_id,)).fetchone()[0])
        model_calls = []
        for row in rows:
            try:
                model_call = json.loads(row[4] or "{}")
            except ValueError:
                model_call = {}
            model_calls.append({"mode": str(model_call.get("mode") or ""),
                                "called": bool(model_call.get("called")),
                                "rounds": len(model_call.get("rounds") or [])})
        return {"intent": intent, "tasks_with_intent": len(rows), "tasks": tasks,
                "outbox_events_per_task": outbox_per_task,
                "research_round_events_per_task": research_rounds,
                "invocation_attempts_total": attempts,
                "budget_linked_attempts_total": budget_events,
                "model_calls": model_calls}
    finally:
        connection.close()


def source_fingerprint() -> dict:
    """Hash the legacy tree's BUSINESS state (not reports/bundles outputs)."""
    targets = [
        DATA / "stock_agent.sqlite3",
        LEGACY_TASKS_DB,
        DATA / "kimi/query_intake.sqlite3",
        DATA / "scheduler_ownership/state.json",
        DATA / "scheduler_ownership/events.jsonl",
    ]
    files = {}
    for target in targets:
        files[target.relative_to(DATA).as_posix()] = (
            hashlib.sha256(target.read_bytes()).hexdigest() if target.is_file() else None)
    return {"root": str(DATA), "files": files}


def run_cli(args: list, *, timeout: int = 300) -> dict:
    """Run one in-image normal entry (python -m stock_agent ...)."""
    completed = subprocess.run([sys.executable, "-m", "stock_agent"] + args,
                               capture_output=True, text=True, timeout=timeout, cwd=str(PROJECT_ROOT))
    return {"args": args, "returncode": completed.returncode,
            "stdout_tail": (completed.stdout or "")[-600:],
            "stderr_tail": (completed.stderr or "")[-600:]}


# --------------------------------------------------------------------------- #
# reset / seed
# --------------------------------------------------------------------------- #
def _require_dedicated_mount(path: Path) -> dict:
    """The drill may only wipe state roots that are dedicated volume mounts."""
    best = {"mountpoint": "", "fstype": ""}
    try:
        with open("/proc/self/mounts", encoding="utf-8") as handle:
            for line in handle:
                fields = line.split()
                if len(fields) < 3:
                    continue
                mountpoint = fields[1].replace("\\040", " ")
                if path.as_posix() == mountpoint:
                    best = {"mountpoint": mountpoint, "fstype": fields[2]}
    except OSError:
        pass
    if best["mountpoint"] != path.as_posix():
        raise SystemExit("refusing: {0} is not a dedicated mountpoint".format(path))
    return best


def cmd_reset() -> int:
    mounts = {str(DATA): _require_dedicated_mount(DATA),
              str(NEW_ROOT): _require_dedicated_mount(NEW_ROOT)}
    removed = []
    for root in (DATA, NEW_ROOT):
        for entry in sorted(root.iterdir()):
            if entry.is_dir():
                shutil.rmtree(entry)
            else:
                entry.unlink()
            removed.append(entry.relative_to(root).as_posix())
    emit({"phase": "reset", "ok": True, "mounts": mounts, "removed": removed})
    return 0


DRILL_BASE_CONFIG = {
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
    "dify": {"enabled": False},
    "scheduler_ownership": {
        "enabled": True,
        "state_dir": "data/scheduler_ownership",
        "heartbeat_stale_seconds": 900,
        "rollback_supported_schema": "1.3",
        "min_free_bytes": 1048576,
    },
}


def _seed_static_state(config: dict) -> None:
    DATA.joinpath("config").mkdir(parents=True, exist_ok=True)
    DATA.joinpath("task_runtime").mkdir(parents=True, exist_ok=True)
    DATA.joinpath("kimi").mkdir(parents=True, exist_ok=True)
    vault = DATA / "vault"
    vault.mkdir(parents=True, exist_ok=True)
    (vault / "restrictions.json").write_text(
        json.dumps({"version": "empty-drill-restrictions", "restricted": []}), encoding="utf-8")
    (vault / "roster.json").write_text(
        json.dumps({"version": "empty-drill-roster", "members": []}), encoding="utf-8")
    (DATA / "config" / "watchlist.drill.json").write_text(
        json.dumps(config, ensure_ascii=False, indent=2), encoding="utf-8")
    for relative in ("stock_agent.sqlite3", "task_runtime/tasks.sqlite3",
                     "kimi/query_intake.sqlite3"):
        target = DATA / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        if not target.exists():
            sqlite3.connect(str(target)).close()


def cmd_seed(queued: int) -> int:
    from stock_agent.research_worker import run_research_task
    from stock_agent.scheduler_ownership import initialize_registry, LEGACY_SCHEDULER_ID

    if (DATA / "config" / "watchlist.drill.json").exists():
        emit({"phase": "seed", "ok": False, "error": "legacy state already seeded (run reset first)"})
        return 2
    config = json.loads(json.dumps(DRILL_BASE_CONFIG))  # deep copy
    config["backup_restore"] = {
        "enabled": True,
        "bundle_root": "data/backup_restore/bundles",
        "drill_root": "data/backup_restore/drills",  # unused by the legacy side
        "event_path": "data/backup_restore/events.jsonl",
        "report_dir": "data/backup_restore/reports",
    }
    _seed_static_state(config)
    registry = initialize_registry(
        DATA / "scheduler_ownership", first_scheduler=LEGACY_SCHEDULER_ID,
        actor="r25-drill", reason="cutover rehearsal: legacy state first")

    store = open_legacy_store()
    try:
        # The legacy scheduler ALREADY ran this slot before cutover: create
        # the intent FIRST (claim_next takes the OLDEST queued task) and run
        # it through the real claim+worker path (in-image code).
        intent = slot_key(LEGACY_PLANNED_SLOT)
        slot_spec = slot_task_spec(config, LEGACY_PLANNED_SLOT)
        slot_task, slot_dedup = store.create_task(
            title="R25-cutover-slot-legacy", input_payload=slot_spec,
            execution_mode="replay", business_intent_id=intent)
        slot_task_id = str(slot_task["task_id"])
        claimed = store.claim_next("w-legacy-slot", ttl_seconds=60)
        if not claimed or str(claimed["task_id"]) != slot_task_id:
            emit({"phase": "seed", "ok": False,
                  "error": "claim_next did not return the slot task",
                  "claimed": str((claimed or {}).get("task_id") or "")})
            return 3
        outcome = run_research_task(store, slot_task_id, worker_id="w-legacy-slot",
                                    config=config, ttl_seconds=60)
        slot_final = store.get_task(slot_task_id)
        if not (str(outcome.get("status")) == "succeeded"
                and str(slot_final.get("status")) == "succeeded"):
            emit({"phase": "seed", "ok": False, "error": "legacy slot task did not succeed",
                  "outcome": str(outcome.get("status")), "final": str(slot_final.get("status"))})
            return 3

        # One synthetic personal-evidence task (same shape as the reviewed
        # precheck fixture): announcement -> located excerpt -> task whose
        # approved_facts reference it -> real claim + worker run with the
        # personal vault view. The reconciliation REQUIRES this chain.
        evidence = _seed_evidence_task(config)
        if not evidence.get("ok"):
            emit({"phase": "seed", "ok": False, "error": "evidence task failed",
                  "evidence": evidence})
            return 3

        seeded_queued = []
        for index in range(1, queued + 1):
            spec = _increment_spec(config, index)
            task, _dedup = store.create_task(
                title="R25-cutover-queued-{0}".format(index),
                input_payload=spec, execution_mode="replay")
            seeded_queued.append(str(task["task_id"]))
    finally:
        store.close()

    emit({"phase": "seed", "ok": True, "queued_seeded": len(seeded_queued),
          "queued_task_ids": seeded_queued,
          "slot_dedup": slot_dedup,
          "evidence": evidence,
          "slot": {"schedule_id": SCHEDULE_ID, "planned_slot": LEGACY_PLANNED_SLOT,
                   "scope_version": SCOPE_VERSION, "intent": intent,
                   "task_id": slot_task_id, "final_status": "succeeded"},
          "registry": {"active": registry["active_scheduler"], "generation": registry["generation"]},
          "accounting": intent_accounting(LEGACY_TASKS_DB, intent)})
    return 0


def _seed_evidence_task(config: dict) -> dict:
    """The reviewed precheck evidence fixture, adapted to this drill's config:
    one synthetic announcement -> located excerpt -> task referencing it ->
    real claim + run with the personal vault view."""
    import hashlib as _hashlib

    from stock_agent.evidence_excerpts import (
        _document_text, create_excerpt, excerpt_selector)
    from stock_agent.personal_evidence import (
        import_personal_document, personal_vault_view)
    from stock_agent.research_worker import run_research_task

    incoming = DATA / "incoming"
    incoming.mkdir(parents=True, exist_ok=True)
    announcement = incoming / "r25_cutover_announcement.html"
    announcement.write_text(
        "<html><body><p>合成公告（非真实披露）：营业收入为 1,234.56 万元。</p></body></html>",
        encoding="utf-8")
    imported = import_personal_document(
        config, EVIDENCE_OWNER, str(announcement),
        source_url="https://www.cninfo.com.cn?r25-cutover-fixture",
        title="R25 切换演练合成公告（非真实披露）",
        published_at="2026-11-23T09:00:00+08:00")
    document = imported["document"]
    view = personal_vault_view(config, EVIDENCE_OWNER)
    text, _version = _document_text(view, document)
    start = text.index("营业收入为")
    end = text.index("。", start) + 1
    excerpt = create_excerpt(view, document["document_id"], start, end)
    selector = excerpt_selector(excerpt)

    spec = slot_task_spec(config, LEGACY_PLANNED_SLOT)  # config-shape only
    spec["research_question"] = "R25 切换演练证据引用 {0}".format(os.urandom(4).hex())
    spec["scheduled_slot"] = {}  # plain task, not a scheduled intent
    spec["public_context"]["evidence_summaries"] = {
        document["document_id"]: {
            "summary": "R25 切换演练合成公告正文", "sha256": document["sha256"]}}
    spec["approved_facts"] = [selector]

    store = open_legacy_store()
    try:
        task, _dedup = store.create_task(
            title="R25-cutover-evidence", input_payload=spec,
            execution_mode="replay")
        task_id = str(task["task_id"])
        claimed = store.claim_next("w-r25-evidence", ttl_seconds=60)
        if not claimed or str(claimed["task_id"]) != task_id:
            return {"ok": False, "error": "claim_next did not return the evidence task",
                    "claimed": str((claimed or {}).get("task_id") or "")}
        outcome = run_research_task(store, task_id, worker_id="w-r25-evidence",
                                    config=view, ttl_seconds=60)
        final = store.get_task(task_id)
        model_call = final.get("model_call") or {}
        facts = ((model_call.get("outbound_context") or {}).get("verified_facts") or [])
        fact = facts[0] if facts else {}
        fact_content_sha = _hashlib.sha256(
            str(fact.get("content") or "").encode("utf-8")).hexdigest()
        ok = (str(outcome.get("status")) == "succeeded"
              and str(final.get("status")) == "succeeded"
              and str(fact.get("document_sha256") or "") == document["sha256"]
              and fact_content_sha == selector["excerpt_sha256"])
        return {"ok": ok, "task_id": task_id, "owner": EVIDENCE_OWNER,
                "document_id": document["document_id"],
                "document_sha256": document["sha256"],
                "excerpt_sha256": selector["excerpt_sha256"],
                "final_status": str(final.get("status"))}
    finally:
        store.close()


# --------------------------------------------------------------------------- #
# drain / fingerprint / gate
# --------------------------------------------------------------------------- #
def cmd_drain() -> int:
    store = open_legacy_store(read_only=True)
    try:
        before = status_counts(store)
    finally:
        store.close()
    depth = before.get("queued", 0)
    result = None
    if depth:
        result = run_cli(["worker-service", "--config", LEGACY_CONFIG_REL,
                          "--worker-id", "w-legacy-drain", "--ttl-seconds", "60",
                          "--idle-sleep-seconds", "0.1", "--max-tasks", str(depth)])
    store = open_legacy_store(read_only=True)
    try:
        after = status_counts(store)
        in_flight = {k: v for k, v in after.items() if k in ("leased", "running")}
    finally:
        store.close()
    ok = (result is None or result["returncode"] == 0) \
        and after.get("queued", 0) == 0 and not in_flight
    emit({"phase": "drain", "ok": ok, "before": before, "after": after,
          "worker": result or {"args": ["(skipped: queue already empty)"],
                               "returncode": 0, "stdout_tail": "", "stderr_tail": ""}})
    return 0 if ok else 4


def cmd_fingerprint(label: str) -> int:
    fingerprint = source_fingerprint()
    emit({"phase": "fingerprint", "label": label, **fingerprint})
    return 0


def cmd_gate(schema_ceiling: str, min_free_mb: float, tasks_db: str) -> int:
    from stock_agent.config import project_path
    from stock_agent.scheduler_ownership import evaluate_cutover_gate
    from stock_agent.task_runtime.store import TASK_SCHEMA_VERSION

    config = legacy_config()
    if schema_ceiling:
        config["scheduler_ownership"]["rollback_supported_schema"] = schema_ceiling
    if min_free_mb:
        config["scheduler_ownership"]["min_free_bytes"] = int(min_free_mb * 1024 * 1024)
    db = project_path(tasks_db) if tasks_db else LEGACY_TASKS_DB
    verdict = evaluate_cutover_gate(
        tasks_db_path=db, ownership_root=DATA / "scheduler_ownership",
        state_root=DATA, runtime_schema_version=TASK_SCHEMA_VERSION, config=config)
    emit({"phase": "gate", "ok": verdict["ok"], "tasks_db": str(db), **verdict})
    return 0 if verdict["ok"] else 21

def cmd_inject_unknown() -> int:
    store = open_legacy_store()
    try:
        task = store.claim_next("w-unknown-inject", ttl_seconds=60)
        if task is None:
            emit({"phase": "inject_unknown", "ok": False, "error": "queue empty"})
            return 5
        task_id = str(task["task_id"])
        attempt = store.prepare_invocation_attempt(
            task_id, worker_id="w-unknown-inject",
            fencing_token=int(task["fencing_token"]), round_index=1,
            provider="synthetic", request_model="replay",
            prompt_sha256="0" * 64)
        store.mark_invocation_dispatched(str(attempt["attempt_id"]),
                                          worker_id="w-unknown-inject",
                                          fencing_token=int(task["fencing_token"]))
        emit({"phase": "inject_unknown", "ok": True, "task_id": task_id,
              "attempt_id": str(attempt["attempt_id"]),
              "state": "dispatched", "sent": "unknown",
              "note": "terminal-less dispatched attempt: the unexplained-unknown"
                      " gate condition (drill injection only)"})
        return 0
    finally:
        store.close()


def cmd_inject_dual() -> int:
    """Two evidences of the dual-scheduler stop condition.

    1. Organic refusal: a foreign scheduler's heartbeat is REFUSED while the
       legacy entry owns scheduling (nothing is recorded).
    2. Registry detection: a synthetic fresh foreign beat written directly
       into actors_seen (the state a racing/stale copy leaves behind) must
       make the cutover gate abort."""
    from stock_agent.scheduler_ownership import (
        DualSchedulerError, evaluate_cutover_gate, load_state,
    )
    from stock_agent.task_runtime.store import TASK_SCHEMA_VERSION

    refused = False
    refusal_error = ""
    try:
        from stock_agent.scheduler_ownership import heartbeat

        heartbeat(DATA / "scheduler_ownership", "container-worker")
    except DualSchedulerError as exc:
        refused = True
        refusal_error = str(exc)
    # Synthetic fresh foreign beat (directly into the state file).
    state_path = DATA / "scheduler_ownership/state.json"
    state = json.loads(state_path.read_text(encoding="utf-8"))
    state.setdefault("actors_seen", {})["container-worker"] = \
        _datetime.datetime.now().astimezone().isoformat(timespec="seconds")
    from stock_agent.audit_log import atomic_write_text

    atomic_write_text(state_path,
                      json.dumps(state, ensure_ascii=False, indent=2, sort_keys=True) + "\n")
    config = legacy_config()
    verdict = evaluate_cutover_gate(
        tasks_db_path=LEGACY_TASKS_DB, ownership_root=DATA / "scheduler_ownership",
        state_root=DATA, runtime_schema_version=TASK_SCHEMA_VERSION, config=config)
    detected = (not verdict["ok"]) and any(
        "dual scheduler" in str(item) for item in verdict.get("blockers") or [])
    state_after = load_state(DATA / "scheduler_ownership")
    ok = refused and detected
    emit({"phase": "inject_dual", "ok": ok,
          "heartbeat_refused": refused, "refusal_error": refusal_error[:200],
          "injected_foreign_heartbeat": True,
          "gate_blocked_on_dual": detected,
          "gate_blockers": verdict.get("blockers"),
          "registry_active_unchanged": state_after.get("active_scheduler")})
    return 0 if ok else 6


# --------------------------------------------------------------------------- #
# snapshot / restore / reconcile
# --------------------------------------------------------------------------- #
def cmd_snapshot() -> int:
    result = run_cli(["backup-create", "--config", LEGACY_CONFIG_REL], timeout=600)
    reports = DATA / "backup_restore/reports"
    latest = ""
    if reports.is_dir():
        candidates = sorted(p for p in reports.iterdir() if p.is_file())
        if candidates:
            latest = candidates[-1].name
    ok = result["returncode"] == 0 and "PASS" in (result["stdout_tail"] or "")
    fingerprint = source_fingerprint()
    NEW_ROOT.mkdir(parents=True, exist_ok=True)
    (NEW_ROOT / "snapshot_fingerprint.json").write_text(
        json.dumps(fingerprint, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8")
    emit({"phase": "snapshot", "ok": ok, "worker": result,
          "latest_report": latest,
          "source_fingerprint": fingerprint,
          "fingerprint_saved": str(NEW_ROOT / "snapshot_fingerprint.json")})
    return 0 if ok else 7


def _restored_root() -> Path:
    drills = NEW_ROOT / "drills"
    candidates = sorted(p for p in drills.glob("drill_*/restored") if p.is_dir())
    if not candidates:
        raise SystemExit("no restored tree under {0}; run restore_new first".format(drills))
    return candidates[-1]


def cmd_restore_new() -> int:
    if NEW_RESTORE_CONFIG.exists():
        emit({"phase": "restore_new", "ok": False,
              "error": "restore config already present (run reset first)"})
        return 2
    # Resolve the just-created bundle from the LEGACY tree's latest pointer
    # (read-only), so the restore side needs no legacy writes at all.
    latest_pointer = DATA / "backup_restore/reports/backup_latest.json"
    if not latest_pointer.is_file():
        emit({"phase": "restore_new", "ok": False,
              "error": "legacy backup_latest.json missing; run snapshot first"})
        return 2
    bundle_relative = str(json.loads(
        latest_pointer.read_text(encoding="utf-8")).get("bundle_path") or "")
    bundle_path = (PROJECT_ROOT / bundle_relative).resolve()
    if not bundle_path.is_dir():
        emit({"phase": "restore_new", "ok": False,
              "error": "bundle not found: {0}".format(bundle_path)})
        return 2
    NEW_ROOT.mkdir(parents=True, exist_ok=True)
    restore_config = json.loads(json.dumps(DRILL_BASE_CONFIG))
    restore_config["backup_restore"] = {
        "enabled": True,
        "bundle_root": "data/backup_restore/bundles",        # read from legacy
        "drill_root": _rel(NEW_ROOT / "drills"),             # restore INTO the new volume
        "event_path": _rel(NEW_ROOT / "backup_restore/events.jsonl"),
        "report_dir": _rel(NEW_ROOT / "backup_restore/reports"),
    }
    NEW_RESTORE_CONFIG.write_text(json.dumps(restore_config, ensure_ascii=False, indent=2),
                                  encoding="utf-8")
    result = run_cli(["restore-drill", "--config", _rel(NEW_RESTORE_CONFIG),
                      "--backup", str(bundle_path)], timeout=600)
    restored = _restored_root()
    ok = result["returncode"] == 0 and "PASS" in (result["stdout_tail"] or "") \
        and (restored / "data/task_runtime/tasks.sqlite3").is_file()
    if not ok:
        emit({"phase": "restore_new", "ok": False, "worker": result,
              "restored_root": str(restored) if restored.exists() else ""})
        return 8
    # The R24 bundle carries business state only: the scheduler-ownership
    # registry is part of the CUTOVER migration, so it is copied explicitly
    # (read from legacy, written into the new tree) — never merged back.
    source_registry = DATA / "scheduler_ownership"
    target_registry = restored / "data/scheduler_ownership"
    if target_registry.exists():
        shutil.rmtree(target_registry)
    shutil.copytree(source_registry, target_registry)
    # Working config for the NEW scheduler: every data path under the restored tree.
    work = json.loads(json.dumps(DRILL_BASE_CONFIG))
    work["storage"] = {"sqlite_path": _rel(restored / "data/stock_agent.sqlite3")}
    work["official_evidence_vault"] = {
        "enabled": True,
        "events_path": _rel(restored / "data/vault/events.jsonl"),
        "index_path": _rel(restored / "data/vault/index.json"),
        "objects_dir": _rel(restored / "data/vault/objects"),
        "quarantine_dir": _rel(restored / "data/vault/quarantine"),
        "report_dir": _rel(restored / "data/vault/reports"),
        "allow_network_fetch": False,
        "automatic_approval": False,
    }
    work["research_operations"] = {
        "enforce_roster": False,
        "roster_path": _rel(restored / "data/vault/roster.json"),
        "restrictions_path": _rel(restored / "data/vault/restrictions.json"),
    }
    work["task_runtime"] = {"db_path": _rel(restored / "data/task_runtime/tasks.sqlite3")}
    work["kimi_sources"] = {"query_store_path": _rel(restored / "data/kimi/query_intake.sqlite3")}
    work["scheduler_ownership"] = dict(DRILL_BASE_CONFIG["scheduler_ownership"])
    work["scheduler_ownership"]["state_dir"] = _rel(target_registry)
    work["backup_restore"] = dict(restore_config["backup_restore"])
    NEW_WORK_CONFIG.write_text(json.dumps(work, ensure_ascii=False, indent=2), encoding="utf-8")

    source_before = source_fingerprint()
    emit({"phase": "restore_new", "ok": True, "worker": result,
          "restored_root": str(restored),
          "new_work_config": str(NEW_WORK_CONFIG),
          "registry_copied_to": str(target_registry),
          "source_fingerprint_after_restore": source_before,
          "source_untouched_note": "legacy business files hashed before and after"
                                   " are compared in source_intact"})
    return 0


def cmd_reconcile() -> int:
    import r25_precheck_drill as precheck

    restored = _restored_root()
    code = precheck.cmd_compare(str(LEGACY_TASKS_DB), str(restored))
    # cmd_compare emitted its own JSON line; add the cutover framing after it.
    emit({"phase": "reconcile", "ok": code == 0, "compare_exit": code,
          "live_db": str(LEGACY_TASKS_DB), "restored_root": str(restored)})
    return code


# --------------------------------------------------------------------------- #
# unique-scheduler switch + same-slot replay
# --------------------------------------------------------------------------- #
def cmd_switch_refuse_no_marker() -> int:
    from stock_agent.scheduler_ownership import OwnershipConflictError, record_switch

    root = registry_root("new")
    refused = False
    error = ""
    try:
        record_switch(root, to="container-worker", actor="r25-drill",
                      cutover_id=CUTOVER_ID)
    except OwnershipConflictError as exc:
        refused = True
        error = str(exc)
    emit({"phase": "switch_refuse_no_marker", "ok": refused,
          "refused": refused, "error": error[:200],
          "registry": str(root)})
    return 0 if refused else 9


def cmd_switch_new() -> int:
    from stock_agent.scheduler_ownership import (
        CONTAINER_SCHEDULER_ID, DualSchedulerError, NotActiveSchedulerError,
        assert_unique_scheduler, disable_legacy, heartbeat, record_switch,
    )

    root = registry_root("new")
    marker = disable_legacy(root, actor="r25-drill", cutover_id=CUTOVER_ID,
                            simulated=True,
                            method="scripts/set_scheduled_tasks_enabled.ps1 -Disable"
                                   " (NOT RUN in rehearsal; real window step)")
    state = record_switch(root, to=CONTAINER_SCHEDULER_ID, actor="r25-drill",
                          reason="R25 cutover rehearsal: forward switch",
                          cutover_id=CUTOVER_ID)
    heartbeat(root, CONTAINER_SCHEDULER_ID)
    guard_ok = True
    guard_error = ""
    legacy_refused = False
    legacy_error = ""
    try:
        assert_unique_scheduler(root, CONTAINER_SCHEDULER_ID)
    except (DualSchedulerError, NotActiveSchedulerError) as exc:
        guard_ok = False
        guard_error = str(exc)
    try:
        assert_unique_scheduler(root, "legacy-powershell")
    except (DualSchedulerError, NotActiveSchedulerError):
        legacy_refused = True
    ok = state["active_scheduler"] == CONTAINER_SCHEDULER_ID and guard_ok and legacy_refused
    emit({"phase": "switch_new", "ok": ok,
          "legacy_disabled_marker": marker,
          "registry": {"active": state["active_scheduler"],
                       "generation": state["generation"],
                       "cutover_id": state.get("cutover_id")},
          "container_guard_ok": guard_ok, "guard_error": guard_error[:200],
          "legacy_side_refused": legacy_refused})
    return 0 if ok else 10


def cmd_replay_slot() -> int:
    """The container scheduler replays the SAME slot through the normal
    store dedup path: no new task, no new execution, no new outbox delivery,
    no new billing — for the SAME business intent."""
    from stock_agent.scheduler_ownership import (
        CONTAINER_SCHEDULER_ID, assert_unique_scheduler, heartbeat,
    )

    root = registry_root("new")
    heartbeat(root, CONTAINER_SCHEDULER_ID)
    assert_unique_scheduler(root, CONTAINER_SCHEDULER_ID)
    db = new_tasks_db()
    intent = slot_key(LEGACY_PLANNED_SLOT)
    before = intent_accounting(db, intent)

    config = new_work_config()
    store = open_new_store()
    try:
        spec = slot_task_spec(config, LEGACY_PLANNED_SLOT)
        task, dedup = store.create_task(
            title="R25-cutover-slot-replay", input_payload=spec,
            execution_mode="replay", business_intent_id=intent)
        replayed_task_id = str(task["task_id"])
        # A second trigger in the same process must converge identically.
        task2, dedup2 = store.create_task(
            title="R25-cutover-slot-replay", input_payload=spec,
            execution_mode="replay", business_intent_id=intent)
        same_id = str(task2["task_id"]) == replayed_task_id
    finally:
        store.close()

    after = intent_accounting(db, intent)
    no_new_task = after["tasks_with_intent"] == before["tasks_with_intent"] == 1
    same_legacy_id = replayed_task_id == (before["tasks"][0]["task_id"] if before["tasks"] else "")
    no_new_outbox = after["outbox_events_per_task"] == before["outbox_events_per_task"]
    no_new_rounds = after["research_round_events_per_task"] == before["research_round_events_per_task"]
    no_new_attempts = after["invocation_attempts_total"] == before["invocation_attempts_total"]
    no_new_billing = after["budget_linked_attempts_total"] == before["budget_linked_attempts_total"]
    status_kept = (after["tasks"][0]["status"] == before["tasks"][0]["status"] == "succeeded")
    ok = (dedup == "DUPLICATE" and dedup2 == "DUPLICATE" and same_id
          and no_new_task and same_legacy_id and no_new_outbox and no_new_rounds
          and no_new_attempts and no_new_billing and status_kept)
    emit({"phase": "replay_slot", "ok": ok,
          "slot": {"schedule_id": SCHEDULE_ID, "planned_slot": LEGACY_PLANNED_SLOT,
                   "intent": intent},
          "dedup_results": [dedup, dedup2], "converged_same_task_id": same_id,
          "before": before, "after": after,
          "checks": {"no_new_task": no_new_task, "same_legacy_task_id": same_legacy_id,
                     "no_new_outbox_delivery": no_new_outbox,
                     "no_new_research_round": no_new_rounds,
                     "no_new_invocation_attempts": no_new_attempts,
                     "no_new_budget_billing": no_new_billing,
                     "status_kept_succeeded": status_kept}})
    return 0 if ok else 11


def cmd_new_slot(index: int, run: bool = True) -> int:
    """Post-cutover incremental slot: a NEW intent on a LATER business day,
    executed through the in-image worker-service CLI (normal entry).
    ``run=False`` only enqueues it (the frozen-drain demo needs one queued)."""
    from stock_agent.scheduler_ownership import (
        CONTAINER_SCHEDULER_ID, assert_intake_allowed, assert_unique_scheduler,
        heartbeat,
    )

    root = registry_root("new")
    heartbeat(root, CONTAINER_SCHEDULER_ID)
    assert_unique_scheduler(root, CONTAINER_SCHEDULER_ID)
    assert_intake_allowed(root)
    planned = "2026-11-2{0}T08:30:00+08:00".format(1 + index)
    intent = slot_key(planned)
    config = new_work_config()
    store = open_new_store()
    try:
        spec = slot_task_spec(config, planned)
        task, dedup = store.create_task(
            title="R25-cutover-newslot-{0}".format(index), input_payload=spec,
            execution_mode="replay", business_intent_id=intent)
        task_id = str(task["task_id"])
    finally:
        store.close()
    result = None
    if run:
        result = run_cli(["worker-service", "--config", _rel(NEW_WORK_CONFIG),
                          "--worker-id", "w-container-{0}".format(index),
                          "--ttl-seconds", "60", "--idle-sleep-seconds", "0.1",
                          "--max-tasks", "1"])
    accounting = intent_accounting(new_tasks_db(), intent)
    expected_status = "succeeded" if run else "queued"
    ok = (dedup == "RECORDED" and (result is None or result["returncode"] == 0)
          and accounting["tasks_with_intent"] == 1
          and accounting["tasks"][0]["status"] == expected_status)
    emit({"phase": "new_slot", "ok": ok, "index": index,
          "slot": {"planned_slot": planned, "intent": intent, "task_id": task_id},
          "dedup": dedup, "worker": result or {"args": ["(enqueued only)"],
                                               "returncode": 0,
                                               "stdout_tail": "", "stderr_tail": ""},
          "accounting": accounting})
    return 0 if ok else 12


# --------------------------------------------------------------------------- #
# rollback
# --------------------------------------------------------------------------- #
def cmd_freeze() -> int:
    from stock_agent.scheduler_ownership import freeze_intake

    root = registry_root("new")
    state = freeze_intake(root, actor="r25-drill",
                          reason="R25 rollback rehearsal: freeze new intake",
                          cutover_id=CUTOVER_ID)
    emit({"phase": "freeze", "ok": True,
          "registry": {"intake_frozen": state["intake_frozen"],
                       "frozen_reason": state["frozen_reason"],
                       "generation": state["generation"]}})
    return 0


def cmd_frozen_refusal() -> int:
    from stock_agent.scheduler_ownership import IntakeFrozenError, assert_intake_allowed

    root = registry_root("new")
    refused = False
    error = ""
    try:
        assert_intake_allowed(root)
    except IntakeFrozenError as exc:
        refused = True
        error = str(exc)
    emit({"phase": "frozen_refusal", "ok": refused, "refused": refused,
          "error": error[:200]})
    return 0 if refused else 13


def cmd_drain_frozen() -> int:
    """Frozen intake blocks NEW intents only: already-queued work still drains
    through the normal worker entry."""
    result = run_cli(["worker-service", "--config", _rel(NEW_WORK_CONFIG),
                      "--worker-id", "w-container-drain-frozen",
                      "--ttl-seconds", "60", "--idle-sleep-seconds", "0.1",
                      "--max-tasks", "1"])
    ok = result["returncode"] == 0 and "processed\": 1" in (result["stdout_tail"] or "")
    emit({"phase": "drain_frozen", "ok": ok, "worker": result})
    return 0 if ok else 14


def cmd_increments() -> int:
    """Reconcile the new tree against the legacy snapshot: every task/outbox
    row that exists ONLY on the new side is the rollback increment, recorded
    in the new tree (the legacy snapshot is never touched)."""
    def snapshot_ids(db_path: Path) -> dict:
        connection = sqlite3.connect("file:{0}?mode=ro".format(db_path.resolve()), uri=True)
        try:
            tasks = [str(row[0]) for row in connection.execute(
                "SELECT task_id FROM research_tasks ORDER BY task_id")]
            outbox_max = int(connection.execute(
                "SELECT COALESCE(MAX(sequence), 0) FROM task_outbox").fetchone()[0])
            return {"tasks": tasks, "outbox_max_sequence": outbox_max}
        finally:
            connection.close()

    legacy = snapshot_ids(LEGACY_TASKS_DB)
    new = snapshot_ids(new_tasks_db())
    legacy_set = set(legacy["tasks"])
    new_tasks = [task for task in new["tasks"] if task not in legacy_set]
    connection = sqlite3.connect("file:{0}?mode=ro".format(new_tasks_db().resolve()), uri=True)
    try:
        new_outbox = [int(row[0]) for row in connection.execute(
            "SELECT sequence FROM task_outbox WHERE sequence > ? ORDER BY sequence",
            (legacy["outbox_max_sequence"],))]
        new_intents = [{"task_id": row[0], "intent": row[1], "status": row[2]} for row in
                       connection.execute(
                           "SELECT task_id, business_intent_id, status FROM research_tasks"
                           " WHERE business_intent_id <> '' ORDER BY task_id")]
    finally:
        connection.close()
    manifest = {
        "schema_version": "1.0", "cutover_id": CUTOVER_ID,
        "generated_at": _datetime.datetime.now().astimezone().isoformat(timespec="seconds"),
        "baseline": {"legacy_tasks": len(legacy["tasks"]),
                     "legacy_outbox_max_sequence": legacy["outbox_max_sequence"]},
        "increment_tasks": new_tasks,
        "increment_outbox_sequences": new_outbox,
        "business_intents_new_side": new_intents,
        "policy": "rollback never overwrites the legacy tree; increments stay"
                  " recorded here for follow-up replay/forward cutover",
    }
    manifest_path = registry_root("new") / "rollback_increments.json"
    from stock_agent.audit_log import atomic_write_text

    atomic_write_text(manifest_path,
                      json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n")
    ok = bool(new_tasks) and bool(new_outbox)
    emit({"phase": "increments", "ok": ok, "manifest_path": str(manifest_path),
          "increment_tasks": len(new_tasks), "increment_outbox_events": len(new_outbox),
          "manifest": manifest})
    return 0 if ok else 15


def cmd_rollback() -> int:
    from stock_agent.scheduler_ownership import (
        enable_legacy, record_switch, thaw_intake,
    )

    root = registry_root("new")
    state = record_switch(root, to="legacy-powershell", actor="r25-drill",
                          reason="R25 rollback rehearsal: return ownership",
                          cutover_id=CUTOVER_ID)
    marker = enable_legacy(root, actor="r25-drill",
                           reason="rollback rehearsal: legacy entry may resume")
    thawed = thaw_intake(root, actor="r25-drill",
                         reason="rollback rehearsal complete")
    ok = (state["active_scheduler"] == "legacy-powershell"
          and not thawed["intake_frozen"])
    emit({"phase": "rollback", "ok": ok,
          "registry": {"active": state["active_scheduler"],
                       "generation": state["generation"]},
          "reenabled_legacy_marker": marker.get("disabled_at"),
          "intake_frozen": thawed["intake_frozen"]})
    return 0 if ok else 16


def cmd_source_intact() -> int:
    current = source_fingerprint()
    saved_path = NEW_ROOT / "snapshot_fingerprint.json"
    saved = json.loads(saved_path.read_text(encoding="utf-8")) if saved_path.is_file() else {}
    files_equal = {name: (saved.get("files") or {}).get(name) == digest
                   for name, digest in current["files"].items()}
    ok = bool(saved) and all(files_equal.values())
    legacy_registry = json.loads(
        (DATA / "scheduler_ownership/state.json").read_text(encoding="utf-8"))
    emit({"phase": "source_intact", "ok": ok,
          "current_fingerprint": current,
          "snapshot_fingerprint": saved,
          "per_file_equal": files_equal,
          "legacy_registry": {"active": legacy_registry.get("active_scheduler"),
                              "generation": legacy_registry.get("generation"),
                              "intake_frozen": legacy_registry.get("intake_frozen")},
          "note": "legacy business files (3 SQLite + registry) byte-identical"
                  " to the quiescent-snapshot moment"})
    return 0 if ok else 17


def cmd_final_counts() -> int:
    def counts(db_path: Path) -> dict:
        connection = sqlite3.connect("file:{0}?mode=ro".format(db_path.resolve()), uri=True)
        try:
            tasks = dict(connection.execute(
                "SELECT status, COUNT(*) FROM research_tasks GROUP BY status").fetchall())
            outbox = int(connection.execute("SELECT COUNT(*) FROM task_outbox").fetchone()[0])
            intents = int(connection.execute(
                "SELECT COUNT(*) FROM research_tasks WHERE business_intent_id <> ''").fetchone()[0])
            schema = connection.execute(
                "SELECT COUNT(*) FROM sqlite_master WHERE type='table'").fetchone()[0]
            return {"tasks_by_status": tasks, "outbox_events": outbox,
                    "scheduled_intents": intents, "tables": schema}
        finally:
            connection.close()

    intent = slot_key(LEGACY_PLANNED_SLOT)
    emit({"phase": "final_counts",
          "legacy": counts(LEGACY_TASKS_DB),
          "new": counts(new_tasks_db()),
          "legacy_slot_accounting": intent_accounting(LEGACY_TASKS_DB, intent),
          "new_slot_accounting": intent_accounting(new_tasks_db(), intent),
          "new_registry": json.loads(
              (registry_root("new") / "state.json").read_text(encoding="utf-8"))})
    return 0


COMMANDS = {
    "reset": lambda args: cmd_reset(),
    "seed": lambda args: cmd_seed(int(args[0]) if args else 5),
    "slot_info": lambda args: emit({"phase": "slot_info", "schedule_id": SCHEDULE_ID,
                                    "planned_slot": LEGACY_PLANNED_SLOT,
                                    "scope_version": SCOPE_VERSION,
                                    "intent": slot_key(LEGACY_PLANNED_SLOT)}) or 0,
    "drain": lambda args: cmd_drain(),
    "fingerprint": lambda args: cmd_fingerprint(args[0] if args else "unlabeled"),
    "gate": lambda args: cmd_gate(
        args[0] if args else "",
        float(args[1]) if len(args) > 1 else 0,
        args[2] if len(args) > 2 else ""),
    "inject_unknown": lambda args: cmd_inject_unknown(),
    "inject_dual": lambda args: cmd_inject_dual(),
    "snapshot": lambda args: cmd_snapshot(),
    "restore_new": lambda args: cmd_restore_new(),
    "reconcile": lambda args: cmd_reconcile(),
    "switch_refuse_no_marker": lambda args: cmd_switch_refuse_no_marker(),
    "switch_new": lambda args: cmd_switch_new(),
    "replay_slot": lambda args: cmd_replay_slot(),
    "new_slot": lambda args: cmd_new_slot(int(args[0]) if args else 1,
                                          run=not (len(args) > 1 and args[1] == "norun")),
    "freeze": lambda args: cmd_freeze(),
    "frozen_refusal": lambda args: cmd_frozen_refusal(),
    "drain_frozen": lambda args: cmd_drain_frozen(),
    "increments": lambda args: cmd_increments(),
    "rollback": lambda args: cmd_rollback(),
    "source_intact": lambda args: cmd_source_intact(),
    "final_counts": lambda args: cmd_final_counts(),
}


def main() -> int:
    if len(sys.argv) < 2 or sys.argv[1] not in COMMANDS:
        print(__doc__, file=sys.stderr)
        return 2
    os.chdir(PROJECT_ROOT)  # relative data/... paths resolve against /app
    return COMMANDS[sys.argv[1]](sys.argv[2:])


if __name__ == "__main__":
    raise SystemExit(main())
