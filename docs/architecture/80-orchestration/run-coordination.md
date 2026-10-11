# [80-04] run_coordination.py — 租约、检查点与运行事实源

| 层 | 职责 | 上游 | 下游 |
|---|---|---|---|
| 80-orchestration | 跨进程互斥、崩溃可解释、检查点幂等、状态投影 | config、audit_log | pipeline、CLI、dashboard、scheduled runner |

## 核心设计

- `RunLease` 使用 `O_CREAT|O_EXCL` 原子创建按模式租约；只允许持有者
  heartbeat/release。
- 过期租约不会自动接管。`run-recover-stale` 必须接收非空人工理由，
  将租约移入 `recovered` 并把恢复事件写入哈希链。
- `CheckpointStore` 复用统一审计链格式，幂等键为
  `run_id|stage|input_fingerprint`。相同载荷返回 `DUPLICATE`，不同载荷
  直接阻断。
- 输出只保存相对路径、大小和 SHA-256，不写入报告正文。
- 技术协调状态与生产就绪状态分开；本地技术 `PASS` 不改变任何业务
  `NO_GO/BLOCKED`。

## 流水线接入

`run` 与 `agent-run` 通过 `_run_with_coordination` 包装既有业务体，
不改变未启用配置和隔离测试行为。租约冲突发生在采集前；各主要阶段
完成后写元数据检查点；成功后记录最终产物指纹，失败后记录失败摘要。

## 运行命令

```powershell
python -m stock_agent run-coordination-status --config config/watchlist.local.json
python -m stock_agent run-recover-stale --config config/watchlist.local.json --mode run --reason "..."
```

该模块不采集行情、不调用 LLM、不执行调仓/交易/发布，也不安装计划
任务。生产级 fencing、跨主机 lease、日历和 pilot manifest 对账仍是后续
门禁，不能由本模块的测试或本机状态代替。
