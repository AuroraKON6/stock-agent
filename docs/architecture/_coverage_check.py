# -*- coding: utf-8 -*-
"""Coverage cross-check: code files vs manifest vs diagram HTML vs cards."""
import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
man = json.loads((ROOT / "docs/architecture/manifest.json").read_text(encoding="utf-8"))
covered = set()
for b in man["blocks"]:
    for f in b["file"].split(" + "):
        covered.add(f)
actual = {p.as_posix().replace(ROOT.as_posix() + "/", "") for p in (ROOT / "stock_agent").rglob("*.py")}
missing = sorted(actual - covered)
extra = sorted(covered - actual)
print("actual py files:", len(actual))
print("manifest blocks:", len(man["blocks"]))
print("NOT covered as its own block:", missing)
print("manifest points to nonexistent:", extra)

htmltxt = (ROOT / "docs/architecture/MASTER_DIAGRAM.html").read_text(encoding="utf-8")
node_ids = set(re.findall(r'id="([a-z0-9_-]+)"', htmltxt))
block_ids = {b["id"] for b in man["blocks"]}
print("blocks missing from diagram HTML:", sorted(block_ids - node_ids))

cards = {p.stem for p in (ROOT / "docs/architecture").rglob("*.md") if p.name != "README.md"}
no_card = sorted(block_ids - cards)
print("blocks without matching card:", no_card)

# also list non-stock_agent code assets for awareness
scripts = sorted(p.name for p in (ROOT / "scripts").glob("*.ps1"))
print("scripts count:", len(scripts))
cfg = sorted(p.name for p in (ROOT / "config").glob("*.json"))
print("config count:", len(cfg))
tests = len(list((ROOT / "tests").glob("test_*.py")))
print("test files:", tests)
