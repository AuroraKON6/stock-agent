# [00-02] text.py — 文本归一原语

| 层 | 行数 | 上游依赖 | 被依赖 |
|---|---|---|---|
| 00-foundation | 28 | 无 | analysis（关键词匹配、signal_id）、sources/rss、evidence_chain、source_remediation |

## 职责
最小文本工具集，是关键词匹配与信号身份哈希的地基。

## 关键构件
- `normalize_text()`（L11）— 去空白/连字符/中英文标点 + 小写。**意味着系统的关键词匹配是"去标点后的子串包含"，无分词**。
- `compact_space()`（L15）— 空白折叠，RSS 标题清洗用。
- `unique_keep_order()`（L19）— 保序去重，被 analysis/evidence_chain/source_remediation 共用。

## 输入 → 输出
纯函数，无 IO。

## 门禁 / 安全
无。

## 备注 / 风险
归一化规则决定了 P0/P1 风险词的召回与误报特性；改一条标点规则会同时改变全部历史 signal_id 的语义（signal_id 含归一化后的标题哈希），属隐性高杠杆文件。
