from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, List

from .config import project_path


@dataclass
class SkillInfo:
    name: str
    path: str
    summary: str


def discover_skills(path_value: str) -> List[SkillInfo]:
    root = project_path(path_value)
    if not root.exists():
        return []
    skill_files = sorted(root.rglob("SKILL.md"))
    by_name = {}
    for path in skill_files:
        skill = _read_skill(path)
        key = skill.name.lower()
        current = by_name.get(key)
        if current is None or len(Path(skill.path).parts) < len(Path(current.path).parts):
            by_name[key] = skill
    return sorted(by_name.values(), key=lambda item: item.name.lower())


def format_skill_table(skills: Iterable[SkillInfo]) -> str:
    rows = list(skills)
    if not rows:
        return "未发现 skills。请把技能目录放入 skills/，并确保每个技能目录包含 SKILL.md。"
    lines = ["已发现 {0} 个 skill:".format(len(rows))]
    for skill in rows:
        lines.append("- {0}: {1} ({2})".format(skill.name, skill.summary, skill.path))
    return "\n".join(lines)


def _read_skill(path: Path) -> SkillInfo:
    text = path.read_text(encoding="utf-8", errors="ignore")
    metadata = _frontmatter(text)
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    heading = next((line.lstrip("#").strip() for line in lines if line.startswith("#")), path.parent.name)
    name = metadata.get("name") or heading or path.parent.name
    summary = metadata.get("description") or ""
    for line in lines:
        if summary:
            break
        if line.startswith("#"):
            continue
        if line.startswith("---"):
            continue
        summary = line[:120]
        break
    return SkillInfo(name=name, path=str(path), summary=(summary or "无摘要")[:180])


def _frontmatter(text: str) -> dict:
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return {}
    metadata = {}
    front_lines = []
    for line in lines[1:]:
        stripped = line.strip()
        if stripped == "---":
            break
        front_lines.append(line)

    index = 0
    while index < len(front_lines):
        line = front_lines[index]
        stripped = line.strip()
        if ":" not in stripped:
            index += 1
            continue
        key, value = stripped.split(":", 1)
        key = key.strip()
        value = value.strip()
        if value in [">", "|"]:
            folded = []
            index += 1
            while index < len(front_lines):
                next_line = front_lines[index]
                next_stripped = next_line.strip()
                if next_stripped and not next_line.startswith((" ", "\t")) and ":" in next_stripped:
                    index -= 1
                    break
                if next_stripped:
                    folded.append(next_stripped)
                index += 1
            metadata[key] = " ".join(folded)
        else:
            metadata[key] = value.strip('"').strip("'")
        index += 1
    return metadata
