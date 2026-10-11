# G0.5 价值验证运行手册 v31

## 1. 目的

G0.5 用 20-30 个真实 P0/P1 历史研究案件回答一个商业问题：在相同证据截止点下，Pi Agent +
DeepSeek 辅助流程是否减少人工耗时，同时不降低研究质量、不增加重大缺陷，并保持 0 次未授权数据外发。

该机制不是模型演示、回测收益证明或上线审批。系统只计算建议，最终只能由真实授权人工记录
`CONTINUE`、`NARROW`、`STOP` 或一次有期限的 `EXTEND_ONCE`。任何决定都不会自动启用生产、发布研究、
连接券商、下单、调仓或安装计划任务。

## 2. 四层状态必须分开

| 层级 | 证明内容 | 当前可由代码证明 | 不代表 |
|---|---|---|---|
| Technical framework | 事件链、输入校验、盲评隔离、指标计算可运行 | 是 | G0 已 READY |
| Study operations | 真实案件、人工计时、冻结版本和盲评已执行 | 只有真实事件后 | 指标达标 |
| Value gate | 效率、质量、缺陷、外发全部过门 | 只有完整样本后 | 业务上线批准 |
| Business decision | 负责人基于证据决定继续、收窄或停止 | 仅真实人工可记录 | 自动生产启用 |

测试、mock、示例 JSON、合成案件和重复执行不能计入 20-30 个业务样本。

## 3. 角色与职责分离

| 动作 | 允许角色 | 关键约束 |
|---|---|---|
| 建立研究、登记案件、生成盲评包、揭盲 | `research_coordinator` 或 `research_supervisor` | 必须是真实名册中的 active human，位于公开信息侧 |
| 冻结队列、冻结 treatment、排除案件、记录最终决定 | `research_supervisor` | 需要 `publication_authorized`，但本模块不执行发布 |
| 记录人工基线和系统辅助观测 | `lead_analyst` | 证券分析师登记必须为 verified；默认同一案件由同一分析师完成两阶段 |
| 独立盲评 A/B | `quality_reviewer` | 证券分析师登记必须为 verified；不得是分析师或盲评包制作者 |

不得新建虚构人员来消除阻断。协调员不得把 sealed mapping 或事件账本交给盲评人。

## 4. 数据与审计产物

- 事实源：`data/value_validation/events.jsonl`，哈希链只追加，不得编辑、覆盖或删除。
- 隔离映射：`data/value_validation/sealed/`，只供揭盲协调员访问。
- 盲评包：`reports/value_validation/blind_assignments/`，只包含 A/B 输出引用和评分规则。
- 最新投影：`reports/value_validation/latest.json` 和 `latest.md`。
- 时间模板：`config/value_baseline_metrics.example.json`、`config/value_assisted_metrics.example.json`。
- 评分模板：`config/value_blind_scores.example.json`。

示例模板含 `"template_only": true`，直接提交会被拒绝。实际记录应在机构批准的工作目录中另建文件，
填入真实值并删除该字段。原始研究输出保存在批准的档案系统，项目只登记不泄露分组的 opaque URI 和 SHA-256。
每份指标 JSON 还必须登记 `measurement_evidence_ref` 和 `measurement_evidence_sha256`，把数值绑定到批准档案
中的原始时间日志；assisted 指标另需 `system_run_ref` 和 `system_run_sha256`，用于对账冻结版本的真实运行摘要。

## 5. 进入条件

开始研究前必须同时满足：

1. 项目范围固定为公开信息侧的 P0/P1 官方公告、财报或重大公司事件。
2. 真实名册含协调员、授权研究主管、主分析师和独立质量审核人。
3. 案件档案可按证据截止时间重建，且结局标签对 assisted 阶段保持隐藏。
4. 不存在把个人信息、内幕信息、密钥或受限名单内容发送给外部 LLM 的路径。
5. G0 可信发布基线可在 baseline 完成后、treatment 冻结前达到 `READY`。

第 5 项未满足时可以准备案件和人工 baseline，但不能冻结 treatment，也不能把任何运行称为正式系统辅助样本。

## 6. 标准运行顺序

### 6.1 只读查看

```powershell
python -m stock_agent value-validation-status --config config/watchlist.local.json
python -m stock_agent value-validation-status --config config/watchlist.local.json --require-ready
```

第一条生成只读状态投影。第二条只有全部价值门禁通过才返回 0；研究尚未开始或证据不足时返回 2 是正确结果。

### 6.2 建立一次研究

