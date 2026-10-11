# -*- coding: utf-8 -*-
"""Generate the single master architecture diagram (self-contained HTML).

Data below is condensed from docs/architecture/ block cards (64 modules).
Re-run after code changes: python docs/architecture/generate_master_diagram.py
"""
import ast
import html
import json
from pathlib import Path

OUT = Path(__file__).resolve().parent / "MASTER_DIAGRAM.html"
SRC_ROOT = Path(__file__).resolve().parents[2]

def symbols_for(file_field):
    """AST-extract every top-level def/class with line numbers (verbatim from source)."""
    out = []
    for part in file_field.split("+"):
        rel = part.strip()
        if not rel:
            continue
        if rel.startswith("sources/"):
            rel = "stock_agent/" + rel
        elif not rel.startswith("stock_agent/"):
            rel = "stock_agent/" + rel
        f = SRC_ROOT / rel
        if not f.exists():
            continue
        try:
            tree = ast.parse(f.read_text(encoding="utf-8"))
        except SyntaxError:
            continue
        for node in tree.body:
            if isinstance(node, ast.FunctionDef):
                out.append(("def", node.name, node.lineno))
            elif isinstance(node, ast.AsyncFunctionDef):
                out.append(("async", node.name, node.lineno))
            elif isinstance(node, ast.ClassDef):
                out.append(("class", node.name, node.lineno))
    return out


def sym_block(file_field, compact=False):
    syms = symbols_for(file_field)
    if not syms:
        return ""
    rows = "".join('<div class="sy"><i>{}</i> {} <em>L{}</em></div>'.format(kind, esc(name), line)
                   for kind, name, line in sorted(syms, key=lambda s: s[2]))
    cols = " c3" if len(syms) > 60 else (" c2" if len(syms) > 18 else "")
    return ('<div class="sym{}"><div class="symh">顶层符号 · AST 实测 {}</div>'
            '<div class="symlist{}">{}</div></div>').format(
        " many" if len(syms) > 18 else "", len(syms), cols, rows)

# 阈值/常量（数字均经子代理对照源码核验）
CONSTS = {
 "analysis": ["P0词 +90 / P1词 +55 / 主题 +12 / 风险词 min(24n,48)", "定级: ≥55→P1 · ≥25→P2", "价格异常 ±3% · ±1亿; 双异常+主题 +60", "证据异常 unknown +65 +bonus 30"],
 "market-data-governance": ["盘中新鲜度 ≤120s · 盘后 ≤36h", "双源共识 ≤0.5% (max-min)/median", "时段: 9:15-9:25 / 9:30-11:30 / 13:00-14:57 / 14:57-15:00 (+8)"],
 "review-queue": ["SLA P0 0.5h / P1 2h / P2 8h / P3 24h (营业时间)", "at_risk 阈值 0.8 · assignment_mode=suggest_only"],
 "llm-governance": ["外发仅 public · 默认分类 internal", "最小化: 6000字符 / 50项 / 深度6", "policy v28 · 四检测全开 · 例外 human_only"],
 "model-operations": ["agent_target 180s · reserve 45s · start 75s · reduce 105s", "SLO: main≥0.95 role≥0.90 usable≥0.95 fully≥0.75"],
 "pi-runtime": ["PINNED 0.84.2 · stdout 4MB / stderr 256KB", "审计写失败→丢弃成功输出", "SAFE_ENV_KEYS 白名单 + HOME 重定向"],
 "research-runtime": ["11 阶段 STAGE_ORDER · 派生式推导", "自动化前缀: system/llm/agent/deepseek/pi/openclaw/bot/stock_agent", "action_history 截断 100 条"],
 "research-workpapers": ["保留期 ≥5 年 · 本地链=shadow ledger", "P0/P1 须 official primary · 模型声明无引用→blocked"],
 "evidence-vault": ["官方域名 6: bse/cninfo/csrc/sac/sse/szse", "五态门禁 · 自动补采 ≤3/轮", "生产档案: 合规+信安双复核, 激活人独立"],
 "provider-registry": ["13 项必填尽调证据", "复核人≠提交人, 角色互异; 链坏→active=∅"],
 "control-evidence": ["16 类证据白名单 · scheme {archive,dms,records,https}", "复核人≠登记人≠主体 · 到期类须 valid_to"],
 "production-pilot": ["19 类门禁 · 审批效期 30 天", "影子资格 14 项 · trigger 必须 scheduled", "GO = 门禁1-17 + 6门互异真人 + 手动激活"],
 "value-validation": ["12 项价值门禁 · cohort 20-30 案 ≥2 层", "人时中位改善 ≥20% · 质量非劣", "sealed mapping chmod 0600 · 绑 release-audit manifest_id"],
 "business-onboarding": ["8 阶段顺序不可跳 · current_phase=首个非PASS", "input_fingerprint: 任一上游变化即失效"],
 "backup-restore": ["载荷 secret scan BLOCKING→终止创建", "drill 仅 data/backup_restore/drills/ · 绝不覆盖源", "production_readiness 硬编码 BLOCKED(人工门)"],
 "release-audit": ["13 个子系统 latest 投影新鲜度 (24h/72h)", "READY 须 5 控制物 PASS + 指纹一致 + git HEAD"],
 "release-controls": ["findings 只记 sha256+长度 (值/片段不落盘)", "SBOM: python+npm+pi_skills 三源", "REVIEW_REQUIRED 保持阻断"],
 "llm-agent": ["动作白名单 {export_review, write_action_log}", "认证错误中止 provider · 75s 重试线"],
 "run-ops": ["预算: ≤4 次 / ≤120s LLM / 总 420s(本地)", "effective_timeout = min(请求, 剩余run, 剩余llm)"],
 "storage": ["signals 单表 30 列 · insert-or-skip", "fetch_many 500 ID/批 · PRAGMA 迁移"],
 "audit-log": ["链坏→AuditLogIntegrityError 拒写", "O_EXCL 文件锁 5s · fsync 持久化"],
 "agent-handoff": ["max_public_signals ≤10 · summary ≤2000字", "数组 ≤20 项/条 ≤1000字 · 扫 buy/sell 词"],
 "openclaw-handoff": ["五道闸 · 白名单 10 键 · 上限 10 条"],
 "doctor": ["约 30 项检查 · 任一 FAIL→总体 FAIL", "敏感字段扫描: api_key/prompt 出现即 FAIL"],
 "brokerage-architecture": ["idempotency_key 查重 · 每事件带 safety_boundary", "quarantine 仅限 _uat/test_ 来源"],
}

# ---------------------------------------------------------------- module data
# badges: FC=fail-closed, HU=人工门, NET=联网需显式--live
# stages: ①-⑯ = agent-run 阶段序号
M = []  # (id, col, file, loc, title, role, fns[(sig,desc)], io_in, io_write, badges, stages, risk)

def m(id, col, file, loc, title, role, fns, io_in="", io_w="", badges="", stages=(), risk=""):
    M.append(dict(id=id, col=col, file=file, loc=loc, title=title, role=role,
                  fns=fns, io_in=io_in, io_w=io_w, badges=badges, stages=list(stages), risk=risk))

# ---- 10-sources
m("local-jsonl", "c10", "sources/local_jsonl.py", 49, "手工情报池",
  "读配置指定的本地 JSONL，人工情报入口",
  [("collect_local_jsonl()", "遍历源清单，跳过禁用/缺失"),
   ("_read_jsonl()", "逐行解析，坏行→source_error")],
  "data/inbox/*.jsonl", "—", "", [1], "一条坏行使整个文件降级")
m("filing-jsonl", "c10", "sources/filing_jsonl.py", 193, "公告 JSONL 回放",
  "消费落地公告文件，重建公告/财务证据",
  [("_row_to_signal()", "字段别名兼容+title 合成"),
   ("_build_filing_evidence()", "5 必填字段，缺→partial+人工复核"),
   ("_build_financial_evidence()", "metrics 空→missing")],
  "落地 JSONL", "—", "", [1], "published_at 缺省回退当前时间")
m("rss", "c10", "sources/rss.py", 128, "RSS/Atom 新闻",
  "纯标准库拉取解析，无 feedparser",
  [("collect_rss()", "UA stock-agent/0.1"),
   ("_parse_rss/_parse_atom()", "按根 tag 区分格式"),
   ("日期归一", "统一本地时区 ISO")],
  "RSS URL", "—", "", [1], "无速率限制")
