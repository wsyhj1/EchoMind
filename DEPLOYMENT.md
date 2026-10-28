# EchoMind Windows 本地部署

目录：`E:\codex demo\Echo Mind`。前后端与三个数据库均已启动。

- 前端：<http://localhost:8501>
- 后端文档：<http://localhost:8000/docs>
- 应用设置：侧边栏「新对话」右侧齿轮，可配置 RAG、画像与记忆；见 [设置说明](docs/settings.md)。
- 逐轮计时与 RAGAS：见 [评测使用说明](evaluation/README.md)。质量评测需额外启动 `.\start.ps1 -Service Evaluator`，当前已在后台运行。
- 本地配置及密钥：`.env`，已加入 Git 忽略。
- 数据库数据、临时文件、运行日志和 Python 下载缓存：`.runtime`。
- Docker 镜像由本机 Docker Desktop 管理；原有容器和镜像保留。

## 使用的环境

| 组件 | 部署方式 |
| --- | --- |
| Python 3.12 | 本机 Python，独立环境 `.venv`，按 `requirements.txt` 安装 |
| Milvus 2.5.10 | 复用已有 `milvusdb/milvus:v2.5.10` 镜像，使用项目独立数据目录 |
| Redis Stack 7.4 | 下载 `redis/redis-stack-server:7.4.0-v8`；实测 Redis 7.4.7，含 RedisJSON 与 Search |
| PostgreSQL 16 | 下载 `postgres:16-alpine`，随机密码已写入 `.env` |

本机 `D:\redis` 为 Redis 5.0.14.1，保持原样。项目对话检查点依赖 RedisJSON 和 Search，因此使用独立 Redis Stack。

曾开始下载 Milvus 2.6.10，随后停止并改用已有 2.5.10；Docker 中可能保留部分下载缓存。最终未使用新版镜像。

## 后续启动

先打开 Docker Desktop，在项目目录的 PowerShell 执行：

```powershell
.\start.ps1 -Service Databases
```

该命令禁止自动拉取镜像。然后在两个终端分别执行：

```powershell
.\start.ps1 -Service Backend
```

```powershell
.\start.ps1 -Service Frontend
```

若脚本执行策略阻止运行，可单次使用 `powershell -NoProfile -ExecutionPolicy Bypass -File .\start.ps1 -Service Frontend`，其他服务同理，无需修改全局策略。

`-Service Databases -PullImages` 会提示并下载 PostgreSQL 与 Redis Stack；Milvus 继续复用现有镜像。

## 停止与检查

本次后台运行的应用和数据库可这样停止，数据库数据保留：

```powershell
.\stop.ps1 -Databases
```

前台终端中的服务按 Ctrl+C 停止。后台 PID 文件位于 `.runtime/frontend.pid`、`.runtime/backend.pid`；对应日志位于 `.runtime/logs`。

```powershell
docker compose ps
docker compose logs --tail 100
.\.venv\Scripts\python.exe -m pip check
```

## 验证与本地修正

- 三个数据库容器均健康，后端完成 PostgreSQL 建表与两个 Milvus 集合初始化。
- 前后端 HTTP 检查通过，DashScope 认证和连续两轮真实问答通过。
- 已为默认用户 1 创建“默认知识库”。服务仅绑定本机访问。
- 修正四个字符串字段使用 `STL_SORT` 的错误，改用 `INVERTED`，通过 Milvus 2.5.10 实测。依据：[Milvus 标量索引文档](https://milvus.io/docs/v2.5.x/scalar_index.md)。
- 真实数据库集合初始化回归检查修复前失败、修复后通过。以下检查不调用模型 API，使用随机临时集合并自动清理：

```powershell
$env:ECHOMIND_TEST_MILVUS_URL = 'http://127.0.0.1:19530'
.\.venv\Scripts\python.exe -m unittest discover -s backend/tests -p test_milvus_schema.py
```

## Docker 启动问题

旧 socket 目录已由用户手动运行修复脚本备份。结束遗留 `docker-sandbox.exe` 进程并重启 Docker 实例后，引擎已恢复。日志位于 `.runtime/logs/docker-repair.log`。`deploy/repair-docker-socket.ps1` 只用于该 socket 故障，不用于日常启动。

Ubuntu-24.04 发行版曾报告其 VHDX 路径不存在，该问题独立于当前运行正常的 Docker 实例。本次未重装或注销发行版。
