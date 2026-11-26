#!/usr/bin/env bash
# Run one research pass in the foreground.
# macOS/Linux entry; Windows keeps scripts/run_once.ps1. See docs/macos-runbook.md.
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
exec python3 "$ROOT/scripts/local_ops.py" run-once "$@"
