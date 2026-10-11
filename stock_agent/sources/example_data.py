"""Example-data provenance marking for local JSONL inputs.

Development fixtures (``*.example.jsonl`` files and files under an ``examples``
directory) must never masquerade as real production intelligence. Both local
JSONL collectors tag every signal they read from such paths with
``payload["data_provenance"] = "example"`` so downstream reports, storage, and
value-validation tooling can separate sample data from real input. The marker
is additive: rows that already carry a ``data_provenance`` value keep it.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict

EXAMPLE_PROVENANCE = "example"


def looks_like_example_path(path: Path) -> bool:
    """True when the file name or a parent directory marks the file as example data."""

    name = path.name.lower()
    if ".example." in name or name.endswith(".example") or name.startswith("example."):
        return True
    return any(part.lower() == "examples" for part in path.parts)


def mark_payload_example(payload: Dict[str, Any]) -> Dict[str, Any]:
    """Return the payload with the example provenance marker set when absent."""

    if payload.get("data_provenance") in (None, ""):
        payload["data_provenance"] = EXAMPLE_PROVENANCE
    return payload
