# 真实知识库图谱引用语义审核（代理）

审查日期：2026-10-23，Asia/Shanghai。审查者：Codex 子代理 `/root/graph_review`。这是代理审核，不是人工签核。

范围限定为 PostgreSQL public 中用户 1、「华为杯」知识库当前已发布 v2 图谱；不修改其他用户、源文档、个人设置或服务开关。本报告中的判断只审查引用是否足以支持已记录谓词，不依据外部常识替引用补事实。

## 结论

3 个文件已完成工程索引状态恢复，但 **READY 不代表关系语义经过审核**。全部 14 条关系/14 份引用中，6 条有支持，7 条不受引用支持，1 条方向不确定。应在启用前拒绝或隔离后 8 条，不能把“引用确实存在”当作“引用支持关系”。

完整原文与引用留在本地忽略目录 `.runtime/rag_accuracy/phase4/debug/public_graph_quote_review_source_v1.json`，避免把完整用户文档复制进仓库报告。

| relation_id | 谓词及端点 | 代理判断 | 依据 |
|---|---|---|---|
| 0cec1ca2-701f-5c5f-91d0-fb26df976291 | 当前可执行系统 USES 约束语料工具 | 支持 | 原文明示系统由这些模块组成，可以支持系统使用自身组件。 |
| 0d4a0b90-de78-515b-b288-c4819c0440fc | 当前可执行系统 USES 导出模块 | 支持 | 同上，属于明确当前组件清单。 |
| 455fa5e0-8b02-54dc-b330-380ca0d55915 | ASR DEPENDS_ON 模型 | 不支持 | 原文为“使后续ASR、模型和图模块独立开发”，并列独立开发不表示依赖。 |
| 45df8437-2c07-5332-ae03-f727ba814d77 | ScamGen USES TeleAntiFraud-28k | 不支持 | 原文分别介绍两项研究，未说前者使用后者；两个名称同在引用不足以支持连边。 |
| 695c5a0c-6152-52fc-ad74-d51f13ba48f7 | index.html USES 规则引擎 | 支持 | 原文明示入口加载规则引擎。此为本次恢复的 PDF 关系。 |
| 6e32e535-2021-5f9a-9aaf-9e34969453f2 | ASR DEPENDS_ON 图模块 | 不支持 | 同独立开发并列清单，方向和依赖均无依据。 |
| 77fb32e5-d6a8-5be2-971b-487d116743c5 | 报告页 DEPENDS_ON 详情页 | 不支持 | 原文仅要求状态说明一致；输出一致不能推出系统依赖。 |
| 8914fc58-e7db-5bb2-a498-97699b928f60 | 当前可执行系统 USES 合成案例 | 支持 | 明确当前组件清单。 |
| c2675c11-45ef-56b6-bc46-509a34d15674 | Vosk offline speech recognition API and models MAINTAINED_BY ALPHA CEPHEI | 不支持 | 引用仅为参考文献署名和标题，不能据此推出当前维护关系。 |
| c4420737-ce48-5456-b950-0f1aaa543742 | 当前可执行系统 USES 浏览器交互层 | 支持 | 明确当前组件清单。 |
| c5b50088-6a97-5df9-972b-0ebe9752d918 | 本地Edge USES 回环静态服务 | 不确定，建议拒绝 | 原文是检查同时使用 Edge 与静态服务，未明确 Edge 为使用者或调用方向。不能用浏览器通常访问服务的常识补边。 |
| c73c0e74-a786-542e-bd74-ccdb5d3af12e | engine.analyze USES Vosk | 不支持 | 原文明示测量“不含”Vosk；描述测量边界并不能推出函数依赖。 |
| e10ebe1e-739f-5850-b514-30c98792376f | 当前可执行系统 USES 确定性规则引擎 | 支持 | 明确当前组件清单。 |
| f5c0b508-ac8f-596d-9b0c-6c0884aef792 | engine.analyze USES MacBERT | 不支持 | 同“不含”测量边界，不能推为函数使用模型。 |

## PDF 失败诊断与恢复

首次真实回填记录 `.runtime/rag_accuracy/phase4/backfill_reports.json` 保留未覆盖：简历 2 父块 READY、DOCX 53 父块 READY，PDF 81 父块 FAILED/ValidationError。

复现得到具体错误：模型把提示中的 JSON Schema `$defs` 放进顶层抽取数据，触发 `extra_forbidden`，位置 `["$defs"]`。捕获到两个连续非法响应的实际父块 `f9df6254-a99e-4fc5-bd1e-7deb664c89bb`；确定性复放通过原 `GraphExtractor.extract` 两次尝试仍严格抛出同一错误。最小复现为 `{"entities":[],"relations":[],"$defs":{}}`，无需用户文档即可说明错误。

