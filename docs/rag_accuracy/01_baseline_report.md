# 阶段一基线与实施结果

日期：2026-10-21。阶段一工程与配对实测已经落地；正式 Gold 审核尚未完成，阶段二 Gate 未通过。
本报告不宣称准确率提升，也不把暂定检索指标称为答案准确率。

## 实施与验证

保留现有工作区修改及长期记忆/画像实现。新增结构化检索服务，保留完整来源、全部子块命中、
复合父块身份、分数空间及重排 index 映射；向量请求、模型、分块和 TopK 预算保持原设置。
工具接口与字符串输出继续兼容现有 Agent。

开发前冻结源文件、Git SHA、Python 和依赖版本。当前环境：Python 3.12.4、PostgreSQL 16.15、
Milvus 2.5.10、Redis 7.4.7；模型配置见 `baseline_manifest.json`。
最初 Windows 沙箱内最小 asyncio 也挂起；faulthandler 定位为 socketpair/accept，
相同命令在沙箱外正常。因此验证在获自动批准的沙箱外执行，未修改项目事件循环。

- 开发前后端基线：30 项，20 通过、10 个未指定环境的集成测试跳过。
- 接入后启用真实数据库的完整后端回归：48 项全部通过、无跳过。
- 后续新增中文知识库过滤检查，连同已有 2 项 RAG 生命周期检查一起重跑：3 项全部通过。
  后端共验证 49 个独立测试；真实数据库使用各测试自建资源，固定测试向量不用于质量指标。
- 新增评测工具检查：5 项全部通过，覆盖文件字节/抽取稳定性、冻结源码校验、Gold 数据校验、比较一致性和清理边界。
- 两组真实 Retriever 各完成 100 题，运行失败均为 0。
- 真实 Agent/tool/Redis QA 冒烟旧工具、新工具各 2 题；生成、工具调用和结果记录见对应原始文件。
- compileall 和 git diff --check 已通过。未运行完整 100 题答案裁判，不报告 Answer Accuracy 或 UnsupportedClaimRate。

## 100 题暂定检索指标

固定同一份导入状态、父块 UUID、数据 SHA、embedding 模型、Reranker、K=5、dense/BM25/RRF 参数。
两组分别串行执行，运行时间存在重叠，因此延迟受共享服务及网络波动影响。
88 道有答案题计入召回指标；12 道不可回答题（含隔离题）不为 Recall/MRR 构造分母。

| 指标 | 冻结旧实现 | 新结构化实现 |
|---|---:|---:|
| Recall@5 | 92.05% | 92.05% |
| Hit@5 | 100.00% | 100.00% |
| CompleteEvidence@5 | 82.95% | 82.95% |
| MRR@5 | 0.9697 | 0.9697 |
| 运行失败 | 0/100 | 0/100 |
| 最终策略降级 | 0/100 | 0/100 |
| 总延迟 P50 | 6664 ms | 6626 ms |
| 总延迟 P95 | 8002 ms | 8475 ms |

新组存在 Reranker 单次超时后重试成功，最终未降级。不能据此声称性能改善。
100 题最终证据集合相同，99 题最终顺序相同。
Q090 为无答案题，候选文本及顺序相同，但外部 Reranker 返回轻微不同分数，导致第 4/5 位互换。
保留该差异，不宣称逐题排序完全一致。见 `01_paired_results.md` 的全部逐题结果。

| 题型 | 题数 | 旧/新最终完整证据率 |
|---|---:|---:|
| single_fact | 20 | 100% |
| multi_hop | 20 | 60% |
| multi_evidence | 20 | 65% |
| ambiguous | 15 | 100% |
| distractor | 10 | 100% |
| isolation | 5，其中有答案 3 | 有答案子集 100% |
| unanswerable | 10 | unavailable；需通过答案评测检查拒答 |

## 真实失败定位与方案调整

