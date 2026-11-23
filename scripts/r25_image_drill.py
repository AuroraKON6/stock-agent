#!/usr/bin/env python3
"""R25 image verification helper — runs INSIDE the candidate image (KONM-38).

The image ships no drill code, so the host driver feeds this file to the
image's own interpreter over stdin:

    docker compose ... run --rm -T research-api python -B - <command> ... \
        < scripts/r25_image_drill.py

Only ``stock_agent`` from the image is imported; every state write goes
through the same ``project_path`` resolution the public CLI uses. Nothing from
the host is mounted except the synthetic, read-only config directory.

Subcommands (each prints one JSON object; exit 0 on success):
    layout                         UID, resolved roots, mounts and permissions
    init <first-scheduler>         empty signal/query stores + ownership registry
    spec <config> <schedule> <slot> <scope> <out>   write one slot task spec
    evidence <config>              one synthetic personal-evidence task, run
    registry <config>              resolved ownership registry + its state
    resolved <config>              registry / task store paths with permissions
    counts <db> [intent]           read-only task, outbox, attempt, budget counts
    outbox <db> <sequence> <limit> SSE cursor read through TaskStore
    compare <live-data-tree> <live-vault> <restored-data-tree>
                                     full-column rows + evidence digests
    intent <schedule> <slot> <scope>    business-intent key of one slot
    provenance <binding-file> <digest>  verify shipped PROVENANCE vs the binding
    wal-fixture <db>               copy a quiescent main DB and uncheckpointed WAL
    wal-fingerprint <db>           hash both files from the read-only cross mount
    wal-retry-fixture <source> <db> <staged-wal>
                                  clone a valid baseline and stage its uncheckpointed WAL
"""
from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import shutil
import stat
import sys
import tempfile
from contextlib import closing
from pathlib import Path

sys.dont_write_bytecode = True
sys.path.insert(0, os.getcwd())


def emit(payload: dict) -> None:
    print(json.dumps(payload, ensure_ascii=False, sort_keys=True), flush=True)


def _project_path(relative: str) -> Path:
    from stock_agent.config import project_path

    return project_path(relative)


def _relative(path: Path) -> str:
    from stock_agent.config import project_relative_path

    return project_relative_path(path)


def _load_config(relative: str) -> dict:
    from stock_agent.config import load_config

    return load_config(relative)


def _mount_for(path: Path) -> dict:
    """Longest-prefix entry of /proc/self/mounts covering ``path``."""
    best = {"mountpoint": "", "fstype": "", "options": ""}
    try:
        with open("/proc/self/mounts", encoding="utf-8") as handle:
            for line in handle:
                fields = line.split()
                if len(fields) < 4:
                    continue
                mountpoint = fields[1].replace("\\040", " ")
                if path.as_posix() == mountpoint or path.as_posix().startswith(mountpoint.rstrip("/") + "/"):
                    if len(mountpoint) > len(best["mountpoint"]):
                        best = {"mountpoint": mountpoint, "fstype": fields[2], "options": fields[3]}
    except OSError:
        pass
    return best


def _file_status(path: Path, *, directory: bool = False) -> dict:
    target = path if path.exists() else path.parent
    info = target.stat()
    return {
        "path": path.as_posix(),
        "exists": path.exists(),
        "owner_uid": info.st_uid,
        "owner_gid": info.st_gid,
        "mode": oct(stat.S_IMODE(info.st_mode)),
        "readable": os.access(path, os.R_OK),
        "writable": os.access(path, os.W_OK) if path.exists() else os.access(path.parent, os.W_OK),
        "checked": "itself" if (path.exists() or not directory) else "parent",
    }


