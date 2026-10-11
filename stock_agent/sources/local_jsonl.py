from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, Iterable, List

from ..config import project_path
from ..models import RawSignal
from .example_data import looks_like_example_path, mark_payload_example


def collect_local_jsonl(source_configs: Iterable[Dict[str, Any]]) -> List[RawSignal]:
    signals: List[RawSignal] = []
    for source_config in source_configs:
        if not source_config.get("enabled", True):
            continue
        path = project_path(source_config["path"])
        if not path.exists():
            continue
        signals.extend(_read_jsonl(path, source_config.get("name", path.name)))
    return signals


def _read_jsonl(path: Path, source_name: str) -> List[RawSignal]:
    is_example = looks_like_example_path(path)
    signals: List[RawSignal] = []
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ValueError("Invalid JSONL at {0}:{1}: {2}".format(path, line_number, exc)) from exc

        title = str(row.get("title") or "").strip()
        if not title:
            continue
        payload = dict(row)
        if is_example:
            mark_payload_example(payload)
        signals.append(
            RawSignal(
                source=str(row.get("source") or source_name),
                source_type=str(row.get("source_type") or "manual"),
                title=title,
                summary=str(row.get("summary") or row.get("content") or ""),
                url=str(row.get("url") or ""),
                published_at=row.get("published_at"),
                payload=payload,
            )
        )
    return signals

