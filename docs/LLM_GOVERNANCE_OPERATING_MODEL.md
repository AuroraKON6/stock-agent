# LLM Governance & Evidence Safety v21

## 适用边界

本控制层用于 A 股内部研究辅助，不提供自动交易、订单执行、仓位调整或自动对外发布。
它不能代替持牌要求、法务意见、供应商尽调、信息安全评估或人工合规审批。

系统使用外部 DeepSeek OpenAI-compatible API 时，机构仍是数据处理和研究质量责任主体。
模型供应商是否保存输入、是否用于训练、服务地域、分包商、删除机制、事件通知时限和退出安排，
必须以真实合同、隐私政策、技术说明和机构审查记录为准；代码不得推定这些事项已经完成。

## 商业运行顺序

生产顺序固定如下，不能从采集直接跳到 LLM：

1. **数据源准入**：确认来源合法、使用权、许可范围、更新频率、质量责任人和退出方案。
2. **研究案件 intake/triage**：确认标的、主题、优先级、公开侧边界和限制名单。
3. **数据分类分级**：按 `public / internal / confidential / restricted` 取最高级别。
4. **最小化与 DLP**：仅抽取研判必需字段；检查凭据、个人/客户数据、内幕或未公开信息。
5. **不可信内容检查**：新闻、公告、网页和手工文本都按数据处理，检测提示注入和越权指令。
6. **外发决策**：生产外部 LLM 只允许 `public`；其他级别 fail-closed 并进入人工例外队列。
7. **模型调用**：使用批准的 provider/model、调用预算、超时、重试和降级路径。
8. **输出证据校验**：每个 thesis 必须引用本轮批准 payload 中实际存在的字段路径。
9. **多角色分析**：基本面、资金、风险和反方角色分别输出；缺角色或无证据输出不可用。
10. **人工研究流程**：证据补充、独立 challenge、质量审核、合规审核和发布批准依次进行。
11. **留痕与复盘**：保存输入哈希、字段清单、准入决定、模型、输出校验和人工处置记录。

## 数据分级矩阵

| 级别 | 典型内容 | 外部 DeepSeek | 处置 |
| --- | --- | --- | --- |
| `public` | 交易所/监管公告、已公开行情、经核验公开新闻 | 允许，经 DLP 与注入检查 | 记录 hash、字段和证据引用 |
| `internal` | 内部研究笔记、未核验手工情报、系统错误、内部任务状态 | 禁止 | 人工研究或证明其已公开后重新采集 |
| `confidential` | 客户/员工信息、账户持仓、合同保密内容、敏感个人信息 | 禁止 | 本地隔离处理，通知数据责任人 |
| `restricted` | 内幕/重大未公开信息、凭据密钥、限制名单标的受限材料 | 绝对禁止 | 停止外发，升级合规与信息安全 |

同一对象包含多个级别时取最高级别。`data_classification=public` 只能说明业务声明，不能绕过
密钥、个人信息、内幕信息、提示注入或限制名单检查。

## 外发字段

允许的基础字段包括信号 ID、公开标题/摘要、公开来源、清理查询参数后的 URL、时间、主题、
证券代码、规则优先级，以及白名单内的行情、公告和来源质量字段。

默认不外发：

- `research_context`、人工决定、人员身份、内部评论和任务分派；
- 未列入证据白名单的 payload；
- URL query/fragment，避免临时 token 或跟踪参数；
- API key、Authorization、Cookie、密码和 access token；
- 客户账号、身份证、手机号、邮箱、银行卡、持仓和客户名单。

## 证据校验

主信号研判必须返回 `evidence_refs`。四角色研判的每个角色也必须返回 `evidence_refs`。
引用使用字段路径，例如 `summary`、`source`、`url`、`evidence.latest`。

- 至少一个引用命中批准 payload，且 thesis 非空：`grounded`。
- 有真实引用但同时出现无效引用：`partial`，强制人工复核。
- 无真实引用、只有编造引用、thesis 为空或角色缺失：`blocked`，不得作为研究结论。
- 被阻断输出仍可保留用于模型质量复盘，但置信度最高降为 0.5，不能通过 analysis gate。

## 人工例外

例外模式固定为 `human_only`，系统不提供自动批准。

人工只能执行以下处理：

1. 提供公开来源证明并把原信号重新采集为新的公开信号；
2. 删除非必要敏感字段后重新形成新的最小化输入；
3. 确认误报并记录合规/信息安全依据；
4. 维持拒绝，改由授权人员在隔离环境内人工研究。

凭据、内幕/重大未公开信息、客户敏感数据和活动限制名单命中不得通过例外直接发送给外部 LLM。
例外处理人应为真实 `compliance_officer` 或 `information_security_officer`，并与研究分析角色分离。

## 审计与隐私

审计文件位于：

