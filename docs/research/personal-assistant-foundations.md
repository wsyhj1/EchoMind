# 工作与学习个人助手：技术基础核对

核对日期：2026-10-27。本文解释技术依据与设计推导，具体交付状态以实现和验收文档为准。读取了本地依赖锁和已有代码，没有读取密钥、连接外部个人账户或调用付费模型。

## 1. 用请求模型确定输入边界

FastAPI 官方说明：请求体声明为 Pydantic 模型后，框架会读取 JSON、转换对应类型、验证数据并生成 OpenAPI schema。因此，任务标题、状态、预计分钟数和版本号应有明确字段与限制，不能直接把任意模型输出写入数据库。[FastAPI 请求体](https://fastapi.tiangolo.com/tutorial/body/)

本项目推导：语法合法不代表业务合法。后端仍须判断任务属于谁、项目是否存在、状态是否允许改变。身份从已有认证上下文取得，不能让模型指定其他人的 `user_id`。

## 2. 用事务保存业务事实与来源

PostgreSQL 事务把多个步骤合为整体：全部成功或全部撤销；其他事务看不到中间未完成状态。提交成功的记录具有数据库持久化保障。[PostgreSQL 事务](https://www.postgresql.org/docs/current/tutorial-transactions.html)

本项目推导：更新任务时，把新状态与“谁、何时、根据什么作出修改”的事件放进同一事务，避免任务变了却没有来源。项目、任务和决策需要准确查询与关联，适合存关系表；向量库负责找相关资料，不能独自承担任务状态真值。用户说“做完了”是用户报告完成，不等于系统独立验证成果。

## 3. 用版本号阻止旧页面覆盖新进度

PostgreSQL 默认 Read Committed；普通查询按语句取得快照，同一事务内连续查询可能看到不同的并发提交。并发更新同一行时，等待后的更新会重新判断 `WHERE` 条件。[PostgreSQL 隔离级别](https://www.postgresql.org/docs/current/transaction-iso.html)

因此可以设计条件更新：

```sql
UPDATE assistant_tasks
SET status = $1, revision = revision + 1
WHERE user_id = $2 AND task_id = $3 AND revision = $4
RETURNING task_id, revision;
```

这是示意表名，并非数据库已建成的声明。只有客户端所见版本仍有效才更新；返回零行后区分不存在和版本冲突，冲突要求重载。当前 `backend/profile_store.py` 已采用同类版本检查，可沿用其思路；不要在应用里先读版本、再无条件写入。

## 4. 区分模型看到的参数与服务端上下文

LangChain 工具文档区分：`state` 是会话中的可变状态；`context` 是每次调用传入的不可变配置；持久 `store` 可跨会话保存信息。`ToolRuntime` 参数由运行时注入，不进入发给模型的工具参数 schema。[LangChain 工具](https://docs.langchain.com/oss/python/langchain/tools)

本项目已有 `backend/schemas.py` 的冻结 `ContextSchema`，以及读取 `runtime.context.user_id` 的工具。新增项目工具应继续从服务端上下文取得用户和允许项目，再验证模型提交的标题、状态等业务参数。模型负责选择工具和组织语言，数据库事务负责确认实际发生了什么。工具失败就返回失败，不能以生成的“已完成”代替写入成功。

## 5. 检查点不等于项目记忆

LangGraph 官方把 checkpointer 定位为单个 thread 的图状态持久化；store 保存图状态之外的应用数据，适合跨 thread 信息。两者服务的范围不同。[LangGraph 持久化](https://docs.langchain.com/oss/python/langgraph/persistence)

本项目推导：新聊天绑定同一项目时，重新读取数据库中的项目、未完成任务和近期决策，构成恢复工作摘要。不要要求用户回到唯一旧会话，也不要把摘要生成结果覆盖权威状态。工具调用增加业务操作键，把操作回执与变更事务化保存；重复请求复用回执，同键不同参数拒绝。检查点本身不提供业务写入恰好一次保证。若以后接外部服务，还需要另行处理外部调用成功、本地回执未提交的故障窗口。

## 6. 前端状态用于交互，后端记录用于恢复

Streamlit 的 `session_state` 跨同一用户会话的 rerun 保存变量；回调先执行，然后脚本从头运行。官方明确说明其绑定 WebSocket，浏览器重载等操作会重置连接及相应状态。[Streamlit Session State](https://docs.streamlit.io/develop/api-reference/caching-and-state/st.session_state)

本项目推导：选中项目、未提交输入和操作提示放前端会话状态；任务和项目进度由API保存。写操作放在明确提交事件中，页面重绘只读取；以持久操作键处理重试，不能靠按钮瞬时状态防重复。刷新后按服务端身份和项目标识重新加载，切换用户时清理前端旧数据。

## 贯通一次“记录任务”

用户明确要求记录任务 → 请求模型校验 → 服务端确定身份和项目 → 业务层校验状态 → 事务保存任务、来源事件与操作回执 → 返回实际结果 → 界面重载。随后另一段聊天读取同一项目，恢复任务与决策。

“建议下周做实验”与“把实验加入待办”表达不同意图。把生成建议存成草稿，只有明确执行要求或用户采纳才成为正式任务，是本方案的产品设计，不是框架自动提供的行为。

## 与当前版本的关系

本地锁定 FastAPI 0.135.3、Pydantic 2.12.5、asyncpg 0.31.0、LangChain 1.2.14、LangGraph 1.1.6、Streamlit 1.56.0。上面链接是核对时的在线官方文档，可能随发布变化；只采用与本地已有 API 一致的基础机制，不为阅读示例升级依赖。尤其不要照抄官方较新示例中的模型名或新增接口。

建议按“请求模型 → 存储方法 → API → 前端 → Agent 工具 → 恢复摘要”顺序阅读新实现，并验证：刷新后仍存在、两个会话共享项目、旧版本无法覆盖、新旧用户互不可见、同一请求重试不重复、失败操作不会显示成功。
