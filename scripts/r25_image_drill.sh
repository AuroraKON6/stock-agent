#!/usr/bin/env bash
# R25 image verification driver (KONM-38) — host side.
#
# Proves the candidate image itself — nothing mounted from the checkout except
# a synthetic read-only config directory — can schedule, persist across a
# container rebuild, back up, restore and hand the scheduler back, using only
# the image's public entry points, project-scoped temp volumes and replay data.
#
# Usage (from anywhere; needs docker compose and python3 for JSON):
#   R25_IMAGE='stock-agent-research:<tag>@sha256:<digest>' \
#     bash scripts/r25_image_drill.sh <evidence-directory>
#
# R25_IMAGE is the reference recorded in deploy/docker/image_binding.json after
# scripts/build_image.sh. Every step's command, output and exit code are written
# to <evidence-directory>; steps.tsv lists them next to the expected exit code.
set -uo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
HELPER="$ROOT/scripts/r25_image_drill.py"
EV="${1:?usage: R25_IMAGE=<image ref> bash scripts/r25_image_drill.sh <evidence-dir>}"
IMAGE_REF="${R25_IMAGE:?set R25_IMAGE to stock-agent-research:<tag>@sha256:<digest>}"
PROJECT="konm38-r25-image-$(date -u +%Y%m%d%H%M%S)-$$"
DRILL_CONFIG=/drill-config/watchlist.json
PROBE="$PROJECT-probe"
SCHEDULE_ID="r25-image-daily"
SCOPE="r25-image-v1"
SLOT_COLD="2026-11-24T08:30:00+08:00"
SLOT_NEXT="2026-11-25T08:30:00+08:00"
ACTOR="r25-image-drill"
CUTOVER_ID="konm38_r25_image_rollback"

EV="$(mkdir -p "$EV" && cd "$EV" && pwd)"
CFG="$EV/config"
FAILED=0
: > "$EV/steps.tsv"

case "$IMAGE_REF" in
  stock-agent-research:*@sha256:*) ;;
  *) echo "R25_IMAGE must be stock-agent-research:<tag>@sha256:<digest>" >&2; exit 2 ;;
esac
STOCK_AGENT_TAG="${IMAGE_REF#stock-agent-research:}"
export STOCK_AGENT_TAG

# deploy/compose.yaml supplies the image pin, APP_* roots, non-root user,
# read_only, cap_drop and no-new-privileges; the overlays only redirect the
# config/state mounts of one isolated project.
compose=(docker compose -p "$PROJECT" -f "$ROOT/deploy/compose.yaml")
overlay="$ROOT/deploy/testing/r25_image.compose.yaml"
readonly_overlay="$ROOT/deploy/testing/r25_image.readonly.compose.yaml"
# Bash arrays do not nest, so the three file sets are spelled out.
base=("${compose[@]}" -f "$overlay")
all=("${compose[@]}" -f "$overlay" -f "$readonly_overlay")

C()  { R25_CONFIG_DIR="$CFG/container" R25_STATE_VOL=cstate R25_EVIDENCE_VOL=cevidence "${base[@]}" "$@"; }
CR() { R25_CONFIG_DIR="$CFG/restore" R25_STATE_VOL=rstate R25_EVIDENCE_VOL=revidence R25_OTHER_VOL=cstate R25_OTHER_PATH=legacy_source "${all[@]}" "$@"; }
CB() { R25_CONFIG_DIR="$CFG/container" R25_STATE_VOL=cstate R25_EVIDENCE_VOL=cevidence R25_OTHER_VOL=rstate R25_OTHER_PATH=restored_readonly "${all[@]}" "$@"; }
L()  { R25_CONFIG_DIR="$CFG/legacy" R25_STATE_VOL=rstate R25_EVIDENCE_VOL=revidence "${base[@]}" "$@"; }
# Remove the project with its temp volumes. A compose invocation only knows the
# volumes its own variables name, so both sides are removed in turn.
cleanup_all() {
  env R25_CONFIG_DIR="$CFG/container" R25_STATE_VOL=cstate R25_EVIDENCE_VOL=cevidence \
    R25_OTHER_VOL=rstate R25_OTHER_PATH=restored_readonly "${all[@]}" down --volumes --remove-orphans || return
  env R25_CONFIG_DIR="$CFG/restore" R25_STATE_VOL=rstate R25_EVIDENCE_VOL=revidence \
    R25_OTHER_VOL=cstate R25_OTHER_PATH=legacy_source "${all[@]}" down --volumes --remove-orphans
}