15 道有答案题缺少至少一份标注证据。所有这些缺失来源都已经出现在原始父块候选池，
随后被 Reranker 排出 Top5。两组均出现相同问题。
在当前小语料中，88 道有答案题的候选池 CompleteEvidence 为 100%，最终降为 82.95%。

| 题号 | 被排出的必要来源 |
|---|---|
| Q021、Q023、Q024、Q025 | order_ownership |
| Q036、Q037、Q039、Q040 | shipping_ownership |
| Q041、Q042 | order_ownership |
| Q045 | order_ownership、order_runbook |
| Q050 | payment_runbook |
| Q055 | stock_runbook |
| Q057 | shipping_ownership |
| Q060 | shipping_runbook |

例如 Q021 询问订单系统使用的缓存由哪个团队维护：架构原文关联到赤霄集群，
维护登记包含赤霄与基础设施甲组，却不直接提订单系统，因此单块文本相关性可能不足以保留它。
Q045 同时要求名称、维护组和排障步骤，维护来源及手册一起丢失。

后续图谱创新应优先验证 **已验证关系链证据的保留配额**，同时分别衡量新增召回和重排保留收益。
单纯追加图谱父块后原样重排取 Top5，不能保证改善这类题。
正式数据需增加干扰文档并审核必要来源，避免小语料候选池几乎全覆盖导致实验失真。

## 失败运行也保留

首次旧实现 20 题 smoke 无失败，完整证据率 81.25%。
首次新实现 smoke 有 2 道 embedding TLS 最终失败及 1 道 Reranker 最终降级，完整证据率 68.75%。
此轮不被覆盖、不被删题，失败按已有 Gold 未命中计入；它不是结构化治理提高或降低准确率的独立证据。
后续完整 100 题重跑恢复，结果与旧实现的指标相同。

## 原始结果与复现

运行命令与资源清理见 `evaluation/RAG_ACCURACY.md`。原始文件均在本地受忽略的运行目录，
包含合成语料正文、来源 UUID 和实际耗时；未写入密钥。

- `.runtime/rag_accuracy/corpus.json`：同一隔离语料状态、文件哈希、真实父块 UUID 映射。
- `.runtime/rag_accuracy/legacy_source/`：经 manifest 核验的未修改实现快照。
- `.runtime/rag_accuracy/runs/baseline_legacy_v1.jsonl`、`.manifest.json`、`.summary.json`。
- `.runtime/rag_accuracy/runs/baseline_structured_v1.jsonl`、`.manifest.json`、`.summary.json`。
- `.runtime/rag_accuracy/runs/baseline_legacy_smoke.jsonl` 与 `baseline_structured_smoke.jsonl`：首轮原始记录。
- `.runtime/rag_accuracy/runs/qa_legacy_smoke.jsonl`、`qa_structured_smoke.jsonl`：实际 Agent/tool 两组冒烟。
- `.runtime/rag_accuracy/checks/backend-tests.json`、`backend-tests.log`：完整 48 项回归原始报告。

## 阶段一 Gate

| 验收项 | 状态 |
|---|---|
| 100 题数据及固定来源 | 已生成、已跑完两组；人工审核待完成 |
| 原始基线与 manifest | 完成；明确为开始时的 dirty worktree |
| 一键 Retriever-only 与独立 Agent QA 入口 | 完成 |
| 父块身份、来源、重排与降级 | 完成并验证 |
| PDF/DOCX、删除、默认范围、多用户、中文知识库 | 真实数据库检查通过 |
| 长期记忆/画像回归 | 原有测试及集成检查通过，无业务修改 |
| 可宣称最终答案准确率或创新收益 | 不满足，尚无审核后的正式 Gold 与完整 QA 实验 |
| 进入阶段二 | 暂未通过正式 Gold 审核门槛；未提前建图或启用补检 |

剩余工作是审核 100 题必要证据及答案，形成正式数据版本并复跑。
阶段二、三尚未实现；后续预留开关保持关闭。