m("eastmoney-filing", "c10", "sources/eastmoney_filing_adapter.py", 326, "东财公告采集",
  "分类+报告期推断+落地，唯一写盘采集器",
  [("_classify_announcement()", "12 类关键词顺序优先"),
   ("_infer_report_period()", "→ {年}A/H1/Q1/Q3/E"),
   ("_write_landing_jsonl()", "追加写落地文件"),
   ("预评分", "反向调 analysis 重要性")],
  "公告 API", "data/inbox/filings/*.jsonl", "", [1], "对 analysis 反向依赖")
m("eastmoney-quote", "c10", "sources/eastmoney_adapter.py", 159, "东财实时行情",
  "批量快照：价/涨跌/额/主力净流入/f124 时间",
  [("collect_eastmoney()", "一次批量 HTTP+Referer"),
   ("_to_secid()", "6/9→沪 1.x，其余→深 0.x"),
   ("published_at", "优先交易所 f124")],
  "symbols", "—", "", [1], "免费接口≠商业授权")
m("akshare", "c10", "sources/akshare_adapter.py", 95, "AkShare 行情(可选)",
  "全市场快照后本地过滤",
  [("collect_akshare()", "spot_em→按 symbols 过滤"),
   ("_safe_value()", "NaN/numpy 转原生"),
   ("quote_timestamp", "恒空串(诚实标注)")],
  "symbols", "—", "", (), "无交易所时间→治理必阻断")
m("pysnowball", "c10", "sources/pysnowball_adapter.py", 245, "雪球行情(可选)",
  "token 环境变量；批量上限 10",
  [("collect_pysnowball()", "XUEQIU/PYSNOWBALL_TOKEN"),
   ("_to_xueqiu_symbol()", "SS/SH/SZ/BJ 映射"),
   ("_build_source_evidence()", "缺 current/percent→partial")],
  "symbols+token", "—", "", (), "del timeout 无请求超时")
m("alltick", "c10", "sources/alltick_adapter.py", 198, "AllTick 逐笔(可选)",
  "trade-tick 逐 symbol 拉取，上限 5",
  [("collect_alltick()", "ret∉(None,0,200) 抛错"),
   ("时间解析", "epoch 毫秒/ISO 双支持"),
   ("pct_change/main_net_inflow", "恒 None(字段诚实)")],
  "symbols+ALLTICK_TOKEN", "—", "", (), "永不触发价格异常规则")
m("free-quote", "c10", "sources/free_quote_adapter.py", 22, "AllTick 协议网关",
  "改名层：source/provider→free_quote",
  [("collect_free_quote()", "委托 alltick+改写标识")],
  "网关配置", "—", "", (), "不算独立供应商(§7.2)")
m("sources-package", "c10", "sources/__init__.py", 21, "适配器注册表",
  "统一导出 9 个 collect_*；错误统一转 source_error 信号（可见但不进主链）",
  [("统一错误语义", "采集异常→RawSignal(source_error)")],
  "config.sources.*", "—", "", (), "")

# ---- 20-rule-engine
m("analysis", "c20", "analysis.py", 371, "规则评分引擎",
  "主题/情绪/优先级定级；signal_id 是全系统去重根基",
  [("signal_id()", "price→11 字段快照哈希(跨源去重)；其余 sha1(source|title|url|ts)"),
   ("_score_priority()", "P0词+90 / P1+55 / 主题+12 / 风险词≤48"),
   ("_score_filing_importance()", "证据异常→+65+30(宁多审)"),
   ("_score_price_anomaly()", "±3% 或 ±1亿；双异常+主题→P1"),
   ("_needs_review()", "P0/P1·无URL·source_error→复核")],
  "RawSignal×N", "payload 回写", "", [2], "无分词子串匹配；改 signal_id 破坏历史去重")
m("research-framework", "c20", "research_framework.py", 188, "研究剧本路由",
  "5 条默认剧本给信号挂支线/问题/缺口",
  [("DEFAULT_PLAYBOOKS", "source_health/价格/事件/风险挑战/深研"),
   ("_matches()", "AND 语义(与 team 的 OR 相反)")],
  "AnalyzedSignal", "research_tracks/questions", "", [2], "")
m("research-team", "c20", "research_team.py", 311, "虚拟投研团队",
  "6 岗位派生映射(非真实人员)，纯内存",
  [("run_research_team()", "OR 语义岗位匹配"),
   ("supervisor_brief", "focus+需决策前10+下一步"),
   ("GUARDRAILS", "no_auto_trading×4")],
  "带 tracks 信号", "—(派生)", "", [2], "")
m("team-ops", "c20", "team_ops.py", 336, "团队运营工件",
  "晨会纪要+人工干预队列+可复制决策命令",
  [("_build_human_intervention_queue()", "7 允许/3 禁止决策"),
   ("_decision_commands()", "生成 decision-confirm 命令串"),
   ("_build_strategy_backlog()", "只输出不改配置")],
  "team 输出", "reports/team/", "", [10], "命令串默认 actor=user")
m("evidence-chain", "c20", "evidence_chain.py", 183, "证据链构建",
  "引用解析归类；解析不了→人工缺口任务",
  [("_resolve_ref()", "属性→payload 点路径→dict 三级回退"),
   ("_gap_task()", "owner=human+4 合法决策"),
   ("_quality()", "≥0.8 零gap=OK / ≥0.3 WARN")],
  "LLM 后信号", "SQLite 写回", "", [7], "验引用ID存在，非验原文支持(§6.4 升级项)")
m("source-health", "c20", "source_health.py", 482, "数据源健康",
  "源打分+状态机+信号回写",
  [("_score_event()", "0.35成功+0.25完整+0.15覆盖+0.15信任+0.10去重"),
   ("状态机", "FAIL>WARN>OK；low_trust 不阻断"),
   ("attach_source_quality()", "写 payload.source_quality")],
  "采集事件", "reports/source_health/", "", [2], "")
m("source-remediation", "c20", "source_remediation.py", 739, "补救队列",
  "降级/健康/缺口三路合流，决策永不丢",
  [("build_remediation_summary()", "四元组去重"),
   ("_apply_decision_records_to_tasks()", "决策日志重放(重启不丢)"),
   ("record_remediation_action()", "5 动作白名单唯一写入口")],
  "健康+gap", "data/remediation/ 日志", "HU", (), "monkeypatch 转发事件(脆弱)")

# ---- 50-gates (列在 model 前)
m("market-resilience", "c50", "market_resilience.py", 491, "行情弹性采集",
  "降级链+验证源+缓存兜底",
  [("collect_..._with_resilience()", "east→ak→snow→alltick→fq→cache"),
   ("verification_providers", "已覆盖仍采(跨源一致性)"),
   ("write/load_quote_cache()", "TTL 60min+cached_from 溯源"),
   ("diagnose_network_error()", "六类诊断+修复指南")],
  "5 行情源", "data/cache/market_quotes/", "", [1], "naive 时间 UTC 口径差")
m("market-data-governance", "c50", "market_data_governance.py", 513, "行情数据门禁 v23",
  "新鲜度/时段/核准/共识→LLM 资格",
  [("_assess_one()", "未核准→BLOCK；盘中>120s→BLOCK"),
   ("_market_session()", "+8 硬编码 A 股时段"),
   ("_consistency()", "≥2源 ≤0.5% 否则 CONFLICT"),
   ("market_signal_llm_allowed()", "llm_agent 直接查询")],
  "price 信号+diagnostics", "reports/market_data_governance/", "FC", [1], "临停/半日市未处理")
m("provider-registry", "c50", "provider_registry.py", 630, "供应商生命周期 v24",
  "候选→尽调→复核→激活→暂停/退役",
  [("_validate_evidence()", "13 项必填证据全非空"),
   ("record_provider_review()", "复核≠提交人；角色互异"),
   ("provider_activation_eligibility()", "scope 含 external_llm+控制证据逐条解析"),
   ("load_provider_registry()", "链坏→active=∅(fail-closed)")],
  "尽调材料引用", "供应商哈希链", "FC|HU", (), "当前 0/2 active")
m("control-evidence", "c50", "control_evidence.py", 507, "控制证据登记 v29",
  "门禁体系证据底座：只存哈希+外部档案 URI",
  [("register_control_evidence()", "16 类白名单；原件不入库"),
   ("_validate_archive_ref()", "scheme 白名单；禁路径/密钥/localhost"),
   ("review_control_evidence()", "复核≠登记人≠主体"),
   ("resolve_verified_control_evidence()", "五重校验 fail-closed")],
  "文件+档案 URI", "控制证据哈希链", "FC|HU", (), "")
