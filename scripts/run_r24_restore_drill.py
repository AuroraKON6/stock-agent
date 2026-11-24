#!/usr/bin/env python3
"""Build and run the isolated KONM-102 Compose drill from a clean checkout."""
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
LOCK = Path("/tmp/stock-agent-konm78-r17-2f18ce9.docker.lock")
BASELINE = "dc8a4cffd51663c0b4f1c67b1f5c03d7f3422071"
CONFIG = "data/config/watchlist.drill.json"


def utc() -> str:
    return datetime.now(timezone.utc).isoformat()


def require(condition: bool, message: str) -> None:
    if not condition:
        raise RuntimeError(message)


class Drill:
    def __init__(self, output: Path):
        self.output = output
        self.project = "stock-agent-r24-" + uuid.uuid4().hex[:12]
        self.image = self.project + ":candidate"
        self.env = {**os.environ, "R25_IMAGE": self.image, "PYTHON": sys.executable}
        self.compose = [
            "docker", "compose", "--env-file", "/dev/null", "--project-name", self.project,
            "-f", "deploy/testing/r25_precheck.compose.yaml", "-f", "deploy/testing/r24_restore.compose.yaml",
        ]
        self.records = []
        self.build_tree = self.output / "image-build"
        self.image_started = False

    def run(self, name: str, command: list[str], *, expected: int = 0, cwd: Path = ROOT) -> str:
        started = utc()
        print(f"{name} $ {shlex.join(command)}", flush=True)
        stdout = self.output / f"{name}.stdout"
        stderr = self.output / f"{name}.stderr"
        with stdout.open("w", encoding="utf-8") as out, stderr.open("w", encoding="utf-8") as err:
            result = subprocess.run(command, cwd=cwd, env=self.env, stdout=out, stderr=err)
        self.records.append({"step": name, "command": command, "exit": result.returncode,
                             "expected": expected, "start_utc": started, "end_utc": utc()})
        (self.output / "commands.json").write_text(json.dumps(self.records, indent=2) + "\n")
        require(result.returncode == expected,
                f"{name} exit {result.returncode}, expected {expected}; see {self.output}")
        return stdout.read_text(encoding="utf-8", errors="replace")

    def service(self, name: str, *command: str, expected: int = 0) -> str:
        return self.run(name, self.compose + ["run", "--rm", "--no-deps", "--pull", "never",
                                              "-T", "drill", *command], expected=expected)

    def fixture(self, name: str, *command: str, expected: int = 0) -> list[dict]:
        output = self.service(name, "python", "-B", "/drills/r24_restore_drill.py", *command, expected=expected)
        return [json.loads(line) for line in output.splitlines() if line.strip()]

    def assert_stopped(self, name: str) -> str:
        running = self.run(name, ["docker", "ps", "--filter", f"label=com.docker.compose.project={self.project}",
                                  "--format", "{{.Names}}"])
        require(not running.strip(), f"Project processes still running: {running}")
        return self.records[-1]["end_utc"]

    def execute(self, candidate: str) -> None:
        self.run("build-checkout", ["git", "worktree", "add", "--detach", str(self.build_tree), candidate])
        self.image_started = True
        self.run("build", ["bash", "scripts/build_image.sh", self.image], cwd=self.build_tree)
        image_id = self.run("image-id", ["docker", "image", "inspect", self.image, "--format", "{{.Id}}"]).strip()
        binding = json.loads((self.build_tree / "deploy/docker/image_binding.json").read_bytes())
        (self.output / "image_binding.json").write_text(json.dumps(binding, indent=2) + "\n")

        config = json.loads(self.run("compose-config", self.compose + ["config", "--format", "json"]))
        service = config["services"]["drill"]
        require(config["networks"]["default"]["internal"] and not service.get("ports")
                and list(service.get("networks", {})) == ["default"], "Expected internal, unexposed Compose network")
        require(service["image"] == self.image, "Compose image tag mismatch")
        expected_mounts = {
            "/app/data": "r25-data", "/app/drill-restore": "r25-restore",
            "/app/drill-archive": "r24-archive", "/app/drill-negatives": "r24-negatives",
        }
        mounts = {mount["target"]: mount["source"] for mount in service["volumes"]
                  if mount["type"] == "volume"}
        require(mounts == expected_mounts and all(
            config["volumes"][name]["name"] == f"{self.project}_{name}"
            and not config["volumes"][name].get("external") for name in mounts.values()),
            "Expected distinct project-owned volumes")
        require(all(mount["read_only"] and Path(mount["source"]).is_relative_to(ROOT)
                    for mount in service["volumes"] if mount["type"] == "bind"),
                "Expected only read-only checkout binds")

        bound_image = f"{self.image}@{binding['image_digest']}"
        bound_id = self.run("bound-image-id", ["docker", "image", "inspect", bound_image,
                                                "--format", "{{.Id}}"]).strip()
        provenance = json.loads(self.service(
            "in-container-provenance", "python", "-B", "-c",
            "import json; from pathlib import Path; "
            "from stock_agent.release_provenance import provenance_sha256; "
            "p = json.loads(Path('/app/PROVENANCE.json').read_bytes()); "
            "print(json.dumps({'record': p, 'sha256': provenance_sha256(p)}))"))
        require(provenance["record"]["source_commit"] == candidate
                and provenance["record"]["source_dirty"] is False
                and provenance["sha256"] == binding["provenance_sha256"]
                and bound_id == image_id, "Image provenance/binding disagrees with clean candidate SHA")
        self.fixture("seed", "seed")
        window_start = self.assert_stopped("writers-exited")
        before, = self.fixture("source-before", "inventory")
        self.service("backup", "python", "-m", "stock_agent", "backup-create", "--config", CONFIG)
        self.assert_stopped("after-backup")
        self.service("restore", "python", "-m", "stock_agent", "restore-drill", "--config", CONFIG)

        evidence, = self.fixture("manifest-and-restore", "collect")
        bundle, restored = evidence["bundle"], evidence["restored"]
        backup, manifest, restore = (evidence[key] for key in ("backup", "manifest", "drill"))
        for filename in ("manifest.json", "manifest.sha256"):
            self.service(filename, "cat", f"{bundle}/{filename}")
            (self.output / f"{filename}.stdout").rename(self.output / filename)
        require(backup["status"] == restore["status"] == "PASS"
                and backup["manifest_id"] == manifest["manifest_id"] == restore["manifest_id"]
                and restore["task_runtime_integrity"]["unknown_attempts_restored"] == 1
                and not restore["source_overwrite_performed"] and not restore["live_project_modified"]
                and manifest["snapshot_control"]["sqlite_online_backup"]
                and not manifest["snapshot_control"]["cross_file_atomic_snapshot"],
                "Backup/restore controls or manifest binding disagreed")
        self.fixture("positive", "compare", bundle, restored, "--raw")

        for mode in ("amount", "state", "missing-workflow", "outbox-payload", "missing-object"):
            mutation, = self.fixture(f"mutate-{mode}", "corrupt", restored, mode)
            target = mutation["path"] if "path" in mutation else mutation["destination_root"]
            old_compare, report = self.fixture(f"negative-{mode}", "compare", bundle, target, expected=13)
            if mode in {"amount", "state"}:
                column = "reconciliation_reason" if mode == "amount" else "state"
                require(report["row_differences"] == [{
                    "table": "task_invocation_attempts",
                    "key": {"attempt_id": mutation["mutation"]["attempt_id"]},
                    "changed_columns": [column], "present_in_source": True, "present_in_restore": True,
                }], f"Negative {mode} did not identify the changed invocation row and column")
            elif mode == "missing-workflow":
                require(any("missing workflow artifact" in error and Path(mutation["mutation"]["path"]).name in error
                            for error in report["errors"]), "Missing workflow was not located")
            elif mode == "outbox-payload":
                require(old_compare["outbox_drift_sample"]
                        and report["row_differences"][0]["changed_columns"] == ["payload"],
                        "Outbox change was not located")
            else:
                require(old_compare["evidence_restored"]["object_exists"] is False,
                        "Missing personal evidence object was not located")

        self.fixture("positive-after-negatives", "compare", bundle, restored)
        after, = self.fixture("source-after", "inventory")
        require(before == after and bool(before), "Source volume file hashes changed")
        window_end = self.assert_stopped("writers-still-exited")
        final_image_id = self.run("image-id-after", ["docker", "image", "inspect", self.image,
                                                     "--format", "{{.Id}}"]).strip()
        require(image_id == final_image_id, "Candidate image ID changed during drill")
        summary = {
            "baseline": BASELINE, "candidate": candidate, "docker_image_id": image_id, "compose_project": self.project,
            "volumes": config["volumes"], "quiescent_start_utc": window_start, "quiescent_end_utc": window_end,
            "manifest_id": manifest["manifest_id"], "manifest_sha256": backup["manifest_sha256"],
            "restored_root": restored, "source_files_unchanged": len(before), "real_provider_requests": 0,
        }
        (self.output / "summary.json").write_text(json.dumps(summary, indent=2) + "\n")

    def cleanup(self) -> None:
        failures = []
        commands = [
            ("cleanup-compose", self.compose + ["down", "--volumes", "--remove-orphans"]),
            ("remaining-containers", ["docker", "ps", "-aq", "--filter"]),
            ("remaining-volumes", ["docker", "volume", "ls", "-q", "--filter"]),
            ("remaining-networks", ["docker", "network", "ls", "-q", "--filter"]),
        ]
        for name, cmd in commands:
            if name.startswith("remaining-"):
                cmd.append(f"label=com.docker.compose.project={self.project}")
            try:
                if self.run(name, cmd).strip() and name.startswith("remaining-"):
                    failures.append(name)
            except RuntimeError as exc:
                failures.append(str(exc))
        if self.image_started:
            try:
                image = self.run("cleanup-image-check", ["docker", "image", "ls", "-q", self.image])
                if image.strip():
                    self.run("cleanup-image", ["docker", "image", "rm", self.image])
                remaining = self.run("remaining-image", ["docker", "image", "ls", "-q", self.image])
                require(not remaining.strip(), "Run-specific image tag still exists")
            except RuntimeError as exc:
                failures.append(str(exc))
        if self.build_tree.exists():
            try:
                self.run("cleanup-checkout", ["git", "worktree", "remove", "--force", str(self.build_tree)])
            except RuntimeError as exc:
                failures.append(str(exc))
        require(not failures, "; ".join(failures))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--commit", required=True, help="Full candidate SHA, equal to clean checkout HEAD")
    parser.add_argument("--output", required=True, type=Path, help="New evidence directory outside the checkout")
    args = parser.parse_args()
    output = args.output.resolve()
    require(not output.is_relative_to(ROOT), "Evidence directory must be outside the checkout")
    output.mkdir(parents=True)
    drill = Drill(output)
    head = drill.run("candidate-head", ["git", "rev-parse", "HEAD"]).strip()
    require(head == args.commit and len(head) == 40, "Clean checkout HEAD must be the requested full SHA")
    require(not drill.run("candidate-status", ["git", "status", "--porcelain"]).strip(), "Checkout must be clean")
    drill.run("candidate-baseline", ["git", "merge-base", "--is-ancestor", BASELINE, head])
    with LOCK.open("a") as lock:
        print(f"Waiting for the shared Docker drill lock at {LOCK}", flush=True)
        fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            drill.execute(head)
        finally:
            drill.cleanup()
    print(f"PASS. Evidence: {output}", flush=True)


if __name__ == "__main__":
    main()
