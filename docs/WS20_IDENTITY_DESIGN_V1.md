# WS20 身份、授权与职责分离：架构设计 v1

> 日期：2026-09-17（Asia/Shanghai）
> 计划依据：`docs/MASTER_PROJECT_PLAN_V31.md` WS20；战略 `docs/PROJECT_GM_STRATEGY_V30.md` §8
> 状态：**DESIGN**（设计文档，不含实现；实现前置条件见 §7）
> 对应 P0 backlog：第 8 项"身份绑定设计"

## 1. 问题陈述

当前所有受保护动作的 `--actor` 是自由字符串。`research_operations` 校验"actor 在名册且角色匹配、
principal_type=human、角色资格、容量、限制名单"（`research_operations.py` L539-560、L171-250），
本地工作台的 actor/role 是表单自报字段（`review_workbench.py` L1198-1235）。这套控制能挡住
**粗心的冒用**和**自动化账号**（`AUTOMATED_ACTOR_PREFIXES` 硬排除），挡不住**故意的身份冒充**：
任何人敲 `--actor 张三 --actor-role quality_reviewer` 即可通过校验（若名册里真有张三）。

主计划验收标准（WS20）：不能通过任意 `--actor` 冒充他人；身份撤销立即影响后续动作。

## 2. 设计原则

1. **身份是外部事实**：系统不注册身份，只绑定外部身份提供者（Windows 账号 / 企业 IdP）的标识。
2. **证明先于授权**：先证明"操作者 = 名册中的某人"，再走现有角色/资格/容量校验。
3. **签名绑定内容**：批准签名绑定被批对象的哈希（案件版本、fingerprint），防"批 A 用 B"。
4. **不新增秘密保管面**：不保存口令/token/私钥；单机阶段用 Windows DPAPI 与文件 ACL。
5. **service/model 永远不持身份**：`AUTOMATED_ACTOR_PREFIXES` 保留为第二道防线。
6. **最小改动路径**：单机阶段不引入新框架，用 Windows 原生能力。

## 3. 阶段 1：单机试点 —— Windows SID 绑定（对应 G2 前）

### 3.1 绑定模型

```text
名册 principal 扩展字段（config/research_roster.local.json）:
  "identity": {
    "provider": "windows_local",
    "subject": "S-1-5-21-...",          # Windows SID
    "bound_at": "ISO-8601",
    "bound_evidence_ref": "control:<id>" # 绑定事件的控制证据引用
  }
```

- 名册变更（含 SID 绑定）本身要求 `research_coordinator` 角色的**已绑定**人工执行——鸡生蛋问题用
  **引导绑定**解决：首位绑定人由用户在受控会话中手工确认（一次性、留审计事件），之后全部走绑定链。
- 运行时校验（新增 `stock_agent/identity.py`，约 200 行）：
  1. 取当前进程 Windows token SID（`ctypes` + `GetTokenInformation`，或 `whoami /user` 子进程，
     无新依赖）。
  2. 与名册 `identity.subject` 精确匹配；不匹配 → 动作拒绝（fail closed）。
  3. 校验通过后生成 `operation_id = HMAC(session_nonce, actor_sid | action | object_hash | timestamp)`。

### 3.2 操作签名

每次受保护动作（`record_runtime_action` 的 quality/compliance/publication 门禁、pilot approval、
provider review、governance exception 处置）在事件中追加：

```json
{
  "actor_identity": {"provider": "windows_local", "subject": "S-1-5-21-..."},
  "operation_signature": "hmac-sha256:...",
  "signed_object_sha256": "被批对象的 canonical hash（workpaper revision / fingerprint / provider 投影）",
  "signed_at": "ISO-8601",
  "policy_fingerprint": "当前名册+策略指纹"
}
```

HMAC 密钥由 DPAPI 保护的本机密钥文件承载（`data/identity/` 下，ACL 限当前用户；文件不入 Git，
加入 `.gitignore`）。签名验证只需本机密钥——单机阶段它证明的是"同一台机器上同一 Windows 账号
在同一策略状态下批准"，不防本机管理员，这与单机威胁模型一致。

### 3.3 CLI/工作台改造点

| 位置 | 现状 | 改造 |
|---|---|---|
| `cli.py` 各 `--actor` 参数 | 自由字符串 | 保留参数但必须与进程 SID 匹配的名册条目一致；新增 `--identity-proof` 可选覆盖（用于服务化场景，阶段 2） |
| `review_workbench.py` 表单 | 自报 actor/role | 会话建立时校验一次浏览器宿主进程 SID（本机单用户模型下即当前登录者），写入会话；后续动作复用 |
| `research_operations.authorize_runtime_action` | 角色校验 | 前置插入 `verify_identity()`；deny 理由新增 `identity_mismatch` |
| `production_pilot.record_pilot_approval` | 角色匹配 | 追加 `signed_object_sha256 = candidate_fingerprint`（现有机制已绑定指纹，签名补"谁"的证明） |

