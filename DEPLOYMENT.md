# P3 运行与部署

## 配置

首次本地开发可复制 `.env.example` 为 `.env`；示例已包含 Compose 所需的 MySQL、Redis 密码和开发用 JWT 密钥。项目本机现有 `.env` 已补上这些缺失键，原有 MySQL/LLM 配置未覆盖。不要提交 `.env`。`APP_ENV` 支持 `development`、`test`、`production`。生产环境至少需要 `MYSQL_PASSWORD` 和 `DEEPSEEK_API_KEY`。显式设置的进程环境变量优先于本地 `.env`；原有 `DEEPSEEK_API_BASE`、`MODEL` 仍可用，推荐改用 `DEEPSEEK_BASE_URL`、`DEEPSEEK_MODEL`。

P4 起生产环境还必须提供至少 32 字符、随机且保密的 `JWT_SECRET_KEY`，并与可信的正式登录/身份服务对接。未配置时应用拒绝启动；非生产环境未配置时使用进程内临时密钥，重启后已签发的 Token 失效。`JWT_ACCESS_TOKEN_EXPIRE_MINUTES` 默认为 30，必须大于 0。

容器启动需要 `MYSQL_ROOT_PASSWORD`、`MYSQL_APP_PASSWORD`、`REDIS_PASSWORD`。Compose 在启动指定服务之前仍会插值整个配置文件，因此即使执行 `docker compose up redis`，缺少 MySQL 密码也会在配置阶段报错。本地示例值仅供开发，生产环境必须全部替换为独立强密码；示例 JWT 密钥刻意短于生产最低长度，不能用于生产。Redis 仅映射到宿主机 `127.0.0.1:${REDIS_PORT:-6379}`，供宿主机 FastAPI 使用，不对外部网络开放；MySQL 仍只在 Compose 内部网络可达。后端对外端口由 `APP_PORT` 控制。Compose 中 backend 固定通过 `http://policy-rag:8000` 调用策略服务；宿主机直接运行 backend 时需单独配置可达的 `POLICY_RAG_SERVICE_URL`。不要把 `.env` 或凭据打进镜像。

Policy RAG 使用独立的 `rag_demo` 库和 `RAG_MYSQL_USER` 账号。一次性 `policy-rag-db-init` 会在已有 MySQL 数据卷上幂等建库、建账号并仅授予 `rag_demo` 库权限；它需要现有 MySQL root 密码可用。`RAG_MYSQL_PASSWORD` 未设置时仅为本地开发使用公开示例值，`APP_ENV=production` 会拒绝该示例值；生产部署前必须提供独立强密码。RAG 使用 Redis DB `RAG_REDIS_DB`（默认 1），与业务默认 DB 0 分开；这只是逻辑隔离，不是 Redis 权限边界。RAG 容器需要 `DEEPSEEK_API_KEY`，并读取 `DEEPSEEK_API_BASE`（默认 `https://api.deepseek.com`）和 `DEEPSEEK_MODEL`。现有 RAG 索引目录挂载自 `../demo3_rag_qa/cache`，模型下载缓存放在独立数据卷。首次加载模型可能较慢，但 backend 不等待 RAG 健康检查完成。

## 启动

人工客服工作台位于 `http://localhost:8502`，使用独立的 `ADMIN_API_KEY` 凭证。未配置时 `/admin/handoff/*` 接口关闭（503），普通用户 JWT 也不能访问。请在本地 `.env` 或部署系统中设置随机密钥，生产环境至少 32 字符；不要把该密钥放入前端环境变量或提交到仓库。Compose 默认只把工作台端口绑定到宿主机 `127.0.0.1`。远程访问应通过有 HTTPS 和身份管理的反向代理；正式生产还需要客服账号、角色及操作审计，当前共享密钥仅用于开发演示。

在 `app/` 目录执行以下命令即可启动完整系统，包括 frontend、backend、policy-rag、MySQL 和 Redis；`migrate` 与 `policy-rag-db-init` 是执行成功后退出的一次性任务：

```powershell
docker compose config --quiet
docker compose up -d
docker compose ps
```

代码或镜像依赖变更后可执行 `docker compose up -d --build`。在 Bash 环境下，也可以运行 `bash scripts/start.sh`，它会构建、启动、等待健康状态，并在失败时输出相关日志。常用运维命令：

```powershell
docker compose down
docker compose restart
docker compose logs -f
```

`docker compose down` 不带 `-v` 时不会删除 MySQL、Redis 和模型缓存数据卷。首次启动 Policy RAG 可能需要下载模型，达到 `healthy` 的时间会比其他服务长；backend 不依赖 RAG 的健康状态，前端只等待 backend 健康。

如果 FastAPI 在宿主机运行、Redis 在容器中运行，宿主机 `.env` 的 `REDIS_PASSWORD` 必须与容器相同；从 `.env.example` 复制的开发配置已保持一致。更改 Redis 密码后应重建容器并重启后端。已有 Redis/MySQL volume 的密码不会因修改 `.env` 自动全部同步；尤其是 MySQL 已初始化数据卷时，若改密码需要按 MySQL 用户管理流程处理，不能直接假设环境变量会重置数据库账号。

MySQL 和 Redis 先通过健康检查；一次性 `migrate` 服务建表并执行现有 P1 trace 迁移，成功后后端才启动。不插入演示业务数据。`GET /health` 是进程存活检查，`GET /ready` 检查 MySQL 和 Redis。查看日志可运行 `docker compose logs backend migrate policy-rag-db-init policy-rag`。首次启动需要为 MySQL 初始化留出时间；已有 MySQL volume 不会因修改密码变量而自动重设账号密码。

## 测试

```powershell
python -m compileall -q core db cache middleware llm services agent main.py infra tests
python -m tests.run_agent_tests
```

默认只运行 Mock 测试，真实 MySQL 用例会跳过。设置 `RUN_AGENT_MYSQL_TESTS=1` 后会创建独立的随机测试库、执行链路测试并清理该测试库；不要把它指向生产 MySQL。CI 在隔离的 MySQL service 上自动开启这些测试。Redis 限流测试用 Mock，不需要真实 Redis。

## P4 模拟登录与身份边界

仅在 `development` / `test` 环境可调用 `POST /auth/login`，输入 `{"user_id": 1}`。接口会检查用户是否存在，然后返回 `access_token` 和 `token_type: bearer`。之后在订单、售后、`/agent/chat`、`/agent/confirm` 请求中传入 `Authorization: Bearer <access_token>`。旧客户端仍可带 Body 中的 `user_id`，但服务端忽略它；`user-id` 请求头也不再用于身份或 Redis 限流。

**此登录接口不是用户身份验证**：知道任意现有 `user_id` 的人，在非生产环境仍可为其获取 Token。生产环境禁用该模拟登录；在对外上线前，必须接入真实凭据校验或可信身份提供方，且保护签名密钥。不能仅凭本次 P4 改造宣称用户已经完成身份认证。

## 运维注意

限流 key 采用 `consumer_agent:rate_limit:{global|user:<id>}`，前缀和 TTL 可配置；TTL 必须不短于令牌桶补满所需时间。Redis 故障时 Agent 接口返回 503，不绕过限流。日志记录时间、级别、request_id、trace_id、task_id、confirmation_id；新增的链路日志不主动记录用户原始消息、业务参数值或 LLM 凭据。当前 `request_id` 与该请求的 `trace_id` 相同。修改环境变量后需要重启进程，配置不会热更新。