def cmd_layout() -> int:
    import pwd
    import grp

    from stock_agent.runtime_layout import resolve_layout

    layout = resolve_layout()
    roots = {
        "config_root": layout.config_root, "state_root": layout.state_root,
        "evidence_root": layout.evidence_root, "report_root": layout.report_root,
        "temp_root": layout.temp_root,
    }
    samples = {}
    for relative in ("config/watchlist.json", "data/task_runtime/tasks.sqlite3",
                     "data/scheduler_ownership/state.json", "data/evidence_vault/objects/sha256",
                     "data/personal_evidence", "reports/backup_restore/latest.json", "logs/scheduled"):
        resolved = _project_path(relative)
        samples[relative] = {
            "resolved": resolved.as_posix(),
            "project_relative_back": _relative(resolved),
            "mount": _mount_for(resolved),
        }
    probes = {}
    for name, root in roots.items():
        if name == "temp_root":
            continue
        probe = root / ".r25_write_probe"
        try:
            root.mkdir(parents=True, exist_ok=True)
            probe.write_text("probe", encoding="utf-8")
            persisted = probe.read_text(encoding="utf-8") == "probe"
            probe.unlink()
            probes[name] = {"writable": True, "read_back": persisted}
        except OSError as exc:
            probes[name] = {"writable": False, "error": "{0}: {1}".format(type(exc).__name__, exc)}
    # The image root must stay read-only for this user.
    try:
        Path("/app/.r25_root_probe").write_text("x", encoding="utf-8")
        root_writable = True
    except OSError:
        root_writable = False
    emit({"phase": "layout", "uid": os.getuid(), "gid": os.getgid(),
          "user": pwd.getpwuid(os.getuid()).pw_name, "group": grp.getgrgid(os.getgid()).gr_name,
          "cwd": os.getcwd(), "python": sys.version.split()[0],
          "app_roots": {name: os.environ.get(name, "") for name in (
              "APP_CONFIG_ROOT", "APP_STATE_ROOT", "APP_EVIDENCE_ROOT", "APP_REPORT_ROOT", "APP_TEMP_ROOT")},
          "layout": layout.snapshot(),
          "roots": {name: {**_file_status(root, directory=True), "mount": _mount_for(root), **probes.get(name, {})}
                    for name, root in roots.items()},
          "image_root_writable": root_writable,
          "project_path_samples": samples})
    return 0


def cmd_init(first_scheduler: str) -> int:
    from stock_agent.scheduler_ownership import initialize_registry

    created = []
    for relative in ("data/stock_agent.sqlite3", "data/kimi/query_intake.sqlite3"):
        target = _project_path(relative)
        target.parent.mkdir(parents=True, exist_ok=True)
        if not target.exists():
            sqlite3.connect(str(target)).close()
            created.append(relative)
    # Empty roster/restrictions so the replay task's operations inputs load.
    vault = _project_path("data/vault")
    vault.mkdir(parents=True, exist_ok=True)
    for name, payload in (("restrictions.json", {"version": "empty-drill-restrictions", "restricted": []}),
                          ("roster.json", {"version": "empty-drill-roster", "members": []})):
        if not (vault / name).exists():
            (vault / name).write_text(json.dumps(payload), encoding="utf-8")
            created.append("data/vault/" + name)
    registry = _project_path("data/scheduler_ownership")
    state = initialize_registry(registry, first_scheduler=first_scheduler, actor="r25-image-drill",
                                reason="isolated image verification: synthetic data only")
    emit({"phase": "init", "created_empty_stores": created, "registry": registry.as_posix(),
          "active_scheduler": state["active_scheduler"], "generation": state["generation"]})
    return 0


def cmd_spec(config_relative: str, schedule_id: str, planned_slot: str, scope: str, out_relative: str) -> int:
    from stock_agent.task_supply import build_task_spec

    config = _load_config(config_relative)
    spec = build_task_spec(
        research_question="R25 镜像验证定时研究 {0} {1} {2}".format(schedule_id, planned_slot, scope),
        symbols=["600519"], execution_mode="replay", config=config)
    spec["bound_signal_ids"] = []
    spec["public_context"]["evidence_summaries"] = {}
    spec["replay_response"] = {
        "schema_version": "1.0", "task_id": "placeholder", "summary": "结论",
        "key_findings": [], "unknowns": [], "evidence_refs": [], "human_questions": [],
        "model_self_assessed_confidence": 0.3}
    spec["scheduled_slot"] = {"schedule_id": schedule_id, "planned_slot": planned_slot,
                              "scope_version": scope}
    target = _project_path(out_relative)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(spec, ensure_ascii=False, indent=2), encoding="utf-8")
    emit({"phase": "spec", "path": target.as_posix(), "scheduled_slot": spec["scheduled_slot"]})
    return 0