m("evidence-vault", "c50", "evidence_vault.py", 1431, "官方原件证据库 v28",
  "内容寻址留存；先留存后外发门禁",
  [("ingest_official_document_bytes()", "HTTPS 白名单/443/防SSRF/魔数嗅探"),
   ("_atomic_write_binary_once()", "temp+fsync+重哈希+replace"),
   ("evidence_status_for_signal()", "五态；RETAINED_VERIFIED→llm_eligible"),
   ("record_archive_review/activate()", "合规+信安双复核；影子库不可当生产档案"),
   ("_looks_like_challenge_page()", "验证码页→隔离不绕过")],
  "本地原件/受控fetch", "data/evidence_vault/", "FC|HU|NET", [3], "原件字节永不发 DeepSeek")
m("evidence-vault-uat", "c50", "evidence_vault_uat.py", 200, "证据库实弹 UAT",
  "临时目录真抓取；生产指纹前后比对证零污染",
  [("run_evidence_vault_uat()", "无--live 拒绝"),
   ("_production_inventory_fingerprint()", "前后不一致即 FAIL")],
  "一个官方 URL", "reports/evidence_vault/uat/", "FC|NET", (), "")

# ---- 30-model-plane
m("llm-governance", "c30", "llm_governance.py", 1077, "LLM 外发治理 v28",
  "仅 public 可外发；敏感/注入/限制名单阻断；例外仅真人",
  [("_evaluate_preflight()", "分类推导→默认 internal；密钥/PII/内幕→升级"),
   ("build_governed_prompt_payload()", "白名单+剥query+6000字/50项/深6"),
   ("_validate_grounding()", "evidence_refs 对照→grounded/partial/blocked"),
   ("record_governance_exception_decision()", "仅合规/信安真人×3 处置"),
   ("审计", "预检/后验→哈希链，密钥值不落盘")],
  "信号+策略", "data/llm_governance/", "FC|HU", [8], "6 条历史例外待真人处置")
m("llm-agent", "c30", "llm_agent.py", 791, "信号级 LLM 研判",
  "signal_llm 主通道；失败永不破坏管道",
  [("enrich_signals()", "门槛→截断→deadline→预检→调用"),
   ("_call_llm()", "stdlib POST json_object；墙钟分块读"),
   ("_enrich_with_provider_fallback()", "provider×attempt 重试；认证错中止"),
   ("_validate_analysis()", "枚举归一+confidence 夹取"),
   ("_validate_actions()", "仅 export_review/write_action_log")],
  "inserted 信号", "SQLite llm_* 列", "FC", [5], "auth 子串误判；dict 包装缺结果")
m("role-analyst", "c30", "role_analyst.py", 765, "多角色研究台",
  "同模型 4 角色批量（虚假独立已标注）",
  [("_build_role_prompt()", "roles+schema+guardrails 单 JSON"),
   ("_normalize_role_response_items()", "5 级映射容错(含 positional 风险)"),
   ("_validate_synthesis()", "任一角色要人工→强制 human_review")],
  "inserted 信号", "SQLite role_* 列", "FC", [6], "JSON 失败无重试(不对称)")
m("model-operations", "c30", "model_operations.py", 570, "SLO/截止/用量 v22",
  "保交付降级；元数据脱敏；成本核算",
  [("select_deadline_candidates()", "180s 目标：<75s skip/<105s reduce"),
   ("sanitize_response_metadata()", "仅 6 字段；prompt/内容不落盘"),
   ("build_model_operations_snapshot()", "成功率≥0.95/0.90·grounding≥0.95")],
  "llm 调用遥测", "reports/model_operations/ usage JSONL", "FC", [12], "")
m("pi-runtime", "c30", "pi_runtime.py", 761, "Pi 隔离运行时 v25",
  "唯一受控 LLM 执行 harness（无工具/会话/插件）",
  [("call_pi_agent()", "钉版0.84.2；--no-tools等9旗标"),
   ("_isolated_environment()", "SAFE_ENV_KEYS 白名单+HOME 重定向"),
   ("parse_pi_json_stream()", "tool_call≠0 即失败"),
   ("审计 fail-closed", "写链失败→丢弃成功输出")],
  "治理批准 prompt", "logs/pi_runtime/ 哈希链", "FC", [5, 6], "HTPPS_PROXY 拼写冗余项")
m("pi-skills", "c30", "pi_skills.py", 448, "方法供应链 v26",
  "注册表+内容锁+按角色编译",
  [("_load_and_validate_registry()", "许可证/commit/路径/12KB/禁词"),
   ("build_pi_skill_lock()", "角色 bundle sha256"),
   ("pi_skill_supply_chain_status()", "lock 不全等→全网阻断"),
   ("compile_role_system_prompt()", "<reviewed_method_bundle> ≤48KB")],
  "config/pi_skills.json + .pi/skills", "reports/pi_agent/skills/", "FC", (), "11 方法 bundle 85289fa6…")
m("openclaw-handoff", "c30", "openclaw_handoff.py", 228, "handoff 构造器 v24",
  "runtime 无关的公共 handoff 核心",
  [("_handoff_allowed()", "5 道闸(public/无敏感/grounded/行情/有研判)"),
   ("_public_signal()", "白名单最小化≤10 条+全文扫描"),
   ("validate_openclaw_handoff()", "计数/哈希/逐条治理复核")],
  "reports/json 最新", "reports/handoff/", "FC", (), "url 未剥 query")
m("agent-handoff", "c30", "agent_handoff.py", 268, "Pi 公共摘要 v25",
  "0 条 public=0 次调用（条件调用）",
  [("run_pi_public_handoff()", "call_required 判定→skip_reason"),
   ("validate_pi_public_summary()", "字段 exact+signal_ids 全等+交易词扫描"),
   ("check_latest_agent_handoff()", "三级哈希链只读验证")],
  "handoff JSON", "reports/pi_agent/public_handoff/", "FC", (), "")

# ---- 40-research-core
m("research-cases", "c40", "research_cases.py", 732, "研究案件聚合",
  "(实体,主题,催化) 幂等聚案；决策日志重放",
  [("_case_id()", "sha1 三元组[:16] 幂等"),
   ("enrich_research_cases()", "≤5 案/轮+ops 预算"),
   ("record_case_decision()", "4 决策；noise/archived 终态")],
  "inserted+tracks", "data/research_cases/", "HU", [13], "本层无角色保护(靠 runtime 兜底)")
m("research-runtime", "c40", "research_runtime.py", 1831, "案件运行时状态机",
  "11 阶段派生状态机+受保护动作三重授权",
  [("_derive_stage()", "由 gates 反推(非增量迁移)"),
   ("_evaluate_gates()", "P0/P1 须 official primary；challenge 须 risk_reviewer+risk_note:"),
   ("_require_protected_actor()", "角色+自动化前缀排除+指派匹配"),
   ("sync_research_runtime()", "fail-closed：空输入保留/缺信号回填/降级诊断"),
   ("record_runtime_action()", "底稿门→运营门→handler→回写")],
  "cases+signals", "data/research_runtime/", "FC|HU", [14], "前缀 bot 可误伤真人")
m("research-workpapers", "c40", "research_workpapers.py", 1584, "工作底稿(事件溯源)",
  "哈希链事件唯一事实源；四类声明分离",
  [("build_research_workpaper()", "revision_basis 幂等版本化"),
   ("_claims_for_signals()", "陈述/观测/推断(无引用→blocked)"),
   ("_authority_tier()", "监管所>披露平台>核准行情>聚合站"),
   ("validate_workpaper_gate()", "批准前 fail-closed；链坏即 raise"),
   ("_production_archive_state()", "本地链=shadow ledger→BLOCKED")],
  "runtime+cases", "data/research_workpapers/events.jsonl", "FC|HU", [15], "")
m("research-operations", "c40", "research_operations.py", 937, "人员名册/授权/SLA",
  "真人资格+限制名单+动作授权+营业时间 SLA",
  [("_principal_ineligibility()", "lead/quality 须核验分析师注册"),
   ("authorize_runtime_action()", "enforce 下逐项 deny 矩阵"),
   ("add_business_hours()", "Asia/Shanghai 08:30-18:00+节假日"),
   ("build_..._snapshot()", "合格/推荐人选 suggest_only")],
  "roster+restrictions JSON", "reports/research_operations/", "FC|HU", (), "当前 0 真人→结构性 BLOCKED(正确)")
m("research-git", "c40", "research_git.py", 619, "Research-as-Git 草稿",
  "草稿→真人 approve/reject 终态门",
  [("stage/commit/approve/reject/defer", "迁移约束在 _record_human_gate"),
   ("_reject_forbidden_text()", "中文执行词命中即 raise"),
   ("stage_llm_research_drafts()", "默认关闭")],
  "llm_analysis", "data/research_git/ JSONL", "HU", (), "无哈希链(强度低于 workpapers)")

