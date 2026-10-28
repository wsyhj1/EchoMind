# EchoMind 逐轮计时与 RAGAS 评测

固定语料的 Retriever-only 基线、100 题草稿、真实 Agent 隔离评测与配对报告，见 [RAG 准确性评测](RAG_ACCURACY.md)。

本框架复用现有 PostgreSQL，不需要新增 Docker 镜像。聊天环境在 `.venv`；RAGAS 0.4.3 与兼容依赖在 `.venv-eval`，完整依赖锁见 `requirements.lock.txt`。其中固定 `langchain-community==0.4.1`，避免较新版本移除 VertexAI 模块导致 RAGAS 导入失败。

## 使用

正常启动数据库、后端、前端后，每条新回答下方自动显示小字计时，展开“性能详情”查看各阶段与时间线。这部分不调用额外模型。

质量评测需要另开一个 PowerShell，在项目目录启动：

```powershell
.\start.ps1 -Service Evaluator
```

本次实施已将评测进程和前后端在后台启动。以后重启电脑需重新启动应用服务。用 `.\stop.ps1 -Databases` 可停止本次后台进程与数据库；前台服务按 Ctrl+C 停止。

展开回答下方的“质量评测”，选择指标并点击“评测本条”。默认不对普通聊天自动评分。提交会调用配置的外部裁判模型，产生额外用量；已完成的相同请求直接复用原任务，失败指标可显式重试，最多三次任务尝试。点击“刷新评测结果”读取状态，回答流不会等待评分。

- 可填写标准答案，启用 Context Recall 和 Factual Correctness F1。
- 可查看本轮证据 ID，再填写人工确认的相关父块 ID，并选中 Precision@K / Recall@K。
- 对本应检索的问题勾选相应选项，漏调工具或空召回会记录失败，不静默排除。
- 缺少标注显示“不适用”，API/裁判失败显示“失败”，均不伪造分数。
- 无参考答案的 Context Precision 标明代理指标，和带参考分数分开统计。

页面 URL 中的 `session` 标识当前短期会话。保留该 URL 刷新页面可恢复新框架保存的对话、耗时和最近评测任务。“清除历史”会开启新会话，不删除旧记录、长期记忆或知识库。旧版未采集的对话不会补造耗时。

## 耗时口径

使用单调时钟；首字指首个可见 answer 片段，工具状态不算。回答耗时包括 Redis、Agent、工具和模型执行到本轮结束，不含随后统计落库、质量评分及客户端网络时间。本机往返耗时另外记录。

向量化包含重试等待；单次 embedding、rerank 请求及等待另有 span。模型总耗时包含所有模型调用，最终回答生成指最后一次输出回答的模型请求。知识库和记忆的来源分开保存。同类并行 span 汇总使用时间区间并集，另外记录工作耗时合计；阶段嵌套不能相加。

每轮快照保存原问题、有效 top_k、检索方式、重排序开关、候选和最终上下文的 ID/文本/顺序、模型输入、回答与 token usage（服务商提供时）。评测读取该快照，不重新查询已变更或删除的文档。

## 配置

以下变量可选，写入项目 `.env` 后重启后端及评测进程：

```dotenv
# 默认使用 AGENT_BASE_MODEL；可独立指定裁判模型
EVAL_MODEL=qwen-plus
# 不配置时分别复用 BASE_URL、DASHSCOPE_API_KEY、EMBEDDING_MODEL
# EVAL_BASE_URL=https://dashscope.aliyuncs.com/compatible-mode/v1
# EVAL_API_KEY=...
# EVAL_EMBEDDING_MODEL=text-embedding-v4
EVAL_TIMEOUT_SECONDS=90
```

当前适配使用 OpenAI 兼容聊天与 embedding 接口；已安装版本的 RAGAS factory 内部设置 Instructor JSON 模式，不能再传 `mode=`。每任务最多 50 次 API 请求，每指标最多 60000 输入字符，不悄悄截断实际证据，评分超时/失败与正常聊天分离。API 用量统计包含服务商返回的 prompt/completion/embedding tokens，不承诺固定费用。默认禁用 RAGAS 使用情况上报，不接入托管追踪平台。

