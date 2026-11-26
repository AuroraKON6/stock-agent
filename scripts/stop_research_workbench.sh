#!/usr/bin/env bash
# Stop the Research Runtime Workbench.
# macOS/Linux entry; Windows keeps scripts/stop_research_workbench.ps1. See docs/macos-runbook.md.
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
exec python3 "$ROOT/scripts/local_ops.py" stop-workbench "$@"
