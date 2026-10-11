# Vault / 精确运行 / DOM 修复复验

日期：2026-09-21。结论：**仍不通过，存在生产契约不匹配和实际浏览器交互缺陷。**

本次不修改业务代码、测试代码、配置或凭据。仅新增报告；没有真实采集、模型调用、计划任务或正式业务审批。

## 1. 环境与验证方式

- 开始时 E 盘可用约 151.05 GB，后续采样约 150.97 GB，未复现此前持续写满的情况。
- 不能只用 logs 总大小判定外部进程写盘。此前写满的责任进程本轮未定位，也未删除文件或终止未知进程。
- 全量测试使用锁定 `.venv312` 解释器并带 `-B`，避免新增 pyc。
- 补充 Python 复现使用合成内存数据和 mock 读取，不创建真实证据或授权。
- 浏览器使用本机已有 Playwright 库和 Chromium，无须安装/修复环境；启动独立无头实例，关闭后退出。页面来自真实 `_render_html`，任务快照与 SSE 通知在浏览器中模拟，不连接正式任务库。此项证明真实 DOM 行为，但不是完整 HTTP/SSE 重连验收。

## 2. D01 [P1] 真实 vault 是字典，新消费者却按列表读取

位置：`stock_agent/research_worker.py:134`、`stock_agent/task_supply.py:131`。

真实生产者 `evidence_vault.py` 的 `documents` 是以 document_id 为键的字典，投影生成处见 642 行；既有核验器按 `.values()` 遍历。新代码直接遍历 documents，取得的是字符串键，随后因不是 dict 而跳过。

隔离复现使用生产 schema：

- `REAL_VAULT_DICT_SUPPLY {}`
- `REAL_VAULT_DICT_DELIVERED []`

因此真实 vault 即便有文件，这条新供给链也会返回空结果。手工构造 `documents: [...]` 的测试夹具掩盖了生产不兼容。

修复标准：优先调用既有 vault 加载/验证接口，不另写猜测 schema 的解析器。集成测试须由真实导入/投影接口在临时工作区生成索引，再走供给和研判上下文，不能单独手写一个非生产索引形状。

## 3. D02 [P1] 查索引不等于实际留存与验真

位置：`stock_agent/research_worker.py:121`、190 行。

`_retained_vault_hashes` 只读取 JSON 中的 sha256，且 retained 缺省为 True。没有验证原始对象存在、文件字节哈希/大小、事件链、文档 captured/quarantined 状态，以及文档与本案件关联。随后设置 `vault_verified=True`。

隔离复现：采用当前测试使用的列表型索引，填入合法哈希但 object_path 不存在，构建器仍投递 `ev-fixture`。这与函数描述的“actually retained and verified”不一致。

既有 `verify_evidence_vault` 在 660 行起已经检查链、对象、哈希和大小，不能用新索引扫描替代。

修复标准：复用权威验真结果，并将摘要绑定到实际文档和案件证据；缺失、篡改、隔离或关联错误必须被阻断。不能仅将循环改成 `.values()` 就认定此项完成，否则会把当前漏读变成未经核验的投递。

## 4. D03 [P1] 时间能解析不代表行情新鲜且一致

位置：`stock_agent/research_worker.py:164`。

新逻辑验证 ISO 可解析和 provider 是否批准，但未执行行情新鲜度、未来时间、交易时段、来源数、冲突和可验证缓存门禁。

隔离复现：批准 provider 下，`source_time=2000-01-01T10:00:00+08:00` 的报价仍出现在 delivered_quotes。

修复标准：共享现有 market-data-governance 的实际判定，不在 worker 中另建较弱门禁。陈旧、未来、无时区/未知时间、来源不足和价格冲突分别补拒绝用例；合法时效正例应使用受控时钟和真实口径。

## 5. D04 [P2] 干净表单更新后消失

位置：`stock_agent/task_board.py:137`。

replaceGroup 遍历 live `holder.childNodes`，同时把节点移出 holder。节点集合长度与索引会在遍历期间变化，因此第二个节点被跳过。对于「主行 + 反馈表单行」，插入主行后跳过表单行，又删除原表单。

真实浏览器复现：awaiting_human 任务的干净表单遇到 version 1 -> 2 更新，`form` 数量从 1 变成 0，而任务仍待人工。

修复标准：使用静态节点快照或逐个转移节点，测试同一任务在待人工状态下更新版本后仍保留可操作表单。必须浏览器执行，不能只断言代码含节点操作函数。

## 6. D05 [P2] dirty 表单仍丢焦点，初始 SSR 行重复

位置：`stock_agent/task_board.py:55`、169 行。

虽然 dirty 分支不重建内容，后续排序仍无条件 appendChild 移动整组节点。本机 Chromium 实测内容值保留，但焦点从 notes 输入框变成 BODY。因此“节点原地不动、焦点天然保留”的描述不成立。

另外 SSR 主行没有 data-task/data-version。首次客户端 render 无法认出 SSR 行，追加新的主行和表单；清理只遍历有 data-task 的行，旧 SSR 行留下。

真实浏览器结果：

| 观察 | 结果 |
|---|---|
| 单个待人工任务首次加载 | tbody 有 3 行，即旧 SSR 主行 + 新主行 + 表单 |
| 输入 notes 后 | activeElement.name 为 notes |
| 同版本模拟 SSE 刷新后 | notes 的文字仍在，但 activeElement 为 BODY |
| 干净表单版本更新后 | form 数量 0，见 D04 |

修复标准：SSR 与客户端使用同一任务键和版本契约；正常刷新不移动已在正确位置的活动表单；必要排序明确恢复焦点/选区。补首次加载无重复、输入值/焦点/选区保留、异步提交成功清理 dirty 状态和冲突处理测试。

## 7. D06 [P1] 显式摘要失效时仍回退 latest

位置：`stock_agent/production_pilot.py:488`、797 行。

显式摘要成功路径已向前推进，但当指定路径不存在时 `_collect_base_state` 回退 `_latest_scheduled_summary()`；指定摘要缺 report_path 或读取失败时，`_build_reconciliation` 回退最新报告。这样仍可能换成另一轮身份。

该问题来自代码检查；本轮未执行有业务写入副作用的 pilot 命令。前置 shell 验证与后续读取之间仍存在文件消失/替换窗口，直接 CLI 调用也不能假定已经前置验真。

修复标准：调用者显式给出摘要时，读取/绑定失败必须 fail closed，不能回退。消费者内部重新验证绑定并使用同一已验证结果；仅调用者未指定且明确允许发现模式时才可选择 latest。补文件缺失、坏 JSON、缺报告字段、绑定后被替换用例。

## 8. 下一批修复策略

暂停继续加功能。此次问题集中体现了「测试自建数据结构与生产事实源不一致」「格式验证代替领域验证」「HTML 字符串断言代替实际交互」三类缺口。

1. 先完成生产契约测试：真实 vault 生产者 -> 权威验真 -> 供给 -> 安全上下文，全链路临时隔离。
2. 用既有行情门禁结果驱动投递，不再在 worker 复制简化规则。
3. 用本机已有浏览器实际跑 D04/D05，并保留失败及修复后的可重复步骤。
4. 对显式摘要保持严格身份，不在错误时自动选择其他运行。
5. 每一项单独报告关闭证据，不能用全量绿覆盖仍能复现的问题。

全量回归实测：`.\.venv312\Scripts\python.exe -B -m unittest discover -s tests`，`Ran 935 tests in 213.093s; OK (skipped=2)`。即 933 项执行通过，2 项跳过。测试通过不意味着上述缺陷已消除。