# bounded <stdin-file> <command...>: run a step under a wall-clock limit. A
# queue consumer given an empty queue never exits by itself, so a wrong
# precondition must fail the step instead of hanging the driver; the limit
# also removes the one-off container the killed client left behind.
bounded() {
  local input="$1" pid watchdog limit="${R25_STEP_TIMEOUT:-240}" code
  shift
  if [ "$input" = "-" ]; then (cd "$ROOT" && "$@") & else (cd "$ROOT" && "$@") < "$input" & fi
  pid=$!
  ( sleep "$limit" ) & watchdog=$!
  while kill -0 "$pid" 2>/dev/null && kill -0 "$watchdog" 2>/dev/null; do sleep 1; done
  if kill -0 "$pid" 2>/dev/null; then
    code=124
    echo "[timeout: no exit within ${limit}s; stopping the step and its container]" >&2
    pkill -TERM -P "$pid" 2>/dev/null; kill -TERM "$pid" 2>/dev/null
    docker ps -q --filter "label=com.docker.compose.project=$PROJECT" \
      --filter "label=com.docker.compose.oneoff=True" | xargs -r docker rm -f > /dev/null 2>&1
  else
    wait "$pid"; code=$?
  fi
  kill "$watchdog" 2>/dev/null; wait "$watchdog" 2>/dev/null
  return "$code"
}

record() {  # record <label> <expected-exit> <log> <code>
  local label="$1" expect="$2" out="$3" code="$4"
  echo "[exit $code] expected $expect" >> "$out"
  printf '%s\t%s\t%s\n' "${out##*/}" "$code" "$expect" >> "$EV/steps.tsv"
  if [ "$code" -eq "$expect" ]; then echo "ok    ${out##*/}"
  else FAILED=$((FAILED + 1)); echo "FAIL  ${out##*/} (exit $code, expected $expect)"; fi
  return 0
}

# step <label> <expected-exit> <command...>: record command, output, exit code.
step() {
  local label="$1" expect="$2" out code; shift 2
  out="$EV/$label.log"
  { echo "# $(date -u +%Y-%m-%dT%H:%M:%SZ)"
    echo "# env: R25_CONFIG_DIR=${R25_CONFIG_DIR:-} R25_STATE_VOL=${R25_STATE_VOL:-} R25_EVIDENCE_VOL=${R25_EVIDENCE_VOL:-} R25_OTHER_VOL=${R25_OTHER_VOL:-} R25_OTHER_PATH=${R25_OTHER_PATH:-} STOCK_AGENT_TAG=$STOCK_AGENT_TAG"
    printf ' %q' "$@"; echo; } > "$out"
  bounded - "$@" >> "$out" 2>&1; code=$?
  record "$label" "$expect" "$out" "$code"
  LAST="$out"
}

# stepin <label> <expected-exit> <stdin-file> <command...>: same, with stdin
# (the in-image helper is fed to the image's own interpreter).
stepin() {
  local label="$1" expect="$2" input="$3" out code; shift 3
  out="$EV/$label.log"
  { echo "# $(date -u +%Y-%m-%dT%H:%M:%SZ)"
    echo "# stdin: ${input#"$ROOT/"}"
    echo "# env: R25_CONFIG_DIR=${R25_CONFIG_DIR:-} R25_STATE_VOL=${R25_STATE_VOL:-} R25_EVIDENCE_VOL=${R25_EVIDENCE_VOL:-} R25_OTHER_VOL=${R25_OTHER_VOL:-} R25_OTHER_PATH=${R25_OTHER_PATH:-} STOCK_AGENT_TAG=$STOCK_AGENT_TAG"
    printf ' %q' "$@"; echo; } > "$out"
  bounded "$input" "$@" >> "$out" 2>&1; code=$?
  record "$label" "$expect" "$out" "$code"
  LAST="$out"
}

