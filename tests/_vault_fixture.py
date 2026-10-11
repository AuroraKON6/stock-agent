"""Shared fixture: build a REAL evidence vault via the production importer.

Tests never hand-write index files. The vault (events chain, content-addressed
objects, projection) is created through ``import_official_document`` inside an
isolated workspace, so every consumer (supply, safe context, worker) sees the
genuine producer schema and the authoritative verification path.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, Tuple


def vault_config(root: Path, base: Dict[str, Any] | None = None) -> Dict[str, Any]:
    config = json.loads(json.dumps(base or {}))
    config["storage"] = {"sqlite_path": str(root / "data" / "stock_agent.sqlite3")}
    config["official_evidence_vault"] = {
        "enabled": True,
        "events_path": str(root / "vault" / "events.jsonl"),
        "index_path": str(root / "vault" / "index.json"),
        "objects_dir": str(root / "vault" / "objects"),
        "quarantine_dir": str(root / "vault" / "quarantine"),
        "report_dir": str(root / "vault" / "reports"),
        "allow_network_fetch": False,
        "automatic_approval": False,
    }
    operations = config.setdefault("research_operations", {})
    operations["enforce_roster"] = False  # test actors pass as unverified principals
    operations["roster_path"] = str(root / "vault" / "roster.json")
    operations["restrictions_path"] = str(root / "vault" / "restrictions.json")
    (root / "vault").mkdir(parents=True, exist_ok=True)
    (root / "vault" / "restrictions.json").write_text(
        json.dumps({"version": "empty-test-restrictions", "restricted": []}), encoding="utf-8"
    )
    return config


def import_document(config: Dict[str, Any], root: Path, *, content: str, title: str,
                    signal_ids=None, seed_signals: bool = True) -> Tuple[str, str]:
    """Import one official document through the real importer.

    WRITE GUARD: every vault write path in the config must resolve INSIDE the
    supplied temporary root. This is the structural prevention for the
    2026-09-21 incident (docs/implementation/INCIDENT_20260921_VAULT_TEST_WRITE.md):
    a config that still points at the real workspace fails HERE, before any
    ledger write, regardless of what the calling fixture remembered to override.

    Returns ``(document_id, sha256)``.
    """

    from stock_agent.evidence_vault import evidence_vault_config, import_official_document
    from stock_agent.models import AnalyzedSignal, now_iso
    from stock_agent.storage import SignalStore

    cfg = evidence_vault_config(config)
    root_resolved = root.resolve()
    for key in ("events_path", "objects_dir", "quarantine_dir", "report_dir"):
        raw = str(cfg.get(key) or "")
        if not raw:
            continue
        candidate = Path(raw)
        if not candidate.is_absolute():
            raise AssertionError(
                "vault write path {0}={1} is relative (would resolve against the real project root); "
                "tests must configure absolute paths under the temporary root".format(key, raw)
            )
        try:
            candidate.resolve().relative_to(root_resolved)
        except ValueError as exc:
            raise AssertionError(
                "vault write path {0}={1} escapes the test root {2}; refusing to touch the real ledger".format(key, raw, root_resolved)
            ) from exc

    signal_ids = ["sig-test-0001"] if signal_ids is None else signal_ids
    if seed_signals:
        database = Path(config["storage"]["sqlite_path"])
        assert database.is_absolute() and database.resolve().is_relative_to(root_resolved)
        store = SignalStore(database)
        try:
            store.upsert_many([AnalyzedSignal(
                signal_id=sid, source="manual", source_type="filing", title=title,
                summary="Synthetic fixture only", url="https://www.cninfo.com.cn/disclosure/fixture",
                published_at=now_iso(), collected_at=now_iso(), symbols=["600519"],
                payload={"data_classification": "public"},
            ) for sid in signal_ids])
        finally:
            store.close()
    source = root / "vault" / "incoming"
    source.mkdir(parents=True, exist_ok=True)
    file_path = source / "{0}.txt".format(abs(hash(title)) % 100000)
    file_path.write_text(content, encoding="utf-8")
    result = import_official_document(
        config,
        str(file_path),
        source_url="https://www.cninfo.com.cn/new/disclosure/detail?plate=szse&orgId=gssz0000001",
        title=title,
        published_at="2026-09-20T18:00:00+08:00",
        actor="test-evidence-collector",
        actor_role="information_analyst",
        signal_ids=signal_ids,
    )
    document = result.get("document") or result
    return str(document.get("document_id") or ""), str(document.get("sha256") or "")
