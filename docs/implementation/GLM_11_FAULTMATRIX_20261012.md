# GLM-11 交付报告（2026-10-12）：离线故障矩阵第一批——并抓到两个真实缺陷

执行：GLM。范围：计划 GLM-11 故障矩阵中可离线实测的六项：数据库锁竞争（阻塞/超时两路径）、并发 claim 不重复、取消 vs 迟到完成竞态、时钟漂移（坏租约时间戳）、cycle 内 worker 崩溃遏制。**矩阵测试抓到两个真实缺陷并修复：①`leased → failed/blocked` 状态转移不可达；②summary_viewer POST 早拒路径的请求体排空竞态（此前两次被误判为环境抖动的 WinError 10053 复发）。**

## 缺陷二（本批追加）：summary_viewer 请求体排空竞态

**复发线索**：test_summary_viewer 的 `test_http_same_host_wrong_port_and_cross_origin_denied` 在 10-12 两次全量回归中各随机失败一次，此前判"环境抖动留观"。本批再次复现且改跑模块 3 次内必现——升级为缺陷排查。

**机制**：POST 被早拒（403/404）时未消费请求体，keep-alive 处理器循环随后把**请求体字节当作下一条请求行**解析→协议错；且 Windows 对接收缓冲含未读数据的套接字 close 发 **RST**，抢先丢弃已排队响应→客户端 `getresponse()` 中断（10053）。

**修复迭代（如实记录三次）**：①仅设 `close_connection=True`——无效，未读数据下关闭同样 RST；②早拒后排空——引入新缺陷：400 分支中请求体**已被读取**（JSON 解析失败但字节已消费），二次排空阻塞等待不存在的字节→线程挂起→4 个用例稳定失败；③最终两段式：框架错误（体未读）→按声明长度排空后再响应；解析/形状错误（体已消费）→连接已字节对齐，正常 keep-alive 响应。**修复后模块 8/8 连续通过**（修复前 3 次内必炸）。

## 抓到的缺陷与修复（本批核心价值）

**缺陷**：worker 在 `claim_next` 之后、`mark_running` 之前崩溃（或命中治理阻断）时，`store.fail()` 走到终态分支会抛 `TaskTransitionError: leased -> failed is not allowed`——原转移表只允许 leased→{running, queued, expired, cancelled}。后果：daily-task-cycle 的崩溃遏制路径对"终态失败"不可达，异常反而逃逸杀死整个周期——正是故障矩阵要防的事故形态。

**修复**：`ALLOWED_TRANSITIONS[leased]` 增加 `failed` 与 `blocked`（带注释说明动机）。修复后崩溃任务轨迹：claim → 崩溃（消耗一次尝试、重排队）→ 再 claim → 再崩溃 → 终态 failed，周期继续处理后续任务。

**顺带改进**：cycle 的崩溃计数原来把"重排队"也计入 failed（夸大终态失败）——现在区分 `retried`（消耗尝试并重排队）与 `failed`（终态），报告不再误导。

## 六项矩阵结果（全部 VERIFIED 离线）

| 故障 | 场景 | 实测结果 |
|---|---|---|
| 数据库锁（阻塞路径） | 连接 A BEGIN IMMEDIATE 持锁，B 写入 | B 阻塞（0.3s 后仍存活），A 提交后 B 完成 RECORDED |
| 数据库锁（超时路径） | busy_timeout=200ms + 外部持锁 | 干净抛 locked 错误；**零部分状态**（无任务行、无 outbox 事件）；锁释放后下一个写入正常 |
| 并发 claim | 两个 store 连接交错 claim 2 任务 | 各领不同任务、不同 worker_id；第三次 claim None |
| 取消竞态 | running 中置取消标志后 worker 迟到 complete | 取消获胜：终态 cancelled（非 succeeded），outbox 有 task_cancelled |
| 时钟漂移 | lease_expires_at 为不可解析字符串 | recover_abandoned 跳过不崩溃、不误重排队 |
| cycle 崩溃遏制 | poison worker 对 doomed 任务抛错 | doomed 终态 failed（错误含 "cycle worker crashed"）、good 任务照常 succeeded；processed=3/failed=1/retried=1 |

## 状态

- 上述六项：**VERIFIED（离线）**
- 其余矩阵项（磁盘满、网络中断、费用未知外呼、多进程并发预算竞态、SSE 慢消费者实测）：**NOT_RUN**（部分已有实现——SSE 有界流、预算先记账后调用——但缺专项实测，列后续批）
- 原生 Windows VM 故障注入：**NOT_RUN**

## 改动文件

- `stock_agent/task_runtime/store.py` — leased 转移表补 failed/blocked（**行为修复一**）
- `stock_agent/task_intake.py` — cycle 崩溃分类计数（retried 新增）
- `stock_agent/summary_viewer.py` — `_drain_request_body` + POST 早拒两段式排空/响应（**行为修复二**）
- 新增测试 `tests/test_fault_matrix.py`（6 用例）

## 实际命令与结果

- 专项 test_fault_matrix 6 OK；test_summary_viewer 修复后 **8/8 连续通过**（修复验证）；邻近 test_first_product_slice / test_daily_task_cycle / test_task_intake OK。
- 全量隔离回归：**Ran 1158 tests, errors=14（既有原生守卫）, skipped=2, failures=0**（以 %TEMP%\glm11_final_regression_20261012.log 为准；中间一次 15 错误即缺陷二的复现与修复依据）。
- 浏览器/原生/live/真人：未执行；零模型调用。

## 开发期修正（如实）

锁竞争测试初版把 `_spec()` 放进子线程构建——路径覆盖上下文线程不可见导致假失败（诊断脚本证明锁行为本身正确）；修正为负载在主线程构建。cycle 计数预期两次修正：先是重排队未计入 processed，后是 retried/failed 语义分离。

## 生产配置、权限、账本变化

无。

## 回滚

还原 store.py 转移表与 task_intake 计数块、删测试。转移表回滚会使 leased 终态失败再次不可达（即回到缺陷态）——不建议。

## 遗留与下一批

- 矩阵剩余项（磁盘满/网络中断/费用未知/多进程预算竞态/SSE 慢消费者实测）后续批。
- 待用户清单不变：ADR-002、来源批准、vault 事故处置、mimosa 完整审计。