```powershell
python -m stock_agent value-study-init --config config/watchlist.local.json `
  --title "P0/P1 官方事件研究价值验证" `
  --scenario "相同证据截止点下比较人工基线与系统辅助研究" `
  --actor <真实协调员actor> --actor-role research_coordinator
```

建立研究时会冻结样本数量、优先级、质量边界和指标门槛。建立后修改这些配置会使 treatment 冻结失败，
不能悄悄调整指标来追求通过。

### 6.3 登记 20-30 个真实案件

```powershell
python -m stock_agent value-case-register --config config/watchlist.local.json `
  --case-id <opaque_case_id> --case-ref archive:<opaque_ref> `
  --case-sha256 <64位SHA256> --priority P1 --stratum filing `
  --evidence-cutoff <ISO8601时间> --data-classification public `
  --actor <真实协调员actor> --actor-role research_coordinator
```

案件必须来自真实历史工作，至少覆盖两个预先定义的层，例如 `filing`、`earnings`、`market_event`。
不得按模型已知表现挑选案例，不得登记结果标签，不得用相同案件的不同副本扩大样本量。

发现预先定义的客观排除原因时，由主管在记录任何 baseline 前执行：

```powershell
python -m stock_agent value-case-exclude --config config/watchlist.local.json `
  --case-id <case_id> --category <预定义类别> --reason <可审计理由> `
  --actor <真实主管actor> --actor-role research_supervisor
```

### 6.4 冻结案件队列

```powershell
python -m stock_agent value-cohort-freeze --config config/watchlist.local.json `
  --actor <真实主管actor> --actor-role research_supervisor
```

冻结后不允许增加案件。冻结事件包含排序后的案件身份和指纹。

### 6.5 记录人工基线

每个案件由主分析师在不调用项目 LLM/Pi 的情况下完成研究，使用独立时间日志记录有效工作时间：

```powershell
python -m stock_agent value-baseline-record --config config/watchlist.local.json `
  --case-id <case_id> --output-ref archive:<不泄露分组的opaque_ref> `
  --output-sha256 <64位SHA256> --metrics-file <真实baseline指标JSON> `
  --actor <真实分析师actor> --actor-role lead_analyst
```

`active_minutes` 必须不超过起止时间，六个工作段之和必须在 5% 或 1 分钟容差内与有效时间一致。
只有 `time_log` 默认计入最终门禁；`calendar_reconstruction` 可留存，但会作为协议偏差排除。
指标中的档案引用和哈希必须指向未修改的原始计时证据，不能只把人工填写的汇总 JSON 当作计时凭证。

### 6.6 冻结系统 treatment

全部 baseline 记录完成且当前 `release-audit` 为 `READY` 后执行：

```powershell
python -m stock_agent value-treatment-freeze --config config/watchlist.local.json `
  --actor <真实主管actor> --actor-role research_supervisor
```

事件会固定 release manifest、候选指纹、Pi runtime、DeepSeek 模型和 Pi 方法包哈希。之后所有正式 assisted
观测必须使用相同 manifest。代码、配置、模型或方法变化需要新研究，不能混入当前队列。

### 6.7 记录系统辅助观测

同一分析师使用冻结版本和同一证据截止点完成每个案件：

```powershell
python -m stock_agent value-assisted-record --config config/watchlist.local.json `
  --case-id <case_id> --output-ref archive:<不泄露分组的opaque_ref> `
  --output-sha256 <64位SHA256> --metrics-file <真实assisted指标JSON> `
  --observed-manifest-id <冻结的manifest_id> `
  --attest-outcome-hidden --attest-evidence-cutoff --unauthorized-egress-count 0 `
  --actor <真实分析师actor> --actor-role lead_analyst
```

`--attest-*` 是具名人工声明，不是系统推断。发现任何未授权外发时必须记录真实数量并立即升级；系统建议将为
`STOP`。不得为了过门把数量填成 0。
assisted 输出引用或 SHA-256 与 baseline 相同会被拒绝；系统运行成本和 token 必须能通过指标中的运行摘要
引用及哈希复核。

### 6.8 独立盲评与揭盲

协调员为每个案件生成随机 A/B 包：

```powershell
python -m stock_agent value-blind-prepare --config config/watchlist.local.json `
  --case-id <case_id> --actor <真实协调员actor> --actor-role research_coordinator
```

只把命令输出的 `package_path` 给独立审核人。不得提供 `sealed_mapping_path`、事件账本、运行日志或能识别
工作流的文件名。审核人分别完成 A/B 评分文件后执行：

