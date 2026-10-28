# 界面按钮延迟排查与修复

日期：2026-10-27。范围：本地界面按钮交互，不涉及模型回复耗时。

## 复现与原因

真实浏览器连续三次点击“设置”，从点击到设置内容出现分别为 2.359、2.204、2.200 秒；“新对话”为 0.202 秒。基准脚本以设置打开中位耗时小于 1 秒为通过条件，修复前实际失败。

对同一个已运行后端，仅切换地址进行对照：

| 请求/连接 | 实测耗时 |
|---|---|
| `http://localhost:8000/health`，三次 | 2.0305、2.0635、2.0315 秒 |
| `http://127.0.0.1:8000/health`，三次 | 0.0036、0.0035、0.0024 秒 |
| TCP 连接 `::1:8000` | 2.0225 秒后拒绝连接 |
| TCP 连接 `127.0.0.1:8000` | 0.0005 秒，成功 |

本机 `localhost` 的地址顺序为 `::1`、`127.0.0.1`，而后端按启动脚本仅监听 IPv4 回环地址。前端每次使用默认 `localhost` 发起新 API 请求，都会先等待 IPv6 连接失败。打开设置会读取最新设置，因此稳定触发这一延迟。身份、会话目录或画像缓存需要刷新时，其他交互也可能遇到相同等待。

Streamlit 仍有服务端重跑和页面渲染开销；本次实测主要额外耗时来自建连回退，不是模型调用。

## 修改

- `frontend/api_client.py` 集中定义 `DEFAULT_API_BASE_URL = 'http://127.0.0.1:8000'`。
- `frontend/app.py` 使用该值作为 `ECHOMIND_API_BASE_URL` 未设置时的默认地址；传输层的缺省地址保持一致。
- `.env.example` 同步示例。真实 `.env`、用户设置与后端监听范围没有修改。
- 显式配置的 URL 保留原样，`localhost` 与 `127.0.0.1` 仍按不同 origin 处理，不自动跨地址转移令牌，也不放宽重定向/认证约束。

前端已重启以清除旧模块缓存并加载新默认值。现有会话仍从持久记录恢复。

## 验证

- 回归用例 `frontend/tests/test_button_latency.py` 通过真实页面的设置按钮检查请求目标。修复前断言收到 `localhost` 而失败；修复后通过，并确认显式后端配置不会被覆盖。
- 全部 59 项前端测试通过，覆盖已有身份、origin、技能、会话、设置与引用交互。
- 子代理只读审核通过，并独立重跑 7 项身份传输检查；确认未合并 origin、未放宽令牌或重定向边界。该组检查属于已有前端覆盖，不额外叠加到59项数量。
- 同一浏览器基准复测，打开设置为 0.428、0.334、0.250 秒，中位数由 2.204 秒降到 0.334 秒；关闭设置与新对话均低于 0.52 秒。该数据是本机本轮测量，不保证任意负载或任意长历史都有相同耗时。
- 测量没有发送聊天、评测或建图请求，没有外部模型调用，也没有保存或修改用户配置。临时浏览器页自动关闭。

本地复核命令：

```powershell
.\.venv\Scripts\python.exe .runtime/ui-review/button-latency.py --tag after
.\.venv\Scripts\python.exe .runtime/run_conversation_checks.py --frontend
```

基准依赖已运行的本地 8501 前端、8000 后端及专用 9227 浏览器调试实例；不在普通用户启动时执行。原始记录位于 `.runtime/button_latency_before.json`、`.runtime/button_latency_after.json`、`.runtime/button_latency_transport_before.json`、`.runtime/button_latency_tests_before.log` 和 `.runtime/button_latency_frontend_checks.log`。

后续若某个特定按钮仍慢，应按该按钮重新测量 API、重绘和历史渲染耗时。应用内多标签、后台流与 React 迁移继续按原计划评估，不作为本问题的必要修复。
