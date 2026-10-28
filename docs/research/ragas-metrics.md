# EchoMind：RAGAS 指标与接入核对

核对日期：2026-10-21。范围：只查 RAGAS 官方文档及官方仓库；没有安装依赖、调用模型或修改业务代码。`stable` 文档和 `main` 源码会变化，以下是方案设计依据，实施时必须选定发布版本并复核其接口。

## 指标口径与字段

| 展示名称 | 建议实现 | 必需样本字段 | 额外模型 | 含义与边界 |
| --- | --- | --- | --- | --- |
| 依据一致性 / Faithfulness | `ragas.metrics.collections.Faithfulness` | `user_input`, `response`, `retrieved_contexts` | 裁判 LLM | 回答中的声明得到实际上下文支持的比例。无需标准答案；上下文本身有错时仍可能高分，因此不是客观正确率。[官方说明](https://docs.ragas.io/en/stable/concepts/metrics/available_metrics/faithfulness/) |
| 回答相关性 | `collections.AnswerRelevancy` | `user_input`, `response` | 裁判 LLM + embedding | 从回答反推问题，再与原问题做 embedding 相似度；不判断事实是否正确。文档标题也称 Response Relevancy。余弦性质意味着分数并非严格保证在 0–1，不应擅自裁剪后仍称原始 RAGAS 分数。[官方说明](https://docs.ragas.io/en/stable/concepts/metrics/available_metrics/answer_relevance/) |
| 检索排序精度（有参考） | `collections.ContextPrecisionWithReference`，短名 `ContextPrecision` | `user_input`, `reference`, `retrieved_contexts`（保序） | 裁判 LLM | 按参考答案判定各片段有用性，再计算 AP 型排序分数；不是简单“相关片段数 / 检索片段数”。[官方说明](https://docs.ragas.io/en/stable/concepts/metrics/available_metrics/context_precision/)、[实现](https://github.com/vibrantlabsai/ragas/blob/main/src/ragas/metrics/collections/context_precision/metric.py) |
| 上下文利用度（无参考代理） | `collections.ContextPrecisionWithoutReference`，包装名 `ContextUtilization` | `user_input`, `response`, `retrieved_contexts`（保序） | 裁判 LLM | 用生成回答作为比较依据，仍为 AP 型分数。不能把它当作独立于回答的真实检索精确率；漏答的信息可能不会受到处罚。[官方说明](https://docs.ragas.io/en/stable/concepts/metrics/available_metrics/context_precision/)、[实现](https://github.com/vibrantlabsai/ragas/blob/main/src/ragas/metrics/collections/context_precision/metric.py) |
| 上下文召回 / 信息覆盖 | `collections.ContextRecall` | `user_input`, `reference`, `retrieved_contexts` | 裁判 LLM | 标准答案的声明中有多少被检索上下文支持；属于参考答案声明覆盖，不等同整个知识库的文档 Recall@K。[官方说明](https://docs.ragas.io/en/stable/concepts/metrics/available_metrics/context_recall/) |
| 答案事实正确性（推荐主指标） | `collections.FactualCorrectness(mode="f1")` | `response`, `reference` | 裁判 LLM | 对回答与标准答案分解声明、做事实匹配，支持 precision / recall / f1。固定 `mode`, `atomicity`, `coverage` 后才适合跨实验比较。[官方说明](https://docs.ragas.io/en/stable/concepts/metrics/available_metrics/factual_correctness/) |
| 答案正确性（可选综合指标） | `collections.AnswerCorrectness` | `user_input`, `response`, `reference` | 裁判 LLM + 默认需要 embedding | 默认组合声明级事实 F1（0.75）与语义相似度（0.25）；不是答对题数占比，亦不等同 FactualCorrectness。设相似度权重为零才可不配 embedding。[官方源码](https://github.com/vibrantlabsai/ragas/blob/main/src/ragas/metrics/collections/answer_correctness/metric.py) |
| ID 检索 Precision / Recall | 官方 `ragas.metrics.IDBasedContextPrecision/Recall`，或项目内确定性公式 | `retrieved_context_ids`, `reference_context_ids` | 无 | 基于去重 ID 交集，分母分别是返回 ID 数、标注相关 ID 数；必须统一到相同粒度和语料版本。[精度说明](https://docs.ragas.io/en/stable/concepts/metrics/available_metrics/context_precision/#id-based-context-precision)、[召回说明](https://docs.ragas.io/en/stable/concepts/metrics/available_metrics/context_recall/#id-basedcontext-recall) |

关键区别：没有 `reference`、参考片段或标注相关 ID，就没有召回分母，无法报告真实召回率。回答相关性、Faithfulness、无参考上下文利用度可以先做，但不能代替有标注召回。[官方明确要求 reference](https://docs.ragas.io/en/stable/concepts/metrics/available_metrics/context_recall/)

这些指标是声明比例、排序分数、相似度或其组合；把 0.9 写成“本回答有 90% 概率正确”没有依据。UI 推荐显示两位小数并附简短定义，失败、缺少标注和不适用分别记录状态，不能以 0 填空。（由上述定义推导的展示建议。）

## 当前推荐 API 路线

新增实现优先使用 `ragas.metrics.collections`，以 `await metric.ascore(**fields)` 获取 `MetricResult.value`；LLM 使用 `ragas.llms.llm_factory`，embedding 使用 `ragas.embeddings.base.embedding_factory` / 现代 `OpenAIEmbeddings`。默认组合是原生异步客户端 + Instructor 结构化输出。避免把已有 LangChain 对象直接塞给 collections 指标。[迁移指南](https://docs.ragas.io/en/stable/howtos/migrations/migrate_from_v03_to_v04/)、[模型定制](https://docs.ragas.io/en/stable/howtos/customizations/customize_models/)

不要照搬旧教程的 `faithfulness` 等全局常量、`single_turn_ascore(SingleTurnSample(...))`、`LangchainLLMWrapper` / `LangchainEmbeddingsWrapper` 和 `evaluate()` 作为新模块主架构。文档将其列入 legacy / deprecated 路线，推荐直接 collections 指标；批量工作流可选 `@experiment`。这些旧接口是否仍可导入取决于固定版本，不能混成一套示例。[迁移指南](https://docs.ragas.io/en/stable/howtos/migrations/migrate_from_v03_to_v04/)

例外：IDBasedContextPrecision / Recall 在本次检查的文档中仍走旧 `ragas.metrics` + `SingleTurnSample`，没有在现代 collections 出口导出。实施建议在 EchoMind 内用短小、可测试的集合公式计算明确命名的 `retrieval_precision_at_k` / `retrieval_recall_at_k`，避免为两条确定性公式引入混合 API。它们与 AP 型 RAGAS ContextPrecision 分开保存。[collections 出口](https://github.com/vibrantlabsai/ragas/blob/main/src/ragas/metrics/collections/__init__.py)、[legacy ID 精度实现](https://github.com/vibrantlabsai/ragas/blob/main/src/ragas/metrics/_context_precision.py)

## DashScope 的适配可行性

RAGAS 支持提供预先构造的 OpenAI 风格客户端及自定义 `base_url`，LLM factory 可配置 Instructor `mode`，现代 embedding 会调用客户端的 `embeddings.create(input=..., model=...)`。[官方适配示例](https://docs.ragas.io/en/stable/howtos/llm-adapters/#local-models-ollama)、[LLM factory 源码](https://github.com/vibrantlabsai/ragas/blob/main/src/ragas/llms/base.py)、[embedding 源码](https://github.com/vibrantlabsai/ragas/blob/main/src/ragas/embeddings/openai_provider.py)

工程推断：项目已使用的 DashScope OpenAI 兼容端点，可作为评测适配的第一选择，复用账号、单独配置裁判模型和 embedding 模型。原生 DashScope embedding 对象不能直接传给现代 `OpenAIEmbeddings`；需要确认所用兼容端点/模型支持 `embeddings.create`，否则写现代 `BaseRagasEmbedding` 适配器。不能仅凭“兼容”承诺全部指标可直接运行：需实施阶段验证结构化 JSON 输出、模型参数、重试及 embedding 批量限制。默认 JSON 模式不适配时，可核对固定版是否支持 `mode=instructor.Mode.MD_JSON`；源码明确列出该选项用于不支持 `response_format` 的后端。本次未调用真实 DashScope，不把适配当作已验证结论。[LLM factory 源码](https://github.com/vibrantlabsai/ragas/blob/main/src/ragas/llms/base.py)、[embedding 源码](https://github.com/vibrantlabsai/ragas/blob/main/src/ragas/embeddings/openai_provider.py)

## 执行、成本与版本建议

- 建议评测 worker 使用独立虚拟环境和锁文件，应用只生产评测样本与读取结果。理由是 RAGAS 当前依赖包括 datasets、Pydantic 2、Instructor，以及 LangChain 多个包，直接加入业务环境可能引起依赖升级冲突。这是基于官方依赖表的隔离设计建议。[官方 pyproject](https://github.com/vibrantlabsai/ragas/blob/main/pyproject.toml)
- 固定发布版 RAGAS、OpenAI 客户端、Instructor 和 embedding 依赖；每次结果保存指标类、参数、裁判模型、评测 embedding 模型、提示词版本、数据集和语料版本。正式比较时不随意改模型。具体版本待实施阶段兼容性验证后锁定，不把 `main` 或“最新版”写进生产要求。（版本化建议；API 变化依据见[官方迁移指南](https://docs.ragas.io/en/stable/howtos/migrations/migrate_from_v03_to_v04/)。）
- 回答完成后异步入队评测，先以手动单轮触发、固定数据集批量评测开始，再提供采样。使用异步 `.ascore()` 并限制并发、超时与有限重试。异步不会免除 API 费用，只能避免阻塞聊天与改善吞吐。[异步客户端支持](https://docs.ragas.io/en/stable/howtos/llm-adapters/#async-support)
- ContextPrecision 会逐片段调用裁判；Faithfulness 等会进行多步判断；AnswerRelevancy 还要 LLM 反推问题和 embedding 调用。不要写成“一轮评测只调用一次模型”，也不在方案阶段承诺固定秒数/金额。[精度实现](https://github.com/vibrantlabsai/ragas/blob/main/src/ragas/metrics/collections/context_precision/metric.py)、[相关性定义](https://docs.ragas.io/en/stable/concepts/metrics/available_metrics/answer_relevance/)
- 在实际客户端边界统计 input/output tokens、embedding 用量、重试次数和耗时，按可配置单价估算费用；不要假设 `MetricResult.value` 自带完整账单。成本旧文档主要基于旧 `evaluate()` + LangChain parser，因此不直接套到现代 worker。[成本文档](https://docs.ragas.io/en/stable/howtos/applications/_cost/)、[现代 LLM 返回实现](https://github.com/vibrantlabsai/ragas/blob/main/src/ragas/llms/base.py)
- 可使用 RAGAS cache 降低相同评测输入的重复调用；缓存键也应包含模型/指标配置/提示词版本，裁判版本改变后不沿用旧成绩。无须下载本地判别模型；HHEM 等方案不是第一期必需项。[缓存指南](https://docs.ragas.io/en/stable/howtos/customizations/_caching/)、[Faithfulness 可选 HHEM](https://docs.ragas.io/en/stable/concepts/metrics/available_metrics/faithfulness/)

## EchoMind 样本建议（工程设计）

保存原始问题、Agent 改写后的每个工具 query、最终回答、历史上下文引用、知识库/记忆分源的每次候选与最终可见片段（文本、稳定 ID、序、score、语料版本）、trace/session/turn ID。Faithfulness 应针对模型实际看到的证据快照；候选召回和重排后的指标分别计算，不能拿候选池冒充最终上下文。父块、子块及文档 ID 必须明确映射，重新分块后不能直接沿用旧 ID 标注。

预先给离线样本标注 `requires_retrieval`。闲聊未检索可以“不适用”；本应检索却未调用工具要计为路由失败，标注 ID 召回为 0，不能静默排除。LLM 指标空列表会被部分实现拒绝，适配层应把路由失败和指标不可运行分别存储；不要让异常样本消失。主报告同时显示有效样本数、缺失标注数、评测失败数、路由失败率。（此段是针对项目结构的设计建议，并非 RAGAS 内置功能。）