### 3.4 撤销与离岗（WS20-08）

- 名册 principal 置 `active: false` → 所有后续动作即时拒绝（现有校验已支持，验证补测试即可）。
- SID 解绑事件追加控制证据 `identity_unbound`，此后该 SID 的任何操作按未注册拒绝。
- 紧急停用：`research_restrictions.local.json` 加 SID 级条目（复用现有 restrictions 机制）。

## 4. 阶段 2：团队部署 —— OIDC/企业 IdP（G2，2027-Q1）

- `identity.provider` 扩展 `oidc`；`subject` = IdP 的 `sub` claim。
- Workbench 增加 OIDC Authorization Code + PKCE 登录；会话 = 短时 JWT（默认 30 分钟，含 `sid`、
  `sub`、角色组映射）；MFA 由 IdP 承担（要求 ACr AMR 强认证策略用于受保护动作）。
- CLI 服务化场景用 client credentials；机器身份单独名册段，`principal_type=service` 永不可 approver。
- 角色映射：IdP group → 名册 role 的映射表进 `research_operations`，映射变更走 Change Request
  （主计划 §10.4）。

## 5. 阶段 3：受保护批准的签名 challenge（G2-G3）

```text
批准 = 签名（批准人身份 + 对象哈希 + 批准门 + 有效期）+ 独立会话证据
```

- 批准前展示 challenge 明文（对象哈希 + 门名 + 影响摘要），批准人显式确认；确认即签名。
- pilot 六类批准在现有 `fingerprint` 绑定（`production_pilot.py` L1161）之上叠加身份签名，形成
  "谁、批了什么版本、何时"三要素的不可抵赖记录。
- approval record 同步登记外部档案 URI + SHA-256（复用 `control_evidence` 机制，类别
  `approval_support`）。

## 6. 职责分离冲突矩阵（WS20-01 收拢）

现有四处硬校验语义相同但分散实现；收拢为一张声明式矩阵（`research_operations` 读取）：

| 动作 A | 动作 B | 同一人 | 现有实现位置 |
|---|---|---|---|
| 供应商尽调提交 | 供应商评审 | 禁止 | provider_registry L189-190 |
| 供应商评审（角色间） | — | 彼此不同人 | provider_registry L191-193 |
| 证据登记 | 证据档案复核 | 禁止 | evidence_vault L451-452、control_evidence L188-189 |
| 档案激活 | 档案复核（任一） | 禁止 | evidence_vault L494-495 |
| lead_analyst / quality / compliance / publication 指派 | 彼此 | 四者互异 | research_runtime L1156 |
| pilot 六类批准门 | 彼此 | 六人互异 | production_pilot 审批门 |
| 主分析 | 质量审核 / 合规放行 | 禁止 | 名册角色 + runtime 指派校验 |
| 发布准备 | 最终激活 | 必须授权发布角色本人 | production_pilot L214-216 |

矩阵驱动：校验函数统一为 `check_conflict(action_a_actor, action_b_actor, matrix_entry)`；
矩阵本体是配置，变更走 Change Request。**角色名称可因机构而异，冲突规则不可弱化**（战略 §8.2）。

## 7. 实现前置条件与切分

| 前置 | 说明 |
|---|---|
| 首个 Git 提交 | 身份代码是新模块，必须有可回滚基线 |
| G0.5 价值门通过 | 主计划关键路径：机构化投资（含身份）在价值验证之后 |
| 真实名册首批人员 | 绑定需要真实人；在此之前只交付代码+测试+模拟 SID 的单元测试 |

工程切分（实现时）：
1. `stock_agent/identity.py`：SID 获取 + 名册匹配 + HMAC 签名（+ 单元测试，~1 天）
2. 名册 schema 扩展 + `research_operations` 前置校验（+ 测试，~0.5 天）
3. CLI/工作台接线（+ 测试，~1 天）
4. 冲突矩阵收拢（重构四处重复校验，+ 测试，~1 天）

## 8. 明确不做

- 不保存口令、token、私钥；不实现自研密码学（仅 HMAC-SHA256 + DPAPI）。
- 不在 G2 前引入 OIDC 依赖；不改造 `data/` 历史事件（旧事件无签名字段属预期，读取时容忍）。
- 不让任何 model/service principal 获得 identity 绑定资格。
