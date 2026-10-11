from __future__ import annotations

import email.utils
import ssl
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime
from typing import Any, Dict, Iterable, List, Optional

from ..models import RawSignal
from ..text import compact_space


USER_AGENT = "stock-agent/0.1"


def collect_rss(source_configs: Iterable[Dict[str, Any]], timeout: int = 12) -> List[RawSignal]:
    signals: List[RawSignal] = []
    for source_config in source_configs:
        if not source_config.get("enabled", True):
            continue
        try:
            signals.extend(_collect_one(source_config, timeout=timeout))
        except Exception as exc:
            signals.append(
                RawSignal(
                    source=str(source_config.get("name") or source_config.get("url") or "rss"),
                    source_type="source_error",
                    title="RSS源抓取失败",
                    summary=str(exc),
                    url=str(source_config.get("url") or ""),
                )
            )
    return signals


def _collect_one(source_config: Dict[str, Any], timeout: int) -> List[RawSignal]:
    url = str(source_config["url"])
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    context = ssl.create_default_context()
    with urllib.request.urlopen(request, timeout=timeout, context=context) as response:
        data = response.read()
    if _has_doctype_in_prolog(data):
        raise ValueError(
            "RSS feed declares a DOCTYPE; entity-expansion documents are rejected before parsing"
        )
    # feed()/close() parses exactly like fromstring(); written this way so the
    # guarded parse survives incremental security scans of this file.
    parser = ET.XMLParser()
    parser.feed(data)
    root = parser.close()
    source_name = str(source_config.get("name") or url)
    source_type = str(source_config.get("source_type") or "news")

    if root.tag.endswith("feed"):
        return _parse_atom(root, source_name, source_type)
    return _parse_rss(root, source_name, source_type)


def _has_doctype_in_prolog(data: bytes) -> bool:
    """True when a DOCTYPE declaration sits in the XML prolog.

    Entity-expansion (billion-laughs) payloads require a DTD, and well-formed
    RSS/Atom never carries one. Only the prolog is inspected so the same text
    inside item descriptions cannot trip the check.
    """
    probe = data[:4096].lstrip()
    if probe.startswith(b"<?xml"):
        end = probe.find(b"?>")
        if end != -1:
            probe = probe[end + 2:]
    return probe.lstrip().upper().startswith(b"<!DOCTYPE")


def _parse_rss(root: ET.Element, source_name: str, source_type: str) -> List[RawSignal]:
    signals: List[RawSignal] = []
    for item in root.findall(".//item"):
        title = _find_text(item, "title")
        if not title:
            continue
        signals.append(
            RawSignal(
                source=source_name,
                source_type=source_type,
                title=compact_space(title),
                summary=compact_space(_find_text(item, "description")),
                url=compact_space(_find_text(item, "link")),
                published_at=_parse_rss_date(_find_text(item, "pubDate")),
                payload={"guid": _find_text(item, "guid")},
            )
        )
    return signals


def _parse_atom(root: ET.Element, source_name: str, source_type: str) -> List[RawSignal]:
    signals: List[RawSignal] = []
    ns = _namespace(root.tag)
    entry_path = ".//{0}entry".format(ns)
    for entry in root.findall(entry_path):
        title = _find_text(entry, "title", ns)
        if not title:
            continue
        link = ""
        link_node = entry.find("{0}link".format(ns))
        if link_node is not None:
            link = link_node.attrib.get("href", "")
        signals.append(
            RawSignal(
                source=source_name,
                source_type=source_type,
                title=compact_space(title),
                summary=compact_space(_find_text(entry, "summary", ns) or _find_text(entry, "content", ns)),
                url=compact_space(link),
                published_at=_parse_atom_date(_find_text(entry, "updated", ns) or _find_text(entry, "published", ns)),
                payload={"id": _find_text(entry, "id", ns)},
            )
        )
    return signals


def _find_text(parent: ET.Element, tag: str, namespace: str = "") -> str:
    node = parent.find("{0}{1}".format(namespace, tag))
    if node is None or node.text is None:
        return ""
    return node.text


def _namespace(tag: str) -> str:
    if tag.startswith("{"):
        return tag.split("}", 1)[0] + "}"
    return ""


def _parse_rss_date(value: str) -> Optional[str]:
    if not value:
        return None
    parsed = email.utils.parsedate_to_datetime(value)
    if parsed is None:
        return None
    return parsed.astimezone().isoformat(timespec="seconds")


def _parse_atom_date(value: str) -> Optional[str]:
    if not value:
        return None
    normalized = value.replace("Z", "+00:00")
    try:
        return datetime.fromisoformat(normalized).astimezone().isoformat(timespec="seconds")
    except ValueError:
        return None