# ---- 60-business + 85-release
m("business-onboarding", "c65", "business_onboarding.py", 747, "业务接入八阶段 v29",
  "顺序不可跳；current_phase=第一个非 PASS",
  [("_governance_scope_stage()", "真实法人 id+3 控制verified"),
   ("_human_roster_stage()", "逐角色合格真人+二分图互异指派"),
   ("_shadow_readiness_stage()", "≥5次/≥3交易日/只认计划任务"),
   ("build_business_onboarding()", "input_fingerprint 聚合(变即失效)")],
  "全部门禁投影", "reports/business_onboarding/", "FC|HU", (), "0/8 停 governance_scope")
m("production-pilot", "c65", "production_pilot.py", 1563, "生产试点 v25",
  "Go/No-Go 裁决：19 门禁+指纹+6 门审批",
  [("_build_gates()", "19 类：链/名册/供应商/行情/LLM/原件/队列/对账/影子…"),
   ("candidate_fingerprint", "关键输入哈希聚合；变→审批全废"),
   ("record_pilot_approval()", "6 门 6 人互异+30 天效期"),
   ("activate_production_candidate()", "仅 publication_approver(须先批 publication)"),
   ("_shadow_qualification_reasons()", "trigger 必须 scheduled×14 项")],
  "全子系统 latest", "reports/production_pilot/", "FC|HU", (), "当前 NO_GO fingerprint 4c6f7624…")
m("value-validation", "c65", "value_validation.py", 1672, "G0.5 价值验证 v31",
  "20-30 真实案件配对+盲评；≥20% 效率门禁",
  [("_authorize_human()", "名册 active 真人+角色+注册核验"),
   ("open_value_study()…reveal", "11 个 value-* 工作流 API 全链"),
   ("sealed mapping", "commit/reveal 三重比对；chmod 0600"),
   ("_build_value_gates()", "12 门禁：配对/盲评覆盖/零偏差/≥20%/非劣/外发=0"),
   ("treatment 冻结", "强制绑 release_audit manifest_id")],
  "案件+人时日志+盲评", "data/value_validation/", "FC|HU", (), "七重防 mock；template_only 拒收")
m("release-controls", "c65", "release_controls.py", 852, "发布控制物 v31",
  "secret scan / SBOM / 许可证清单",
  [("build_secret_scan()", "只记 sha256+长度；value/snippet 不落盘"),
   ("parse_python/npm/skills_lock()", "三来源 SBOM 汇总"),
   ("许可证三级", "REVIEW_REQUIRED 保持阻断(非法务批准)")],
  "候选文件+local 敏感件", "reports/release/", "FC|NET", (), "--fetch-python-metadata 才联网")
m("python-environment", "c65", "python_environment.py", 339, "环境可复现审计 v31",
  ".venv311/312 vs requirements.lock 精确比对",
  [("build_python_environment_audit()", "missing/mismatch/extra"),
   ("--run-tests", "不跑即 BLOCKED；只存 output_sha256"),
   ("源指纹前后", "审计中源变动→blocker")],
  "隔离环境", "—(审计结果)", "FC", (), "不创建环境不装依赖")
m("release-audit", "c65", "release_audit.py", 792, "只读发布审计 v31",
  "git 身份+控制物+新鲜度→manifest_id",
  [("READY 门禁", "HEAD+worktree 净+5 控制物 PASS+指纹一致"),
   ("LATEST_ARTIFACTS", "13 子系统投影新鲜度(≤24h/72h)"),
   ("operation_contract", "无网络/采集/LLM/账本写/审批")],
  "各 latest.json", "reports/release_audit/", "FC", (), "无 git HEAD→必 NOT READY")
m("backup-restore", "c65", "backup_restore.py", 1368, "备份与恢复演练 v32",
  "凭据无关备份+隔离演练；生产 DR 永久人工门",
  [("_discover_sources()", "不含 *.local/名册/sealed mapping"),
   ("_sensitive_key()", "restore 配置脱敏 ${REDACTED}"),
   ("_scan_sqlite()", "载荷扫描 BLOCKING→终止创建"),
   ("run_restore_drill()", "仅 drills/ 目录；绝不覆盖源")],
  "选定事实源", "data/backup_restore/", "FC|HU", (), "包未加密(诚实声明)；production_readiness 恒 BLOCKED")

# ---- 70-delivery
m("reports", "c70", "reports.py", 1016, "日报/JSON/预警",
  "每轮运行的最终物化(24 章节)",
  [("write_outputs()", "top20+四视图+utf-8-sig"),
   ("_render_markdown()", "治理/案件/底稿/复盘全章节"),
   ("JSON 全量", "P3 也入 JSON(下游数据源)")],
  "inserted+stats", "reports/daily|json|alerts/", "", [16], "日报统计基于截断 top_n")
m("alerts", "c70", "alerts.py", 485, "预警/通知",
  "约 13 类触发；无触发也写正常状态",
  [("_check_triggers()", "P0/P1/高置信待决/SLA/pilot 三项…"),
   ("generate_notifications()", "坏行静默跳过")],
  "6 类 latest", "reports/notifications/", "", (), "docstring 与实现不一致")
m("dashboard", "c70", "dashboard.py", 1907, "本地静态看板",
  "单文件零 JS HTML，约 29 section",
  [("_load_dashboard_context_scoped()", "19 数据源容错读取"),
   ("_section_value_validation()", "12 门禁表+系统建议vs人工决定"),
   ("_section_backup_restore()", "技术演练vs生产灾备双状态")],
  "各 latest", "reports/dashboard/", "", (), "_esc 不转义引号")
m("review-workbench", "c70", "review_workbench.py", 1901, "回环复核工作台",
  "127.0.0.1:8767 四视图人工操作",
  [("四视图", "remediation/runtime/review/pilot(只读)"),
   ("安全", "CSRF+Origin/Host+64KiB+CSP+关停HMAC"),
   ("submit_runtime_..._action()", "写路径=record_runtime_action"),
   ("按阶段出表单", "quality/compliance/approval/monitor…")],
  "浏览器", "经各核心模块", "FC|HU", (), "无用户认证(单机模型)")
m("workspace-inbox", "c70", "workspace_inbox.py", 306, "工作区收件箱",
  "报告→收件箱对象(信号/证据/评论/草稿)",
  [("sync_workspace_inbox()", "计数进 run stats")],
  "reports/json", "data/workspace/inbox/", "", (), "")
m("review", "c70", "review.py", 513, "复盘汇总",
  "completed 复盘→有效率/优化建议(只建议)",
  [("汇总统计", "false_positive 按词/源归因"),
   ("effect_window_days=5", "有效性窗口")],
  "data/review/completed", "reports/review/", "", (), "当前 completed=0 闭环未转")
m("review-queue", "c70", "review_queue.py", 572, "人工复核队列 v23",
  "append-only 投影：去重+状态机+SLA",
  [("TRANSITIONS", "pending→in_review→completed/deferred/superseded"),
   ("ACTION_ROLES", "start/complete→lead；reopen→协调/主管"),
   ("build_review_queue()", "营业时间 SLA+升级排序")],
  "review JSONL×3", "data/review/queue_index.json", "FC|HU", [11], "154 条积压=正确暴露组织缺口")
m("review-priority", "c70", "review_priority.py", 220, "优先级队列",
  "补救+复核合并排序(纯建议)",
  [("write", "reports/workbench/priority_queue_*")],
  "remediation+queue", "reports/workbench/", "", (), "")
m("decisions", "c70", "decisions.py", 258, "上司决策台账",
  "7 决策/6 结果；交易类硬编码拒绝",
  [("record_decision()", "FORBIDDEN(auto_trade/buy/sell…)→ValueError"),
   ("compute_decision_summary()", "pipeline 决策上下文注入")],
  "CLI/工作台", "data/decisions/", "HU", (), "actor 宽松(默认 user)，与 queue 平行")

# ---- top strip 80
m("pipeline", "strip", "pipeline.py", 751, "主流水线编排",
  "run(纯规则)/agent-run(含LLM) 两主线；isolated 隔离档；LLM 仅 inserted",
  [("阶段序 ①-⑯", "collect→分析→vault→storage→LLM→链→治理→动作→…→输出"),
   ("project_path_override", "全程 IO 锁进项目根"),
   ("_update_signal_*", "LLM/角色/证据链写回 SQLite")],
  "config", "全部产物入口", "", list(range(1, 17)), "3 处直写 UPDATE 绕过 SignalStore")