def cmd_evidence(config_relative: str) -> int:
    """The reviewed KONM-27/KONM-28 fixture: synthetic announcement -> located
    excerpt -> task whose approved facts reference it -> real claim + run."""
    from stock_agent.evidence_excerpts import _document_text, create_excerpt, excerpt_selector
    from stock_agent.personal_evidence import import_personal_document, personal_vault_view
    from stock_agent.research_worker import run_research_task
    from stock_agent.task_runtime import TaskStore

    config = _load_config(config_relative)
    owner = "lead-analyst"
    incoming = _project_path("data/incoming")
    incoming.mkdir(parents=True, exist_ok=True)
    announcement = incoming / "r25_image_announcement.html"
    announcement.write_text(
        "<html><body><p>合成公告（非真实披露）：营业收入为 1,234.56 万元。</p></body></html>",
        encoding="utf-8")
    imported = import_personal_document(
        config, owner, str(announcement),
        source_url="https://www.cninfo.com.cn?r25-image-fixture",
        title="R25 镜像验证合成公告（非真实披露）", published_at="2026-11-23T09:00:00+08:00")
    document = imported["document"]
    view = personal_vault_view(config, owner)
    text, _version = _document_text(view, document)
    start = text.index("营业收入为")
    excerpt = create_excerpt(view, document["document_id"], start, text.index("。", start) + 1)
    selector = excerpt_selector(excerpt)

    from stock_agent.task_supply import build_task_spec

    spec = build_task_spec(research_question="R25 镜像验证证据引用",
                           symbols=["600519"], execution_mode="replay", config=config)
    spec["bound_signal_ids"] = []
    spec["public_context"]["evidence_summaries"] = {
        document["document_id"]: {"summary": "R25 镜像验证合成公告正文", "sha256": document["sha256"]}}
    spec["replay_response"] = {
        "schema_version": "1.0", "task_id": "placeholder", "summary": "结论",
        "key_findings": [], "unknowns": [], "evidence_refs": [], "human_questions": [],
        "model_self_assessed_confidence": 0.3}
    spec["approved_facts"] = [selector]
    db = _project_path(str((config.get("task_runtime") or {}).get("db_path") or "data/task_runtime/tasks.sqlite3"))
    store = TaskStore(db)
    try:
        task, _dedup = store.create_task(title="R25-image-evidence", input_payload=spec,
                                         execution_mode="replay")
        task_id = str(task["task_id"])
        claimed = store.claim_next("w-r25-evidence", ttl_seconds=60)
        if not claimed or str(claimed["task_id"]) != task_id:
            emit({"phase": "evidence", "ok": False, "error": "claim_next did not return the evidence task"})
            return 3
        outcome = run_research_task(store, task_id, worker_id="w-r25-evidence", config=view, ttl_seconds=60)
        final = store.get_task(task_id)
    finally:
        store.close()
    facts = (((final.get("model_call") or {}).get("outbound_context") or {}).get("verified_facts") or [])
    fact = facts[0] if facts else {}
    emit({"phase": "evidence", "ok": str(final.get("status")) == "succeeded" == str(outcome.get("status")),
          "task_id": task_id, "final_status": final.get("status"), "owner": owner,
          "announcement_path": str(announcement),
          "document_id": document["document_id"], "document_sha256": document["sha256"],
          "excerpt_sha256": selector["excerpt_sha256"],
          "verified_fact_document_sha256": fact.get("document_sha256", "")})
    return 0 if str(final.get("status")) == "succeeded" else 3


