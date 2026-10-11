from __future__ import annotations

import re
from typing import Iterable, List


SPACE_RE = re.compile(r"\s+")
PUNCT_RE = re.compile(r"[\s\-_—,，.。!！?？:：;；'\"“”‘’()（）\[\]【】]+")


def normalize_text(value: str) -> str:
    return PUNCT_RE.sub("", value or "").lower()


def compact_space(value: str) -> str:
    return SPACE_RE.sub(" ", value or "").strip()


def unique_keep_order(values: Iterable[str]) -> List[str]:
    seen = set()
    result = []
    for value in values:
        if not value or value in seen:
            continue
        seen.add(value)
        result.append(value)
    return result