```powershell
python -m stock_agent value-blind-evaluate --config config/watchlist.local.json `
  --assignment-id <assignment_id> --scores-a-file <A评分JSON> --scores-b-file <B评分JSON> `
  --attest-blinding --attest-no-ledger-access --notes <复核说明> `
  --actor <独立审核人actor> --actor-role quality_reviewer
```

完成评分后协调员才能揭盲：

```powershell
python -m stock_agent value-blind-reveal --config config/watchlist.local.json `
  --assignment-id <assignment_id> --actor <真实协调员actor> --actor-role research_coordinator
```

sealed 文件缺失、被修改或 commitment 不一致时必须停止该研究并调查，不能手工补写映射。

### 6.9 状态、建议和人工决定

全部案件完成后重新运行状态命令。系统按冻结规则计算建议，但不作批准：

```powershell
python -m stock_agent value-gate-decision --config config/watchlist.local.json `
  --decision CONTINUE --notes <决策依据> --evidence-ref archive:<价值验证决策包> `
  --actor <真实主管actor> --actor-role research_supervisor
```

`CONTINUE` 只有所有门禁 PASS 时允许。`STOP` 和 `NARROW` 可用于中止风险或价值不足的研究。
`EXTEND_ONCE` 必须带 `--decision-deadline`，只能用于证据接近完整且延期有明确边界的情况。

## 7. 指标口径

### 7.1 人工时间

- 主指标：每案 `(baseline active - assisted active) / baseline active` 的中位数。
- 辅助指标：全队列总有效时间的加权改善。
- 目标：中位数至少改善 20%。
- 六段时间：collection、evidence、analysis、drafting、review、correction。
- 系统运行秒数和 token 不得混入人工有效时间。

### 7.2 研究质量

独立审核人按 0-100 分评价 evidence quality、analytical soundness、completeness、actionability、clarity、
citation accuracy，六项等权形成 composite quality。默认要求 assisted 平均质量不低于 baseline。

### 7.3 重大缺陷

分别统计 material errors、material omissions、unsupported claims。任一 assisted 总数高于 baseline，系统建议
`STOP`。缺陷不得用较高的平均分抵销。

### 7.4 成本与数据治理

成本记录 prompt、completion、reasoning token 和估算人民币成本。任何未授权外发是独立停止门，不得用效率、
质量或低成本抵销。

## 8. 完整价值门禁

1. 事件链和语义完整。
2. 研究已建立，政策与建立时完全一致。
3. 20-30 个真实案件已冻结且覆盖至少两个 strata。
4. treatment 绑定的 G0 release manifest 在冻结时为 `READY`。
5. 每个案件 baseline、assisted、盲评和揭盲完整，且无协议偏差。
6. 独立盲评覆盖率 100%。
7. 人工时间中位数改善至少 20%。
8. 研究质量不低于 baseline。
9. 三类重大缺陷均未增加。
10. 未授权外发为 0。

任一项缺失时 `--require-ready` 必须失败。`READY_FOR_DECISION` 也不等于生产 GO。

## 9. 失败和恢复

- 哈希链或语义错误：停止追加，保留原件，复制环境做取证；不得直接改 JSONL。
- 案件输出丢失或哈希不符：按预定义规则排除或重开整个研究，不能替换成新输出后沿用旧哈希。
- 版本漂移：停止 assisted 队列，用新 manifest 建立新研究。
- 盲法破坏：该案件记协议偏差；若系统性泄露，停止研究并重新设计。
- 时间日志不完整：保留事件，但不计入 qualifying 样本；不得事后估算成 `time_log`。
- 未授权外发：立即停止，进入信息安全事件流程，并如实记录非零值。
- 样本不足：建议 `INSUFFICIENT_EVIDENCE`；不能用测试或重复案件补足。

## 10. 当前状态与下一真实门

截至 2026-09-02，技术框架、CLI、只追加事件、盲评隔离、指标计算和报告已经实现。当前本地研究未由真实
人员建立，20-30 个案件、人工基线和独立盲评均未发生；G0 release audit 也仍受 Git 基线和许可证人工
复核阻断。因此当前业务结论必须是 `NOT_STARTED` / `INSUFFICIENT_EVIDENCE`，不能报告价值已验证。

真正的下一步由项目负责人完成三项外部门禁：批准首个 Git 基线、轮换曾暴露的 DeepSeek key、完成许可证
法律/合规判断。随后由真实研究主管批准协议和人员名册，才能建立 G0.5 研究。
