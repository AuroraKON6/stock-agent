# ADR：Kimi 数据传输宿主边界

日期：2026-10-12。状态：**已建议，待负责人确认**（与 K01 权利矩阵一并审阅）。

关联：[K01 清单](KIMI_K01_INVENTORY_20261012.md)、[Kimi 接入计划 §5.1/§8-K02](../GLM_KIMI_INTEGRATION_PLAN_20261012.md)。

## 决策

1. **默认宿主边界是项目内小型 Python 受控客户端**（`stock_agent/integrations/kimi/`），
   零第三方依赖（标准库 urllib/json），依赖注入 transport，可完全离线测试。
2. **首版不引入 MCP 传输。** 12 个常驻 MCP server 进程、手写 JSON-RPC 循环与本项目
   的单任务受控采集模型不匹配（计划 F05）。若未来需要 MCP，只评估官方 Python SDK
   且版本按本项目环境锁定审核（K07）。
3. **现有 `kimi_gateway.py` 仅作为"待确认传输"的协议参考，不在生产路径中 import。**
   其认证方式（桌面密钥自动发现）与 base_url 处理（env 任意覆盖）与本项目边界冲突（F02），
   且来自桌面插件逆向、无服务方支持承诺。
4. **不引入 agent-gw SDK 整包。** README 所述公开 wheel 未完成来源真实性、许可证、
   传递依赖与 SBOM 审核（F04）；为一个 HTTP 端点引入整包 SDK 不成比例。
5. **认证只走显式凭据引用**：配置中的 `credential_ref` 指向负责人明确提供的凭据来源，
   缺失即阻断（结果码 `credential_missing`），不回退环境变量、不扫描桌面/浏览器/共享文件。
   凭据值不进命令行、提示词、日志或错误文本。
6. **出站目标固定白名单**：默认唯一 origin `https://agent-gw.kimi.com`（精确 scheme/host/port），
   路径 `/coding/v1/tools`；HTTPS-only、拒绝任何重定向（跨域或降级一律 `destination_denied`），
   Authorization 头不出现在非白名单 origin 的请求上。
7. **受支持连接方式先于账号可用性**：在服务方确认逆向网关可用于外部自动化之前，
   transport 保持 disabled；离线开发不受影响，获准资料可从官方客户端人工导出。

## 备选方案与拒绝理由

| 备选 | 拒绝理由 |
| --- | --- |
| 复制外部 mcp.json（12 进程常驻） | 裸 python 启动、共享凭据、共享故障域（F04/F06）；与 TaskStore 单任务模型冲突 |
| 扩展手写 MCP server | JSON-RPC 错误层级、生命周期协商均有缺陷（F05）；官方 SDK 存在时无理由手写 |
| agent-gw SDK | 未经许可/来源/SBOM 审核；运行时安装风险；单 HTTP 调用不需要整包 |
| 直接 import kimi_gateway | 密钥自动发现、任意 base_url、落盘函数不可控（F01/F02）；修改外部包被禁止 |
| Pi/主模型侧接入 | 违反"模型不做数据决定"边界；数据线归宿主调度（计划 §5.1） |

## 后果

- 正面：F01-F06 的核心风险在客户端边界内解决；离线测试可覆盖 26 项验收矩阵中除 T06/T25 外的全部
  客户端侧场景；回滚只需停用集成配置，不触碰其他源。
- 负面：放弃 MCP 生态即插即用；后续接 MCP 需重新走 K07 审核。逆向网关协议变化时需人工比对
  （契约快照哈希会标 `CONTRACT_CHANGED`，fail-closed）。
- 中性：官方客户端人工导出路径保留为替代供给方式（计划 §F02 要求）。

## 验证与生效

- 本 ADR 生效以负责人在 K01 权利矩阵上的真实决定为准；GLM 不代填。
- 技术验证随 K02 离线测试交付（tests/test_kimi_client_v1.py 等），live 验证归 K06。