def _registry_state(root: Path) -> dict:
    from stock_agent.scheduler_ownership import load_state

    state = load_state(root)
    events = []
    events_path = root / "events.jsonl"
    if events_path.is_file():
        events = [json.loads(line) for line in events_path.read_text(encoding="utf-8").splitlines()]
    digest = lambda path: hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else None  # noqa: E731
    return {"owner": state["active_scheduler"], "generation": state["generation"],
            "frozen": state["intake_frozen"], "freeze_id": state.get("freeze_id", ""),
            "cutover_id": state.get("cutover_id", ""), "audit_events": [event["event"] for event in events],
            "reconciliation_events": [event for event in events
                                      if event["event"].startswith("rollback_reconciliation_")],
            "legacy_disabled_marker": (root / "legacy_disabled.json").is_file(),
            "sha256": {name: digest(root / name) for name in
                       ("state.json", "events.jsonl", "rollback_increments.json",
                        "rollback_increments.previous.json")}}


def _resolved(config_relative: str) -> dict:
    from stock_agent.scheduler_ownership import scheduler_ownership_config

    config = _load_config(config_relative)
    ownership = scheduler_ownership_config(config)
    registry = _project_path(ownership["state_dir"])
    tasks_db = _project_path(str((config.get("task_runtime") or {}).get("db_path") or "data/task_runtime/tasks.sqlite3"))
    return {
        "config": config_relative,
        "scheduler_id": ownership["scheduler_id"],
        "registry_root": registry.as_posix(),
        "task_store": tasks_db.as_posix(),
        "task_store_project_relative": _relative(tasks_db),
        "registry": {**_file_status(registry, directory=True), "mount": _mount_for(registry)},
        "registry_files": {name: _file_status(registry / name) for name in
                           ("state.json", "events.jsonl") if (registry / name).exists()},
        "task_store_file": {**_file_status(tasks_db), "mount": _mount_for(tasks_db)},
        "task_store_dir": _file_status(tasks_db.parent, directory=True),
    }


def cmd_registry(config_relative: str) -> int:
    from stock_agent.scheduler_ownership import scheduler_ownership_config

    registry = _project_path(scheduler_ownership_config(_load_config(config_relative))["state_dir"])
    emit({"phase": "registry", "config": config_relative, "registry_root": registry.as_posix(),
          **_registry_state(registry)})
    return 0


def cmd_resolved(*configs: str) -> int:
    resolved = [_resolved(config) for config in configs]
    ok = all(item["registry_root"] == resolved[0]["registry_root"] for item in resolved)
    emit({"phase": "resolved", "shared_registry": ok, "configs": resolved})
    return 0 if ok else 5


def cmd_provenance(binding_path_value: str, observed_digest: str) -> int:
    """Verify the image's shipped PROVENANCE.json against the external binding
    (read from the read-only cross mount) and the digest the host observed."""
    from stock_agent.release_provenance import (
        PROVENANCE_FILENAME, provenance_sha256, verify_image_provenance,
    )

    raw_provenance = (Path.cwd() / PROVENANCE_FILENAME).read_bytes()
    shipped = json.loads(raw_provenance)
    binding_path = Path(binding_path_value)
    binding = json.loads(binding_path.read_text(encoding="utf-8"))
    expected = str(binding.get("provenance_sha256") or "")
    verdict = verify_image_provenance(shipped, expected, observed_digest, binding)
    static = {relative: (Path.cwd() / relative).is_file() for relative in
              ("pyproject.toml", "requirements.lock.txt", "config/watchlist.example.json",
               "requirements.linux-py312.lock.txt", "stock_agent/release_provenance.py")}
    emit({"phase": "provenance", "shipped_provenance": shipped,
          "shipped_provenance_sha256": provenance_sha256(shipped),
          "provenance_file_sha256": hashlib.sha256(raw_provenance).hexdigest(), "binding": binding,
          "observed_digest": observed_digest, "verdict": verdict, "required_static_present": static})
    return 0 if verdict["verified"] and all(static.values()) else 6


def cmd_intent(schedule_id: str, planned_slot: str, scope: str) -> int:
    """The business-intent key two sides must derive identically for one slot."""
    from stock_agent.worker_service import scheduled_slot_key

    key = scheduled_slot_key(schedule_id, planned_slot, scope_version=scope)
    emit({"phase": "intent", "intent": key, "schedule_id": schedule_id,
          "planned_slot": planned_slot, "scope_version": scope})
    return 0


