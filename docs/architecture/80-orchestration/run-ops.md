# [80-03] run_ops.py — 运行时预算与阶段遥测

| 层 | 行数 | 上游依赖 | 被依赖 |
|---|---|---|---|
| 80-orchestration | 287 | config | pipeline（两主线的 ops）、llm_agent/role_analyst/research_cases（预算协议）、model_operations（deadline） |

## 职责
单次运行的资源治理：阶段计时、总时长预算、LLM 调用次数/秒数预算、降级记录、进度落盘。

## 关键构件
- `RunOpsContext`（L35）— run_id（时间戳）、stages/degradations/llm_calls 三账本。
- `stage()`（L59）— 上下文管理器：阶段计时 + 超 warn 阈值自动记 stage_slow 降级 + FAIL 冒泡。
- 预算三闸 `reserve_llm_call()`（L107）— ①调用数 ≥ llm_max_calls（默认 4）→ 拒；②累计超时 + 本次 > llm_max_seconds（默认 120s）→ 拒；③已耗时 + 本次 > max_run_seconds（默认 300s，本地配置 420s）→ 拒。拒绝即记降级，调用方降级跳过。
- `effective_timeout()`（L99）— min(请求超时, 剩余 run, 剩余 llm 预算)，至少 1s。
- `record_llm_call()`（L124）— **只记元数据**（provider/model/request_id/finish_reason/usage/时长/错误类），无内容。
- `summary()`（L201）— 状态收敛：任一 FAIL 阶段/降级 → FAIL；WARN 同理；超时 → FAIL。
- 进度 — `logs/run_ops/progress_latest.jsonl`（每轮重置）+ `write_run_summary()`（L240）写 latest 与时间戳副本。

## 备注 / 风险
预算是"软门槛"：拒绝时调用方把信号标记跳过并 needs_review——保交付而降级，不阻塞管道。
