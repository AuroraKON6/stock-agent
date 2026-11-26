#!/usr/bin/env bash
# Stop the background watch daemon.
# macOS/Linux entry; Windows keeps scripts/stop_agent.ps1. See docs/macos-runbook.md.
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
exec python3 "$ROOT/scripts/local_ops.py" stop-agent "$@"
