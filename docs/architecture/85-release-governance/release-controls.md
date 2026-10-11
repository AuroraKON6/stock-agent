# [85-01] release_controls.py — G0 发布控制物生成（v31 新增）

| 层 | 行数 | 上游依赖 | 被依赖 |
|---|---|---|---|
| 85-release-governance | 852 | config、audit_log | release_audit（PASS 门禁）、value_validation（间接）、cli（release-controls）、dashboard |

## 职责
离线生成三类发布控制物：secret scan、CycloneDX SBOM、许可证清单。核心纪律：**扫描结果永不写出密钥值或上下文片段**。

## 关键构件
- 候选圈定 `discover_release_candidate_files()`（L242）— 根文件 + stock_agent/tests/scripts/docs + .pi 与 pi_runtime 方法包 + `config/*.json` 但**排除 `*.local.*`**；`build_release_candidate_identity()`（L265）只产 path+sha256 指纹，不保留内容。
- **Secret scan** `build_secret_scan()`（L294）— 扫候选 + 本地敏感文件（`config/*.local.json`、`.env*`、`pi_runtime/agent_home/auth.json`）；findings 只记 pattern_id/path/line/**match_sha256/match_length**，显式置 `value_recorded: False, snippet_recorded: False`（L324-334）——值与片段任何情况不落盘不输出；占位符（test/fake/changeme）归 PLACEHOLDER，其余 BLOCKING。
- SBOM（L471）— 汇总 `parse_python_lock()`（L364，含 --hash）、`parse_npm_lock()`（L390）、`parse_pi_skills()`（L434）。
- 许可证（L556）— ALLOWED/REVIEW_REQUIRED/DENIED 三级；**REVIEW_REQUIRED 保留为阻断项，PASS 不是法律批准**（Agent 不得代替法务批准 copyleft/复合/未知许可证）。
- 门禁 — 三者全 PASS 才 PASS；`--fetch-python-metadata` 是唯一显式联网（仅 pypi.org 精确版本元数据，2MB 上限）；`--require-pass` 不达标 exit 2。

## 输出
`reports/release/{secret_scan_latest.json, sbom.cdx.json, license_report_latest.json, python_registry_metadata.json, latest.json/md}`。

## 备注 / 风险
与 AGENTS.md 的红线一致：只有显式 `--fetch-python-metadata` 才允许访问 pypi.org。
