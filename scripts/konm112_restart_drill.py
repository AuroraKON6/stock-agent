#!/usr/bin/env python3
"""KONM-112: quiescent backup/restore and cross-restart replay on ONE fixed image.

Runs, on an already built and digest-pinned candidate, the R24 seed / backup /
restore / reconciliation and then the joint restart evidence:

  1. seed the synthetic R24 source (four attempt states with budget
     reservations, a referenced personal-evidence task, a frozen workflow);
  2. prove no writer is running, inventory the source volume, back up, restore
     into another empty volume, reconcile row by row, run the five negatives;
  3. deliver the outbox once to an isolated idempotent consumer, take a full
     ledger, stop every container, recreate the service from the SAME digest,
     and submit the same business intents again;
  4. require the ledger (task IDs per intent, attempts, reservations, token
     usage, simulated receipts, outbox sequence/ID/hash) to be unchanged, and
     require no event to be delivered a second time;
  5. prove the source volume's file hashes and the clean project inventory.

Synthetic data, project-scoped temp volumes, internal network only.
"""
from __future__ import annotations

import argparse
import fcntl
import json
import os
import shlex
import subprocess
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
LOCK = Path("/tmp/stock-agent-konm112.docker.lock")
BASELINE = "e1c75a0db07cd12fd766187496cdf427882dca50"
CONFIG = "data/config/watchlist.drill.json"
HELPER = "/drills/konm112_restart_helper.py"
RECEIPTS = Path("/drill-receipts")
NEGATIVES = ("amount", "state", "missing-workflow", "outbox-payload", "missing-object")


def utc() -> str:
    return datetime.now(timezone.utc).isoformat()


def require(condition: bool, message: str) -> None:
    if not condition:
        raise RuntimeError(message)


