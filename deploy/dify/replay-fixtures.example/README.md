# Replay fixtures (example, empty)

The isolated R17 drill records a synthetic answer per case and role:
`<case_id>/<role_id>.json`, or `<role_id>.attempt-<n>.json` to pin the n-th
attempt of that step. No file means the attempt has no recorded answer and
fails retryably. `scripts/r17_role_drill.py prepare` generates its own
fixtures in a private temporary directory; nothing here is used at runtime.
