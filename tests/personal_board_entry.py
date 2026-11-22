"""A93 browser acceptance launcher: isolated session-mode board + replay worker.

Test-framework owned environment preparation (the part the taskbook allows the
framework to do): it builds the personal-workbench board server with a
PersonalSessionStore and a fixture authenticator that accepts exactly ONE
test principal, and it runs replay worker steps against the owner's personal
vault view. Every write stays inside the caller-supplied temporary root; the
isolation guard from ``scripts/run_isolated_tests.py`` is installed first.

Subcommands:

- ``serve --config PATH --port N``: serve until killed (prints READY);
- ``work --config PATH --task-id ID``: claim exactly the expected task and
  run ONE replay worker pass over the owner's personal vault view;
- ``state --config PATH --task-id ID``: read-only dump of the persisted task
  (status, versions, snapshots, acknowledgement, output digest) so the
  browser driver can assert page-vs-record consistency.

The USER actions themselves are never performed here — they happen in the
real browser page driven by ``tests/browser_personal_flow_isolated.cjs``.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

OWNER = "a93-browser-user"


def _load_config(path: str) -> dict:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def _install_isolation(real_root: str, temp_root: str) -> None:
    sys.path.insert(0, str(Path(real_root).resolve()))
    from scripts.run_isolated_tests import install_guard
    import stock_agent.config as config_module

    install_guard(config_module.PROJECT_ROOT)
    # Handler threads do not inherit a contextvar override; the module-level
    # root applies process-wide so every relative default lands in the temp
    # workspace, never in the real checkout.
    config_module.PROJECT_ROOT = Path(temp_root).resolve()


def cmd_serve(args: argparse.Namespace) -> int:
    _install_isolation(args.root, args.temp)
    config = _load_config(args.config)
    from stock_agent.identity_providers import PersonalSessionStore
    from stock_agent.task_board import create_task_board_server
    from stock_agent.task_runtime import TaskStore

    def authenticator(board_config, actor, symbols):
        if actor != OWNER:
            raise ValueError("unknown principal: {0}".format(actor))
        return {"actor": actor, "provider": "windows_local",
                "subject": "S-1-a93-browser", "policy_sha256": "b" * 64}

    store = TaskStore(config["task_runtime"]["db_path"])
    server = create_task_board_server(
        store, host="127.0.0.1", port=int(args.port), config=config,
        session_store=PersonalSessionStore(ttl_seconds=3600),
        login_authenticator=authenticator)
    print("READY {0}".format(server.server_address[1]), flush=True)
    try:
        server.serve_forever()
    finally:
        server.server_close()
        store.close()
    return 0


def cmd_work(args: argparse.Namespace) -> int:
    _install_isolation(args.root, args.temp)
    config = _load_config(args.config)
    from stock_agent.personal_evidence import personal_vault_view
    from stock_agent.research_worker import run_research_task
    from stock_agent.task_runtime import TaskStore

    store = TaskStore(config["task_runtime"]["db_path"])
    try:
        claimed = store.claim_next(args.worker_id)
        if not claimed:
            print(json.dumps({"claimed": None}))
            return 0
        if args.task_id and claimed["task_id"] != args.task_id:
            raise SystemExit(
                "claim mismatch: wanted {0} got {1}".format(args.task_id, claimed["task_id"]))
        # The replay worker runs against the OWNER's personal vault view, so
        # the imported sources are verifiable in the namespace that owns them.
        view = personal_vault_view(config, OWNER)
        result = run_research_task(store, claimed["task_id"],
                                   worker_id=args.worker_id, config=view)
        print(json.dumps({
            "claimed": claimed["task_id"],
            "status": result.get("status"),
        }, ensure_ascii=False))
    finally:
        store.close()
    return 0


def cmd_state(args: argparse.Namespace) -> int:
    _install_isolation(args.root, args.temp)
    config = _load_config(args.config)
    from stock_agent.task_runtime import TaskStore

    store = TaskStore(config["task_runtime"]["db_path"])
    try:
        task = store.get_task(args.task_id)
        output = task.get("output") or {}
        acknowledgement = output.get("personal_acknowledgement") or None
        payload = task.get("input_payload") or {}
        summaries = ((payload.get("public_context") or {})
                     .get("evidence_summaries") or {})
        print(json.dumps({
            "task_id": task["task_id"],
            "status": task["status"],
            "input_version": task["input_version"],
            "row_version": task["version"],
            "cancel_requested": bool(task.get("cancel_requested")),
            "question": payload.get("research_question"),
            "extra_note": payload.get("extra_note"),
            "evidence_documents": sorted(summaries),
            "snapshot_count": len(store.input_snapshots(args.task_id)),
            "has_output": bool(output),
            "output_summary": str(output.get("summary") or "")[:200],
            "acknowledgement": acknowledgement,
            "last_error": str(task.get("last_error") or ""),
            "origin": payload.get("origin") or {},
        }, ensure_ascii=False))
    finally:
        store.close()
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    serve = sub.add_parser("serve")
    serve.add_argument("--root", required=True)
    serve.add_argument("--temp", required=True)
    serve.add_argument("--config", required=True)
    serve.add_argument("--port", type=int, default=0)

    work = sub.add_parser("work")
    work.add_argument("--root", required=True)
    work.add_argument("--temp", required=True)
    work.add_argument("--config", required=True)
    work.add_argument("--task-id", default="")
    work.add_argument("--worker-id", default="w-a93-browser")

    state = sub.add_parser("state")
    state.add_argument("--root", required=True)
    state.add_argument("--temp", required=True)
    state.add_argument("--config", required=True)
    state.add_argument("--task-id", required=True)

    args = parser.parse_args()
    return {"serve": cmd_serve, "work": cmd_work, "state": cmd_state}[args.command](args)


if __name__ == "__main__":
    raise SystemExit(main())