# jget <query>: read one value out of the last step log. "key.sub" addresses
# its last JSON line; "text:<label>" addresses the CLI's "label: value" line.
jget() {
  python3 - "$LAST" "$1" <<'JGET'
import json, re, sys
query = sys.argv[2]
value = None
if query.startswith("text:"):
    label = re.escape(query[5:])
    for line in open(sys.argv[1], encoding="utf-8"):
        match = re.match(r"^{0}: (.*)$".format(label), line.rstrip("\n"))
        if match:
            value = match.group(1)
else:
    for line in open(sys.argv[1], encoding="utf-8"):
        line = line.strip()
        if line.startswith("{"):
            try:
                value = json.loads(line)
            except ValueError:
                pass
    for key in query.split("."):
        value = value[key]
print(value)
JGET
}

# Public entry points of the image (stock_agent.cli), as real argv.
CLI=(python -m stock_agent)
SUBMIT=("${CLI[@]}" scheduled-task-submit --schedule-id "$SCHEDULE_ID" --scope-version "$SCOPE" --execution-mode replay)
WORKER=("${CLI[@]}" worker-service --ttl-seconds 60 --idle-sleep-seconds 0.1)

project_resources() {
  local kind="$1"
  if [ "$kind" = container ]; then
    docker container ls -aq --filter "label=com.docker.compose.project=$PROJECT"
  else
    docker "$kind" ls -q --filter "label=com.docker.compose.project=$PROJECT"
  fi
}

echo "== temporary Compose workspace"
for kind in container volume network; do
  existing="$(project_resources "$kind")" || exit 2
  [ -z "$existing" ] || { echo "Project $PROJECT already has $kind resources" >&2; exit 2; }
done
trap 'cleanup_all >> "$EV/cleanup_on_exit.log" 2>&1' EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

echo "== image identity"
IMAGE_ID="$(docker image inspect "$IMAGE_REF" --format '{{.Id}}')" || exit 2
REPO_DIGEST="${IMAGE_REF##*@}"
PLATFORM="$(docker version --format '{{.Server.Os}}/{{.Server.Arch}}')" || exit 2
step 010-image-inspect 0 docker image inspect "$IMAGE_REF" --format \
  '{{json .Id}} {{json .RepoDigests}} {{json .Descriptor}} {{.Os}}/{{.Architecture}}'
step 011-image-platform-manifest 0 docker image inspect --platform "$PLATFORM" "$IMAGE_REF" --format \
  '{{json .Id}} {{json .Descriptor}}'
{
  echo "project=$PROJECT"; echo "image_ref=$IMAGE_REF"; echo "image_id=$IMAGE_ID"; echo "repository_manifest_digest=$REPO_DIGEST"
  echo "binding=$(tr -d '\n' < "$ROOT/deploy/docker/image_binding.json")"
  echo "engine=$(docker version --format '{{.Server.Os}}/{{.Server.Arch}} docker {{.Server.Version}}')"
} | tee "$EV/image_identity.txt"

echo "== synthetic configs (non-sensitive, replay only)"
python3 - "$CFG" "$ROOT/deploy/docker/image_binding.json" <<'CONFIG'
import json, shutil, sys
from pathlib import Path

config_root, binding_source = map(Path, sys.argv[1:])


