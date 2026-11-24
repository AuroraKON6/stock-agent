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

The candidate image provides the worker and backup/restore. The checkout
supplies the read-only task store, R25 CLI, ownership module, PowerShell entry
and drivers (including KONM-27's full-column reconciliation).

Every subcommand prints one JSON object to stdout and exits 0 on success /
non-zero on a failed assertion, so the host-side runner can record real
container commands, exit codes and before/after states verbatim.

State layout:
    /app/data                                  legacy temp state (source tree)
    /app/data/config/watchlist.drill.json      legacy drill config
    /app/r25-new/scheduler_ownership/          shared ownership registry
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
LEGACY_RESUME_CONFIG = NEW_ROOT / "watchlist.cutover-legacy-resume.json"
SHARED_REGISTRY = NEW_ROOT / "scheduler_ownership"
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


def _store_counts(db_path: Path) -> dict:
    connection = sqlite3.connect("file:{0}?mode=ro".format(db_path.resolve()), uri=True)
    try:
        tasks = int(connection.execute("SELECT COUNT(*) FROM research_tasks").fetchone()[0])
        outbox = int(connection.execute("SELECT COUNT(*) FROM task_outbox").fetchone()[0])
        return {"tasks": tasks, "outbox": outbox}
    finally:
        connection.close()


def _registry_hash(root: Path) -> dict:
    files = {}
    for name in ("state.json", "events.jsonl"):
        path = Path(root) / name
        files[name] = hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else None
    return files


def _scheduled_entry(side: str, planned_slot: str, *, run: bool = True,
                     spec_path: Path | None = None) -> dict:
    """Invoke the actual PowerShell or container scheduled entry in a child process."""
    from stock_agent.config import project_path

    if side == "legacy":
        config_path = _rel(LEGACY_RESUME_CONFIG) if LEGACY_RESUME_CONFIG.is_file() else LEGACY_CONFIG_REL
    elif side == "new":
        config_path = _rel(NEW_WORK_CONFIG)
    else:
        raise SystemExit("side must be legacy or new")
    config = load_config_at(config_path)
    db_path = project_path(config["task_runtime"]["db_path"])
    root = project_path(config["scheduler_ownership"]["state_dir"])
    before = _store_counts(db_path), _registry_snapshot(root)
    if spec_path is None:
        spec_path = NEW_ROOT / "scheduled_slot_spec.json"
        spec_path.write_text(json.dumps(slot_task_spec(config, planned_slot)), encoding="utf-8")
    if side == "legacy":
        locked_venv = PROJECT_ROOT / ".venv312"
        if not locked_venv.exists():
            locked_venv.symlink_to(Path(sys.executable).parent.parent)
        invocation = _run([
            "/opt/r25-pwsh/pwsh", "-NoProfile", "-NonInteractive", "-File",
            "/app/scripts/run_daily_research.ps1", "-Mode", "daily", "-TriggerSource", "scheduled",
            "-Config", config_path, "-ScheduleId", SCHEDULE_ID, "-PlannedSlot", planned_slot,
            "-ScopeVersion", SCOPE_VERSION, "-ScheduledTaskSpec", str(spec_path),
            "-ScheduledExecutionMode", "replay"])
    else:
        invocation = run_cli([
            "scheduled-task-submit", "--config", config_path, "--scheduler", "container-worker",
            "--schedule-id", SCHEDULE_ID, "--planned-slot", planned_slot, "--scope-version", SCOPE_VERSION,
            "--spec", str(spec_path), "--execution-mode", "replay"])
    after = _store_counts(db_path), _registry_snapshot(root)
    accepted = invocation["returncode"] == 0
    outcome = {"side": side, "planned_slot": planned_slot, "config": config_path,
               "registry": str(root), "task_db": str(db_path), "accepted": accepted,
               "submission": invocation, "store_before": before[0], "store_after_submit": after[0],
               "registry_before": before[1], "registry_after": after[1]}
    if not accepted:
        try:
            outcome.update(json.loads(invocation["stderr_tail"]))
        except ValueError:
            pass
        outcome["no_writes"] = after == before
        return outcome

    outcome.update(json.loads(invocation["stdout_tail"]))
    worker = None
    if run:
        worker = run_cli(["worker-service", "--config", config_path,
                          "--worker-id", "w-{0}-{1}".format(side, planned_slot[:10]),
                          "--ttl-seconds", "60", "--idle-sleep-seconds", "0.1",
                          "--max-tasks", "1"])
    outcome["worker"] = worker
    outcome["accounting"] = intent_accounting(db_path, outcome["intent"])
    outcome["store_after"] = _store_counts(db_path)
    return outcome


def _registry_snapshot(root: Path) -> dict:
    from stock_agent.scheduler_ownership import load_state

    state = load_state(root)
    events_path = root / "events.jsonl"
    events = [json.loads(line)["event"] for line in events_path.read_text(encoding="utf-8").splitlines()]
    return {"owner": state["active_scheduler"], "generation": state["generation"],
            "frozen": state["intake_frozen"], "audit_events": events, "sha256": _registry_hash(root)}


def _scheduled_entry_ok(entry: dict, *, expect_status: str) -> bool:
    if not entry.get("accepted"):
        return False
    worker = entry.get("worker") or {"returncode": 0}
    return (worker.get("returncode") == 0
            and entry["accounting"]["tasks_with_intent"] == 1
            and entry["accounting"]["tasks"][0]["status"] == expect_status)


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
        DATA / "incoming/r25_cutover_announcement.html",
        *sorted(path for path in (DATA / "personal_evidence").rglob("*") if path.is_file()),
    ]
    files = {}
    for target in targets:
        files[target.relative_to(DATA).as_posix()] = (
            hashlib.sha256(target.read_bytes()).hexdigest() if target.is_file() else None)
    return {"root": str(DATA), "files": files}


