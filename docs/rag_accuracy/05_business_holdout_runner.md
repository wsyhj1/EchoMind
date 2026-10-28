# 业务留出集执行前检查

复验日期：2026-10-25（Asia/Shanghai）。入口为evaluation/business_holdout_runner.py，默认只做preflight。它读取独立子代理审核的v2集；原v1草稿、来源、v2审核集、审核报告及manifest均未修改。

## 本地命令

在项目根目录用PowerShell运行：

```powershell
$env:PYTHONUTF8 = '1'
.venv\Scripts\python.exe -m evaluation.business_holdout_runner
```

默认输出docs/rag_accuracy/05_business_holdout_dry_run.json。可用--output指定另一份验收JSON；程序拒绝覆盖数据、来源、审核文件或原草稿。

运行本地测试：

```powershell
New-Item -ItemType Directory -Force '.runtime\tmp' | Out-Null
$env:TEMP = (Resolve-Path '.runtime\tmp').Path
$env:TMP = $env:TEMP
.venv\Scripts\python.exe -m unittest evaluation.tests.test_business_holdout_runner -v
```

2026-10-25实际执行：14项测试全部通过，默认dry-run通过。还实际运行以下两个不支持模式，均以退出码1拒绝，输出文件未被错误模式重写：

```powershell
.venv\Scripts\python.exe -m evaluation.business_holdout_runner --mode span_single_query
.venv\Scripts\python.exe -m evaluation.business_holdout_runner --mode document_only
```

第一个缺少actual_message_history，不能运行BH029/BH030；第二个缺少source_span_mapping，不能把整个文档ID命中当作来源跨度命中。--execute尚未实现，会在调用模型或检索前以退出码2明确拒绝。

## 本次验收结果

| 项目 | 结果 |
|---|---:|
| 审核集问题 | 32 |
| 来源文档 | 12 |
| 通用分段生成的父片段 | 14 |
| 计分Gold原文跨度 | 33 |
| 必须保留实际历史的题 | 2 |
| 新模型调用 | 0 |
| 新检索调用 | 0 |
| 答案准确率 | null，未执行 |
| 实测召回率/完整证据率 | null，未执行 |

分段只读取来源文本及身份，使用确定性字符窗口，默认max_chars=1000、overlap=200。偏移为Python Unicode字符位置，左闭右开，不做大小写或空白归一化。来源跨度可由多个实际选中片段的区间并集覆盖；重叠区间不重复计数。只选BH001长文开头片段只能覆盖两个Gold跨度之一，不能因同doc_id而判完整。

runtime_request只包含question_id、user_id、knowledge_base_id、as_of、messages。history按原user/assistant顺序逐字保留，最后追加当前用户问题；不从Gold补代词主体，不注入gold_sources、gold_answer、expected_claims或review_evidence。澄清/拒答题的review_evidence仅用于离线来源校验，不创建召回分母。

验收检查了manifest、独立审核与原草稿之间的指纹绑定，逐来源完整正文hash、逐条quote/offset/span_id和租户/库范围，未知片段及跨权限片段直接拒绝。实际生成的JSON中所有fingerprints已与当前磁盘文件逐项重新核对。

## 本次输入指纹

| 输入 | SHA256 |
|---|---|
| v2审核数据 | 0e0d125ff7415bbb0aebfdba49a82288ad6f5eb9f1b9141917e932a40aa53d25 |
| v2 manifest | 7605508dad77b87b4efc05ddfb391961b05940463383053430303a3e424373cc |
| v1来源 | 84bce6e6bd9bde6d710bea5dbeb21ce9a72d3f84efab9be988cccd6f7458cf97 |
| 独立审核JSON | f52e65c3b170a263df44572d8ce44b5926034a965910296526e297459c7c33c6 |
| 保留的v1草稿 | ffaf421bc01a8302d0326506aeda095b4166a8665edaf2faff2237d3b34cc791 |
| runner源码 | 1e096614ec4d9e30e763c67af6042bedc876a89c0cb91de0d25b92349466a91e |

这份JSON证明执行准备条件和映射检查通过，没有摄入索引、调用图谱抽取、embedding、重排或生成32题答案。所有授权来源都能映射Gold跨度只是结构可达性，不能当作真实检索或回答成绩。后续真实adapter仍需记录真正发给模型的消息和实际可见选中片段，再调用离线跨度评分函数；本轮不提供或宣称完成该live adapter。
