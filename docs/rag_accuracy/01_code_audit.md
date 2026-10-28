# 阶段一实现说明

## 数据契约

`backend/rag/types.py` 定义 `RetrievalScope`、`RetrievalOptions`、`ChildHit`、
`RetrievalCandidate`、`RetrievalTrace`、`RetrievalResult`。

候选身份为用户、知识库、父块 ID。每个候选保留全部命中子块的 ID、排名、分数及分数类型，
多路来源保存在 `sources`。RRF、cosine 和 Reranker 分数分别记录，不相加。
Trace 不保存 Query 或正文；记录候选、最终与缺失父块的复合身份、耗时及降级原因。

`KnowledgeBaseManager.retrieve_knowledge_hits` 返回有序子块命中。
旧的 `hybrid_retrieval_knowledge_base` 保留原列表接口，两个接口复用相同 Milvus 请求参数。
直接调用检索核心不要求处于 LangGraph 流式运行上下文。

`PostgreSQLParentClient.get_parents_with_metadata` 在 SQL 内执行用户/知识库过滤，返回 user_id 和来源信息。
旧 `get_parent_records` 和 `get_parents` 仍保留。新服务只按完整复合身份关联命中与合法 SQL 父块，
丢失或 file_hash 已失配的父块显式记入 trace，避免把旧向量行错误对应到新来源。

## 重排及兼容

`backend/rag/retrieval_service.py` 依次完成命中、父块加载、保序合并、重排及上下文记录。
使用 Reranker 输入 index 对应原候选，禁止按文本合并。
布尔 index、重复 index、越界 index、非有限分数和格式错误会回退原检索顺序。
API index 只能指向实际提交的 min(3K,30) 个文档。
超时或异常不丢来源 ID；取消请求会继续向外传播。

知识库工具名称、签名、字符串前缀、片段分隔和 Prompt 保持原样，
内部改为调用检索服务。原计时和评测快照继续收到 candidate/parent/final 事件。
生产诊断日志仅在 `RAG_TRACE_ENABLED=true` 时输出匿名请求 ID、计数、耗时、错误类型。
现有受控评测快照仍按原功能保留 Query 与证据，需沿用其原访问控制。

新结构化 API 对 embedding 最终失败抛出运行错误，避免把服务故障写成正常空召回。
旧 parent-ID API 保留 embedding 失败返回空列表的行为，供冻结基线和旧调用方使用。

## 验证入口

- `test_rag_retrieval.py`：契约、索引映射、失效来源、分数空间、回退、取消、固定数据及指标。
- `test_rag_integration.py`：真实 PG/Milvus，固定测试向量，PDF/DOCX 生命周期与租户/知识库隔离。
- 原 `test_evaluation_framework.py`：保留工具接口测试，mock 对应新的结构化接口。
- 真实云模型基线和对照：`evaluation.retrieval`。
- 真实 Agent/tool/Redis：`evaluation.answer_eval`；答案正确率和无依据陈述率在未审核/未评分时保持 unavailable。

## 回退

此次没有数据库迁移，不更改集合 Schema、分块参数、长期记忆或画像。
关闭 `RAG_TRACE_ENABLED` 只关闭新增诊断日志。
图谱与证据检查尚未实现；其预留开关保持 false。
如需回到开发前业务实现，可使用 `.runtime/rag_accuracy/legacy_source` 中经 manifest 验证的
`tools.py`、`knowledeg_base_manager.py`、`milvus_client.py`、`postgresql_client.py`，
按文件审核恢复本次差异，不能用 `git reset --hard` 覆盖用户之前的未提交修改。
