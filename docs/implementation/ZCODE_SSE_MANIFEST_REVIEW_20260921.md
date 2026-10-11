# SSE 页面与 manifest 消费端复验

日期：2026-09-21。结论：**暂不通过本批端到端验收**。本次不修改业务代码，仅新增此报告。

## 已确认进展

- SSE 使用默认 message 事件，事件类型放入 data；页面已创建 EventSource 并使用 onmessage。
- 新增 JSON 快照刷新、连接徽标、fetch 人工反馈及 CSRF 头。
- 新增 run-manifest-bind CLI 与三个 Python 绑定测试。
- BASELINE 另记录 F01-F09 修复和真实模型调用，但本次不把交付记录当作所有问题已关闭的证据。
- 检查了当前源文件、测试及调度脚本；补充复现使用临时目录和合成数据，没有调用真实模型、启动计划任务或修改正式业务数据。

## 本批发现

### B01 [P1] 调度绑定命令不能处理当前项目路径中的空格

位置：`scripts/run_daily_research.ps1:313`。

`--summary $LatestDailySummary` 拼接到待执行脚本时没有给路径加引号。当前工作区 `E:\stock agent` 必然包含空格，传给 Python 的参数会分裂。

使用 PowerShell AST 对实际命令形状做无执行解析，得到两个参数：`E:\stock` 和 `agent\logs\scheduled\run_test.summary.json`。因此 Python 函数测试通过不能证明计划脚本能调用绑定命令。

修复验收：含空格和中文的临时工作目录里，脚本将同一完整路径作为一个 argv 传给 CLI。优先以结构化参数传递，避免继续拼接命令；不为测试执行生产脚本。

### B02 [P1] scheduled 来源未传给真正的 pipeline

位置：`scripts/run_daily_research.ps1:171`、`stock_agent/cli.py:1464`、`stock_agent/pipeline.py:658`。

PowerShell 虽有 TriggerSource，但 agent-run 命令未传它，CLI 也不设置 `_trigger_source`。pipeline 生成 manifest 时默认 manual，因此真正计划运行的 manifest 不具备 pilot eligibility。

现有测试直接在 Python config 中注入 `_trigger_source='scheduled'`，绕过了实际缺失的调用环节。

修复验收：从隔离的脚本/CLI 入口走到管线，验证 scheduled 与 manual 分别产生正确身份；不只直接调用 Python 函数。触发来源声明仍不等于真实计划执行证据，不能靠传参数补造影子运行次数。

### B03 [P1] pilot 选取的是最新任意摘要，不是最新 daily 摘要

位置：`scripts/run_daily_research.ps1:309`。

脚本筛选所有 `run_*.summary.json` 后按文件名取最后一份，未读取 mode。较新的 dashboard/review/pilot 摘要会遮蔽应对账的 daily；pilot 再次运行也可能选中自己上一次摘要。

与此同时，production_pilot.py 自己的摘要选择函数按 mode=daily 过滤。前后两个控制步骤可能对账不同运行。

修复验收：混合 daily/dashboard/review/pilot 摘要时，只选定符合交易日/运行窗口与状态要求的精确 daily 身份，后续所有步骤消费同一路径。不以随时变化的 latest 重新选择。

### B04 [P1] 先写 pilot 对账，再验证 manifest，验证未成为门禁

位置：`scripts/run_daily_research.ps1:302`、`stock_agent/production_pilot.py:123`。

脚本先执行 production-pilot-sync，再执行 run-manifest-bind。sync 默认 record_run=True，在其自身条件满足时可以追加对账事件和写快照；它没有调用新的绑定验证。因此后面的绑定失败只能改变脚本退出状态，无法防止前面的记录写入。

修复验收：先完成精确绑定，再让 pilot 消费已验证的同一 manifest/summary；缺失或篡改 manifest 时不得记录成功运行或将其计入影子样本。允许写明确失败诊断，但不可把后置错误码当作已阻断副作用。

### B05 [P1] 缺少 SHA 和报告路径仍然 PASS

