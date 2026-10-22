# Agent 核心链路测试

在项目 `app` 目录运行：

```powershell
.\venv\Scripts\python.exe -m tests.run_agent_tests
```

默认执行 P0/P1 mock 回归，并将真实 MySQL 用例标记为 skipped；不调用 LLM、不访问业务数据库。
现在也可以运行 `python -m unittest discover -q`；旧手动脚本在导入时不再自行连接 Redis 或启动执行线程。

需要验证真实持久化时，显式开启隔离集成测试：

```powershell
$env:RUN_AGENT_MYSQL_TESTS = '1'
.\venv\Scripts\python.exe -m tests.run_agent_tests
Remove-Item Env:RUN_AGENT_MYSQL_TESTS
```

集成测试创建随机命名的 `p2_agent_test_<12位十六进制>` 数据库，建表并插入专用测试数据，完成后仅删除自己创建的该数据库。需要 MySQL 测试账号具有 `CREATE DATABASE`、建表和 `DROP DATABASE` 权限。Input Analyzer 的 LLM 路由和参数提取边界用确定性替身代替，但 Analyzer 函数、Task Service、Context、订单解析、Preview、Confirmation Service、Action Executor、售后 Service、Repository 和 Step Trace 均实际运行。

测试分类：

| 文件 | 类型 | 覆盖 |
| --- | --- | --- |
| `test_confirmation_p0.py` | mock | 状态机、确认、回放、权限、执行失败、条件 SQL |
| `test_agent_step_trace.py` | mock | 步骤顺序、失败、异常、耗时、trace、缺失参数、脱敏、埋点故障 |
| `tests/test_agent_flow.py` | 真实 MySQL（可选） | 完整售后、两轮合并、业务失败、权限、幂等、并发、数据库状态和 Trace |
| `tests/test_infrastructure.py` | mock | 配置优先级与生产校验、日志字段、Redis key/TTL/故障处理、就绪检查 |
| `tests/test_auth_jwt.py` | HTTP + mock | 模拟登录、Bearer Token、过期/篡改、Body 冒用、订单/售后/确认越权 |

项目根目录及 `agent/`、`tools/`、`cache/` 下的其他 `test_*.py` 包含手动冒烟脚本或不适合无条件自动运行的示例。统一入口只加载上表中的稳定用例，不移动或删除这些文件；两个曾在导入时执行操作的脚本现已改为仅显式运行时执行。

未覆盖：真实 LLM 识别质量、真实 Redis 运行时限流、Policy/Graph 分支、进程崩溃恢复及跨进程高压并发。P3/P4 使用 TestClient 验证限流中间件、健康检查和 JWT 身份边界；P4 还在可选隔离 MySQL 用例中验证真实登录与订单/任务归属。原 P0/P1/P2 业务用例仍主要直接调用 FastAPI 路由函数，不覆盖完整网络部署。模拟登录不验证密码或第三方身份，不能用于生产环境。
