#!/usr/bin/env bash
# Controlled read-only Go/No-Go checklist.
# macOS/Linux entry; Windows keeps scripts/go_live_check.ps1. See docs/macos-runbook.md.
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
exec python3 "$ROOT/scripts/local_ops.py" go-live-check "$@"
