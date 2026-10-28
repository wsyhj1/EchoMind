# 图谱管理、许可与断点恢复

新增管理路由由主应用统一身份中间件保护。客户端 user_id 必须与服务端可信身份一致；独立挂载 router 而不设置可信身份时返回 401。接口只接受真实知识库，拒绝“默认知识库”作为写入范围。

管理 GET 不调用模型、不执行 DDL；提交只排队，模型抽取由后台 worker 执行。本模块子代理开发验证未发起真实模型请求；随后主代理依据用户既有授权验证了作品报告v3任务，见 [本轮验收](rag_accuracy/06_improvements_acceptance.md)。真实 `.env` 和个人图谱开关未变。

## 接口约定

`GET /graph-status?user_id=1&knowledge_base_id=实际知识库` 返回：

- `service`：仅 `host`、`model`、`target_id`；不公开完整 URL、账号、令牌、API key。
- `extraction_version`：新任务的配置版本；默认 v3，可显式 pin v2。v3 兼容读取已有 v2，按每文件最高 READY 版本确定 `effective_ready_version`，不自动重抽旧文件。
- `schema_ready`、`limits`、`documents`；`limit` 为 1..100，默认 50，`offset` 支持分页。
- 每文档包含 file_hash、file_name、job_id、status、error_type、parents_total、parents_completed、progress_verified、generation、model_calls、lifetime_model_calls、max_parents、max_calls、cancel_requested、explicit_requested 和 effective_ready_version。
- 每文档还包含 raw_relations/usable_relations/rejected_relations、raw_entities/usable_entities/rejected_entities。`usable` 表示当前 READY 版本、当前原文与线上语义守卫允许的资格，不保证任意问题都会召回。`rejected` 是原始存储减当前有资格数，包含旧版被遮蔽、作业未就绪及语义拒绝，并不全是模型错误。没有任何可用关系的孤立实体不计入 usable_entities。
- 每文件最多评估 1000 条当前版本关系；超过时 `counts_complete=false`，usable/rejected 返回 null，不能把截断样本当精确总数。原始存储总数仍由 SQL 聚合计算。

旧索引没有 checkpoint 时，READY 的进度来自既有完成状态，`progress_verified=false`；迁移无法补造历史全文哈希。新任务逐块核对当前文本 SHA256，进行中进度只统计当前配置可复用的完成块。

提交单文件：

```json
POST /graph-jobs
{
  "user_id": 1,
  "knowledge_base_id": "实际知识库",
  "file_hash": "64位小写SHA256",
  "external_model_consent": true,
  "consent_target": "刚才GET中service.target_id的64位值",
  "force": false
}
```

用户必须先看到服务 host/model 并明确勾选许可。`external_model_consent` 使用严格布尔值，字符串 `"true"` 不合法。`consent_target` 将这次许可绑定到显示时的服务配置；GET 之后服务改变，即使同主机但路径改变也返回 409 `ConsentTargetChanged`，不入队。作业审计记录 host/model、指纹和许可时间；worker 在每次实际模型请求预留预算前再次核对配置，排队后目标改变也会失败，需刷新目标并重新同意。

READY 重建必须 `force=true`。当前版本已有 PENDING/RUNNING 的重复提交幂等；一个原来等待自动抽取的 PENDING 作业，收到明确许可后会转为显式管理队列。FAILED/取消后的重试会复用仍然合格的断点，不重置累计调用预算。客户端不能提高预算。

`POST /graph-jobs/cancel` 请求体只需要 user_id、knowledge_base_id、file_hash，不需要外发许可。它只取消该用户/真实知识库/文件的未完成兼容版本作业，不删除已发布图边；没有未完成任务且文件已经 READY 时返回 409。PENDING 立即进入 STALE/GraphJobCancelled；RUNNING 先标记 cancel_requested，后台观察器随后停止。已经发送给外部服务的请求无法撤回，取消确认的保证是禁止新的预算预留，并取消本地在途任务，不是删除服务商已经接收的数据。

## 后台队列与预算

主应用应始终启动 graph.worker；它在图表不存在时空等，不初始化图表或外发文档。显式队列优先处理，即使 `GRAPH_EXTRACTION_ENABLED=false` 也会运行用户已经明确提交的单文件作业；未明确授权的旧 PENDING 自动任务只在全局自动抽取开启时处理。默认保持全局自动抽取关闭。

