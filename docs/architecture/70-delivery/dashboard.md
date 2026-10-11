# [70-03] dashboard.py — 本地静态看板

| 层 | 行数 | 上游依赖 | 被依赖 |
|---|---|---|---|
| 70-delivery | 1907 | 约 13 个业务模块（只读 latest 投影） | cli（dashboard 命令）、review_workbench（动作后刷新）、scripts/create_dashboard_shortcut |

## 职责
聚合全部子系统的单文件静态 HTML 状态页。**零 JavaScript**、内联 CSS、700px 响应式断点、每类数据缺失显示 empty-state 而非报错——容错读取是硬约定。

## 关键构件
- `generate_dashboard()`（L21）— `_load_dashboard_context_scoped()`（L39-120）聚合约 19 个数据源（最新报告、cases、remediation、alerts、review queue/summary、decisions、workspace、runtime、model ops、market governance、pilot、onboarding、pi skills、workpapers、vault、scheduled summary、**v31 新增：value_validation、backup_restore**）。
- 渲染 `_render_dashboard_html()`（L304-399）— 约 29 个 section：状态栏（7 个 flag）→ KPI 卡 → Brokerage Architecture → 生产试运行 → Business Onboarding → **Value Validation（`_section_value_validation` L674：11 门禁表 + 系统建议 vs 人工决定）** → **Backup/Restore（`_section_backup_restore` L734：技术演练 vs 生产灾备双状态，固定文案"本机 PASS 不代表生产批准"）** → Pi Methods → 投研团队 → Human Workbench → Source Health → Remediation Queue → Run/Model Ops → External Deps → Cases/Runtime/Vault/Workpapers/Research-as-Git/Workspace → 行情快照 → 信号 → LLM 研判 → Multi-Role → Review Queue → 复盘统计。
- `_esc()`（L1750 附近）— & < > 转义，**不转义引号**（与 review_workbench 的 quote=True 不一致，可收紧点）。

## 输出
`reports/dashboard/latest.html` + 时间戳副本（utf-8-sig）。

## 备注 / 风险
每个动作触发全量重建（workbench 同步调用），数据量大时延迟可见。
