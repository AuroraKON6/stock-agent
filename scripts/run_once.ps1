Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$Root = Split-Path -Parent $PSScriptRoot
Set-Location $Root

# Entry points must use the locked interpreter; PATH python is not a supported
# fallback because it resolves to an uncontrolled environment (e.g. Anaconda).
$Python = Join-Path $Root ".venv312\Scripts\python.exe"
if (-not (Test-Path -LiteralPath $Python)) {
    throw "Locked interpreter not found: $Python (expected .venv312; PATH python is not a supported fallback)"
}

# Prefer the real local config; the example config is development-only and its
# use is always announced so sample input can never silently masquerade as real.
$ConfigRel = "config/watchlist.example.json"
if (Test-Path -LiteralPath (Join-Path $Root "config\watchlist.local.json")) {
    $ConfigRel = "config/watchlist.local.json"
} else {
    Write-Host "WARNING: config/watchlist.local.json not found; using development example config."
}

& $Python -m stock_agent run --config $ConfigRel
exit $LASTEXITCODE
