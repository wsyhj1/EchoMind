# 图谱启用 回填和回滚

阶段二交付时默认保持原有行为，实验只操作隔离语料。2026-10-23 的代理审核、
CLI修正与受控真实库启用另见 [第四阶段执行记录](04_activation_report.md)，不要把历史交付状态当作实时运行状态。

## 本地操作

从仓库根目录运行，先只读检查。

```powershell
.\.venv\Scripts\python.exe -m backend.graph_cli reindex --user-id 1001 --knowledge-base-id demo_kb
.\.venv\Scripts\python.exe -m backend.graph_cli status --user-id 1001 --knowledge-base-id demo_kb
```

reindex 默认 dry-run。执行会调用配置的抽取模型并写入当前数据库的图表：

```powershell
.\.venv\Scripts\python.exe -m backend.graph_cli migrate --execute
.\.venv\Scripts\python.exe -m backend.graph_cli reindex --user-id 1001 --knowledge-base-id demo_kb --max-files 10 --execute
# 指定文件、失败重试或显式重建
.\.venv\Scripts\python.exe -m backend.graph_cli reindex --user-id 1001 --knowledge-base-id demo_kb --file-hash 实际文件SHA256 --force --execute
```

默认最多处理 20 文件，可设 1..100。文件筛选、作业认领和状态查询均限制用户及实际知识库，
不接受“默认知识库”作存储范围。READY 同版本默认跳过，`--force` 才重建；变更抽取规则应增加版本。
多次执行普通回填会跳过已 READY 文件并继续后续批次；FAILED/STALE/租约耗尽作业需要显式 `--force`，不占普通批次名额。
每批只认领计划中选中的文件。退出码 0 表示请求范围全部 READY（或空库）；1 表示失败、需显式重试或指定文件不存在；2 表示本批成功但仍有后续或运行中任务。
不能把“本次未认领”解释成完成。
`status` 同时显示图索引状态和删除补偿任务，FAILED、STALE、PENDING 都不能冒充 READY。

启用新的上传抽取与在线查询可分别配置：

```dotenv
GRAPH_EXTRACTION_ENABLED=true
GRAPH_RAG_ENABLED=true
GRAPH_EXTRACTION_VERSION=v2
GRAPH_MAX_HOPS=2
GRAPH_MAX_SEEDS=3
GRAPH_MAX_RELATIONS=20
GRAPH_MAX_EVIDENCE_PARENTS=5
GRAPH_QUERY_TIMEOUT_MS=400
GRAPH_BRIDGE_QUOTA=2
# 可独立指定，否则使用 AGENT_BASE_MODEL
# GRAPH_EXTRACTION_MODEL=你的可用模型
```

重启后端以加载 `.env` 与新后台 worker。关系不完整或索引未就绪时自动使用原检索。
历史数据需要显式 CLI 回填，新上传在原索引完成后自动入队，不会把 HTTP 上传成功等同图谱 READY。

## 删除部分失败

原 API 会如实返回 PG 已删、Milvus 失败。后台补偿有重试上限，必要时显式重试：

```powershell
.\.venv\Scripts\python.exe -m backend.graph_cli retry-deletes --user-id 1001 --knowledge-base-id demo_kb
.\.venv\Scripts\python.exe -m backend.graph_cli retry-deletes --user-id 1001 --knowledge-base-id demo_kb --execute
```

知识库范围的未完成清理会暂时阻止同名重建和新上传。不要手工删除补偿行后立刻重传。

## 隔离实验

```powershell
.\.venv\Scripts\python.exe -m evaluation.graph_benchmark
.\.venv\Scripts\python.exe -m evaluation.graph_benchmark --execute --output .runtime/rag_accuracy/phase2/index_v2_snapshot.json
.\.venv\Scripts\python.exe -m evaluation.retrieval --dataset evaluation/datasets/rag_v1.jsonl --mode vector_plus_graph --execute --output .runtime/rag_accuracy/phase2/graph_v2_100.jsonl
.\.venv\Scripts\python.exe -m evaluation.compare_retrieval --baseline .runtime/rag_accuracy/runs/baseline_structured_v1.jsonl --candidate .runtime/rag_accuracy/phase2/graph_v2_100.jsonl --output docs/rag_accuracy/02_paired_results.md
```

首个命令不调用模型。后续命令复用 corpus.json 的独立 PG schema 与 Milvus 集合，不重建父块 UUID。
对照仍固定 K、模型、原文和同一 Gold。原始运行文件不可覆盖，重跑使用新名字。
Gold 仍待人工审核，不能把这次工程性对照当正式生产准确率。

## 回滚

对目标用户，在设置中将图谱辅助召回显式设为关闭，从下一次提问生效。
`GRAPH_RAG_ENABLED=false` 只改变跟随服务默认的用户，不能覆盖个人已保存的 true。
将 `GRAPH_EXTRACTION_ENABLED=false` 并重启后端可停止后台新抽取；显式CLI回填仍需独立控制。
不需要删除图表或更改 Milvus Schema，现有向量检索继续运行。
保留图表可继续完成删除补偿；复合外键会阻止越权图关系。
阶段三已实现独立可选的证据检查与一次补检，见 [证据检查说明](03_evidence_check.md)。