- 每个 generation 最多 200 父块、400 次模型请求预留；服务端可用 `GRAPH_JOB_MAX_PARENTS`/`GRAPH_JOB_MAX_CALLS` 下调，不能突破硬上限。空文档或父块超限在提交时拒绝，后台也再次核对。
- 每一次真实模型 create 前调用预算钩子，包括格式重试、网络重试；不存在“重试不计数”。原抽取仍最多两次尝试，预算/租约/取消异常不进入模型重试。
- model_calls 是保守的预留次数，包含可能在真正发送前取消的请求，不等同于计费成功请求或 Token。lifetime_model_calls 是新增管理机制后的累计预留，不能追溯补造旧 003 索引的历史账单。
- FAILED 的再次提交保持同 generation 和 model_calls；即使带 force 也不能给失败任务清零预算。预算耗尽返回 409，需人工检查，而不是无限重试。
- 只有已经 READY 且明确 force 的重建开启新 generation、清旧 checkpoint 并重置本 generation 计数；lifetime 仍保留。用户主动发起的新版本任务拥有独立 job_id/预算。
- 取消观察默认约每 200 ms 检查，单次数据库检查最多 2 秒；每个新请求仍先原子核对 lease/cancel/剩余预算。故障时保守停止，不靠后台循环无限加次数。

## 持久断点与一致性

005 新建 kg_parent_checkpoints，并在 kg_index_jobs 添加管理字段；003 文件及其 checksum 未修改。GraphRepository 按 003/005/006 顺序分别校验并事务迁移，不会代替主应用执行 007 身份或 008 引用迁移。

每个断点绑定 job_id、user/KB/file、version、generation、父块 SHA256 和服务配置指纹。写入必须持有当前有效租约，并再次读取真实父块核对全文。恢复时只有当前新租约可以接管旧成功断点，旧租约无法预留调用或覆盖结果；原文变化、版本不同、范围不同及配置不同均不复用。

每个父块成功即持久化；同批另一块失败时，已提交的成功断点保留，其余并发任务被取消。最终图谱仍通过原 GraphIndexer → GraphRepository.publish 完整原子发布：再次校验当前租约、cancel、父块顺序与全文，最后才 READY。不会把部分 checkpoint 当作已完成图谱。

旧 003-only schema 的索引测试仍可走原兼容路径；它没有管理许可、断点和预算能力，不能据此宣称新管理已就绪。正式管理提交及应用启动迁移应先应用 005。

## CLI

```powershell
# status 输出管理目标及 service.target_id，不调用模型。
.\.venv\Scripts\python.exe -m backend.graph_cli status --user-id 1 --knowledge-base-id 实际知识库
# queue 默认 dry-run；execute 也只排入独立显式队列。
.\.venv\Scripts\python.exe -m backend.graph_cli queue --user-id 1 --knowledge-base-id 实际知识库 --file-hash 实际64位哈希
.\.venv\Scripts\python.exe -m backend.graph_cli queue --user-id 1 --knowledge-base-id 实际知识库 --file-hash 实际64位哈希 --consent-external-model --consent-target 刚才显示的target_id --execute
.\.venv\Scripts\python.exe -m backend.graph_cli cancel --user-id 1 --knowledge-base-id 实际知识库 --file-hash 实际64位哈希 --execute
```

原 reindex --execute 仍是运维人员显式执行的同步回填入口，遵守当前硬预算与断点；READY force 重建会清断点。它不是管理 UI 的隐式回填，也不会在代码更新后自动调用。

## 验证范围

相关回归覆盖真实 Pydantic/GraphExtractor 钩子（模型响应为 mock）、随机隔离 PostgreSQL 的许可/作用域/幂等、断点复用与内容变化、旧租约和跨版本隔离、预算耗尽、取消、全局关闭下显式队列和未授权自动队列隔离、旧 003-only 兼容及身份保护。未以真实业务文档调用云模型，不把以前 72 次问答等同于本次管理代码的效果实验。

2026-10-24 本子代理验证：完整图谱模块 58 项通过（78.610 秒）；之后对进度统计及有界收尾的最后改动，管理专项 16 项重新通过（38.772 秒）。编译与相关文件 diff 检查通过。以上运行全部模拟外部模型，数据库写入仅随机隔离 schema。主代理随后进行的真实显式构建/集成验收应单独记录，不能据此回写本子代理为真实外发执行者。