def _run(command: list, *, timeout: int = 300) -> dict:
    completed = subprocess.run(command, capture_output=True, text=True,
                               timeout=timeout, cwd=str(PROJECT_ROOT))
    return {"args": command, "returncode": completed.returncode,
            "stdout_tail": completed.stdout[-4000:], "stderr_tail": completed.stderr[-4000:]}


def run_cli(args: list, *, timeout: int = 300) -> dict:
    return _run([sys.executable, "-B", "-m", "stock_agent", *args], timeout=timeout)


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
        "scheduler_id": "legacy-powershell",
        "state_dir": _rel(SHARED_REGISTRY),
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
        SHARED_REGISTRY, first_scheduler=LEGACY_SCHEDULER_ID,
        actor="r25-drill", reason="cutover rehearsal: legacy state first")

    open_legacy_store().close()
    legacy_entry = _scheduled_entry("legacy", LEGACY_PLANNED_SLOT)
    if not _scheduled_entry_ok(legacy_entry, expect_status="succeeded"):
        emit({"phase": "seed", "ok": False, "legacy_entry": legacy_entry})
        return 3
    store = open_legacy_store()
    try:
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
          "legacy_entry": legacy_entry,
          "evidence": evidence,
          "slot": {"schedule_id": SCHEDULE_ID, "planned_slot": LEGACY_PLANNED_SLOT,
                   "scope_version": SCOPE_VERSION, "intent": legacy_entry["intent"],
                   "task_id": legacy_entry["task_id"], "final_status": "succeeded"},
          "registry": {"active": registry["active_scheduler"], "generation": registry["generation"]},
          "accounting": legacy_entry["accounting"]})
    return 0