class Drill:
    def __init__(self, image: str, commit: str, output: Path):
        self.image = image
        self.commit = commit
        self.output = output
        self.project = "stock-agent-konm112-" + uuid.uuid4().hex[:12]
        self.env = {**os.environ, "R25_IMAGE": image}
        self.compose = [
            "docker", "compose", "--env-file", "/dev/null", "--project-name", self.project,
            "-f", str(ROOT / "deploy/testing/r25_precheck.compose.yaml"),
            "-f", str(ROOT / "deploy/testing/r24_restore.compose.yaml"),
            "-f", str(ROOT / "deploy/testing/konm112_restart.compose.yaml"),
        ]
        self.records = []

    def run(self, name: str, command: list[str], *, expected: int = 0, env: dict | None = None) -> str:
        started = utc()
        print(f"{name} $ {shlex.join(command)}", flush=True)
        stdout, stderr = self.output / f"{name}.stdout", self.output / f"{name}.stderr"
        with stdout.open("w", encoding="utf-8") as out, stderr.open("w", encoding="utf-8") as err:
            result = subprocess.run(command, cwd=ROOT, env=env or self.env, stdout=out, stderr=err)
        self.records.append({"step": name, "command": command, "exit": result.returncode,
                             "expected": expected, "start_utc": started, "end_utc": utc()})
        self.flush()
        require(result.returncode == expected,
                f"{name} exit {result.returncode}, expected {expected}; see {self.output}")
        return stdout.read_text(encoding="utf-8", errors="replace")

    def flush(self) -> None:
        lines = ["\t".join((r["step"], str(r["exit"]), str(r["expected"]), r["start_utc"], r["end_utc"],
                            shlex.join(r["command"]))) for r in self.records]
        (self.output / "steps.tsv").write_text("step\texit\texpected\tstart_utc\tend_utc\tcommand\n"
                                               + "\n".join(lines) + "\n", encoding="utf-8")

    def service(self, name: str, *command: str, expected: int = 0, env: dict | None = None) -> str:
        return self.run(name, self.compose + ["run", "--rm", "--no-deps", "--pull", "never",
                                              "-T", "drill", *command], expected=expected, env=env)

    def helper(self, name: str, *command: str, expected: int = 0) -> dict:
        output = self.service(name, "python", "-B", HELPER, *command, expected=expected)
        lines = [line for line in output.splitlines() if line.strip()]
        return json.loads(lines[-1])

    def fixture(self, name: str, *command: str, expected: int = 0) -> dict:
        output = self.service(name, "python", "-B", "/drills/r24_restore_drill.py", *command, expected=expected)
        return json.loads([line for line in output.splitlines() if line.strip()][-1])

    def stopped(self, name: str) -> str:
        running = self.run(name, ["docker", "ps", "-q", "--filter", f"label=com.docker.compose.project={self.project}"])
        require(not running.strip(), f"Project processes still running: {running!r}")
        return self.records[-1]["end_utc"]

    def inventory(self, name: str) -> list[str]:
        label = f"label=com.docker.compose.project={self.project}"
        found = []
        for kind, command in (("container", ["docker", "ps", "-aq"]), ("volume", ["docker", "volume", "ls", "-q"]),
                              ("network", ["docker", "network", "ls", "-q"])):
            listing = self.run(f"{name}-{kind}", command + ["--filter", label]).split()
            found += [f"{kind}:{item}" for item in listing]
        return found

    def execute(self, binding: dict, image_id: str) -> None:
        config = json.loads(self.run("compose-config", self.compose + ["config", "--format", "json"]))
        service = config["services"]["drill"]
        require(config["networks"]["default"]["internal"] and not service.get("ports")
                and list(service.get("networks", {})) == ["default"], "Expected internal, unexposed Compose network")
        require(service["image"] == self.image, "Compose image tag mismatch")
        volumes = {mount["source"] for mount in service["volumes"] if mount["type"] == "volume"}
        require(all(config["volumes"][name]["name"] == f"{self.project}_{name}"
                    and not config["volumes"][name].get("external") for name in volumes),
                "Expected project-owned volumes only")
        require(all(mount["read_only"] and Path(mount["source"]).is_relative_to(ROOT)
                    for mount in service["volumes"] if mount["type"] == "bind"),
                "Expected only read-only checkout binds")

        provenance = json.loads(self.service(
            "in-container-provenance", "python", "-B", "-c",
            "import json; from pathlib import Path; "
            "from stock_agent.release_provenance import provenance_sha256; "
            "p = json.loads(Path('/app/PROVENANCE.json').read_bytes()); "
            "print(json.dumps({'record': p, 'sha256': provenance_sha256(p)}))"))
        require(provenance["record"]["source_commit"] == self.commit
                and provenance["record"]["source_dirty"] is False
                and provenance["sha256"] == binding["provenance_sha256"],
                "Image provenance disagrees with the clean candidate SHA / binding")

        # ---- R24: quiescent snapshot, independent restore, row-level ledger ----
        self.fixture("seed", "seed")
        # Drain what the seed left queued, so the quiescent snapshot holds no
        # never-started task: only then is an empty queue after the restart a
        # statement about resends. The worker waits for work once the queue is
        # empty, so the time limit (124) is the expected outcome.
        self.service("drain-in-flight", "timeout", "45", "python", "-B", "-m", "stock_agent", "worker-service",
                     "--config", CONFIG, "--worker-id", "w-konm112-drain", "--max-tasks", "20",
                     "--idle-sleep-seconds", "0.2", expected=124)
        drained = self.helper("ledger-after-drain", "ledger")
        require(not drained["outbox_summary"]["task_counts"].get("queued")
                and not drained["outbox_summary"]["task_counts"].get("leased"),
                f"Queue not drained: {drained['outbox_summary']}")
        self.service("access-log-quiescent", "cat", "/app/data/dify/access_log.jsonl")
        window_start = self.stopped("writers-exited")
        before = self.fixture("source-before", "inventory")
        self.service("backup", "python", "-m", "stock_agent", "backup-create", "--config", CONFIG)
        self.stopped("after-backup")
        self.service("restore", "python", "-m", "stock_agent", "restore-drill", "--config", CONFIG)

        evidence = self.fixture("manifest-and-restore", "collect")
        bundle, restored = evidence["bundle"], evidence["restored"]
        backup, manifest, restore = (evidence[key] for key in ("backup", "manifest", "drill"))
        for filename in ("manifest.json", "manifest.sha256"):
            self.service(filename, "cat", f"{bundle}/{filename}")
            (self.output / f"{filename}.stdout").rename(self.output / filename)
        require(backup["status"] == restore["status"] == "PASS"
                and backup["manifest_id"] == manifest["manifest_id"] == restore["manifest_id"]
                and restore["task_runtime_integrity"]["unknown_attempts_restored"] == 1
                and not restore["source_overwrite_performed"] and not restore["live_project_modified"],
                "Backup/restore controls or manifest binding disagreed")
        self.fixture("positive", "compare", bundle, restored, "--raw")
        for mode in NEGATIVES:
            self.fixture(f"mutate-{mode}", "corrupt", restored, mode)
            self.fixture(f"negative-{mode}", "compare", bundle, f"/app/drill-negatives/{mode}", expected=13)
        self.fixture("positive-after-negatives", "compare", bundle, restored)

        # ---- cross restart: one fixed image, same intents, no second delivery ----
        def receipt(name: str) -> str:
            return str(RECEIPTS / name)

        ledger0 = self.helper("ledger-before-restart", "ledger")
        first = self.helper("consumer-deliver-before-restart", "consume", "data", receipt("live.sqlite3"),
                            "pre-restart", "0")
        require(first["read"] == first["delivered"] == first["receipts_total"] == len(ledger0["outbox"]),
                "First delivery did not deliver each outbox event exactly once")
        tail = self.helper("read-tail-before-restart", "read", "data", str(first["cursor_end"]), "100")
        require(not tail["sequences"], "Events appeared after the last delivered sequence")

        stop_start = utc()
        self.run("down-for-restart", self.compose + ["down", "--remove-orphans"])
        self.stopped("stopped-before-recreate")
        self.run("recreate-service", self.compose + ["up", "--detach", "--no-build", "--pull", "never", "drill"])
        container = self.run("recreated-container", ["docker", "ps", "-aq", "--filter",
                                                     f"label=com.docker.compose.project={self.project}"]).split()
        require(len(container) == 1, f"Expected exactly one recreated service container: {container}")
        recreated = json.loads(self.run("recreated-inspect", ["docker", "inspect", container[0]]))[0]
        require(recreated["Image"] == image_id, "Recreated service does not run the fixed image ID")
        self.run("down-after-recreate", self.compose + ["down", "--remove-orphans"])
        stop_end = self.stopped("stopped-after-recreate")

        ledger1 = self.helper("ledger-after-restart", "ledger")
        require(ledger1 == ledger0, "Ledger changed across the restart, before any resubmission")
        cursor = str(ledger1["outbox"][-1]["sequence"])
        require(not self.helper("read-tail-after-restart", "read", "data", cursor, "100")["sequences"],
                "Cursor read after the restart returned events")

        resubmit = self.helper("resubmit-same-intents", "resubmit")
        expected = {entry["intent"]: entry["task_ids"] for entry in ledger1["per_intent"]}
        require(all(item["verdict"] == "DUPLICATE" and expected.get(item["intent"]) == [item["task_id"]]
                    for item in resubmit["results"]) and len(resubmit["results"]) == len(expected),
                f"Resubmission did not converge on the original task IDs: {resubmit}")
        ledger2 = self.helper("ledger-after-resubmit", "ledger")
        require(ledger2 == ledger0, "Ledger changed after resubmitting the same intents")

        second = self.helper("consumer-resume-after-restart", "consume", "data", receipt("live.sqlite3"),
                             "post-restart-resume", cursor)
        replay = self.helper("consumer-replay-from-zero", "consume", "data", receipt("live.sqlite3"),
                             "post-restart-replay", "0")
        require(second["read"] == second["delivered"] == 0 and replay["read"] == replay["duplicates"]
                and replay["delivered"] == 0 and replay["receipts_total"] == first["receipts_total"],
                "An event was delivered a second time after the restart")
        live_receipts = self.helper("receipts-live", "receipts", receipt("live.sqlite3"))["receipts"]

        # The independent restore replays the identical sequence and delivers
        # it once to its own consumer; its receipts equal the live side's.
        restored_data = str(Path(restored) / "data")
        ledger_restored = self.helper("ledger-restored", "ledger", restored_data)
        require(ledger_restored["outbox"] == ledger0["outbox"]
                and ledger_restored["table_counts"] == ledger0["table_counts"],
                "Restored outbox sequence/ID/hash differs from the live store")
        restored_read = self.helper("read-restored-from-zero", "read", restored_data, "0", "100")
        require([(e["sequence"], e["event_id"], e["sha256"]) for e in restored_read["events"]]
                == [(e["sequence"], e["event_id"], e["sha256"]) for e in ledger0["outbox"]],
                "Restored cursor read differs from the live outbox")
        restored_first = self.helper("consumer-restored-first", "consume", restored_data,
                                     receipt("restored.sqlite3"), "restored-first", "0")
        restored_again = self.helper("consumer-restored-second", "consume", restored_data,
                                     receipt("restored.sqlite3"), "restored-second", "0")
        restored_receipts = self.helper("receipts-restored", "receipts", receipt("restored.sqlite3"))["receipts"]
        require(restored_first["delivered"] == len(ledger0["outbox"]) and restored_again["delivered"] == 0
                and [(r[0], r[1], r[2]) for r in restored_receipts]
                == [(r[0], r[1], r[2]) for r in live_receipts],
                "Restored delivery does not match the live receipts")

        # A worker started on the rebuilt service finds nothing to resend: an
        # empty queue never exits by itself, so the time limit (124) is the
        # expected outcome.
        self.service("worker-after-restart", "timeout", "20", "python", "-B", "-m", "stock_agent", "worker-service",
                     "--config", CONFIG, "--worker-id", "w-konm112-restart", "--max-tasks", "1",
                     "--idle-sleep-seconds", "0.2", expected=124)
        ledger3 = self.helper("ledger-after-worker", "ledger")
        require(ledger3 == ledger0, "Ledger changed after the restarted worker ran")

        self.service("access-log-final", "cat", "/app/data/dify/access_log.jsonl")
        after = self.fixture("source-after", "inventory")
        # The gateway's access audit is an append-only record OF the resubmission
        # (its gate logs every call, duplicate or not). It may grow; nothing
        # else on the source volume may change.
        audit = "dify/access_log.jsonl"
        changed = sorted(key for key in set(before) | set(after) if before.get(key) != after.get(key))
        require(bool(before) and changed == [audit] and after[audit]["bytes"] > before[audit]["bytes"],
                f"Source volume files other than the access audit changed: {changed}")
        quiescent_log = (self.output / "access-log-quiescent.stdout").read_text(encoding="utf-8")
        final_log = (self.output / "access-log-final.stdout").read_text(encoding="utf-8")
        require(final_log.startswith(quiescent_log), "Access audit was rewritten rather than appended to")
        access_audit = {"appended_bytes": after[audit]["bytes"] - before[audit]["bytes"],
                        "lines_before": len(quiescent_log.splitlines()), "lines_after": len(final_log.splitlines()),
                        "appended_lines": final_log[len(quiescent_log):].splitlines()}
        window_end = self.stopped("writers-still-exited")
        self.summary = {
            "baseline": BASELINE, "candidate": self.commit, "image": self.image, "docker_image_id": image_id,
            "recreated_container": {key: recreated[key] for key in ("Id", "Image", "Created") },
            "compose_project": self.project, "volumes": sorted(volumes),
            "quiescent_start_utc": window_start, "quiescent_end_utc": window_end,
            "stop_write_start_utc": stop_start, "stop_write_end_utc": stop_end,
            "manifest_id": manifest["manifest_id"], "manifest_sha256": backup["manifest_sha256"],
            "restored_root": restored, "source_files_unchanged": len(before) - 1,
            "source_files_total": len(before), "access_audit": access_audit,
            "outbox_events": len(ledger0["outbox"]), "table_counts": ledger0["table_counts"],
            "per_intent": ledger0["per_intent"], "resubmit": resubmit["results"],
            "delivery": {"first": first, "resume_after_restart": second, "replay_from_zero": replay,
                         "restored_first": restored_first, "restored_second": restored_again},
            "ledger_equal_across": ["before-restart", "after-restart", "after-resubmit", "after-worker"],
            "real_provider_requests": 0,
        }

    def cleanup(self) -> list[str]:
        self.run("cleanup-compose", self.compose + ["down", "--volumes", "--remove-orphans"])
        remaining = self.inventory("remaining")
        (self.output / "cleanup_inventory.json").write_text(json.dumps(remaining, indent=2) + "\n", encoding="utf-8")
        return remaining


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--image", required=True, help="stock-agent-research:<tag>@sha256:<digest>")
    parser.add_argument("--commit", required=True, help="Full candidate SHA, equal to clean checkout HEAD")
    parser.add_argument("--binding", required=True, type=Path, help="External image binding JSON for the digest")
    parser.add_argument("--output", required=True, type=Path, help="New evidence directory outside the checkout")
    args = parser.parse_args()
    output = args.output.resolve()
    require(not output.is_relative_to(ROOT), "Evidence directory must be outside the checkout")
    require(not output.exists(), f"Evidence directory already exists: {output}")
    output.mkdir(parents=True)
    drill = Drill(args.image, args.commit, output)
    head = drill.run("candidate-head", ["git", "rev-parse", "HEAD"]).strip()
    require(head == args.commit and len(head) == 40, "Clean checkout HEAD must be the requested full SHA")
    require(not drill.run("candidate-status", ["git", "status", "--porcelain"]).strip(), "Checkout must be clean")
    drill.run("candidate-baseline", ["git", "merge-base", "--is-ancestor", BASELINE, head])
    binding = json.loads(args.binding.read_text(encoding="utf-8"))
    require(args.image.endswith("@" + binding["image_digest"]), "R25 image is not the bound digest")
    image_id = drill.run("image-id", ["docker", "image", "inspect", args.image, "--format", "{{.Id}}"]).strip()
    require(image_id.startswith("sha256:"), "No runnable image ID for the pinned reference")
    with LOCK.open("a") as lock:
        print(f"Waiting for the shared Docker drill lock at {LOCK}", flush=True)
        fcntl.flock(lock, fcntl.LOCK_EX)
        failure = None
        try:
            drill.execute(binding, image_id)
        except BaseException as exc:  # the summary must record a failure too
            failure = f"{type(exc).__name__}: {exc}"
            raise
        finally:
            remaining = drill.cleanup()
            summary = {**getattr(drill, "summary", {}), "steps": len(drill.records),
                       "steps_matching_expected": sum(r["exit"] == r["expected"] for r in drill.records),
                       "cleanup_remaining": remaining, "failure": failure,
                       "verdict": "FAIL" if failure or remaining else "PASS"}
            (output / "summary.json").write_text(json.dumps(summary, indent=2, ensure_ascii=False) + "\n",
                                                 encoding="utf-8")
            drill.run("final-image-id", ["docker", "image", "inspect", args.image, "--format", "{{.Id}}"])
            require(drill.records[-1]["exit"] == 0 and not remaining, f"Cleanup left resources: {remaining}")
    print(f"PASS. Evidence: {output}", flush=True)


if __name__ == "__main__":
    main()