def _connect(db: Path) -> sqlite3.Connection:
    from stock_agent.scheduler_ownership import _open_readonly

    return _open_readonly(db)


def cmd_counts(db_value: str, intent: str = "") -> int:
    db = _project_path(db_value) if not db_value.startswith("/") else Path(db_value)
    connection = _connect(db)
    try:
        by_status = dict(connection.execute(
            "SELECT status, COUNT(*) FROM research_tasks GROUP BY status").fetchall())
        payload = {
            "db": db.as_posix(), "tasks_by_status": by_status,
            "tasks_total": sum(by_status.values()),
            "outbox_events": int(connection.execute("SELECT COUNT(*) FROM task_outbox").fetchone()[0]),
            "outbox_last_sequence": int(connection.execute("SELECT COALESCE(MAX(sequence), 0) FROM task_outbox").fetchone()[0]),
            "outbox_by_type": dict(connection.execute(
                "SELECT event_type, COUNT(*) FROM task_outbox GROUP BY event_type").fetchall()),
            "invocation_attempts": int(connection.execute("SELECT COUNT(*) FROM task_invocation_attempts").fetchone()[0]),
            "budget_linked_attempts": int(connection.execute(
                "SELECT COUNT(*) FROM task_invocation_attempts WHERE budget_event_id <> ''").fetchone()[0]),
            "tables": {table: int(connection.execute("SELECT COUNT(*) FROM {0}".format(table)).fetchone()[0])
                       for table in ("research_tasks", "task_outbox", "task_input_snapshots", "task_platform_runs",
                                     "task_invocation_attempts", "principal_verifications", "workflow_dispatches")},
        }
        if intent:
            rows = connection.execute(
                "SELECT task_id, status, attempt_count FROM research_tasks WHERE business_intent_id = ?",
                (intent,)).fetchall()
            payload["intent"] = intent
            payload["intent_tasks"] = [{"task_id": r[0], "status": r[1], "attempt_count": r[2]} for r in rows]
            payload["intent_outbox_events"] = sum(int(connection.execute(
                "SELECT COUNT(*) FROM task_outbox WHERE task_id = ?", (r[0],)).fetchone()[0]) for r in rows)
            payload["intent_invocation_attempts"] = sum(int(connection.execute(
                "SELECT COUNT(*) FROM task_invocation_attempts WHERE task_id = ?", (r[0],)).fetchone()[0]) for r in rows)
            payload["intent_budget_linked_attempts"] = sum(int(connection.execute(
                "SELECT COUNT(*) FROM task_invocation_attempts WHERE task_id = ? AND budget_event_id <> ''",
                (r[0],)).fetchone()[0]) for r in rows)
    finally:
        connection.close()
    emit({"phase": "counts", **payload})
    return 0


def cmd_outbox(db_value: str, sequence: int, limit: int) -> int:
    from stock_agent.task_runtime import TaskStore

    db = _project_path(db_value) if not db_value.startswith("/") else Path(db_value)
    store = TaskStore(db, read_only=True)
    try:
        events = store.events_after(sequence, limit=limit)
    finally:
        store.close()
    emit({"phase": "outbox", "cursor": sequence, "limit": limit,
          "sequences": [event["sequence"] for event in events],
          "types": [event.get("event_type") for event in events]})
    return 0


def _tree_digests(base: Path) -> tuple:
    """SHA-256 of the evidence ORIGINALS below ``base`` (objects, index, event
    and import logs), plus the names of the derived ``reports/`` projections,
    which are output rather than backup sources."""
    originals, derived = {}, {}
    for path in sorted(base.rglob("*")) if base.is_dir() else []:
        if not path.is_file():
            continue
        name = path.relative_to(base).as_posix()
        if "reports" in path.relative_to(base).parts[:-1]:
            derived[name] = None
        else:
            originals[name] = hashlib.sha256(path.read_bytes()).hexdigest()
    return originals, derived


def _arg_path(value: str) -> Path:
    """A project-relative path (through project_path) or an absolute one."""
    return Path(value) if value.startswith("/") else _project_path(value)