def _seed_evidence_task(config: dict) -> dict:
    """The reviewed precheck evidence fixture, adapted to this drill's config:
    one synthetic announcement -> located excerpt -> task referencing it ->
    real claim + run with the personal vault view."""
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
        fact_content_sha = hashlib.sha256(
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


def _public_switch(config_path: str, target: str, generation: int) -> dict:
    return run_cli(["cutover-switch", "--to", target, "--actor", "r25-drill",
                    "--reason", "R25 isolated rehearsal", "--cutover-id", CUTOVER_ID,
                    "--config", config_path, "--expected-generation", str(generation)])


def cmd_switch_blocked(mode: str) -> int:
    """Inject one condition, then assert both public commands preserve the registry."""
    from stock_agent.audit_log import atomic_write_text
    from stock_agent.scheduler_ownership import disable_legacy, load_state

    root = SHARED_REGISTRY
    disable_legacy(root, actor="r25-drill", cutover_id=CUTOVER_ID, simulated=True)
    config = legacy_config()
    injection = {}
    if mode == "unknown":
        store = open_legacy_store()
        try:
            task = store.claim_next("w-blocked-unknown", ttl_seconds=60)
            kwargs = {"worker_id": "w-blocked-unknown", "fencing_token": int(task["fencing_token"])}
            attempt = store.prepare_invocation_attempt(
                str(task["task_id"]), round_index=1, provider="synthetic",
                request_model="replay", prompt_sha256="0" * 64, **kwargs)
            store.mark_invocation_dispatched(str(attempt["attempt_id"]), **kwargs)
            injection = {"attempt_id": attempt["attempt_id"], "state": "dispatched"}
        finally:
            store.close()
    elif mode == "dual":
        state = load_state(root)
        seen_at = _datetime.datetime.now().astimezone().isoformat(timespec="seconds")
        state["actors_seen"]["container-worker"] = seen_at
        atomic_write_text(root / "state.json", json.dumps(state))
        injection = {"foreign_heartbeat": seen_at}
    elif mode == "schema":
        config["scheduler_ownership"]["rollback_supported_schema"] = "1.0"
    elif mode == "capacity":
        config["scheduler_ownership"]["min_free_bytes"] = 10 ** 15
    else:
        raise SystemExit("unknown injection mode: {0}".format(mode))

    config_path = NEW_ROOT / "watchlist.cutover-negative.json"
    config_path.write_text(json.dumps(config), encoding="utf-8")
    before = _registry_snapshot(root)
    gate = run_cli(["cutover-gate", "--config", _rel(config_path)])
    after_gate = _registry_snapshot(root)
    switch = _public_switch(_rel(config_path), "container-worker", 1)
    after = _registry_snapshot(root)
    fragment = {"unknown": "unexplained unknown", "dual": "dual scheduler",
                "schema": "non-rollbackable schema", "capacity": "insufficient capacity"}[mode]
    gate_blockers = json.loads(gate["stdout_tail"])["blockers"]
    ok = (gate["returncode"] == 1 and switch["returncode"] == 21
          and len(gate_blockers) == 1 and fragment in gate_blockers[0]
          and fragment in switch["stderr_tail"] and before == after_gate == after)
    emit({"phase": "switch_blocked", "ok": ok, "mode": mode, "injection": injection,
          "before": before, "after_gate": after_gate, "after_switch": after,
          "gate": gate, "switch": switch})
    return 0 if ok else 18


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
    work["scheduler_ownership"]["scheduler_id"] = "container-worker"
    work["backup_restore"] = dict(restore_config["backup_restore"])
    NEW_WORK_CONFIG.write_text(json.dumps(work, ensure_ascii=False, indent=2), encoding="utf-8")

    source_before = source_fingerprint()
    emit({"phase": "restore_new", "ok": True, "worker": result,
          "restored_root": str(restored),
          "new_work_config": str(NEW_WORK_CONFIG),
          "shared_registry": str(SHARED_REGISTRY),
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
    before = _registry_snapshot(SHARED_REGISTRY)
    result = _public_switch(_rel(NEW_WORK_CONFIG), "container-worker", 1)
    after = _registry_snapshot(SHARED_REGISTRY)
    ok = result["returncode"] == 22 and "legacy entry is not disabled" in result["stderr_tail"] and before == after
    emit({"phase": "switch_refuse_no_marker", "ok": ok, "switch": result,
          "before": before, "after": after})
    return 0 if ok else 9


def cmd_switch_new() -> int:
    from stock_agent.scheduler_ownership import disable_legacy

    marker = disable_legacy(SHARED_REGISTRY, actor="r25-drill", cutover_id=CUTOVER_ID, simulated=True)
    gate = run_cli(["cutover-gate", "--config", _rel(NEW_WORK_CONFIG)])
    before = _registry_snapshot(SHARED_REGISTRY)
    result = _public_switch(_rel(NEW_WORK_CONFIG), "container-worker", 1)
    after = _registry_snapshot(SHARED_REGISTRY)
    ok = (gate["returncode"] == result["returncode"] == 0
          and after["owner"] == "container-worker" and after["generation"] == 2)
    emit({"phase": "switch_new", "ok": ok, "legacy_disabled_marker": marker,
          "gate": gate, "switch": result, "before": before, "after": after})
    return 0 if ok else 10


def cmd_replay_slot(side: str = "new", planned_slot: str = LEGACY_PLANNED_SLOT) -> int:
    """Start a fresh scheduler process and compare all per-intent accounting."""
    db = new_tasks_db()
    intent = slot_key(planned_slot)
    before = intent_accounting(db, intent)
    entry = _scheduled_entry(side, planned_slot, run=False)
    after = intent_accounting(db, intent)
    ok = (entry.get("dedup") == "DUPLICATE" and before == after
          and before["tasks_with_intent"] == 1
          and before["tasks"][0]["status"] == "succeeded"
          and entry["task_id"] == before["tasks"][0]["task_id"])
    emit({"phase": "replay_slot", "ok": ok, "entry": entry, "before": before, "after": after})
    return 0 if ok else 11


def cmd_replay_equivalent_slots() -> int:
    """Replay the restored payload with two equivalent spellings and one changed request."""
    from contextlib import closing

    utc_slot = "2026-11-20T00:30:00Z"
    intent = slot_key(LEGACY_PLANNED_SLOT)
    db_path = new_tasks_db()
    before = intent_accounting(db_path, intent)
    task_id = before["tasks"][0]["task_id"]
    with closing(open_new_store(read_only=True)) as store:
        saved_spec = store.get_task(task_id)["input_payload"]
    same_path = NEW_ROOT / "scheduled_slot_restored_spec.json"
    same_path.write_text(json.dumps(saved_spec), encoding="utf-8")
    changed_path = NEW_ROOT / "scheduled_slot_changed_spec.json"
    changed_path.write_text(
        json.dumps(dict(saved_spec, research_question="R25 changed research request")), encoding="utf-8")

    attempts = []
    for planned_slot in (LEGACY_PLANNED_SLOT, utc_slot):
        entry = _scheduled_entry("new", planned_slot, run=False, spec_path=same_path)
        attempts.append(entry)
    conflict = _scheduled_entry("new", utc_slot, run=False, spec_path=changed_path)
    after = intent_accounting(db_path, intent)
    with closing(open_new_store(read_only=True)) as store:
        payload_unchanged = store.get_task(task_id)["input_payload"] == saved_spec
    ok = (saved_spec["scheduled_slot"]["planned_slot"] == LEGACY_PLANNED_SLOT
          and all(entry["submission"]["returncode"] == 0
                  and entry["dedup"] == "DUPLICATE"
                  and entry["task_id"] == task_id
                  and entry["accounting"] == before for entry in attempts)
          and conflict["submission"]["returncode"] == 1
          and "already names a different request" in conflict["submission"]["stderr_tail"]
          and conflict["no_writes"] and before == after and payload_unchanged)
    emit({"phase": "replay_equivalent_slots", "ok": ok, "before": before, "after": after,
          "stored_planned_slot": saved_spec["scheduled_slot"]["planned_slot"],
          "payload_unchanged": payload_unchanged, "attempts": attempts, "conflict": conflict})
    return 0 if ok else 11


def cmd_new_slot(index: int, run: bool = True) -> int:
    """Post-cutover incremental slot: a NEW intent on a LATER business day,
    submitted and executed through the container side's normal scheduled
    entry. ``run=False`` only enqueues it (the frozen-drain demo needs one
    queued)."""
    planned = "2026-11-2{0}T08:30:00+08:00".format(1 + index)
    entry = _scheduled_entry("new", planned, run=run)
    expected_status = "succeeded" if run else "queued"
    ok = _scheduled_entry_ok(entry, expect_status=expected_status)
    emit({"phase": "new_slot", "ok": ok, "index": index,
          "slot": {"planned_slot": planned,
                   "intent": entry.get("intent"),
                   "task_id": entry.get("task_id")},
          "dedup": entry.get("dedup"),
          "worker": entry.get("worker"),
          "accounting": entry.get("accounting")})
    return 0 if ok else 12


# --------------------------------------------------------------------------- #
# rollback
# --------------------------------------------------------------------------- #
def cmd_freeze() -> int:
    result = run_cli(["cutover-freeze", "--actor", "r25-drill", "--config", _rel(NEW_WORK_CONFIG),
                      "--reason", "R25 rollback rehearsal: freeze new intake", "--cutover-id", CUTOVER_ID])
    emit({"phase": "freeze", "ok": result["returncode"] == 0,
          "cli": result, "registry": _registry_snapshot(SHARED_REGISTRY)})
    return result["returncode"]


def cmd_frozen_submit() -> int:
    planned = "2026-11-26T08:30:00+08:00"
    entries = [_scheduled_entry(side, planned, run=False) for side in ("legacy", "new")]
    ok = all(entry["no_writes"] and entry["submission"]["returncode"] == 20
             and entry["refusal_kind"] == expected
             for entry, expected in zip(entries, ("DualSchedulerError", "IntakeFrozenError")))
    emit({"phase": "frozen_submit", "ok": ok, "entries": entries})
    return 0 if ok else 13


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
    result = run_cli(["cutover-reconcile-rollback", "--config", _rel(NEW_WORK_CONFIG),
                      "--baseline-config", LEGACY_CONFIG_REL])
    emit({"phase": "increments", "ok": result["returncode"] == 0, "cli": result,
          "manifest": json.loads(result["stdout_tail"]) if result["returncode"] == 0 else None})
    return result["returncode"]


def cmd_rollback_refused(fragment: str) -> int:
    before = _registry_snapshot(SHARED_REGISTRY)
    result = _public_switch(_rel(NEW_WORK_CONFIG), "legacy-powershell", 2)
    after = _registry_snapshot(SHARED_REGISTRY)
    ok = result["returncode"] == 21 and fragment in result["stderr_tail"] and before == after
    emit({"phase": "rollback_refused", "ok": ok, "switch": result, "before": before, "after": after})
    return 0 if ok else 16


def cmd_rollback() -> int:
    from stock_agent.scheduler_ownership import enable_legacy

    before = _registry_snapshot(SHARED_REGISTRY)
    result = _public_switch(_rel(NEW_WORK_CONFIG), "legacy-powershell", 2)
    if result["returncode"]:
        emit({"phase": "rollback", "ok": False, "switch": result})
        return 16
    # The original state tree remains the rollback reference. Legacy now uses
    # the reconciled task store, so both intent keys and SSE cursors survive.
    legacy = new_work_config()
    legacy["scheduler_ownership"]["scheduler_id"] = "legacy-powershell"
    LEGACY_RESUME_CONFIG.write_text(json.dumps(legacy), encoding="utf-8")
    enable_legacy(SHARED_REGISTRY, actor="r25-drill", reason="simulated rollback task restore")
    thaw = run_cli(["cutover-thaw", "--actor", "r25-drill", "--config", _rel(LEGACY_RESUME_CONFIG),
                    "--reason", "R25 rollback rehearsal complete"])
    after = _registry_snapshot(SHARED_REGISTRY)
    ok = (thaw["returncode"] == 0 and after["owner"] == "legacy-powershell"
          and after["generation"] == 3 and not after["frozen"])
    emit({"phase": "rollback", "ok": ok, "switch": result, "thaw": thaw, "before": before,
          "after": after, "legacy_resume_config": str(LEGACY_RESUME_CONFIG)})
    return 0 if ok else 16


def cmd_outbox_after(sequence: int, limit: int, expected: list[int]) -> int:
    store = open_new_store(read_only=True)
    try:
        events = store.events_after(sequence, limit=limit)
    finally:
        store.close()
    actual = [event["sequence"] for event in events]
    emit({"phase": "outbox_after", "cursor": sequence, "limit": limit,
          "sequences": actual, "expected": expected, "ok": actual == expected})
    return 0 if actual == expected else 19


def cmd_source_intact() -> int:
    current = source_fingerprint()
    saved_path = NEW_ROOT / "snapshot_fingerprint.json"
    saved = json.loads(saved_path.read_text(encoding="utf-8")) if saved_path.is_file() else {}
    files_equal = {name: (saved.get("files") or {}).get(name) == digest
                   for name, digest in current["files"].items()}
    ok = bool(saved) and current == saved and all(current["files"].values())
    emit({"phase": "source_intact", "ok": ok, "current_fingerprint": current,
          "snapshot_fingerprint": saved, "per_file_equal": files_equal})
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
    # Verify the original source also survives legacy takeover.
    current = source_fingerprint()
    saved_path = NEW_ROOT / "snapshot_fingerprint.json"
    saved = json.loads(saved_path.read_text(encoding="utf-8")) if saved_path.is_file() else {}
    per_file_equal = {name: (saved.get("files") or {}).get(name) == digest
                      for name, digest in current["files"].items()}
    emit({"phase": "final_counts",
          "legacy": counts(LEGACY_TASKS_DB),
          "new": counts(new_tasks_db()),
          "legacy_slot_accounting": intent_accounting(LEGACY_TASKS_DB, intent),
          "new_slot_accounting": intent_accounting(new_tasks_db(), intent),
          "shared_registry": _registry_snapshot(SHARED_REGISTRY),
          "post_takeover_fingerprint_equal": per_file_equal})
    return 0 if saved and current == saved and all(current["files"].values()) else 17


def _emit_entry(args) -> int:
    if not args:
        emit({"phase": "slot_submit", "ok": False, "error": "side required"})
        return 2
    side = args[0]
    planned = args[1] if len(args) > 1 else "2026-11-27T08:30:00+08:00"
    entry = _scheduled_entry(side, planned, run=not (len(args) > 2 and args[2] == "norun"))
    emit({"phase": "slot_submit", "side": side, "entry": entry,
          "ok": bool(entry.get("accepted"))})
    return entry["submission"]["returncode"]


COMMANDS = {
    "reset": lambda args: cmd_reset(),
    "seed": lambda args: cmd_seed(int(args[0]) if args else 5),
    "slot_info": lambda args: emit({"phase": "slot_info", "schedule_id": SCHEDULE_ID,
                                    "planned_slot": LEGACY_PLANNED_SLOT,
                                    "scope_version": SCOPE_VERSION,
                                    "intent": slot_key(LEGACY_PLANNED_SLOT)}) or 0,
    "drain": lambda args: cmd_drain(),
    "fingerprint": lambda args: cmd_fingerprint(args[0] if args else "unlabeled"),
    "switch_blocked": lambda args: cmd_switch_blocked(args[0]),
    "snapshot": lambda args: cmd_snapshot(),
    "restore_new": lambda args: cmd_restore_new(),
    "reconcile": lambda args: cmd_reconcile(),
    "switch_refuse_no_marker": lambda args: cmd_switch_refuse_no_marker(),
    "switch_new": lambda args: cmd_switch_new(),
    "replay_slot": lambda args: cmd_replay_slot(*args),
    "replay_equivalent_slots": lambda args: cmd_replay_equivalent_slots(),
    "new_slot": lambda args: cmd_new_slot(int(args[0]) if args else 1,
                                          run=not (len(args) > 1 and args[1] == "norun")),
    "freeze": lambda args: cmd_freeze(),
    "frozen_submit": lambda args: cmd_frozen_submit(),
    "drain_frozen": lambda args: cmd_drain_frozen(),
    "increments": lambda args: cmd_increments(),
    "rollback": lambda args: cmd_rollback(),
    "rollback_refused": lambda args: cmd_rollback_refused(args[0]),
    "outbox_after": lambda args: cmd_outbox_after(int(args[0]), int(args[1]), [int(v) for v in args[2:]]),
    "source_intact": lambda args: cmd_source_intact(),
    "final_counts": lambda args: cmd_final_counts(),
    "slot_submit": lambda args: _emit_entry(args),
}


def main() -> int:
    if len(sys.argv) < 2 or sys.argv[1] not in COMMANDS:
        print(__doc__, file=sys.stderr)
        return 2
    os.chdir(PROJECT_ROOT)  # relative data/... paths resolve against /app
    return COMMANDS[sys.argv[1]](sys.argv[2:])


if __name__ == "__main__":
    raise SystemExit(main())
