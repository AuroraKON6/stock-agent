# [00-06] skills.py — 本地技能发现（非生产）

| 层 | 行数 | 上游依赖 | 被依赖 |
|---|---|---|---|
| 00-foundation | 99 | 无 | 仅 cli 的 `list-skills` 命令 |

## 职责
扫描本地 `skills/` 目录的 SKILL.md 并解析 frontmatter，供 CLI 展示。**与生产 Pi 方法供应链无关**——生产方法在 pi_skills.py + `.pi/skills/`，本模块只是历史/离线发现通道。

## 关键构件
- `SkillInfo`（L10）— name/description/path。
- `discover_skills()`（L17）— `rglob("SKILL.md")`，同名取路径更浅者。
- `_frontmatter()`（L61）— YAML frontmatter 解析（支持 `>`/`|` 折叠块），失败回退首个标题/首行。
- `format_skill_table()`（L32）— 中文清单输出。

## 输入 → 输出
输入：skills 目录。输出：内存清单。无写盘。

## 门禁 / 安全
无。README 与 AGENTS.md 均明确：`skills/` 扫描结果不代表生产批准，生产 skill 必须满足 v26 注册表 + 内容锁。

## 备注 / 风险
命名易误导：新人容易把 `skills.py`/`skills/` 与生产方法链（`pi_skills.py`/`.pi/skills/`）混淆；战略 v30 要求 OpenClaw 类历史边界集中化，此块属"保留但需明确标注非生产"。