def _arg_path(value: str) -> Path:
    """A project-relative path (through project_path) or an absolute one."""
    return Path(value) if value.startswith("/") else _project_path(value)


def cmd_compare(live_root_value: str, live_vault_value: str, restored_root_value: str) -> int:
    """Full-column research_tasks / task_outbox rows, seven-table counts and
    every evidence original's digest. The arguments are DATA trees (the
    container side's volume mounted read-only stands for its live data/ tree,
    its evidence volume for the vault) and the restored tree's data/."""
    live_root, live_vault, restored_root = (
        _arg_path(live_root_value), _arg_path(live_vault_value), _arg_path(restored_root_value))
    live_db = live_root / "task_runtime/tasks.sqlite3"
    restored_db = restored_root / "task_runtime/tasks.sqlite3"
    report = {"phase": "compare", "live_db": live_db.as_posix(), "restored_db": restored_db.as_posix(),
              "errors": []}
    left, right = _connect(live_db), _connect(restored_db)
    try:
        for table, key in (("research_tasks", "task_id"), ("task_outbox", "sequence")):
            l_rows = {row[0]: tuple(row[1:]) for row in left.execute("SELECT {0}, * FROM {1}".format(key, table))}
            r_rows = {row[0]: tuple(row[1:]) for row in right.execute("SELECT {0}, * FROM {1}".format(key, table))}
            differing = sorted(str(k) for k in set(l_rows) | set(r_rows) if l_rows.get(k) != r_rows.get(k))
            report[table] = {"live": len(l_rows), "restored": len(r_rows),
                             "all_columns_equal_incl_payload": not differing, "differing_keys": differing[:5]}
            if differing:
                report["errors"].append("{0} rows differ".format(table))
        counts = {}
        for table in ("research_tasks", "task_outbox", "task_input_snapshots", "task_platform_runs",
                      "task_invocation_attempts", "principal_verifications", "workflow_dispatches"):
            try:
                counts[table] = [int(left.execute("SELECT COUNT(*) FROM {0}".format(table)).fetchone()[0]),
                                 int(right.execute("SELECT COUNT(*) FROM {0}".format(table)).fetchone()[0])]
            except sqlite3.Error as exc:
                counts[table] = "error: {0}".format(exc)
        report["table_counts_live_restored"] = counts
        if any(value[0] != value[1] for value in counts.values() if isinstance(value, list)):
            report["errors"].append("table counts differ")
    finally:
        left.close()
        right.close()

    evidence, originals_total = {}, 0
    trees = (("personal_evidence", live_root / "personal_evidence", restored_root / "personal_evidence"),
             ("evidence_vault", live_vault, restored_root / "evidence_vault"))
    for name, live_tree, restored_tree in trees:
        live_files, live_derived = _tree_digests(live_tree)
        restored_files, restored_derived = _tree_digests(restored_tree)
        originals_total += len(live_files)
        evidence[name] = {
            "live_tree": live_tree.as_posix(), "restored_tree": restored_tree.as_posix(),
            "live_originals": len(live_files), "restored_originals": len(restored_files),
            "digests_equal": live_files == restored_files,
            "live_only_derived_reports": sorted(live_derived),
            "content_addressed_names_ok": all(
                digest in Path(entry).name for entry, digest in restored_files.items() if "/objects/" in entry),
            "sha256": {name + "/" + entry: digest for entry, digest in sorted(restored_files.items())},
        }
        if live_files != restored_files:
            report["errors"].append("{0} evidence originals differ".format(name))
    if not originals_total:
        report["errors"].append("no evidence originals on the live side to compare")
    report["evidence"] = evidence
    report["ok"] = not report["errors"]
    emit(report)
    return 0 if report["ok"] else 13