m("cli", "strip", "cli.py", 3054, "命令行接口",
  "约 90 子命令；_policy_call→Policy denied exit2；--live/--write/--require-* 显式门槛",
  [("分组", "运行/复核/案件/证据/治理/Pi/试点/发布/备份/价值…"),
   ("value-* ×11", "全部强制 --actor/--actor-role")],
  "argv", "经各模块", "FC|HU", (), "最大单文件(§18.2 待拆)")
m("run-ops", "strip", "run_ops.py", 287, "运行预算/遥测",
  "阶段计时+LLM 预算+降级账本",
  [("reserve_llm_call()", "≤4 次/≤120s/总 420s 三闸"),
   ("effective_timeout()", "min(请求,剩余run,剩余llm)"),
   ("summary()", "FAIL/WARN 冒泡收敛")],
  "config.run_ops", "logs/run_ops/", "", [1], "")
m("doctor", "strip", "doctor.py", 1295, "健康体检",
  "约 30 项检查；治理异常宁可 FAIL",
  [("策略硬校验", "auto_*/external_publication=false 否则 FAIL"),
   ("model_ops 敏感扫描", "快照含 api_key/prompt→FAIL"),
   ("_probe_llm_connectivity()", "唯一主动联网之一(8s)")],
  "各 latest+环境", "reports/health/", "FC", (), "import llm_agent 私有函数")
m("brokerage-architecture", "strip", "brokerage_architecture.py", 1089, "事件台账/投影",
  "幂等事件+状态投影+压缩+健康指标(无哈希链)；上游发送方：cases/git/runtime/remediation/decisions",
  [("append_event()", "idempotency_key 查重"),
   ("quarantine_test_entity()", "仅 _uat/test_ 来源可隔离"),
   ("event-compact/health", "去重压缩+重复率")],
  "各模块 best-effort", "data/events/", "", (), "monkeypatch 转发三处(§18.2)")

# ---- bottom strip 00
m("config", "base", "config.py", 154, "路径/配置装载",
  "project_path+contextvars override+约85 目录契约",
  [("project_path_override()", "线程/task 作用域，退出必恢复"),
   ("ensure_directories()", "v31 新增 value/backup 目录")],
  "—", "目录骨架", "", (), "三处旧 monkeypatch 待迁移")
m("text", "base", "text.py", 28, "文本归一原语",
  "normalize/compact/unique——匹配与 signal_id 地基",
  [("normalize_text()", "去标点小写(无分词)")],
  "—", "—", "", (), "改规则即改历史 signal_id 语义")
m("models", "base", "models.py", 71, "核心数据模型",
  "RawSignal / AnalyzedSignal(30字段) / PipelineResult",
  [("now_iso()", "全系统唯一时间戳")],
  "—", "—", "", (), "payload 隐式契约靠测试维持")
m("audit-log", "base", "audit_log.py", 169, "哈希链原语",
  "全部防篡改承诺的最终落点",
  [("load_hash_chained_jsonl()", "逐条重算；断链→FAIL+丢弃"),
   ("append_hash_chained_jsonl()", "链坏拒写；O_EXCL 锁+fsync")],
  "event dict", "各模块链 JSONL", "FC", (), "防改不防整链回滚(需 WORM)")
m("storage", "base", "storage.py", 215, "SQLite 持久层",
  "signals 单表 30 列；insert-or-skip 去重",
  [("upsert_many()", "exists→insert，重复不更新"),
   ("_migrate_add_columns()", "PRAGMA+ALTER 最小迁移")],
  "AnalyzedSignal", "data/stock_agent.sqlite3", "", [4], "")
m("skills", "base", "skills.py", 99, "技能发现(非生产)",
  "skills/ 目录扫描展示；生产方法在 pi-skills",
  [("discover_skills()", "rglob SKILL.md")],
  "skills/", "—", "", (), "命名易与生产方法链混淆")
m("package-entry", "base", "__main__.py + __init__.py", 9, "包入口",
  "python -m stock_agent → cli.main()；__version__=0.1.0",
  [("__main__.main()", "6 行薄入口，唯一启动路径")],
  "argv", "—", "", (), "")

EXT = [
    ("ext-deepseek", "DeepSeek API", "deepseek-v4-pro / Chat Completions", "仅经治理批准 payload；密钥只在环境变量"),
    ("ext-official", "官方域名白名单", "cninfo / sse / szse / bse / csrc / sac", "原件受控抓取；挑战页隔离不绕过"),
    ("ext-vendor", "行情供应商(未授权)", "eastmoney / alltick / 雪球 / akshare", "可达≠授权：未过 registry 一律 BLOCK"),
]

COLS = [
    ("c10", "10-sources 采集层", "统一 RawSignal；错误即信号"),
    ("c20", "20-rule-engine 规则层", "评分/路由/健康/补救"),
    ("c50", "50-gates 数据门禁", "fail-closed：原件/行情/供应商"),
    ("c30", "30-model-plane 受控推理", "治理先行；Pi 唯一执行"),
    ("c40", "40-research-core 研究核心", "案件状态机+哈希链底稿"),
    ("c65", "60/85 业务与发布门禁", "8 阶段 / 19 门禁 / 11 门禁 / G0 控制物"),
    ("c70", "70-delivery 交付层", "只读投影；人机界面"),
]

# ---------------------------------------------------------------- edges
# (from, to, label, kind)  kind: data蓝 gate红 audit紫 release绿 back灰back strip灰dash
E = [
    ("c10-h", "analysis", "① RawSignal ×9（经 pipeline collect）", "data"),
    ("eastmoney-quote", "market-resilience", "price 信号", "data"),
    ("market-resilience", "market-data-governance", "报价+observations", "data"),
    ("provider-registry", "market-data-governance", "attested providers（0/2）", "gate"),
    ("market-data-governance", "llm-agent", "llm_allowed 门（BLOCK→人工复核）", "gate"),
    ("control-evidence", "provider-registry", "resolve_verified（13 项逐条）", "gate"),
    ("evidence-vault", "llm-governance", "RETAINED_VERIFIED 才 llm_eligible", "gate"),
    ("evidence-vault", "research-workpapers", "retained 哈希→authority_tier", "data"),
    ("analysis", "llm-agent", "过门槛候选（P2+·治理预检）", "data"),
    ("llm-agent", "llm-governance", "预检 prepare / 后验 grounding", "gate"),
    ("llm-agent", "role-analyst", "rule_llm_analysis 输入", "data"),
    ("llm-governance", "role-analyst", "prepare_llm_batch(role_llm)", "gate"),
    ("pi-skills", "pi-runtime", "compile_role_system_prompt（lock 全等）", "gate"),
    ("llm-agent", "pi-runtime", "provider_uses_pi 分流", "data"),
    ("role-analyst", "pi-runtime", "role_id=research_desk", "data"),
    ("agent-handoff", "pi-runtime", "role_id=public_handoff（0 信号=0 调用）", "data"),
    ("openclaw-handoff", "agent-handoff", "构造+校验复用", "data"),
    ("llm-agent", "model-operations", "deadline/usage 元数据", "data"),
    ("analysis", "research-cases", "catalyst_path+优先级", "data"),
    ("research-cases", "research-runtime", "案件投影", "data"),
    ("research-workpapers", "research-runtime", "validate_workpaper_gate（fail-closed）", "gate"),
    ("research-runtime", "research-workpapers", "动作→修订事件", "data"),
    ("research-runtime", "research-operations", "validate_runtime_action（角色/限制/容量）", "gate"),
    ("research-operations", "llm-governance", "例外处置人名册校验", "gate"),
    ("review-queue", "research-operations", "SLA/名册复用", "data"),
    ("research-operations", "business-onboarding", "roster 合格真人（阶段2）", "gate"),
    ("provider-registry", "business-onboarding", "≥2 active（阶段3）", "gate"),
    ("evidence-vault", "business-onboarding", "P0/P1 原件覆盖（阶段4）", "gate"),
    ("research-workpapers", "business-onboarding", "archive ready（阶段6）", "gate"),
    ("business-onboarding", "production-pilot", "preflight（阶段闸）", "gate"),
    ("review-queue", "production-pilot", "产能门（0 逾期/0 critical unassigned）", "gate"),
    ("release-controls", "release-audit", "secret_scan/SBOM/许可证 PASS", "release"),
    ("python-environment", "release-audit", "环境 PASS", "release"),
    ("backup-restore", "release-audit", "备份+演练 PASS", "release"),
    ("release-audit", "value-validation", "manifest_id（treatment 冻结锚）", "release"),
    ("research-operations", "value-validation", "真人授权（七重防 mock 之一）", "gate"),
    ("reports", "openclaw-handoff", "最新 JSON→五道闸", "data"),
    ("pipeline", "reports", "⑯ write_outputs", "strip"),
    ("review-queue", "alerts", "SLA/积压触发", "data"),
    ("production-pilot", "alerts", "NO_GO/事件触发", "data"),
    ("review-workbench", "research-runtime", "runtime-action（表单自报 actor→名册校验）", "back"),
    ("review-workbench", "review-queue", "review-action", "back"),
    ("review-workbench", "source-remediation", "remediation 动作", "back"),
    ("source-remediation", "brokerage-architecture", "monkeypatch 转发（脆弱）", "back"),
    ("research-runtime", "brokerage-architecture", "stage_changed 转发（monkeypatch）", "back"),
    ("audit-log", "research-workpapers", "events.jsonl 链", "audit"),
    ("audit-log", "evidence-vault", "vault 事件链", "audit"),
    ("audit-log", "pi-runtime", "calls_*.jsonl 链", "audit"),
    ("audit-log", "provider-registry", "供应商链（坏→active=∅）", "audit"),
    ("audit-log", "backup-restore", "events.jsonl 链", "audit"),
    ("doctor", "production-pilot", "读 latest（约30 项体检）", "strip"),
    ("doctor", "value-validation", "读 latest", "strip"),
    ("model-operations", "run-ops", "预算协议 reserve/record", "strip"),
    ("ext-official", "evidence-vault", "受控 fetch（--live+白名单+每跳校验）", "data"),
    ("ext-vendor", "eastmoney-quote", "可达≠授权", "back"),
    ("pi-runtime", "ext-deepseek", "隔离子进程 stdin", "data"),
    ("llm-agent", "ext-deepseek", "HTTP 兼容路径（rollback_only）", "back"),
]
STAGE_BADGE = {1: "①采集", 2: "②规则", 3: "③原件", 4: "④入库", 5: "⑤signal_llm", 6: "⑥role_llm",
               7: "⑦证据链", 8: "⑧治理汇总", 10: "⑩团队", 11: "⑪复核队列", 12: "⑫model_ops",
               13: "⑬案件", 14: "⑭运行时", 15: "⑮底稿", 16: "⑯输出"}

