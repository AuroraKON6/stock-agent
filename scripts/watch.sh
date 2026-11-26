#!/usr/bin/env bash
# Run research continuously in the foreground.
# macOS/Linux entry; Windows keeps scripts/watch.ps1. See docs/macos-runbook.md.
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
exec python3 "$ROOT/scripts/local_ops.py" watch "$@"