def base(scheduler_id):
    return {
        "storage": {"sqlite_path": "data/stock_agent.sqlite3"},
        "official_evidence_vault": {
            "enabled": True,
            "events_path": "data/evidence_vault/events.jsonl",
            "index_path": "data/evidence_vault/index.json",
            "objects_dir": "data/evidence_vault/objects/sha256",
            "quarantine_dir": "data/evidence_vault/quarantine",
            "report_dir": "data/evidence_vault/reports",
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
        "pi_agent": {"enabled": False},
        "scheduler_ownership": {
            "enabled": True,
            "scheduler_id": scheduler_id,
            "state_dir": "data/scheduler_ownership",
            "heartbeat_stale_seconds": 900,
            "rollback_supported_schema": "1.3",
            "min_free_bytes": 1048576,
        },
        "backup_restore": {
            "enabled": True,
            "bundle_root": "data/backup_restore/bundles",
            "drill_root": "data/backup_restore/drills",
            "event_path": "data/backup_restore/events.jsonl",
            "report_dir": "reports/backup_restore",
        },
    }


def write(role, name, config):
    target = config_root / role
    target.mkdir(parents=True, exist_ok=True)
    (target / name).write_text(json.dumps(config, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


write("container", "watchlist.json", base("container-worker"))
# The restore target is this side's own (empty) state volume; the bundles are
# read from the container side's volume, mounted read-only inside it.
restore = base("legacy-powershell")
restore["backup_restore"]["bundle_root"] = "data/legacy_source/backup_restore/bundles"
write("restore", "watchlist.json", restore)
for role in ("container", "restore"):
    shutil.copyfile(binding_source, config_root / role / "image_binding.json")
print(sorted(path.relative_to(config_root).as_posix() for path in config_root.rglob("*") if path.is_file()))
CONFIG

echo "== merged Compose runtime configuration"
step 020-compose-config-container 0 env R25_CONFIG_DIR="$CFG/container" R25_STATE_VOL=cstate R25_EVIDENCE_VOL=cevidence "${base[@]}" config
step 021-compose-config-restore 0 env R25_CONFIG_DIR="$CFG/restore" R25_STATE_VOL=rstate R25_EVIDENCE_VOL=revidence R25_OTHER_VOL=cstate R25_OTHER_PATH=legacy_source "${all[@]}" config

echo "== acceptance 2: cold start on an empty volume"
# A freshly created named volume mounted at a path the image does not
# pre-create is owned by root; hand it to the service user once, before any
# application command runs. cap_drop ALL also drops CHOWN, so this single
# init step adds only that capability.
step 029-registry-volume-owner 0 env R25_CONFIG_DIR="$CFG/container" R25_STATE_VOL=cstate R25_EVIDENCE_VOL=cevidence \
  "${compose[@]}" -f "$overlay" run --rm -T --user root --cap-add CHOWN research-api \
  chown app:app /var/stock-agent/state/scheduler_ownership
step 029b-registry-volume-owner-inspect 0 docker run --rm --user root --network none \
  --mount type=volume,source="$PROJECT-registry",target=/r --entrypoint sh "$IMAGE_REF" \
  -c 'stat -c "%u:%g %a %n" /r'
stepin 030-layout-cold 0 "$HELPER" C run --rm -T research-api python -B - layout
stepin 031-provenance 0 "$HELPER" C run --rm -T research-api python -B - provenance /drill-config/image_binding.json "$REPO_DIGEST"
stepin 032-registry-init 0 "$HELPER" C run --rm -T research-api python -B - init container-worker
stepin 033-spec-cold 0 "$HELPER" C run --rm -T research-api python -B - spec "$DRILL_CONFIG" "$SCHEDULE_ID" "$SLOT_COLD" "$SCOPE" data/drill/spec_cold.json
stepin 034-intent-cold 0 "$HELPER" C run --rm -T research-api python -B - intent "$SCHEDULE_ID" "$SLOT_COLD" "$SCOPE"
INTENT="$(jget intent)"
step 040-submit-cold 0 C run --rm -T research-api "${SUBMIT[@]}" --config "$DRILL_CONFIG" --scheduler container-worker --planned-slot "$SLOT_COLD" --spec data/drill/spec_cold.json
TASK_ID="$(jget task_id)"; COLD_DEDUP="$(jget dedup)"
stepin 041-counts-submitted 0 "$HELPER" C run --rm -T research-api python -B - counts data/task_runtime/tasks.sqlite3 "$INTENT"
step 042-worker-cold 0 C run --rm -T research-api "${WORKER[@]}" --config "$DRILL_CONFIG" --worker-id w-image-cold --max-tasks 1
stepin 043-counts-executed 0 "$HELPER" C run --rm -T research-api python -B - counts data/task_runtime/tasks.sqlite3 "$INTENT"
stepin 044-evidence-task 0 "$HELPER" C run --rm -T research-api python -B - evidence "$DRILL_CONFIG"
ANNOUNCEMENT="$(jget announcement_path)"
# One institutional evidence object through the public vault entry, so the
# evidence volume (APP_EVIDENCE_ROOT) carries real content as well.
step 044b-vault-import 0 C run --rm -T research-api "${CLI[@]}" evidence-vault-import \
  --config "$DRILL_CONFIG" --file "$ANNOUNCEMENT" \
  --source-url "https://www.cninfo.com.cn?r25-image-fixture" \
  --title "R25 镜像验证合成公告（非真实披露）" --published-at "2026-11-23T09:00:00+08:00" \
  --actor r25-image-drill --actor-role information_analyst
stepin 045-counts-before-rebuild 0 "$HELPER" C run --rm -T research-api python -B - counts data/task_runtime/tasks.sqlite3 "$INTENT"
CURSOR="$(jget outbox_last_sequence)"
BEFORE="$(jget tasks_total)/$(jget outbox_events)"

echo "== rebuild the container on the same volumes"
step 050-down-for-rebuild 0 env R25_CONFIG_DIR="$CFG/container" R25_STATE_VOL=cstate R25_EVIDENCE_VOL=cevidence "${base[@]}" down --remove-orphans
step 051-containers-after-down 0 docker ps -aq --filter "label=com.docker.compose.project=$PROJECT"
stepin 052-layout-rebuilt 0 "$HELPER" C run --rm -T research-api python -B - layout
step 053-submit-replay-after-rebuild 0 C run --rm -T research-api "${SUBMIT[@]}" --config "$DRILL_CONFIG" --scheduler container-worker --planned-slot "$SLOT_COLD" --spec data/drill/spec_cold.json
REPLAY_ID="$(jget task_id)"; REPLAY_DEDUP="$(jget dedup)"
stepin 054-counts-after-rebuild 0 "$HELPER" C run --rm -T research-api python -B - counts data/task_runtime/tasks.sqlite3 "$INTENT"
AFTER="$(jget tasks_total)/$(jget outbox_events)"
stepin 055-outbox-cursor-after-rebuild 0 "$HELPER" C run --rm -T research-api python -B - outbox data/task_runtime/tasks.sqlite3 "$CURSOR" 4
stepin 056-registry-after-rebuild 0 "$HELPER" C run --rm -T research-api python -B - registry "$DRILL_CONFIG"

echo "== acceptance 3: in-image backup of the quiescent store, restore into another empty volume"
step 060-cutover-freeze 0 C run --rm -T research-api "${CLI[@]}" cutover-freeze --config "$DRILL_CONFIG" \
  --actor "$ACTOR" --reason "R25 image verification: quiesce intake before the snapshot" --cutover-id "$CUTOVER_ID"
stepin 061-spec-next 0 "$HELPER" C run --rm -T research-api python -B - spec "$DRILL_CONFIG" "$SCHEDULE_ID" "$SLOT_NEXT" "$SCOPE" data/drill/spec_next.json
step 062-submit-frozen-refused 20 C run --rm -T research-api "${SUBMIT[@]}" --config "$DRILL_CONFIG" --scheduler container-worker --planned-slot "$SLOT_NEXT" --spec data/drill/spec_next.json
step 070-backup-create 0 C run --rm -T research-api "${CLI[@]}" backup-create --config "$DRILL_CONFIG" --require-pass
BUNDLE="$(jget text:Bundle)"                 # project-relative on the container side
BUNDLE_RO="data/legacy_source/${BUNDLE#data/}"  # same bundle through the read-only mount
step 071-backup-verify 0 C run --rm -T research-api "${CLI[@]}" backup-verify --config "$DRILL_CONFIG" --backup "$BUNDLE" --require-pass
stepin 072-counts-quiescent 0 "$HELPER" C run --rm -T research-api python -B - counts data/task_runtime/tasks.sqlite3 "$INTENT"
step 080-restore-drill 0 CR run --rm -T research-api "${CLI[@]}" restore-drill --config "$DRILL_CONFIG" \
  --backup "$BUNDLE_RO" --require-pass
RESTORED="$(jget "text:Restored root")"
stepin 081-compare 0 "$HELPER" CR run --rm -T research-api python -B - compare data/legacy_source /live-evidence "$RESTORED/data"
stepin 082-resolved-container 0 "$HELPER" C run --rm -T research-api python -B - resolved "$DRILL_CONFIG"

echo "== rollback: the legacy side takes the recovered store"
python3 - "$CFG" "$RESTORED" <<'CONFIG'
import json, sys
from pathlib import Path

config_root, restored = Path(sys.argv[1]), sys.argv[2].rstrip("/")
container = json.loads((config_root / "container" / "watchlist.json").read_text(encoding="utf-8"))


def on_tree(config, prefix):
    config = json.loads(json.dumps(config))
    tree = "{0}/{1}/data".format(prefix, restored.replace("data/", "", 1)) if prefix else "{0}/data".format(restored)
    config["storage"]["sqlite_path"] = "{0}/stock_agent.sqlite3".format(tree)
    config["task_runtime"]["db_path"] = "{0}/task_runtime/tasks.sqlite3".format(tree)
    config["kimi_sources"]["query_store_path"] = "{0}/kimi/query_intake.sqlite3".format(tree)
    return config


# Baseline for the increment reconciliation: the recovered store, read
# through the read-only cross mount below the container side's state root.
(config_root / "container" / "baseline.json").write_text(
    json.dumps(on_tree(container, "data/restored_readonly"), ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
wal_baseline = json.loads(json.dumps(container))
wal_baseline["task_runtime"]["db_path"] = "data/restored_readonly/drill/wal_probe/tasks.sqlite3"
(config_root / "container" / "baseline-wal.json").write_text(
    json.dumps(wal_baseline, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
retry = json.loads(json.dumps(container))
retry["task_runtime"]["db_path"] = "data/restored_readonly/drill/retry/tasks.sqlite3"
(config_root / "container" / "baseline-retry.json").write_text(
    json.dumps(retry, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
# The legacy side's working config: same shared registry, recovered store.
legacy = on_tree(container, "")
# The evidence the legacy side must see is the RECOVERED evidence: its vault
# and personal namespace point into the restored tree.
vault = "{0}/data/evidence_vault".format(restored)
legacy["official_evidence_vault"].update({
    "events_path": vault + "/events.jsonl", "index_path": vault + "/index.json",
    "objects_dir": vault + "/objects/sha256", "quarantine_dir": vault + "/quarantine",
    "report_dir": vault + "/reports"})
legacy["personal_evidence"] = {"root": "{0}/data/personal_evidence".format(restored)}
legacy["scheduler_ownership"]["scheduler_id"] = "legacy-powershell"
target = config_root / "legacy"
target.mkdir(parents=True, exist_ok=True)
(target / "watchlist.json").write_text(json.dumps(legacy, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
print("recovered_store={0}/data".format(restored))
CONFIG
WAL_PROBE=data/drill/wal_probe/tasks.sqlite3
WAL_PROBE_RO=data/restored_readonly/drill/wal_probe/tasks.sqlite3
stepin 086-wal-fixture 0 "$HELPER" L run --rm -T research-api python -B - wal-fixture "$WAL_PROBE"
stepin 087-wal-inspect-before 0 "$HELPER" CB run --rm -T research-api python -B - wal-fingerprint "$WAL_PROBE_RO"
WAL_BEFORE="$(jget sha256)"
step 088-reconcile-unread-wal-refused 21 CB run --rm -T research-api "${CLI[@]}" cutover-reconcile-rollback \
  --config "$DRILL_CONFIG" --baseline-config /drill-config/baseline-wal.json
if ! grep -q 'nonempty WAL could not be read' "$LAST"; then FAILED=$((FAILED + 1)); fi
stepin 089-wal-inspect-after 0 "$HELPER" CB run --rm -T research-api python -B - wal-fingerprint "$WAL_PROBE_RO"
[ "$(jget sha256)" = "$WAL_BEFORE" ] || FAILED=$((FAILED + 1))
step 090-rollback-refused-without-reconciliation 21 CB run --rm -T research-api "${CLI[@]}" cutover-switch --config "$DRILL_CONFIG" \
  --to legacy-powershell --actor "$ACTOR" --reason "R25 image verification rollback" --cutover-id "$CUTOVER_ID" --expected-generation 1
RETRY_BASELINE=data/drill/retry/tasks.sqlite3
RETRY_BASELINE_RO=data/restored_readonly/drill/retry/tasks.sqlite3
RETRY_CONFIG=/drill-config/baseline-retry.json
stepin 091a-wal-retry-fixture 0 "$HELPER" L run --rm -T research-api python -B - wal-retry-fixture \
  "$RESTORED/data/task_runtime/tasks.sqlite3" "$RETRY_BASELINE" data/drill/retry.wal
step 091b-reconcile-before-retry 0 CB run --rm -T research-api "${CLI[@]}" cutover-reconcile-rollback \
  --config "$DRILL_CONFIG" --baseline-config "$RETRY_CONFIG"
stepin 091c-registry-before-retry 0 "$HELPER" CB run --rm -T research-api python -B - registry "$DRILL_CONFIG"
BEFORE_RETRY_STATE="$(jget owner)/$(jget generation)/$(jget freeze_id)"
step 091d-expose-retry-wal 0 L run --rm -T research-api sh -c \
  'mv "$APP_STATE_ROOT/drill/retry.wal" "$APP_STATE_ROOT/drill/retry/tasks.sqlite3-wal"'
stepin 091e-retry-wal-before 0 "$HELPER" CB run --rm -T research-api python -B - wal-fingerprint "$RETRY_BASELINE_RO"
RETRY_WAL_HASHES="$(jget sha256)"
step 091f-reconciliation-retry-refused 21 CB run --rm -T research-api "${CLI[@]}" cutover-reconcile-rollback \
  --config "$DRILL_CONFIG" --baseline-config "$RETRY_CONFIG"
if ! grep -q 'nonempty WAL could not be read' "$LAST"; then FAILED=$((FAILED + 1)); fi
stepin 091g-retry-wal-after 0 "$HELPER" CB run --rm -T research-api python -B - wal-fingerprint "$RETRY_BASELINE_RO"
[ "$(jget sha256)" = "$RETRY_WAL_HASHES" ] || FAILED=$((FAILED + 1))
stepin 091h-registry-after-retry 0 "$HELPER" CB run --rm -T research-api python -B - registry "$DRILL_CONFIG"
[ "$(jget owner)/$(jget generation)/$(jget freeze_id)" = "$BEFORE_RETRY_STATE" ] || FAILED=$((FAILED + 1))
REGISTRY_AFTER_RETRY="$(jget sha256)"
step 091i-rollback-refused-after-retry 21 CB run --rm -T research-api "${CLI[@]}" cutover-switch --config "$DRILL_CONFIG" \
  --to legacy-powershell --actor "$ACTOR" --reason "R25 image verification rollback" --cutover-id "$CUTOVER_ID" --expected-generation 1
if ! grep -q 'rollback switch blocked by readiness checks' "$LAST"; then FAILED=$((FAILED + 1)); fi
stepin 091j-registry-after-refused-switch 0 "$HELPER" CB run --rm -T research-api python -B - registry "$DRILL_CONFIG"
[ "$(jget sha256)" = "$REGISTRY_AFTER_RETRY" ] || FAILED=$((FAILED + 1))
step 091k-rollback-reconcile 0 CB run --rm -T research-api "${CLI[@]}" cutover-reconcile-rollback \
  --config "$DRILL_CONFIG" --baseline-config /drill-config/baseline.json
step 092-rollback-switch 0 CB run --rm -T research-api "${CLI[@]}" cutover-switch --config "$DRILL_CONFIG" \
  --to legacy-powershell --actor "$ACTOR" --reason "R25 image verification rollback" --cutover-id "$CUTOVER_ID" --expected-generation 1
step 093-rollback-thaw 0 L run --rm -T research-api "${CLI[@]}" cutover-thaw --config "$DRILL_CONFIG" --actor "$ACTOR" --reason "R25 image verification: legacy resumed"
stepin 094-resolved-legacy 0 "$HELPER" L run --rm -T research-api python -B - resolved "$DRILL_CONFIG"
stepin 095-registry-legacy 0 "$HELPER" L run --rm -T research-api python -B - registry "$DRILL_CONFIG"

echo "== the legacy side accepts the next slot from the recovered store"
stepin 100-spec-legacy-next 0 "$HELPER" L run --rm -T research-api python -B - spec "$DRILL_CONFIG" "$SCHEDULE_ID" "$SLOT_NEXT" "$SCOPE" data/drill/spec_next.json
step 101-legacy-submit-next 0 L run --rm -T research-api "${SUBMIT[@]}" --config "$DRILL_CONFIG" --scheduler legacy-powershell --planned-slot "$SLOT_NEXT" --spec data/drill/spec_next.json
LEGACY_TASK="$(jget task_id)"; LEGACY_DEDUP="$(jget dedup)"
step 102-legacy-worker 0 L run --rm -T research-api "${WORKER[@]}" --config "$DRILL_CONFIG" --worker-id w-image-legacy --max-tasks 1
stepin 103-spec-legacy-cold 0 "$HELPER" L run --rm -T research-api python -B - spec "$DRILL_CONFIG" "$SCHEDULE_ID" "$SLOT_COLD" "$SCOPE" data/drill/spec_cold.json
step 104-legacy-replay-cold-slot 0 L run --rm -T research-api "${SUBMIT[@]}" --config "$DRILL_CONFIG" --scheduler legacy-powershell --planned-slot "$SLOT_COLD" --spec data/drill/spec_cold.json
LEGACY_REPLAY_ID="$(jget task_id)"
LEGACY_DB="$RESTORED/data/task_runtime/tasks.sqlite3"
stepin 105-counts-legacy 0 "$HELPER" L run --rm -T research-api python -B - counts "$LEGACY_DB" "$INTENT"
stepin 106-outbox-legacy 0 "$HELPER" L run --rm -T research-api python -B - outbox "$LEGACY_DB" "$CURSOR" 8
stepin 107-layout-legacy 0 "$HELPER" L run --rm -T research-api python -B - layout

echo "== probe container: the runtime configuration Docker actually applied"
step 110-probe-run 0 C run --name "$PROBE" -T research-api python -B -c 'print("probe")'
step 111-probe-inspect 0 docker inspect "$PROBE"
OBSERVED_IMAGE_ID="$(docker inspect "$PROBE" --format '{{.Image}}')"
echo "$OBSERVED_IMAGE_ID" > "$EV/container_image_id.txt"
[ "$OBSERVED_IMAGE_ID" = "$IMAGE_ID" ] || FAILED=$((FAILED + 1))
step 112-probe-remove 0 docker rm "$PROBE"

echo "== cleanup, inventoried by project label"
step 120-cleanup 0 cleanup_all
: > "$EV/cleanup_inventory.txt"
for kind in container volume network; do
  count="$(project_resources "$kind" | wc -l | tr -d ' ')" || exit 2
  echo "$kind $count" | tee -a "$EV/cleanup_inventory.txt"
  [ "$count" -eq 0 ] || FAILED=$((FAILED + 1))
done
[ "$FAILED" -ne 0 ] || trap - EXIT

{
  echo "cold_task_id=$TASK_ID"; echo "cold_dedup=$COLD_DEDUP"
  echo "replay_task_id=$REPLAY_ID"; echo "replay_dedup=$REPLAY_DEDUP"
  echo "tasks/outbox before rebuild=$BEFORE"; echo "tasks/outbox after rebuild=$AFTER"
  echo "outbox_cursor_after_rebuild_read_from=$CURSOR"
  echo "bundle=$BUNDLE"; echo "restored_root=$RESTORED"
  echo "legacy_next_task_id=$LEGACY_TASK"; echo "legacy_next_dedup=$LEGACY_DEDUP"
  echo "legacy_replay_task_id=$LEGACY_REPLAY_ID"; echo "failed_steps=$FAILED"
} | tee "$EV/summary.txt"

[ "$COLD_DEDUP" = "RECORDED" ] && [ "$TASK_ID" = "$REPLAY_ID" ] && [ "$REPLAY_DEDUP" = "DUPLICATE" ] \
  && [ "$BEFORE" = "$AFTER" ] && [ "$FAILED" -eq 0 ] && [ "$TASK_ID" = "$LEGACY_REPLAY_ID" ] \
  && [ "$LEGACY_TASK" != "$TASK_ID" ] && [ "$LEGACY_DEDUP" = "RECORDED" ]
