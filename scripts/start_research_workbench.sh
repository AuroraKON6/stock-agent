#!/usr/bin/env bash
# Start the local Research Runtime Workbench (loopback only).
# macOS/Linux entry; Windows keeps scripts/start_research_workbench.ps1. See docs/macos-runbook.md.
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
exec python3 "$ROOT/scripts/local_ops.py" start-workbench "$@"
