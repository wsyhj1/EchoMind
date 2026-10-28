# 当前检索链路与实施范围

审计日期：2026-10-21。起点 HEAD：`273edce40b0db09bf245219a5601bf7fdbc614fc`。
本次开始时工作区已存在大量未提交的逐轮计时、RAGAS、用户画像和部署修改。
基线定义为这些修改全部存在时的实际实现，不是上游 HEAD。原始代码指纹、依赖和非敏感配置见 `baseline_manifest.json`。

## 实际调用链

`api.py /chat_with_agent/stream` → `agent.py stream_agent_response` →
`tools.py search_knowledge_base` → `milvus_client.py` →
`knowledeg_base_manager.py` → `PostgreSQLParentClient` → `rerank_documents`。

Agent 的知识库工具仍限制每轮调用一次；默认 K=5，dense 候选 7K、BM25 候选 3K、RRF 返回 3K。
混合检索失败后 dense 返回 K；Reranker 输入最多 min(3K,30)，失败后保留检索顺序。
父块切分 1000/200，子块 200/50，模型和预算未在阶段一调整。

`默认知识库` 是当前 user_id 的跨知识库查询标志，不是实际知识库。
指定知识库的 Milvus filter 和 SQL 都包含用户及知识库限制。
当前 HTTP API 仍由调用方提供 user_id，现有认证问题未在本次扩展。

上传由 `BackgroundTasks` 执行 `DocumentProcessor.process_document`，接口成功返回不代表索引完成。
上传支持 PDF/DOC/DOCX；本次测试覆盖 PDF/DOCX。父块在 PG，子块在 Milvus。
文件与知识库删除先提交 PG，后删除 Milvus，两库不存在共同事务。

## 与原计划假设的差异

1. 当前工作区已经保序去重，没有 `list(set(...))`；已经提供 `get_parent_records`，且旧工具已经按 index 做 Reranker 映射。本次复用这些改动，没有再次将它们宣称为新收益。
2. 已有 `observability.RunTrace` 和 `evaluation`，因此复用现有目录，不再平行建立 `eval/` 或第二套 RAGAS 框架。
3. `parent_chunks` 主键是 `(parent_id, knowledge_base_id, user_id)`，旧 `get_parent_records` 的 Python 映射只使用 parent_id。新接口和检索服务保留完整复合身份，避免默认范围中两个知识库同 ID 串源。
4. 基线源码冻结在 `.runtime/rag_accuracy/legacy_source`。执行 legacy 评测先核对 manifest 中的源码 SHA256，再加载原工具、检索管理器、PG 读取和 Reranker；不能把改造后的新路径改名为 legacy。
5. 评测导入使用独立 PG schema 和独立 Milvus 集合。语料包含 14 篇主语料及 2 篇权限哨兵文件，不向用户现有知识库添加测试数据。

## 已知风险及后续依赖

- 跨文件重复子块会被跳过写入，父块却分别创建；删除最早文件后，后续文件可能失去向量入口。阶段一记录这一风险，阶段二需做来源关联修复，不能先用错误图谱引用掩盖它。
- 上传的 `gather` 不是原子事务，存在 PG 已写而 Milvus 未完成的可能。评测导入用独立状态与按文件清理重试；生产上传状态/补偿在阶段二处理。
- PG 删除成功、Milvus 删除失败的补偿状态尚未实现；新服务会排除找不到合法 PG 父块的残留向量命中。
- Embedding SDK 与 Reranker 的外部服务存在 TLS/超时波动。原始失败运行保留，不能当正常空结果或凭空补分。
- 原模型、提示词、分块预算不能在同一配对实验内调整。
- 100 题为可溯源的合成 Gold 草稿，尚无人工审核。正式准确率和阶段二 Gate 不能由这批草稿自动宣称通过。

## 阶段边界

本次按计划先实施阶段一。图谱建表、抽取、两跳扩展和证据补检尚未实现。
`.env.example` 的后续阶段开关只是预留，默认关闭，不代表对应功能已存在。
阶段一的源码、数据与评测结果稳定且 Gold 完成人工审核后，再实施阶段二。

## 后续方案调整

当前合成语料暴露了一个需要区分的瓶颈：部分多跳/多证据题的 Gold 已经进入原始候选池，
但在最终 Top5 中缺失。后续不能把这类问题笼统写成“向量召回失败”。
应分别统计候选池 CompleteEvidence 与最终 CompleteEvidence，再验证图路径配额是否能保留必要的桥接证据。
仅把图谱父块追加到候选末尾然后原样 Top5，不一定改善这批题。

由于主语料仅 14 篇短文档，最终正式数据还需要更丰富的干扰源和更明确的必要证据标注，
用来区分新增召回收益、重排保留收益和小语料带来的高覆盖率。
目前的冻结基线不得在修改语料或 Gold 后继续沿用；应形成 v2 并对各组一起重跑。