- `data/llm_governance/audit/audit_YYYYMMDD.jsonl`
- `data/llm_governance/exceptions/pending_*.jsonl`
- `data/llm_governance/exceptions/decisions.jsonl`
- `reports/llm_governance/latest.md`
- `reports/llm_governance/latest.json`

审计保存：policy version、signal/component、分类、决定、原因、敏感类别、payload SHA-256、字节数、
外发字段清单、证据引用校验和时间。治理报告不保存原始 prompt、新闻正文、密钥、个人数据或受限内容。
研究业务档案至少按配置保留五年；更长的信息技术审计保存义务由机构制度和适用规则另行确定。

## 事件响应

发现疑似泄露时按以下顺序处理：

1. 停止 `agent-run` 的外部 LLM 调用，保留规则分析；
2. 撤销或轮换可能泄露的凭据；
3. 固化 audit ID、payload hash、provider、时间和影响信号范围；
4. 通知信息安全、合规、数据责任人和供应商；
5. 依据合同和适用规则确认删除、事件报告和个人通知义务；
6. 修复分类/DLP 规则并用对抗样本回归；
7. 由授权人工批准恢复，不得由 Agent 自行恢复。

## 日常命令

```powershell
python -m stock_agent agent-run --config config/watchlist.local.json
python -m stock_agent llm-governance-status --config config/watchlist.local.json
python -m stock_agent doctor --config config/watchlist.local.json
```

查看状态后，只有已经写入 `research_operations` 真实名册、处于 active 状态并持有对应角色的人工
`compliance_officer` 或 `information_security_officer` 可以记录例外处置：

```powershell
python -m stock_agent llm-governance-review `
  --config config/watchlist.local.json `
  --exception-id <status命令列出的ID> `
  --disposition deny_and_close `
  --actor <真实名册actor_id> `
  --actor-role compliance_officer `
  --notes "维持阻断；材料改由隔离环境人工核验"
```

`deny_and_close` 关闭并维持拒绝；`request_sanitized_reingest` 要求附 `--evidence-ref`，由人工另行形成
一个新的最小化公开信号；`confirm_false_positive` 也必须附留存依据。后两者都只记录决定，不会自动重采、
重送、放行或调用 LLM。service/Agent 账号、未登记人员、非授权角色和已关闭例外一律拒绝。

`doctor` 中 policy 不安全、非 public 外发、未启用审计或存在无审计决定属于 `FAIL`；治理正常阻断
敏感输入或无证据输出属于 `WARN`，表示控制有效但有人工作业待完成。

## 上线前人工清单

- 法务/采购确认 DeepSeek 服务协议、保密、输入留存、训练使用、分包、地域和删除条款；
- 信息安全完成供应商风险评估、网络边界、密钥轮换、日志访问、备份与退出演练；
- 数据责任人确认每个数据源许可与公开性判定；
- 合规批准分类制度、限制名单责任人、例外 SOP 和事件报告流程；
- 模型负责人完成提示注入、敏感信息、证据伪造、超时和 provider 故障 UAT；
- 真实人员名册和职责分离通过 `research-ops-check`；
- 小范围内部试运行后，由授权人工签署上线，不得把自动化测试通过等同于业务批准。

## 规则依据

- 中国证监会《证券基金经营机构信息技术管理办法》：信息技术治理、数据全生命周期、分类分级、
  最小权限、审计日志、外部信息技术服务审查和退出安排。
  https://www.csrc.gov.cn/csrc/c106256/c1653896/content.shtml
- 中国证券业协会《发布证券研究报告执业规范》（2025）：来源依据、工作底稿、发布前质量控制、
  专职或独立证券分析师质量审核。
  https://www.sac.net.cn/zlgl/zlgz/202512/t20251231_69839.html
- 《中华人民共和国数据安全法》：数据分类分级、全流程安全管理、风险监测和事件处置。
  https://www.cac.gov.cn/2021-06/11/c_1624994566919140.htm
- 《中华人民共和国个人信息保护法》：合法、正当、必要、最短保存、委托处理监督、敏感个人信息
  和影响评估要求。
  https://www.npc.gov.cn/WZWSREL25wYy9jMi9jMzA4MzQvMjAyMTA4L3QyMDIxMDgyMF8zMTMwODguaHRtbD9yZWY9aW1i
- 国家网信办等《生成式人工智能服务管理暂行办法》：生成内容、个人信息和数据安全风险治理。
  https://www.cac.gov.cn/2023-07/13/c_1690898326795531.htm
- GB/T 42775-2023《证券期货业数据安全风险防控 数据分类分级指引》：证券期货数据分类分级参考。
  https://openstd.samr.gov.cn/bzgk/std/newGbInfo?hcno=DB820CE40307DA73731814F2AB0E2DD6
- OWASP LLM Prompt Injection Prevention Cheat Sheet：不可信内容隔离、最小权限、监控和对抗测试。
  https://cheatsheetseries.owasp.org/cheatsheets/LLM_Prompt_Injection_Prevention_Cheat_Sheet.html
