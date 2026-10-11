# Research Workpapers v27

## 1. 定位与边界

Research Workpapers v27 把规则分析、Pi/DeepSeek 推理、多人复核和 Research Runtime
组织为案件级结构化工作底稿。它用于内部研究辅助和受控试运行，不是证券研究报告牌照、
合规证明、正式档案系统或交易系统。

系统可以自动生成草稿、证据索引、模型推断、反证问题和待办，不能自动完成人员分派、
独立质量审核、合规放行、发布批准、对外发布或交易。任何 `BLOCKED` 必须由真实数据、
真实人员和真实授权解决，不能由 Agent 代填。

## 2. 商业运行顺序

| 顺序 | 责任主体 | 必须输入 | 系统产出 | 人工控制与退回路径 |
| --- | --- | --- | --- | --- |
| 1. intake | 协调员 | 覆盖标的、主题、研究边界 | `case_id`、标的、主题、初始信号 | 边界不清则停留在 intake |
| 2. triage | 协调员 | 时效、事件类型、P0-P3 | 优先级、研究路线、任务依赖 | 噪声关闭，信息不足退回 intake |
| 3. evidence_collection | 信息分析与数据责任人 | 公告、财报、行情、新闻、调研材料 | 来源记录、时间点、哈希、层级、缺口 | P0/P1 无官方一手来源不得进入批准链；行情治理不通过则停留补证 |
| 4. analysis | 主分析师与并行分析角色 | 已留痕证据、口径和假设 | 事实、数据观测、模型推断、风险点、可证伪问题 | 冲突或缺证据退回 evidence_collection |
| 5. challenge | 独立风险复核人 | 分析稿和支持证据 | `risk_note:`、反证、下行情景、补证要求 | LLM 风险项只能提供问题，不能替代独立人工挑战 |
| 6. quality_review | 独立质量审核人 | 完整底稿、挑战记录 | 批准或退回、审核意见 | 与主分析师必须分离；退回 analysis |
| 7. compliance_review | 合规人员 | 质量通过稿、信息分类、限制名单、披露 | 放行或阻断、检查记录 | 未公开信息、利益冲突、静默期或披露问题不得放行 |
| 8. human_approval | 发布批准人 | 质量与合规均通过、归档证据 | 内部发布批准 | 未接入获批生产档案不得批准发布 |
| 9. published/monitoring | 系统与主分析师 | 已批准版本、新事件 | 内部版本、持续监控、论点变化 | 重大新证据使旧结论回到补证/分析 |
| 10. closed | 主管 | 关闭理由、最终归档 | 完整案件历史 | 新重大事实只能由受控动作重新开启 |

`evidence_collection` 之后不是一条固定直线。基本面、市场背景、假设分析和来源核验可以并行，
但所有分支必须汇入同一案件底稿，独立挑战完成后才能进入质量审核。

## 3. 底稿记录模型

每个案件底稿至少包含以下结构：

| 区域 | 关键字段 | 目的 |
| --- | --- | --- |
| `case` | 标的、主题、优先级、信号 ID、缺失信号 | 固定研究对象和输入边界 |
| `source_records` | 来源 URL、域名、来源层级、发布时间、采集时间、行情时间、SQLite 定位符、内容哈希、数据分类 | 证明使用了什么、何时获得、保存在哪里 |
| `claims` | `source_statement`、`data_observation`、`model_inference`、`human_judgment` | 强制分开事实、观测、模型推断和人工判断 |
| `analysis` | 论点、分角色分析、冲突、风险、复核问题 | 保留推理结果及分歧，不覆盖原始证据 |
| `challenge` | 模型反证问题、独立人工挑战状态和引用 | 模型不能冒充独立风险复核人 |
| `workflow` | 阶段、分派、审批、动作历史 | 将底稿与 Research Runtime 对齐 |
| `governance` | LLM 外发治理、行情治理、数据分类 | 记录模型与数据可用边界 |
| `methods` | 模型、Pi 审计事件、skill IDs、registry/lock/bundle 哈希 | 重建当时使用的方法版本 |
| `retention` | 最低保存年限、归档后端、批准证据 | 区分本地影子账本和正式生产档案 |

证据层级采用严格映射：监管机构、交易所和巨潮资讯官方披露平台可成为官方一手来源；
东方财富、AkShare、雪球等聚合渠道只能作为二手或发现来源。行情只有在供应商内部核准、
新鲜度、交易时段、时间戳和多源一致性门禁通过后，才可标记为内部研究可验证观测。

`source_locator=sqlite:signals:<signal_id>` 指向当时进入系统的不可变信号记录，
`content_sha256` 固定该记录内容。它不等于已经保存公告 PDF 原件；若原件未落地，底稿会明确产生
`original source document is not retained` 警告，而不是伪造“已归档”。

## 4. 版本和审计

事件文件 `data/research_workpapers/events.jsonl` 是唯一事实源：

