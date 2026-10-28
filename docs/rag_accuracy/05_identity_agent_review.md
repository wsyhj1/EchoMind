# 身份隔离独立代理复核

2026-10-24。本审核由子代理对照磁盘实现与本地mock测试完成，不代表真人安全审计或完整渗透测试。未创建真实账号、修改.env、请求真实用户数据或调用外部服务。主代理负责修复实现；本子代理仅新增独立测试。

## 已发现并修复后复验的问题

| 问题 | 原触发与影响 | 修复后的独立验证 |
|---|---|---|
| PATCH绕过认证封装 | APIRequests缺少patch方法，__getattr__返回原requests.patch；画像两个设置使用PATCH，在账号模式下不带Bearer，返回401。本机模式与只mock requests.patch的旧测试难发现。 | get/post/put/patch/delete全部验证同源Bearer与禁重定向，通过。 |
| 重定向策略可被覆盖 | allow_redirects用setdefault设置，调用方显式True可覆盖“不携带session凭证跟随跳转”的策略。不是已经观察到外站收到真实令牌。 | 同源调用显式True仍强制False，通过。 |
| 显式头可越过跨源边界 | 异源URL不会自动注入session令牌，但原实现保留显式传入的Authorization。这使捕获头传给外站的组合突破封装承诺；正常业务调用没有观察到该组合。 | 异源显式Bearer被移除，Accept保留，通过；主代理同时移除Cookie。 |
| 令牌签发源未绑定 | 原bootstrap先覆盖API base，再命中60秒旧身份缓存；配置改变后可把A的令牌注入B。固定部署未观察到外部攻击入口。 | 令牌绑定签发origin；未绑定、base改变均不发送；bootstrap跨源先清空身份、画像、历史run缓存及queryparams，再发请求，通过。 |

独立测试frontend/tests/test_identity_transport_review.py首轮4项中3项失败，主代理修复后扩展为7项，7项全部通过。测试全用虚构令牌与mock HTTP，不发生网络访问。

## 后端请求身份与路由

本机模式同时验证直接连接loopback、Host本机名、Origin白名单与cross-site提示；账号模式从Bearer会话解析服务端user_id。中间件拒绝所有query中不匹配的user_id，包括重复值，再统一注入可信ID。仅修改body不会重写服务端身份：上传表单及图谱提交/取消的body user_id还要经过require_matching_user。

业务路由、运行与评测、图谱管理和引用路由均位于IdentityMiddleware之后。公开路由限身份状态/登录、健康与Swagger规范页面，不返回用户业务记录。未知身份模式关闭访问；缺失、过期或禁用会话不会进入受保护handler。没有发现通过query/body重复参数或未认证业务路由访问另一用户的路径。

独立执行原backend/tests/test_auth.py：5项本地/ASGI mock与密码测试通过，2项PG会话测试明确跳过，不把跳过记为通过。它们覆盖query/form伪造、远端peer/Host/Origin、本机边界、Bearer身份、未知配置和密码hash参数。

## 线程、会话、缓存与删除快照

后台操作在主线程捕获当时的user_id、headers和当前会话Queue，再交给线程；线程不读取Streamlit session_state。回调写入捕获的Queue并带user_id，界面只消费当前Queue且再次校验user_id。登录、退出及身份切换清空session_state，从而使旧线程持有的Queue与新工作区脱离。没有发现全局Queue造成跨用户结果复用。

同步HTTP的会话令牌逐次读取，不保存在全局requests.Session；后台aiohttp使用显式捕获头。检查了本地已安装aiohttp的重定向代码，跨origin会移除Authorization、Cookie与Proxy-Authorization；未观察到后台跨host转发Bearer。此为源码检查，不是生产代理链流量测试。

画像与历史run等缓存位于session_state，身份切换清空。当前引用原文接口每次重新校验user、知识库、parent、file_hash、当前正文及删除任务，不使用旧snapshot给来源继续背书。历史运行快照仍可供同一账号查看，界面明确标注为历史审计；这属于保留策略，不等于删除文档时同时删除历史聊天/审计快照。若产品另要求彻底擦除历史文本，需独立定义数据保留与清理范围。

## 账号分配器

只读代码确认新账号不接收远程调用者指定的ID。创建时锁users表，比较数据库序列与已导入的最大user_id；落后时先setval到最大，再使用INSERT DEFAULT VALUES分配。已有用户的绑定仅由本机管理员CLI显式指定，auth_accounts主键防止重复绑定。没有发现复用历史用户ID的代码路径。

新增backend/tests/test_identity_allocator_review.py，验证手工导入user_id=120000而序列落后时，新账号ID必须大于120000，旧用户不能被创建账号采用。该用例只允许隔离随机schema；本子代理尚未执行，交由主代理PG全套运行后记录结果。

## 部署边界和残留事项

start.ps1显式绑定前后端到127.0.0.1，推荐启动路径符合本机模式。审核时README仍提供裸streamlit run app.py；若该命令把Streamlit暴露到LAN，浏览器通过前端服务器访问loopback后端，后端只能看到前端peer，不能识别远程浏览器。应把README改为显式loopback绑定，并使远程部署必须选择账号模式；不能用可伪造的浏览器IP提示作为身份认证。此条件不影响当前start.ps1已绑定本机的路径。

此外，citation_service.visible_evidence与engine的逐行JSON识别需保持一致。若用str.splitlines()，U+2028/U+2029/U+0085会把ensure_ascii=False序列化的有效JSON拆碎，导致真实来源漏认。该问题不会放宽授权，但会影响引用审计准确性；应使用ASCII换行边界并补Unicode分隔符回归。

此次没有针对真实账号并发、反向代理、TLS终止、全部网络重定向、跨设备退出传播或浏览器自动填充进行端到端验证；不能从mock通过推出这些边界已全面验收。

## 2026-10-25修复后复验

后续独立复验已完成前端39项及引用13项测试，全部通过且无跳过；引用测试包含隔离PostgreSQL schema。前文在2026-10-24指出的两个待修事项现已修复：前端本机监听限制在fresh、缓存身份及身份返回前均执行，默认配置和README显式绑定127.0.0.1；引用JSON逐行识别改为ASCII换行，U+2028/U+2029/U+0085回归通过。

39项前端测试包含7项认证传输、5项监听策略、4项真实app函数AST导出的后台Queue隔离、4项引用/图谱交互和19项工作台回归。未使用镜像实现代替后台逻辑：独立测试直接执行app中的初始化、任务投递、结果消费与应用函数，确认用户切换后延迟结果仍回到原Queue，且外来user结果不会更新当前工作区。

本次运行探测先在服务切换期间不可达，根代理恢复后重新读取：health、auth/status、auth/me均返回200，运行模式为local、用户为1，前后端8000/8501均监听127.0.0.1。此为只读时间点检查，没有切换真实认证模式、创建真实账号或修改用户资料。完整本次审核范围与仍未验收的真实多用户、TLS、灾备边界见 [最终复核](06_final_identity_operations_agent_review.md)。
