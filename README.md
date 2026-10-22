# 智能客服 Agent 与售后工作流

Python / FastAPI 项目，支持订单和售后查询、售后申请预览与确认、政策证据查询以及人工转接。使用 LangChain 的结构化输出进行意图识别和参数抽取，确定性代码负责工具白名单、订单归属和业务校验；可选 LangGraph 路径。Streamlit 提供用户界面和管理工作台。

## 架构和业务边界

`/agent/chat` 校验用户和会话，识别意图并查询工具，缺少参数时追问；Agent 写操作生成预览并持久化，`/agent/confirm` 确认后执行。Policy RAG 为独立服务，通过 `/rag/query` 返回来源、文件 hash、版本和证据，客服结合订单和演示规则判断是否可以申请。RAG 不直接写售后数据。

MySQL 保存订单、售后、消息、Agent 任务、操作和 trace。确认 ID 作为创建售后的幂等键；`confirmed → executing` 通过条件更新抢占。Agent Task 使用数据库执行归属与租约；后台恢复只根据原确认 ID、幂等结果或持久化操作凭据协调终态，无法证明结果时进入待核查状态，不重放业务写入。取消及类型变更也在执行时检查归属和状态。Redis Lua 对 `/agent` 接口做全局及用户级令牌桶，失败时返回 503。

重要边界：直接 `POST /after-sales` 并不经过 Agent 确认，普通用户的售后状态接口也需要进一步完善角色权限。开发登录仅用于演示，不能作为生产身份认证。业务结果缺少可靠凭据时会保留为 `recovery_review`，需要人工核查，不会自动重试。WebSocket 广播为进程内实现。政策规则为演示性质，不代表正式商家政策。

## 配置与运行

参考 `.env.example`、`requirements.txt`、`DEPLOYMENT.md`。独立运行前配置 MySQL、Redis、模型和可访问的 Policy RAG 服务；示例凭据仅供本地开发。现有 `docker-compose.yml` 的 policy-rag 构建上下文与索引挂载依赖同级 `../demo3_rag_qa` 项目；若使用 Compose，须将独立 RAG 仓库克隆至该相邻路径并自行准备索引与配置。仓库不包含本地数据库、索引、政策草案或用户数据。

主要接口包括 `/auth/login`（非生产演示）、`/agent/chat`、`/agent/confirm`、`/agent/cancel`、`/users/{user_id}/orders`、`/after-sales` 和 `/agent/trace/{task_id}`。具体数据结构见 `schemas/` 和 FastAPI 文档。

## 测试与限制

`tests/` 和根目录包含权限、确认、状态、政策、超时及并发相关测试。请先阅读 `tests/README.md` 和 `DEPLOYMENT.md`，只在隔离服务中运行 `python -m tests.run_agent_tests`；部分 MySQL 集成测试需要显式开启并创建测试库。本次发布准备未运行测试，不声称测试通过或线上性能。模型调用与集成测试可能产生费用或写入数据；不要针对正式业务环境运行。示例政策文档未包含，相关场景需要经授权的本地资料。
