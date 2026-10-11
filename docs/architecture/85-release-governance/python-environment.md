# [85-02] python_environment.py — Python 环境可复现审计（v31 新增）

| 层 | 行数 | 上游依赖 | 被依赖 |
|---|---|---|---|
| 85-release-governance | 339 | config | release_audit（PASS 门禁）、cli（python-env-audit） |

## 职责
验证 `.venv311/.venv312` 隔离环境与 `requirements.lock.txt` 的精确一致性 + 关键包导入 + 完整测试结果。**不创建环境、不安装依赖**——只验证。

## 关键构件
- `build_python_environment_audit()`（L37）— 对每个隔离环境探针（内嵌 probe 代码枚举包版本），与 lock 精确比对（missing/mismatch/extra），跑 smoke（akshare/pysnowball/yaml 导入）。
- 测试执行 — **仅当显式 `--run-tests`** 才跑 `unittest discover`（900s 超时），且只保留 test_count/skipped/exit_code/**output_sha256**，`raw_output_recorded: False`（L270-278）——原始输出不落盘。
- PASS 条件 — minor ∈ {3.11, 3.12} + lock 一致 + smoke PASS + tests PASS；**不跑 --run-tests 整体必为 BLOCKED**（缺任一版本同理）。
- 源指纹防竞态 — 审计前后各取一次源指纹，不一致即 blocker（防审计途中源变动）。
- `_safe_error()`（L321）— 错误文本脱敏（sk-…→[REDACTED]）。

## 输出
供 release_audit 消费的审计结果（含在 release 控制物体系内）。

## 备注 / 风险
与 AGENTS.md 边界一致：本模块不负责创建环境或安装依赖；主环境仍是 Anaconda Python 3.8.5（保持可回退，不被静默替换）。