位置：`stock_agent/run_manifest.py:524`，相关可选校验位于 400/403 行。

绑定函数将缺失的 `run_manifest_sha256` 和 `report_path` 转为空串；加载函数只在字符串非空时比较。因此声称的三重对账实际上允许省略两项。

隔离复现：复用 ManifestBindingTests 的有效 scheduled agent-run 夹具，删除摘要中的 report_path 和 run_manifest_sha256，调用 bind_scheduled_summary，返回 **PASS**。

修复验收：run_id、精确 manifest 路径、报告路径、合法 SHA 都为必填；缺失/空值/类型错误分别返回 BLOCKED。增加实际 CLI 非零退出测试，而非只覆盖错误 SHA。

### B06 [P2] SSE 刷新会清空正在编辑的人工反馈

位置：`stock_agent/task_board.py:123`、153 行。

每条事件都会触发 fetch，随后以 tbody.innerHTML 整体替换所有行，包含 actor、notes、input_patch 表单。任何其他任务产生事件，都可能让当前用户的未提交输入与焦点丢失。并发 fetch 没有顺序保护，也可能用晚到的旧响应覆盖新状态。

此项是代码路径确定的交互缺陷；本轮尚未完成真实浏览器复现。当前页面测试仅验证源码字符串存在，不能关闭此项。

修复验收：按任务 ID 更新展示，保留 dirty 表单和焦点；有新版本时提示冲突，不自动把旧输入绑定到新版本。合并刷新请求或校验响应序号，避免旧快照覆盖新快照。浏览器测试须覆盖输入过程中别的任务产生事件、快速多事件和断线重连。

## 顺带发现的研究闭环阻断

### B07 [P1] 治理后的模型输入丢失行情和证据上下文

位置：`stock_agent/research_worker.py:126`、`stock_agent/llm_governance.py:261`。

worker 从 safe_payload.public_context 读取研究资料，但共享 build_governed_prompt_payload 没有生成这个字段，只生成信号元数据和白名单 evidence。治理启用并正常生成 safe payload 时，模型接收的 public_context 变成空对象。

隔离复现：构造含行情和证据的任务，用真正 `_egress_carrier -> build_governed_prompt_payload -> build_user_prompt` 转换，得到 `public_context={}`。

影响：真实 API 调用成功、返回澄清问题和 tokens，只能证明模型连通及协议路径工作，不能证明它读到了用户所说的行情与证据。交付记录中“自动供给快照后的分析”需要重新验证。

修复要求：定义 research_task 专用的最小化数据契约，逐项校验来源、行情质量、官方原件、字段和引用版本，再生成有内容的 safe context。禁止直接把原始 public_context 加回去绕过治理。模型引用检查必须绑定实际发送的安全证据集合，而不是原始输入中的任意 ID。

验收：输入带不同合法事实时，捕获的模型 stub 参数确实包含对应安全事实；被阻断事实不出现；空上下文不得冒充完成基于资料的研究。

## 验收边界与下一步

本轮不重新调用真实模型，不读取凭据。Playwright CLI 本地帮助命令启动出现 Node 断言错误，未据此完成浏览器操作验收；没有把 HTML 接线测试当成浏览器测试。

下一批仍是修复与复验，而非 W10 live 推进：

1. B01-B05：从真实命令参数边界补齐调度身份、精确摘要选择、绑定前置和必填校验。
2. B07：用合法公开 fixture 验证实际发给模型的安全上下文；禁止只验调用成功。
3. B06：补真实浏览器输入保留、刷新顺序、提交冲突与重连测试。
4. 提供对应失败用例的红绿结果、全量回归和同一运行的 manifest 证据后，再申请验收。

全量回归实测：`.\.venv312\Scripts\python.exe -m unittest discover -s tests`，`Ran 915 tests in 174.100s; OK (skipped=2)`。即 913 项执行通过、2 项跳过。任何测试通过都不抵消上述隔离复现与调用链缺口。