def cmd_wal_fixture(path_value: str) -> int:
    destination = _project_path(path_value)
    destination.parent.mkdir(parents=True)
    with tempfile.TemporaryDirectory() as workspace:
        db = Path(workspace) / destination.name
        with closing(sqlite3.connect(db)) as connection:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute("PRAGMA wal_autocheckpoint=0")
            connection.execute("CREATE TABLE research_tasks (task_id TEXT PRIMARY KEY, status TEXT)")
            connection.execute("CREATE TABLE task_outbox (sequence INTEGER PRIMARY KEY, task_id TEXT)")
            connection.commit()
            assert connection.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone() == (0, 0, 0)
            connection.execute("INSERT INTO research_tasks VALUES ('wal-task', 'succeeded')")
            connection.execute("INSERT INTO task_outbox VALUES (1, 'wal-task')")
            connection.commit()
            shutil.copyfile(db, destination)
            shutil.copyfile(db.with_name(db.name + "-wal"), destination.with_name(destination.name + "-wal"))
            tasks = connection.execute("SELECT COUNT(*) FROM research_tasks").fetchone()[0]
            outbox = connection.execute("SELECT COUNT(*) FROM task_outbox").fetchone()[0]
    emit({"tasks": tasks, "outbox": outbox})
    return 0 if (tasks, outbox) == (1, 1) else 1


def cmd_wal_retry_fixture(source_value: str, baseline_value: str, staged_wal_value: str) -> int:
    baseline = _project_path(baseline_value)
    staged_wal = _project_path(staged_wal_value)
    baseline.parent.mkdir(parents=True)
    with tempfile.TemporaryDirectory() as workspace:
        scratch = Path(workspace) / baseline.name
        with closing(_connect(_project_path(source_value))) as source, closing(sqlite3.connect(scratch)) as db:
            source.backup(db)
            db.execute("PRAGMA journal_mode=WAL")
            db.execute("PRAGMA wal_autocheckpoint=0")
            db.execute("CREATE TABLE r25_retry_fixture (note TEXT)")
            db.commit()
            if db.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone() != (0, 0, 0):
                raise RuntimeError("could not checkpoint the retry fixture")
            shutil.copyfile(scratch, baseline)
            db.execute("INSERT INTO r25_retry_fixture VALUES ('committed WAL frames')")
            db.commit()
            shutil.copyfile(scratch.with_name(scratch.name + "-wal"), staged_wal)
    emit({"baseline": str(baseline), "staged_wal": str(staged_wal),
          "wal_bytes": staged_wal.stat().st_size})
    return 0 if staged_wal.stat().st_size > 0 else 1


def cmd_wal_fingerprint(path_value: str) -> int:
    db = _project_path(path_value)
    wal = db.with_name(db.name + "-wal")
    hashes = {path.name: hashlib.sha256(path.read_bytes()).hexdigest() for path in (db, wal)}
    files = sorted(path.name for path in db.parent.iterdir())
    emit({"sha256": hashes, "files": files, "wal_bytes": wal.stat().st_size,
          "mount": _mount_for(db)})
    readonly = "ro" in _mount_for(db)["options"].split(",")
    return 0 if readonly and sorted(hashes) == files and wal.stat().st_size > 0 else 1


COMMANDS = {
    "layout": lambda args: cmd_layout(),
    "init": lambda args: cmd_init(args[0]),
    "spec": lambda args: cmd_spec(*args[:5]),
    "evidence": lambda args: cmd_evidence(args[0]),
    "registry": lambda args: cmd_registry(args[0]),
    "resolved": lambda args: cmd_resolved(*args),
    "counts": lambda args: cmd_counts(*args[:2]),
    "outbox": lambda args: cmd_outbox(args[0], int(args[1]), int(args[2])),
    "compare": lambda args: cmd_compare(args[0], args[1], args[2]),
    "intent": lambda args: cmd_intent(*args[:3]),
    "provenance": lambda args: cmd_provenance(args[0], args[1]),
    "wal-fixture": lambda args: cmd_wal_fixture(args[0]),
    "wal-fingerprint": lambda args: cmd_wal_fingerprint(args[0]),
    "wal-retry-fixture": lambda args: cmd_wal_retry_fixture(*args),
}


def main() -> int:
    if len(sys.argv) < 2 or sys.argv[1] not in COMMANDS:
        print(__doc__, file=sys.stderr)
        return 2
    return COMMANDS[sys.argv[1]](sys.argv[2:])


if __name__ == "__main__":
    raise SystemExit(main())
