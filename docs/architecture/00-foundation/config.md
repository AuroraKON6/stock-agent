# [00-01] config.py — 路径解析与目录契约

| 层 | 行数 | 上游依赖 | 被依赖 |
|---|---|---|---|
| 00-foundation | 154 | 无（纯 stdlib） | 全部模块（唯一路径入口） |

## 职责
全系统唯一的路径解析入口和目录契约。所有模块通过 `project_path()` 把相对路径解析为项目内绝对路径，保证测试可以用 `project_path_override` 在不改生产代码的情况下重定向整个文件系统视图。

## 关键构件
- `PROJECT_ROOT`（L10）— 项目根，由 `__file__` 上溯确定。
- `project_path(path_value)`（L22）— 绝对路径原样返回；相对路径先查 override 再落 `PROJECT_ROOT`。
- `project_path_override(resolver)`（L33）— 基于 `contextvars` 的上下文管理器：override 只作用于当前线程/asyncio task，退出必恢复，异常不泄漏（L33-50 docstring 明确此契约）。
- `load_config(path_value)`（L53）— 支持 JSON 与 YAML（YAML 需 PyYAML，缺失时 RuntimeError 指引安装）。
- `ensure_directories()`（L69-154）— 创建约 85 个固定目录（data/reports/logs 全谱系）。

## 输入 → 输出
输入：CLI `--config` 路径。输出：内存 dict 配置 + 全套目录骨架。不写业务数据。

## 门禁 / 安全
本身无门禁，但它是所有 fail-closed 模块（哈希链、证据库、底稿）路径受控的前提——路径逃逸防护的第一层。

## 备注 / 风险
- v31 新增目录：`data/value_validation(/sealed)`、`data/backup_restore(/bundles、/drills)`、`reports/value_validation(/blind_assignments)`、`reports/backup_restore`、`reports/release_audit`。
- 旧式 monkeypatch（research_cases/research_git/research_runtime 向 brokerage_architecture 转发事件时临时替换对方模块级 `project_path`）尚未迁移到本模块的 override 机制，并发下有踩踏风险（战略 v30 §18.2 列入收敛清单）。
