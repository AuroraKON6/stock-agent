#!/usr/bin/env bash
# Start the background watch daemon.
# macOS/Linux entry; Windows keeps scripts/start_agent.ps1. See docs/macos-runbook.md.
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
exec python3 "$ROOT/scripts/local_ops.py" start-agent "$@"