- 红色反馈命令：`.venv/Scripts/python.exe .runtime/rag_accuracy/phase4/debug/replay_pdf_validation.py --red`，预期退出 1 且错误位置为 `$defs`。
- 假设核对：不带 `--red` 的相同脚本断言严格拒绝，再仅在诊断副本删除 `$defs` 验证其余结构合法；未清洗生产响应、未直接发布清洗结果。
- 实际恢复：80 个合法父块按内容哈希缓存；仅对最后目录页追加一次“仅返回 entities/relations 数据，禁止 Schema 元数据”的格式纠正请求，严格返回空抽取。目录正文确实不提供可表示的当前关系，空结果有原文依据。
- 发布前重新核对 81 个父块的用户、知识库、文件、v2 版本、排序、当前全文 SHA256 和 `validated`，经原 GraphIndexer 的租约/当前全文核对/原子发布事务完成，未绕过原发布路径。
- 恢复报告 `.runtime/rag_accuracy/phase4/debug/pdf_cache_restore_v1.json`：PDF READY，81 父块、1 接受关系、87 拒绝关系、135 次实体提及；另外两个 READY 作业的状态和统计完全不变。
- 诊断新增请求：第一段可确认 46 次发起记录，暂停时另有最多 1 次在途；第二段 44 次，最终纠正 1 次，共 **91 次可确认，最多 92 次发起**。这不是服务端计费请求数；不能补造被中断请求的结算状态，也不包含首次回填的未知调用量。缓存发布阶段新增云调用 0。

为保持主代理最后一轮 QA 的源文件指纹一致，上述诊断和恢复仅新增 `.runtime` 脚本与缓存，未改业务源码；永久提示/精简错误反馈修复须另行记录。非法原始响应保存在本地 `debug/pdf_invalid_response_*.json`、`pdf_validation_failure_*.json`，没有通过放宽 Schema 把它们标记为成功。

## 修正建议

仅修正 `$defs` 格式不足以解决上述关系语义问题。至少需要：对每种谓词要求明确肯定且方向一致的原文支持；并列清单、参考文献、测量排除、状态相同不能当依赖或维护。缺乏肯定支持时宁可不入图，继续原有向量检索。

抽取提示应明确顶层仅有 entities/relations；第一次 ValidationError 后，可在原总计两次尝试内附精简 loc/type 反馈，不回传错误 inputs，不增加无界重试。Schema 和 grounded quote 验证应继续严格保留。后续对真实图的修正与重新审核，应另写新记录，不能覆盖本报告的初始问题清单。

## 格式修复追加记录

最后 72 次 QA 完成、源文件冻结窗口结束后，已按授权修正 `backend/graph/extractor.py`：首次提示明确仅返回 entities/relations 数据，禁止复制 Schema 元数据；第一次 Pydantic ValidationError 后只追加最多 8 条 loc/type，路径段数/长度有上限，不包含错误 input、原模型响应或额外用户原文。原始文档仍作为原有抽取输入，总尝试次数仍为 2；第二次非法仍抛出，取消不重试，没有删除非法字段的自动清洗逻辑。

新增 `backend/tests/test_graph_extractor_feedback.py` 的 4 项真实调用边界回归。先在冻结窗口内以 `.runtime` 同内容测试验证旧代码出现 1 个失败、1 个错误（其余 2 项通过），再在解冻后修正并全部转绿。常量非法 `$defs` 的严格拒绝回放仍须保持拒绝；修正的是有结构反馈的受控重试，而不是允许非法响应通过。

最终完整图谱回归 **28 项全部通过**（26.548 秒），包含随机隔离 PostgreSQL schema 的存储/隔离/删除/回填测试及新增反馈测试；未调用云模型。`py_compile` 和相关文件 `git diff --check` 通过。

- QA 测量时 extractor SHA256：`db017f29330415435db0a2d3c92c4b65ca0b2267f48998e548ac9beb85053192`。
- 交付格式修复后 SHA256：`86d63a0afab0c3fa525e8dfd181f18bd6c75c0b79d1f001120fab8b7833b25f6`。
- 该修复在 72 次 QA 之后，不能将既有云端 QA 描述为用新 extractor 重跑；真实来源 QA 记录及原始结果保留不改。
- 此格式修复不解决前述 8 条关系的语义问题，也没有擅自开启个人图谱设置。语义隔离仍需主代理继续处理。