BADGE_TXT = {"FC": ("fail-closed", "bfc"), "HU": ("人工门", "bhu"), "NET": ("--live 显式", "bnet")}
KIND = {
    "data": ("#1d4ed8", "主数据流", False),
    "gate": ("#b91c1c", "治理/门禁", False),
    "audit": ("#6d28d9", "哈希链审计", False),
    "release": ("#047857", "发布证据链", False),
    "back": ("#9a3412", "回边/转发", True),
    "strip": ("#64748b", "编排/读取", True),
}

def esc(s):
    return html.escape(str(s), quote=True)

def box(m_):
    chips = "".join('<span class="chip {}">{}</span>'.format("c" + b, BADGE_TXT[b][0]) for b in m_["badges"].split("|") if b)
    st = "".join('<span class="stage">{}</span>'.format(esc(STAGE_BADGE[s])) for s in m_["stages"] if s in STAGE_BADGE)
    fns = "".join('<div class="fn"><code>{}</code><span>{}</span></div>'.format(esc(a), esc(b)) for a, b in m_["fns"])
    io = ""
    if m_["io_in"]:
        io += '<div class="io"><b>入</b> {}</div>'.format(esc(m_["io_in"]))
    if m_["io_w"] and m_["io_w"] != "—":
        io += '<div class="io"><b>写</b> {}</div>'.format(esc(m_["io_w"]))
    risk = '<div class="risk">⚠ {}</div>'.format(esc(m_["risk"])) if m_["risk"] else ""
    sym_html = sym_block(m_["file"])
    const_rows = "".join('<div class="cn">• {}</div>'.format(esc(x)) for x in CONSTS.get(m_["id"], []))
    const_html = '<div class="consts"><div class="symh">阈值 / 常量</div>{}</div>'.format(const_rows) if const_rows else ""
    return '''<div class="box" id="{id}">
  <div class="bh"><span class="fid">{fid}</span><span class="loc">{loc} 行</span></div>
  <div class="bt">{title}{chips}</div>
  <div class="br">{role}</div>
  {fns}{io}{st}{const_html}{sym_html}{risk}
</div>'''.format(id=m_["id"], fid=esc(m_["file"]), loc=m_["loc"], title=esc(m_["title"]),
                 chips=chips, role=esc(m_["role"]), fns=fns, io=io,
                 st='<div class="stages">{}</div>'.format(st) if st else "", risk=risk,
                 const_html=const_html, sym_html=sym_html)

