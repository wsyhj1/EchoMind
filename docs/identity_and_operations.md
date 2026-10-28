# 身份、检查与运维

默认 `ECHOMIND_AUTH_MODE=local`、`ECHOMIND_LOCAL_USER_ID=1`，保持当前本机工作区。前后端必须只监听 loopback；使用 `start.ps1`。本机模式不是多人账号，不能通过公开代理向多人提供服务。

共享部署先在本机创建账号，再将后端切到 `ECHOMIND_AUTH_MODE=password` 并重启。没有远程注册接口。管理员可以把既有数据绑定到明确的已有用户，或省略参数创建新用户：

```powershell
.\.venv\Scripts\python.exe -m backend.auth_cli create-user --username owner --existing-user-id 1
.\.venv\Scripts\python.exe -m backend.auth_cli create-user --username reader
```

命令交互输入密码，不要把密码放到命令行、Git或日志。密码至少12字符。密码用随机盐和 scrypt（N=2^17、r=8、p=1）存储，参数采用 [OWASP 的 scrypt 建议](https://cheatsheetseries.owasp.org/cheatsheets/Password_Storage_Cheat_Sheet.html)。数据库仅保存不透明会话令牌的 SHA256，12小时过期；退出登录撤销当前会话。登录有限速和并发哈希上限。

账号模式下，前端展示登录表单；服务端从会话决定 user_id，伪造 query/body 的其他用户ID会被拒绝。切换后端或登录身份清除画像、历史、引用、后台结果缓存，令牌仅发送到登录时的API origin。远程部署还需要受信任的HTTPS入口、独立实例密钥管理和限定 `ECHOMIND_ALLOWED_ORIGINS`；不要直接把数据库或无TLS密码登录端口暴露公网。本轮没有为当前用户创建密码账号，也没有更改真实 `.env`。

## 回归与发布记录

```powershell
.\.venv\Scripts\python.exe scripts/run_checks.py
.\.venv\Scripts\python.exe scripts/run_checks.py --integration
.\.venv\Scripts\python.exe deploy/release_manifest.py --output .runtime/releases/release-unique.json
```

默认检查不访问真实模型，集成检查使用 `.env` 的本机数据库地址与隔离随机 schema/collection。可选 RAGAS 适配器需在单独的评测环境检查，不把缺失的可选包记为通过。GitHub Actions执行普通检查；集成检查需本机服务。源码清单记录 HEAD、工作区变更及逐文件哈希，不自动提交现有改动。CI文件已经准备，但未推送或在GitHub实际执行。

## 调用预算与观测

`CHAT_TOTAL_TIMEOUT_SECONDS=120` 在扣除已有耗时后，约束Redis准备、画像读取、Agent准备与生成流；超时后另外给审计落库最多5秒的等待，协作式取消和连接清理不等同于HTTP严格硬停机时限。`CHAT_MAX_MODEL_CALLS=4` 限制发起模型调用次数，`CHAT_MAX_OUTPUT_TOKENS=4096` 限制单次输出。服务器设置上限，客户端不能提高。图谱逐块持久断点和每generation调用预算见 [图谱管理](graph_management.md)。

设置 → 模型与服务 → 运行统计，按需查看当前用户最近24小时的请求数、成功/失败、P50/P95、失败阶段和服务商报告的token；聚合最多最近500条，不展示别人的请求或原始文档。这里只覆盖已保存回答run的model span，不包含独立建图worker、embedding或rerank的费用。模型未返回usage时不能推算费用；这些统计不是账单。

## 冷备份与隔离恢复演练

```powershell
.\.venv\Scripts\python.exe deploy/backup.py backup
# 先正常停止应用；以下命令会暂时停止三个数据库容器。
.\.venv\Scripts\python.exe deploy/backup.py backup --execute --acknowledge-downtime
.\.venv\Scripts\python.exe deploy/backup.py verify --directory .runtime/backups/备份目录
.\.venv\Scripts\python.exe deploy/backup.py restore-drill --directory .runtime/backups/备份目录 --target .runtime/restore-drills/新目录 --execute
```

默认仅dry-run。备份同时停止PostgreSQL、Milvus和Redis，归档本机数据目录，最后重启数据库；不复制 `.env`。数据归档仍包含私有文档、聊天和密码哈希，需像原数据库一样控制访问。恢复先校验SHA256和归档路径，只允许写入新的演练目录，拒绝覆盖真实库、已有目录、符号链接和路径越界。恢复命令仅验证和解包，不自动把数据接回生产服务。

本轮用合成夹具验证归档/恢复、损坏和恶意路径拒绝，没有为正在使用的数据库安排停机，也没有执行真实冷备份或宣称完成数据库级灾难恢复验收。
