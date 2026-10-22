#!/usr/bin/env bash
# R08 build wrapper: the ONLY supported way to build the candidate image.
#
# Responsibilities the Dockerfile cannot do for itself:
#   1. freeze the build context's provenance (commit/dirty/tree/lock hashes)
#      into PROVENANCE.json BEFORE `docker build` (it ships inside the image);
#   2. after the build, bind the observed image digest to the provenance hash
#      EXTERNALLY (deploy/docker/image_binding.json) — the digest never goes
#      inside the image;
#   3. refuse floating base images and dirty worktrees unless --allow-dirty
#      is given (the binding then records source_dirty=true, never hides it).
#
# Usage: scripts/build_image.sh [--allow-dirty] [IMAGE_TAG]
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ALLOW_DIRTY="${1:-}"
if [ "${ALLOW_DIRTY}" = "--allow-dirty" ]; then shift; else ALLOW_DIRTY=""; fi
TAG="${1:-stock-agent-research:candidate}"
LOCK="requirements.linux-py312.lock.txt"
BINDING_DIR="${ROOT}/deploy/docker"

cd "${ROOT}"

# --- base-image pin guard: every FROM must carry @sha256: -------------------
if ! grep -E '^ARG (PYTHON_IMAGE|NODE_IMAGE)=' deploy/docker/Dockerfile \
        | grep -q '@sha256:[0-9a-f]{64}$'; then
  echo "BLOCKED: a base image is not pinned by digest (A39 stop condition)" >&2
  exit 2
fi

# --- source identity (builder side has .git by definition) -------------------
COMMIT="$(git rev-parse HEAD 2>/dev/null || echo unknown)"
DIRTY=0
if [ -n "$(git status --porcelain 2>/dev/null)" ]; then
  DIRTY=1
  if [ "${ALLOW_DIRTY}" != "--allow-dirty" ]; then
    echo "BLOCKED: worktree is dirty; pass --allow-dirty to record (not hide) it" >&2
    exit 2
  fi
fi

# --- frozen provenance into the build context --------------------------------
python - "$ROOT" "$COMMIT" "$DIRTY" <<'PY'
import hashlib, json, sys
from pathlib import Path

root = Path(sys.argv[1])
commit, dirty = sys.argv[2], sys.argv[3] == "1"
sys.path.insert(0, str(root))
from stock_agent.release_provenance import build_provenance, provenance_sha256

provenance = build_provenance(
    root,
    source_commit=commit,
    source_dirty=dirty,
    python_locks={
        "requirements.lock.txt":
            hashlib.sha256((root / "requirements.lock.txt").read_bytes()).hexdigest(),
        "requirements.linux-py312.lock.txt":
            hashlib.sha256((root / "requirements.linux-py312.lock.txt").read_bytes()).hexdigest(),
    },
    pi_package_lock=root / "pi_runtime" / "package-lock.json",
)
(root / "PROVENANCE.json").write_text(
    json.dumps(provenance, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
    encoding="utf-8")
print("provenance sha256:", provenance_sha256(provenance))
PY

# --- build -------------------------------------------------------------------
docker build -f deploy/docker/Dockerfile -t "${TAG}" .

# --- external digest binding (NEVER inside the image) ------------------------
DIGEST="$(docker image inspect "${TAG}" --format '{{index .RepoDigests 0}}' \
          | sed 's/.*@//')"
if [ -z "${DIGEST}" ] || [ "${DIGEST}" = "@" ]; then
  # Locally built images have no RepoDigest until pushed; fall back to the
  # config digest (identical content, different namespace) and label it.
  DIGEST="sha256:$(docker image inspect "${TAG}" --format '{{.Id}}' | sed 's/.*://')"
  echo "NOTE: image not pushed; binding uses the config digest ${DIGEST}" >&2
fi
python - "$ROOT" "${DIGEST}" <<'PY'
import json, sys
from pathlib import Path

root = Path(sys.argv[1]); digest = sys.argv[2]
sys.path.insert(0, str(root))
from stock_agent.release_provenance import write_image_binding

provenance = json.loads((root / "PROVENANCE.json").read_text(encoding="utf-8"))
binding = write_image_binding(
    root / "deploy" / "docker" / "image_binding.json",
    image_digest=digest, provenance=provenance,
    builder="scripts/build_image.sh")
print("binding:", binding["image_digest"], binding["provenance_sha256"])
PY
rm -f PROVENANCE.json   # keep the working tree clean; it lives in the image + binding
echo "OK: ${TAG} ${DIGEST}"