1. 每个修订事件包含前一事件哈希并形成链。
2. 同一案件输入未变化时不新增版本。
3. 来源、分析、方法、治理或工作流变化时新增版本，不覆盖旧版本。
4. `data/research_workpapers/latest.json` 和 `reports/research_workpapers/latest.*` 都是可重建投影。
5. 投影被删除或篡改不改变事实源；事件链被篡改则技术状态 `FAIL`，受保护门禁关闭。
6. Runtime 人工动作成功持久化后再追加底稿修订，并记录 `runtime_action_id`。

本地哈希链提供完整性检查，但没有证明 WORM、不变备份、访问审计、灾备和保留策略已经获得
机构批准。因此默认 `production_archive_approved=false`，发布批准保持阻断。

## 5. 模型与方法留痕

每次 DeepSeek/Pi 输出通过 LLM Governance 校验后，底稿仅保存必要执行元数据：运行时、供应商、
模型、请求 ID、结束原因、Pi 哈希链审计事件 ID、系统指纹和非敏感用量字段。API key、原始内部
提示词和不允许外发的数据不会写入底稿。

生产方法来自 `config/pi_skills.json` 和 `config/pi_skills.lock.json`。底稿记录实际编译到角色提示词的
skill IDs 及 registry、lock、bundle 哈希。动态 skill、工具、Shell 和会话继续关闭；方法变化会改变
生产候选关键配置指纹，使旧批准失效。

## 6. 门禁语义

- `technical_status=READY`：事件链、字段、引用和政策结构可验证；不代表案件可发布。
- `business_status=BLOCKED`：仍缺原始文件、官方来源、真实人工挑战/审批、正式档案或其他业务条件。
- `approval_eligible=true`：当前证据和字段足以送入下一人工门；不等于质量、合规或发布已批准。
- `production_archive_ready=true`：只有真实批准证据配置后成立；Agent 不得自行设置。

`quality_approve`、`compliance_clear` 和 `publication_approve` 在 `enforce=true` 时先调用底稿门禁。
角色、在职状态、资格、授权、覆盖范围和限制名单仍由 Research Operations/Runtime 独立校验。

## 7. 值班命令

```powershell
# 日常流水线会自动在 Runtime 后同步底稿
python -m stock_agent agent-run --config config/watchlist.local.json

# 对现有案件/报告补建或刷新底稿
python -m stock_agent research-workpaper-sync --config config/watchlist.local.json

# 查看全部案件或单一案件
python -m stock_agent research-workpaper-status --config config/watchlist.local.json
python -m stock_agent research-workpaper-status --config config/watchlist.local.json --case-id CASE_ID

# CI/值班只要求技术完整性；退出码 2 表示技术未就绪
python -m stock_agent research-workpaper-verify --config config/watchlist.local.json --require-ready

# 发布准备检查；退出码 3 表示业务条件未就绪
python -m stock_agent research-workpaper-verify --config config/watchlist.local.json --require-business-ready
```

禁止直接编辑 `events.jsonl`、删除历史版本或通过修改 `latest.json` 改状态。人工审批必须使用
`research-runtime-action`，由真实 active 名册人员按角色提交。

## 8. 上线验收

技术上线至少要求：全量测试通过、底稿链 `OK`、Doctor 无底稿技术 `FAIL`、Dashboard 可见案件和
阻断原因、流水线阶段顺序通过隔离端到端测试、政策变化使生产候选指纹失效。

业务启用还要求：真实人员名册和登记/授权、内部供应商核准、官方源与原始文件留存、限制名单责任人、
真实独立挑战及三道人工门、获批生产档案和备份/访问审计。缺少任一项都应如实保持 `BLOCKED`。

## 9. 公开规则映射

本设计仅作工程控制映射，不替代法律意见或机构内部制度：

- 中国证券业协会《发布证券研究报告执业规范》：信息来源合法性和核实、严谨研究方法、工作底稿、发布前质量控制及审核意见落实。
  https://www.sac.net.cn/sjb/zlgl/zlgz/202512/t20251231_69839.html
- 中国证券业协会《证券分析师执业行为准则》：独立、客观、审慎，使用合法合规信息并核实引用来源。
  https://www.sac.net.cn/sjb/zlgl/zlgz/202512/t20251231_69840.html
- 中国证监会《发布证券研究报告暂行规定》：独立、客观、公平、审慎及利益冲突防范。
  https://www.csrc.gov.cn/csrc/c101838/c1022037/content.shtml
- 中国证券业协会《证券公司合规管理有效性评估指引》：发布研究报告档案不少于五年，并检查工作底稿、质量和合规记录。
  https://www.sac.net.cn/zlgl/zlgz/202512/t20251231_69856.html
- 中国证券业协会《证券公司信息隔离墙制度指引》：研究、质量管理和合规审查应处于受控业务流程。
  https://www.sac.net.cn/zlgl/zlgz/202512/t20251231_69829.html