对 DashScope 的 Qwen 混合思考模型，评测显式使用 `enable_thinking=false`，以支持非流式 JSON；不改变业务聊天模型。依据：[阿里云思考模式说明](https://www.alibabacloud.com/help/en/model-studio/deep-thinking)。

新环境需先创建 `.venv-eval`，再按 `evaluation/requirements.lock.txt` 安装，安装会下载 Python 包；现有业务 `requirements.txt` 无需改变。

## 离线批量评测

准备 JSONL，每行一个人工审核的样本。可指定已保存的 `run_id`，也可指定问题和知识库以重新执行同一业务链路。示意结构如下，必须替换为真实问题、答案和固定语料版本中的证据 ID：

```json
{"case_id":"kb-001","user_input":"问题","knowledge_base_id":"你的知识库名称","reference":"审核后的标准答案","reference_context_ids":["相关父块UUID"],"requires_retrieval":true,"corpus_version":"snapshot-001"}
```

默认仅校验与显示配置，不调用模型：

```powershell
.\.venv-eval\Scripts\python.exe -m evaluation.batch .\你的数据集.jsonl
```

启动评测进程后，显式执行：

```powershell
.\.venv-eval\Scripts\python.exe -m evaluation.batch .\你的数据集.jsonl --execute --top-k 5
```

可用 `--no-rerank`、`--retrieval-mode dense` 做配对实验。默认最多 50 个样本，可显式设置 `--max-cases`。重新执行时每题使用独立短期会话、关闭画像和记忆工具，不向对话原始表写入实验消息，不触发实验对话压缩。

JSON 和 CSV 报告默认写入 `.runtime/evaluation-reports`，包括逐题记录、状态、失败数、有效样本数、缺标注数、P50/P95 及分数。公平比较需要固定语料/分块版本、裁判模型和参数；不同实验配置分开运行，候选上下文不能冒充最终上下文。父块 UUID 重建后需更新标注。报告没有用系统答案自动生成 ground truth。

Precision@K 使用固定 K 作分母（少返回的位置计未命中）；Recall@K 使用标注相关 ID 去重数量作分母。二者与 RAGAS 的 AP 型 Context Precision、参考答案声明覆盖 Context Recall 分开。分数不是正确概率，不默认合成总分。

## API 与数据

- `GET /runs?user_id=1&session_id=...`：恢复会话。
- `GET /runs/{run_id}?user_id=1`：读取完整快照。
- `POST /runs/{run_id}/evaluations?user_id=1`：提交允许的指标、可选 reference/reference_context_ids/requires_retrieval，返回 202。
- `GET /evaluations/{job_id}?user_id=1`：查看状态与结果。
- `POST /evaluations/{job_id}/retry?user_id=1`：显式重试失败评测。
- `DELETE /runs/{run_id}?user_id=1`：删除该条快照及其关联评测任务，不删除知识库、原始对话或记忆。

`evaluation_runs.snapshot` JSONB 保存 spans/contexts/model_inputs，`evaluation_jobs` 保存任务、租约及结果。worker 使用 PostgreSQL `SKIP LOCKED` 认领、120 秒租约与心跳，重启后可恢复。模型执行在事务外，幂等键包含 run、标注、指标和裁判配置。崩溃恢复是至少一次执行，外部已完成但未落库的调用可能重做；不能保证外部计费严格恰好一次。

数据默认本地保存，但手动评分会将所需证据发送到配置的模型服务。不要将 `.env`、`.runtime`、含私人资料的数据集提交 Git。框架沿用当前本机应用的 user_id 范围过滤，没有新增账户认证系统，仍应只在本机使用。

## 验证命令

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s backend/tests -p test_evaluation_framework.py
.\.venv-eval\Scripts\python.exe -m unittest discover -s evaluation/tests
```

PostgreSQL 集成检查通过 `ECHOMIND_TEST_DATABASE_URL` 指定测试库，只创建随机临时 schema 并在结束后清理。RAGAS 适配回归使用内存 HTTP 传输，不发起外部调用。
