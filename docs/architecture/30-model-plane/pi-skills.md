# [30-06] pi_skills.py — 研究方法供应链（策略 v26.0）

| 层 | 行数 | 上游依赖 | 被依赖 |
|---|---|---|---|
| 30-model-plane | 448 | audit_log | pi_runtime（编译提示词）、doctor、production_pilot、dashboard |

## 职责
"审核注册表 + 内容锁 + 按角色编译"的方法接入：批准的 SKILL.md 被当作版本化方法文本，校验出处/权限/哈希后编译进角色提示词。生产 Pi 动态 skill 加载恒关闭。

## 关键构件
- `ALLOWED_ROLES`（L25-32）— 6 个白名单角色：signal_analyst、research_desk、case_analyst、public_handoff、review_optimizer、runtime_uat。
- `DENIED_PERMISSIONS`（L33-42）— network/shell/filesystem_write/trading/approval/publication/assignment/config_mutation **九项必须逐项显式 false**；白名单外权限为 true 即拒绝。
- `_load_and_validate_registry()`（L291）— 硬校验：policy_version、`runtime_mode=compiled_prompt_only`、dynamic_skill_loading=false、production_tools_enabled=false；skill id 命名正则；许可证白名单（Project-Owned/Apache-2.0/MIT）；改编 skill 必须钉死 repo URL + 40 位 commit + adaptation 说明；路径限 `.pi/skills/<id>/SKILL.md` 无穿越；单文件 ≤12000 字节；正文扫 `PROHIBITED_TEXT_MARKERS`（L46-68：```、curl、eval(、subprocess、place_order、提示词覆盖短语等）。
- **内容锁** `build_pi_skill_lock()`（L85）— 按角色生成 bundle（bytes+sha256+skill_ids）；`pi_skill_supply_chain_status()`（L138）重算并与 `config/pi_skills.lock.json` 全等比对，**不一致 → 整个供应链 BLOCKED** → pi_runtime_status 失败、所有 Pi 调用被阻断（内容寻址防篡改闭环）。
- `compile_role_system_prompt()`（L240）— 以 `<reviewed_method_bundle>` XML 包裹追加到固定 system prompt 尾部，声明"方法从属于系统契约、不授予任何工具/数据/交易/发布权限"；bundle ≤48000 字节。

## 输入 → 输出
输入：`config/pi_skills.json` + `.pi/skills/*/SKILL.md`。输出：编译后 system prompt + `reports/pi_agent/skills/latest.*`；lock 重建需 CLI `pi-skills-lock --write` 显式授权。

## 备注 / 风险
当前 11 个批准方法（5 自有 + 6 改编），bundle sha256 `85289fa6…`；任何方法/来源/权限/角色映射/lock 变化都会使旧生产候选指纹失效（production_pilot 关键输入）。
