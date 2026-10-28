# EchoMind 可复现检索评测

阶段二图谱配置、回填、隔离实验及回滚见 [图谱运行说明](../docs/rag_accuracy/02_graph_reindex.md)。

阶段三证据检查、单轮补检和实时 A/B/C/D 消融见 [证据检查说明](../docs/rag_accuracy/03_evidence_check.md)。
检索运行新增 `vector_plus_check`、`vector_plus_graph_plus_check`；所有对照显式设置功能开关，不继承个人偏好。

从仓库根目录运行。现有在线 RAGAS 评测继续见 `evaluation/README.md`。
这里区分直接 Retriever 与真实 Agent 两个入口，不把工具未调用误判成检索算法未命中。

## 数据与 Gold

`datasets/rag_v1.jsonl` 是 100 题草稿：20 单事实、20 两跳、20 多证据、15 同义模糊、
10 干扰、10 无答案、5 隔离。`rag_smoke_v1.jsonl` 是分层抽取的 20 题。
来源原文及答案在 `rag_fixtures.py` 中固定，无需模型生成答案。
每条 Gold 包含 doc_id 和原文 quote；导入后按租户、知识库、文件哈希、全文引文解析到真实父块 UUID。
不会把示意父块 ID 当线上 UUID，也不会用答案相似度生成 Gold。

这批标注尚未人工审核，`gold_review=pending_human_review`，所有结果均为 provisional。
审核需检查正确答案、所有必要证据、题型、时态及权限范围；应特别检查干扰项是否确实需要每份标注来源。
审核后形成新数据集版本并重跑，不修改已有运行的 Gold 或丢弃负面样本。

2026-10-23 按用户要求由独立子代理审核全部100题，另存
`datasets/rag_v2_agent_reviewed.jsonl`（`gold_review=agent_reviewed`、`human_reviewed=false`）。
该审核作为本次受控本地验证的依据；历史数据集和报告的审核状态保留。
审核修正、98道不变查询的离线复算及真实回答复验，见
[Gold审核](../docs/rag_accuracy/04_gold_agent_review.md)、
[离线复算](../docs/rag_accuracy/04_reviewed_retrieval_rescore.md)与
[启用记录](../docs/rag_accuracy/04_activation_report.md)。
Q082/Q093改变了查询，不能复用旧检索作为新题成绩。真实答案审核按实际模型可见原文逐项完成，独立报告代理审核的分母、错误与引用缺失。

离线复算命令（不调用模型，也不覆盖旧记录）：

```powershell
.\.venv\Scripts\python.exe -m evaluation.rescore_reviewed --help
```

当前RAG工具在所有模式均提供真实文件名与E编号；证据检查关闭时不生成coverage，也不触发补检。
旧实验使用的工具文本包装应保留在各自源码指纹中，不能把不同上下文包装的引用差异全部归因于图谱。

## 生成并导入

```powershell
.\.venv\Scripts\python.exe -m evaluation.rag_fixtures
.\.venv\Scripts\python.exe -m evaluation.rag_corpus import
.\.venv\Scripts\python.exe -m evaluation.rag_corpus import --execute
```

默认 dry-run；`--execute` 才导入并调用现有 embedding 模型。
文档由真实 `DocumentProcessor` 处理，生成 PDF/DOCX 的字节稳定。
每次实验资源名 `rag_accuracy_<12位随机hex>`，分别作为私有 PG schema 和独立 Milvus 集合。
当前应用的 public 表、记忆与既有集合不参与导入。
状态写入 `.runtime/rag_accuracy/corpus.json`，仅全部父块与向量成功并解析 Gold 后置 READY。
失败重试复用状态，已完成文件跳过，未完成文件先清除自己的部分数据再导入。
外部服务计费不保证恰好一次。不要手动重建 READY 语料后继续比较旧父块 ID。

## 直接检索对照

首次改动业务代码前冻结的源文件在 `.runtime/rag_accuracy/legacy_source`，
源码 SHA256 在 `docs/rag_accuracy/baseline_manifest.json`。
legacy 模式先校验这四个实际执行模块：tools、知识库管理器、PG 读取、Reranker。
该本地冻结目录为基线必要依赖，不提交用户源码副本；换机器必须保留同一冻结快照，
否则只能重新冻结并明确它是新基线。原始基线不是上游 HEAD 的成绩。