def build_html():
    by_col = {}
    for m_ in M:
        by_col.setdefault(m_["col"], []).append(m_)
    strip = sorted(by_col.get("strip", []), key=lambda x: -x["loc"])
    base = sorted(by_col.get("base", []), key=lambda x: x["loc"])

    cols_html = ""
    for cid, cname, cdesc in COLS:
        boxes = "".join(box(m_) for m_ in by_col.get(cid, []))
        cols_html += '''<div class="col" data-col="{cid}">
  <div class="ch" id="{cid}-h"><div class="cht">{cname}</div><div class="chd">{cdesc}</div></div>
  {boxes}
</div>'''.format(cid=cid, cname=esc(cname), cdesc=esc(cdesc), boxes=boxes)

    ext_html = ('<div class="col extcol" data-col="ext"><div class="ch exth" id="ext-h">'
                '<div class="cht">EXTERNAL 外部依赖</div><div class="chd">可达 ≠ 授权 · 一切外发受门禁</div></div>'
                + "".join('''<div class="box ext" id="{id}"><div class="bt ext-t">{t}</div>
      <div class="br">{d}</div><div class="risk">{n}</div></div>'''.format(
                    id=i, t=esc(t), d=esc(d), n=esc(n)) for i, t, d, n in EXT)
                + '</div>')

    strip_html = "".join('''<div class="sbox" id="{id}"><div class="bt">{t}<span class="chip cst">{fid} · {loc}行</span></div>
      <div class="br">{role}</div><div class="fn"><code>—</code><span>{f0}</span></div>{risk}{sym}</div>'''.format(
        id=x["id"], t=esc(x["title"]), fid=esc(x["file"]), loc=x["loc"], role=esc(x["role"]),
        f0=esc(x["fns"][0][1] if x["fns"] else ""),
        risk='<div class="risk">⚠ {}</div>'.format(esc(x["risk"])) if x["risk"] else "",
        sym=sym_block(x["file"])) for x in strip)

    base_html = "".join('''<div class="bbox" id="{id}"><div class="bt">{t}<span class="chip cbse">{fid} · {loc}行</span></div>
      <div class="br">{role}</div><div class="fn"><code>—</code><span>{f0}</span></div>{risk}{sym}</div>'''.format(
        id=x["id"], t=esc(x["title"]), fid=esc(x["file"]), loc=x["loc"], role=esc(x["role"]),
        f0=esc(x["fns"][0][1] if x["fns"] else ""),
        risk='<div class="risk">⚠ {}</div>'.format(esc(x["risk"])) if x["risk"] else "",
        sym=sym_block(x["file"])) for x in base)

    legend_edges = "".join('<span class="lg"><i style="background:{}"></i>{}</span>'.format(v[0], v[1]) for v in KIND.values())
    legend_chips = '<span class="chip cFC">fail-closed</span><span class="chip cHU">人工门</span><span class="chip cNET">--live 显式</span>'
    legend_stages = " ".join('<b>{}</b>'.format(v) for k, v in sorted(STAGE_BADGE.items()))

    edges_js = json.dumps([list(e) for e in E], ensure_ascii=False)

    doc = '''<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>A股投研系统 · 架构总图</title>
<style>
:root{--paper:#f2efe6;--ink:#1c2433;--line:#c9c2b2;--box:#fffdf7;--accent:#8c1d18;
 --c10:#eef3e2;--c20:#e8eef7;--c50:#f7e9e4;--c30:#e9f2f2;--c40:#f1ecf7;--c65:#eaf3ef;--c70:#f5f0e3;
 --mono:"Cascadia Mono","Consolas",monospace;--sans:"Microsoft YaHei","PingFang SC",sans-serif;}
*{box-sizing:border-box;margin:0;padding:0}
body{background:var(--paper);color:var(--ink);font-family:var(--sans);
 background-image:repeating-linear-gradient(0deg,transparent 0 31px,rgba(28,36,51,.045) 31px 32px),
  repeating-linear-gradient(90deg,transparent 0 31px,rgba(28,36,51,.045) 31px 32px);}
#bar{position:fixed;top:0;left:0;right:0;height:44px;background:#141b28;color:#e8e4d8;display:flex;
 align-items:center;gap:14px;padding:0 18px;z-index:99;font-size:13px;box-shadow:0 2px 10px rgba(0,0,0,.35)}
#bar b{letter-spacing:.12em}
#bar .sp{flex:1}
#bar button{background:#2a3550;border:1px solid #46527a;color:#e8e4d8;border-radius:4px;
 padding:4px 12px;cursor:pointer;font-family:var(--sans)}
#bar button:hover{background:#3a4a78}
#zoomlv{min-width:52px;text-align:center;font-family:var(--mono)}
#viewport{padding:60px 24px 30px;overflow:auto}
#canvas{position:relative;width:fit-content;margin:0 auto;transform-origin:0 0}
#sheet{position:relative;border:2px solid #2a3550;outline:1px solid #2a3550;outline-offset:6px;
 background:rgba(255,253,247,.6);padding:18px}
#titleblock{display:flex;gap:0;border:2px solid var(--ink);margin-bottom:16px;background:#fffdf7}
#titleblock>div{padding:10px 16px;border-right:2px solid var(--ink);font-size:12px;line-height:1.55}
#titleblock .tb-name{font-size:22px;font-weight:800;letter-spacing:.08em;min-width:340px;display:flex;
 flex-direction:column;justify-content:center}
#titleblock .tb-name small{font-weight:400;letter-spacing:.25em;color:#5a6478}
#titleblock b{display:block;color:#5a6478;font-size:10px;letter-spacing:.18em;margin-bottom:2px}
#edsheet{width:100%;position:absolute;top:0;left:0;height:100%;pointer-events:none;z-index:5}
.strips{position:relative;z-index:6}
.strip{border:2px solid var(--ink);background:#e5e9f2;margin-bottom:16px;padding:10px}
.strip h3{font-size:12px;letter-spacing:.2em;color:#3d4a66;margin-bottom:8px}
.srow{display:flex;gap:10px;flex-wrap:wrap}
.sbox{flex:1;min-width:300px;background:var(--box);border:1.5px solid var(--ink);padding:8px 10px}
.sbox .bt{font-size:13px;font-weight:800}
.bbox{flex:1;min-width:230px;background:var(--box);border:1.5px solid var(--ink);padding:7px 9px}
.bbox .bt{font-size:12px;font-weight:800}
.chip{display:inline-block;font-size:9px;padding:1px 5px;border-radius:2px;margin-left:6px;
 vertical-align:2px;letter-spacing:.05em;border:1px solid}
.cFC{background:#8c1d18;color:#fff;border-color:#8c1d18}
.cHU{background:#fff;border-color:#8c1d18;color:#8c1d18}
.cNET{background:#fff;border-color:#1d4ed8;color:#1d4ed8}
.cst{background:#e5e9f2;border-color:#3d4a66;color:#3d4a66;font-weight:400}
.cbse{background:#efece2;border-color:#8a8471;color:#5a5648;font-weight:400}
.sbox .br,.bbox .br{font-size:10.5px;color:#42506b;margin:2px 0}
.cols{display:flex;gap:26px;align-items:stretch;position:relative;z-index:6;
 margin-top:120px;margin-bottom:120px}
.col[data-col=c10]{background:var(--c10)}.col[data-col=c20]{background:var(--c20)}
.col[data-col=c50]{background:var(--c50)}.col[data-col=c30]{background:var(--c30)}
.col[data-col=c40]{background:var(--c40)}.col[data-col=c65]{background:var(--c65)}
.col[data-col=c70]{background:var(--c70)}
.col{padding:10px;border:1px dashed var(--line)}
.col{width:352px;flex:0 0 352px;display:flex;flex-direction:column;gap:12px}
.col.extcol{width:236px;flex:0 0 236px}
.exth{border-color:var(--accent)}
.exth .cht{color:var(--accent);font-size:12.5px}
.ch{border:2px solid var(--ink);padding:8px 10px;background:#fffdf7}
.cht{font-size:14px;font-weight:800;letter-spacing:.06em}
.chd{font-size:10.5px;color:#5a6478;margin-top:2px}
.box{background:var(--box);border:1.5px solid var(--ink);padding:8px 10px;position:relative;
 box-shadow:2px 2px 0 rgba(28,36,51,.18)}
.box.ext{border-style:dashed;border-color:var(--accent);background:#fdf3f1}
.bh{display:flex;justify-content:space-between;font-family:var(--mono);font-size:10px;color:#5a6478}
.bt{font-size:13.5px;font-weight:800;margin:1px 0}
.br{font-size:10.5px;color:#42506b;margin-bottom:4px}
.fn{display:flex;gap:6px;font-size:10.5px;line-height:1.45;margin:2px 0;align-items:baseline}
.fn code{font-family:var(--mono);font-size:10px;color:#0f2f6b;white-space:nowrap;max-width:46%;
 overflow:hidden;text-overflow:ellipsis;flex:0 0 auto}
.fn span{color:#2c3850}
.io{font-size:10px;color:#5a6478;margin-top:2px}
.io b{color:#8c1d18;font-family:var(--mono);font-size:9px;margin-right:4px}
.risk{font-size:10px;color:#8c1d18;margin-top:3px;border-top:1px dashed #d8b4b0;padding-top:3px}
.sym{margin-top:4px;border-top:1px dashed var(--line);padding-top:3px}
.symh{font-size:8.5px;color:#5a6478;letter-spacing:.12em;margin-bottom:2px;font-family:var(--mono)}
.sy{font-family:var(--mono);font-size:9.5px;color:#0f2f6b;line-height:1.55;display:flex;gap:5px}
.sy i{color:#8a8471;font-style:normal;font-size:8.5px;flex:0 0 auto;width:30px}
.sy em{color:#8a8471;font-style:normal;margin-left:auto;font-size:8.5px}
.sy.more{color:#8a8471;font-size:9px}
.symlist{display:flex;flex-direction:column}
.sym.many .symlist{display:block;columns:2;column-gap:12px}
.sym.many.c3 .symlist{columns:3}
.sym.many .sy{break-inside:avoid}
.consts{margin-top:4px;border-top:1px solid #d8b4b0;padding-top:3px}
.cn{font-size:9.5px;color:#7c2d12;line-height:1.55}
.stages{margin-top:3px}
.stage{display:inline-block;font-size:9px;background:#141b28;color:#e8e4d8;border-radius:2px;
 padding:1px 5px;margin:1px 3px 0 0;font-family:var(--mono)}
.ext-t{color:var(--accent)}
#legend{border:2px solid var(--ink);background:#fffdf7;margin-top:16px;padding:10px 14px;
 display:flex;flex-wrap:wrap;gap:10px 22px;align-items:center;font-size:11.5px;position:relative;z-index:6}
#legend .lg i{display:inline-block;width:26px;height:0;border-top:3px solid;vertical-align:middle;
 margin-right:6px}
#legend .stg{font-family:var(--mono);font-size:10.5px;color:#3d4a66}
#legend .cap{font-size:10px;color:#5a6478;width:100%}
</style></head><body>
<div id="bar"><b>A股投研系统 · 架构总图</b><span style="opacity:.7">64 模块块 · 40,162 行 · 1,440 个符号全量细节</span>
 <span class="sp"></span>
 <button onclick="zm(-0.15)">−</button><span id="zoomlv">100%</span><button onclick="zm(0.15)">＋</button>
 <button onclick="fitw()">适应宽度</button><button onclick="zset(1)">1:1</button></div>
<div id="viewport"><div id="canvas"><div id="sheet">
<div id="titleblock">
 <div class="tb-name">系统架构总图<small>MASTER ARCHITECTURE DIAGRAM</small></div>
 <div><b>项目</b>A股官方证据驱动的<br>内部研究运营平台</div>
 <div><b>图纸依据</b>源码全量通读 v31/v32<br>docs/architecture/ 64 块卡 · AST 全符号</div>
 <div><b>制图</b>Zcode · 2026-09-06<br>比例 1:1 · 可缩放</div>
 <div><b>铁律</b>规则管边界 · LLM管推理<br>人工管受保护决定</div>
</div>
<svg id="edsheet"></svg>
<div class="strips"><div class="strip"><h3>80-ORCHESTRATION · 编排与遥测（横向贯穿全部层）</h3><div class="srow">{STRIP}</div></div></div>
<div class="cols">{EXT}{COLS}</div>
<div class="strips"><div class="strip"><h3>00-FOUNDATION · 地基（被全部 64 块依赖）</h3><div class="srow">{BASE}</div></div></div>
<div id="legend">
 <span style="font-weight:800;letter-spacing:.15em">图例</span>{LEGE}
 <span style="width:100%"></span>{LEGC} <span class="stg">阶段徽标（agent-run 顺序）：</span><span class="stg">{LEGS}</span>
 <span class="cap">读法：列＝层，自左向右为主数据流；红＝治理门禁（fail-closed 处阻断），紫＝哈希链审计写入，绿＝v31/32 发布证据链，橙虚＝回边/转发（含待收敛 monkeypatch），灰虚＝编排/读取。锁标=代码级 fail-closed；描红“人工门”=须名册内真实人类；蓝“--live”=联网/计费须显式标志。⚠ 为该块已知风险（详见 docs/architecture/ 同名卡）。</span>
</div>
</div></div></div>
<script>
const EDGES={EDGES};
const KIND={KINDJS};
const svg=document.getElementById('edsheet');
function anchor(id,side){const el=document.getElementById(id);if(!el)return null;
 const c=document.getElementById('sheet').getBoundingClientRect(),r=el.getBoundingClientRect();
 const x=side==='r'?r.right-c.left:r.left-c.left,y=r.top-c.top+r.height/2;
 return side==='t'?{x:r.left-c.left+r.width/2,y:r.top-c.top}:
        side==='b'?{x:r.left-c.left+r.width/2,y:r.bottom-c.top}:{x,y};}
const STRIP_IDS=new Set({STRIP_IDS_JS});const BASE_IDS=new Set({BASE_IDS_JS});
function draw(){const sheet=document.getElementById('sheet');
 svg.setAttribute('width',sheet.scrollWidth);svg.setAttribute('height',sheet.scrollHeight);
 const colsEl=document.querySelector('.cols');
 const fnd=document.querySelectorAll('.strips')[1];
 const topCorr=colsEl.offsetTop-100, botCorr=colsEl.offsetTop+colsEl.offsetHeight+20;
 let out='<defs>'+Object.entries(KIND).map(([k,v])=>'<marker id="ar-'+k+'" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" markerHeight="7" orient="auto-start-reverse"><path d="M0 0L10 5L0 10z" fill="'+v[0]+'"/></marker>').join('')+'</defs>';
 let nTop=0,nBot=0;
 function vhv(x1,y1,x2,y2,ym){const r=12,s1=y1<ym?1:-1,s2=ym<y2?1:-1,sH=x2<x1?-1:1;
  return 'M'+x1+' '+y1+' L'+x1+' '+(ym-s1*r)+' Q'+x1+' '+ym+' '+(x1+sH*r)+' '+ym+
   ' L'+(x2-sH*r)+' '+ym+' Q'+x2+' '+ym+' '+x2+' '+(ym+s2*r)+' L'+x2+' '+y2;}
 for(const [f,t,label,kind] of EDGES){
  const col=KIND[kind][0],dash=KIND[kind][2]?'stroke-dasharray="7 5"':'';
  const fc=colOf(f),tc=colOf(t);
  let a,b,d,mid;
  if(STRIP_IDS.has(f)){           // 顶部条 → 列内目标：经顶部走廊
   a=anchor(f,'b');b=anchor(t,'t');
   if(!a||!b)continue;
   const ym=topCorr+16+(nTop++%5)*17;d=vhv(a.x,a.y,b.x,b.y,ym);mid={x:(a.x+b.x)/2,y:ym-6};
  }else if(STRIP_IDS.has(t)){     // 列内 → 顶部条(brokerage/run-ops)：经顶部走廊
   a=anchor(f,'t');b=anchor(t,'b');
   if(!a||!b)continue;
   const ym=topCorr+16+(nTop++%5)*17;d=vhv(a.x,a.y,b.x,b.y,ym);mid={x:(a.x+b.x)/2,y:ym-6};
  }else if(BASE_IDS.has(f)){      // 地基条(audit-log) → 列内目标：经底部走廊
   a=anchor(f,'t');b=anchor(t,'b');
   if(!a||!b)continue;
   const ym=botCorr+(nBot++%5)*17;d=vhv(a.x,a.y,b.x,b.y,ym);mid={x:(a.x+b.x)/2,y:ym-6};
  }else if(fc!==null&&tc!==null&&tc===fc){
   a=anchor(f,'r');b=anchor(t,'r');
   if(!a||!b)continue;
   const bx=Math.max(a.x,b.x)+80;
   d='M'+a.x+' '+a.y+' C '+bx+' '+a.y+', '+bx+' '+b.y+', '+b.x+' '+b.y;
   mid={x:bx,y:(a.y+b.y)/2};
  }else if(fc!==null&&tc!==null&&tc<fc){   // 回边：底部走廊
   a=anchor(f,'b');b=anchor(t,'b');
   if(!a||!b)continue;
   const ym=botCorr+(nBot++%5)*17;d=vhv(a.x,a.y,b.x,b.y,ym);mid={x:(a.x+b.x)/2,y:ym-6};
  }else if(fc!==null&&tc!==null&&tc-fc>=2){ // 跨层前向：顶部走廊
   a=anchor(f,'t');b=anchor(t,'t');
   if(!a||!b)continue;
   const ym=topCorr+16+(nTop++%5)*17;d=vhv(a.x,a.y,b.x,b.y,ym);mid={x:(a.x+b.x)/2,y:ym-6};
  }else{
   a=anchor(f,'r');b=anchor(t,'l');
   if(!a||!b)continue;
   const dx=Math.max(56,(b.x-a.x)/2);
   d='M'+a.x+' '+a.y+' C '+(a.x+dx)+' '+a.y+', '+(b.x-dx)+' '+b.y+', '+b.x+' '+b.y;
   mid={x:(a.x+b.x)/2,y:(a.y+b.y)/2-6};
  }
  out+='<path d="'+d+'" fill="none" stroke="'+col+'" stroke-width="'+(kind==='data'?2.2:1.8)+'" '+dash+' marker-end="url(#ar-'+kind+')" opacity="0.92"/>';
  if(label){out+='<text x="'+mid.x+'" y="'+mid.y+'" font-size="10" text-anchor="middle" font-family="Microsoft YaHei" fill="'+col+'" stroke="#fffdf7" stroke-width="4" paint-order="stroke" font-weight="700">'+label+'</text>';}}
 svg.innerHTML=out;}
function colOf(id){const el=document.getElementById(id);if(!el)return null;let p=el;
 while(p&&p!==document.body){if(p.dataset&&p.dataset.col)return colIndex(p.dataset.col);p=p.parentElement;}
 if(id.endsWith('-h'))return colIndex(id.slice(0,-2));return null;}
function colIndex(c){const order=['ext','c10','c20','c50','c30','c40','c65','c70'];return order.indexOf(c);}
let Z=1;function zset(z){Z=Math.min(2.5,Math.max(0.25,z));document.getElementById('canvas').style.transform='scale('+Z+')';
 document.getElementById('zoomlv').textContent=Math.round(Z*100)+'%';
 document.getElementById('canvas').style.marginBottom=(Z-1)*document.getElementById('sheet').offsetHeight+'px';}
function zm(d){zset(Z+d)}function fitw(){zset(Math.min(1,(document.getElementById('viewport').clientWidth-40)/document.getElementById('sheet').offsetWidth))}
window.addEventListener('resize',()=>{draw()});
window.addEventListener('load',()=>{draw();fitw();});setTimeout(()=>{draw();},400);
</script></body></html>'''
    kindjs = json.dumps({k: list(v) for k, v in KIND.items()})
    strip_ids = json.dumps(sorted(x["id"] for x in strip))
    base_ids = json.dumps(sorted(x["id"] for x in base))
    doc = doc.replace("{EDGES}", edges_js).replace("{KINDJS}", kindjs)
    doc = doc.replace("{STRIP_IDS_JS}", strip_ids).replace("{BASE_IDS_JS}", base_ids)
    doc = doc.replace("{STRIP}", strip_html).replace("{BASE}", base_html)
    doc = doc.replace("{EXT}", ext_html).replace("{COLS}", cols_html)
    doc = doc.replace("{LEGE}", legend_edges).replace("{LEGC}", legend_chips).replace("{LEGS}", legend_stages)
    OUT.write_text(doc, encoding="utf-8")
    print("written:", OUT, OUT.stat().st_size, "bytes")

if __name__ == "__main__":
    build_html()
