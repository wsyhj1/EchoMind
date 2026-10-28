# ReAct、上下文与状态编排：一手资料核查与参考校准

调研日期：2026-10-26。范围：设计依据与版本边界；不改变执行代码。项目基线由本次仓库审查提供：`langchain==1.2.14`、`langgraph==1.1.6`；下列“建议”是 EchoMind 的设计选择，不能当作框架默认保证。

## 1. 当前循环如何命名

ReAct 原论文将推理轨迹与外部行动交替组织，用行动获取信息、用反馈更新后续行动。LangChain 官方将其核心循环描述为模型请求工具、执行工具、将结果反馈模型，直到不再请求工具。因此，EchoMind 的 `create_agent` 可称“ReAct 风格的工具反馈循环”；不能仅凭函数名声称严格复现论文提示模板或公开了模型内部推理。[ReAct 原论文](https://arxiv.org/abs/2210.03629)、[Agents](https://docs.langchain.com/oss/python/langchain/agents)

用户指定的卡码笔记将 Loop Engineering 拆为上下文、独立状态/记忆、预算、动态工具、终止五个治理维度，并以可观测性贯穿循环；其核心主张是在保留 ReAct 循环的基础上增加工程治理。本项目采用这五维作为需求组织方式。该来源是工程面试文章，不是行业标准或框架 API 的依据；具体能力和签名仍以官方文档及锁版本源码为准。[用户指定参考，第 5 节](https://notes.kamacoder.com/interview/llm/loop_engineering_interview.html#_5-react-%E2%86%92-loop-engineering-%E6%BC%94%E8%BF%9B%E5%AF%B9%E6%AF%94)

参考文章对“裸 ReAct”的简化对比不能直接套用到当前 EchoMind：仓库审查已确认项目具备 LangGraph checkpoint、4 次模型调用预算、时间/工具上限和 RunTrace；待补的是围绕业务阶段与证据的显式治理。也不沿用文中“Token 成本指数上涨”的概括；实际增长取决于每轮输入、输出、压缩和计费策略，设计应计量实际用量。可借鉴官方 harness 定义，将提示、工具和 middleware 作为循环外层的控制设施。[用户参考](https://notes.kamacoder.com/interview/llm/loop_engineering_interview.html)、[Agents](https://docs.langchain.com/oss/python/langchain/agents)

## 2. 上下文与可变状态必须分开

官方区分静态 runtime context、会话 state、跨会话 store。`context_schema` 用于注入本次调用的身份、连接和配置；循环内变化的数据应通过 state 更新。项目建议保留现有 ContextSchema 的身份、skill 和 profile 快照，将阶段、证据引用、工具状态、预算及终止原因放入独立状态契约；不要把可变进度塞回 runtime context。[Context engineering](https://docs.langchain.com/oss/python/langchain/context-engineering)、[Runtime](https://docs.langchain.com/oss/python/langchain/runtime)、[1.1.6 StateGraph 源码](https://raw.githubusercontent.com/langchain-ai/langgraph/1.1.6/libs/langgraph/langgraph/graph/state.py)

StateGraph 节点返回部分状态更新，reducer 决定多个更新如何合并。建议为字段逐项声明所有者、生命周期、覆盖/合并规则；消息使用消息 reducer，证据按稳定 ID 去重，阶段采用确定性转移，避免任意列表追加造成重复或无限增长。官方 middleware 可在模型、工具调用前后处理上下文与控制流程；先扩展现有 agent，出现明确的跨阶段路由需求时再引入外层图。[Graph API](https://docs.langchain.com/oss/python/langgraph/graph-api)、[Middleware overview](https://docs.langchain.com/oss/python/langchain/middleware/overview)

## 3. 每次模型调用前构建上下文

官方允许临时修改本次模型看到的消息和工具，而不改持久 state。建议构建有预算的输入视图：固定规则、当前请求、已验证证据、必要记忆、近期完整交互；完整工具载荷保留在受控证据层，模型视图只放必要片段及来源 ID。[Context engineering](https://docs.langchain.com/oss/python/langchain/context-engineering)

裁剪必须保留 assistant 的 tool call 与对应 ToolMessage；不能直接截最后 N 条而留下孤立工具结果。摘要用于压缩历史，项目应额外规定“摘要不作为事实凭证”：保留来源、版本、时间与原始证据引用，防止摘要中的解释被反复提升为事实。这一证据规则是项目建议；官方短期记忆文档提供裁剪、删除与摘要机制，并明确消息协议完整性要求。[Short-term memory](https://docs.langchain.com/oss/python/langchain/short-term-memory)

工具反馈建议统一描述 `ok/empty/error/limited`、可重试性、证据 ID 和内容摘要；编排层确定下一步是继续、降级、澄清还是终止。应覆盖空结果、超时、参数错误与次数耗尽，避免把这些字符串当作成功证据。框架提供 middleware 处理工具错误和调用控制，但反馈语义需项目定义。[Middleware overview](https://docs.langchain.com/oss/python/langchain/middleware/overview)

## 4. 持久化、恢复与隔离

Checkpointer 保存单个 thread 的图状态；store 保存跨 thread 的应用数据。建议以服务端授权的用户/会话/业务 scope 解析 thread，读写证据与记忆均检查 scope；thread ID 本身不替代权限验证。RunTrace 应继续承担审计职责，不能把存在审计日志等同于存在可恢复 checkpoint。[Persistence](https://docs.langchain.com/oss/python/langgraph/persistence)

恢复可能重放节点或任务，并非从中断的那行继续。已开始却未完成的任务可能再次执行，因此 checkpoint 不保证外部副作用 exactly-once。未来记忆写入等操作应有幂等键和结果核对，并与可重放计算分离。[Functional API：replay 与 idempotency](https://docs.langchain.com/oss/python/langgraph/functional-api#idempotency)

`interrupt()` 需要 checkpointer 与稳定 thread ID，并用 `Command(resume=...)` 继续。建议仅在确需外部输入时使用；恢复还应定义 checkpoint 过期、取消和版本变化的行为。框架允许等待外部输入，但具体 Redis 保留期与业务等待期必须另行核验，不能承诺永久可恢复。[Interrupts](https://docs.langchain.com/oss/python/langgraph/interrupts)

## 5. 锁版本与开发门槛

已经核查官方 **1.2.14 factory 源码**含 `state_schema`、`context_schema`、middleware 状态合并及 StateGraph 构造；这支持在现有 agent 上扩展状态的方案。具体 hook 签名、异步实现、provider 消息协议仍应在开发阶段按锁版本验证。[LangChain 1.2.14 factory](https://raw.githubusercontent.com/langchain-ai/langchain/langchain%3D%3D1.2.14/libs/langchain_v1/langchain/agents/factory.py)

**不要照抄最新文档的 streaming 示例。** 当前官方推荐的 typed-projection event streaming 是 LangGraph **1.2 新增**，部分页面已使用 `stream_events(..., version="v3")`。锁定的 **1.1.6 源码**可核查 `stream/astream`、`version="v1"/"v2"` 与 `updates/messages/custom` 模式；设计应沿该版本接口，若采用新接口需单独升级验证。[Streaming 版本说明](https://docs.langchain.com/oss/python/langgraph/streaming)、[LangGraph 1.1.6 runtime](https://raw.githubusercontent.com/langchain-ai/langgraph/1.1.6/libs/langgraph/langgraph/pregel/main.py)

建议将现有 120 秒、4 次模型调用和工具次数上限保留为硬约束，补充上下文 token、证据条数、重复无进展与取消检查；任何终止都产出机器可读原因。图 recursion limit 是最后保护，不能替代业务完成条件与全链路预算。[Graph API：recursion limit](https://docs.langchain.com/oss/python/langgraph/graph-api#recursion-limit)

开发验收应覆盖：裁剪后工具消息成对、摘要后证据仍可定位、空/错/限流反馈、预算耗尽、跨 scope 隔离、同 thread 并发、恢复重复写入及 checkpoint 过期。本次只做资料与设计核查，未调用模型、数据库或运行服务。