以后开展新的基线研究时，可在任何业务修改前用 `python -m evaluation.freeze_rag_baseline`
冻结。若已有 manifest 或源码目录，该命令会拒绝覆盖。不同实验需要独立保存原始快照和 manifest，
当前 legacy runner 读取本次约定位置，不能混用另一轮快照。

```powershell
.\.venv\Scripts\python.exe -m evaluation.retrieval --dataset evaluation/datasets/rag_v1.jsonl --mode legacy --execute --output .runtime/rag_accuracy/runs/baseline_legacy_v1.jsonl
.\.venv\Scripts\python.exe -m evaluation.retrieval --dataset evaluation/datasets/rag_v1.jsonl --mode vector_only --execute --output .runtime/rag_accuracy/runs/baseline_structured_v1.jsonl
.\.venv\Scripts\python.exe -m evaluation.compare_retrieval --baseline .runtime/rag_accuracy/runs/baseline_legacy_v1.jsonl --candidate .runtime/rag_accuracy/runs/baseline_structured_v1.jsonl --output docs/rag_accuracy/01_paired_results.md
```

不加 `--execute` 只检查数据；去掉 `--dataset` 使用 20 题 smoke。
`vector_only` 指不使用图谱，仍保留 dense+BM25+RRF，并非纯 dense。
消融可用 `--retrieval-mode dense`、`--no-rerank`，两边必须用相同设置。
拒绝覆盖原始结果；重试使用新文件名，保留失败运行。

每次输出逐题 JSONL、manifest、summary；包括候选、最终 ID、分数空间、来源、错误/降级、分阶段耗时。
manifest 固定数据 SHA、语料映射摘要、模型、K、模式、源码指纹。
对比拒绝混用不同数据、语料、Gold、模型与预算。缺参考/无答案题的召回、完整证据、MRR 为 null，
其拒答准确率只能在答案评测中判断。无召回的有答案题是 0；服务故障保留为 failed，已解析 Gold 时按未命中计。

## 真实 Agent 答案评测

```powershell
.\.venv\Scripts\python.exe -m evaluation.answer_eval --max-cases 2 --execute --output .runtime/rag_accuracy/runs/qa_structured_smoke.jsonl
# 可另开独立输出测冻结旧工具，模型与 Prompt 保持相同
.\.venv\Scripts\python.exe -m evaluation.answer_eval --mode legacy --max-cases 2 --execute --output .runtime/rag_accuracy/runs/qa_legacy_smoke.jsonl
# 可选，使用已配置的 RAGAS 裁判及其独立环境
.\.venv-eval\Scripts\python.exe -m evaluation.answer_eval --judge --max-cases 2 --execute --output .runtime/rag_accuracy/runs/qa_judged.jsonl
```

通过真实 Agent 与 search_knowledge_base 工具运行，每题使用全新 Redis 会话，关闭记忆与画像，
不会将实验对话写入原始对话表或触发压缩。仍可能在 Redis 留下带 TTL 的实验检查点。
记录实际回答、工具路由、最终模型可见证据、LLM usage 和总耗时。
没有人工答案审核时 Answer Accuracy、UnsupportedClaimRate 均保持 unavailable，
不把检索指标或 RAGAS F1 改名为答案准确率。

## 测试和清理

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s backend/tests -v
```

真实数据库集成测试读取 `ECHOMIND_TEST_DATABASE_URL` 与 `ECHOMIND_TEST_MILVUS_URL`，
仅在显式设置时运行，自己创建并清理隔离 schema/集合；固定 8 维测试向量不用于质量测量。

```powershell
.\.venv\Scripts\python.exe -m evaluation.rag_corpus status
.\.venv\Scripts\python.exe -m evaluation.rag_corpus cleanup --execute
```

cleanup 只接受受命名保护的测试资源，保留 state 与结果用于审计。
清理后 UUID 映射失效，再导入需重测，不能与之前结果配对。
Windows 受限沙箱若连 `asyncio.run(asyncio.sleep(0))` 都挂起，可用 faulthandler 定位；
本次确认为沙箱 socketpair 阻塞，沙箱外测试正常。不为此修改项目默认事件循环。
