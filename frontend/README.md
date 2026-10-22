# Streamlit 演示前端

前端只调用 FastAPI 接口；Agent 决策、身份校验、确认与售后落库仍由后端负责。

本地运行：先启动后端，再安装 `frontend/requirements.txt`，运行 `streamlit run frontend/app.py`。默认后端地址为 `http://localhost:8000`，可通过 `BACKEND_URL` 指定。Docker Compose 中前端使用 `http://backend:8000`（Compose 服务名），启动后访问 `http://localhost:8501`；`FRONTEND_PORT` 可调整宿主机端口。

页面流程：登录（调用 `/auth/login`）→ 对话（`/agent/chat`）→ 查看政策依据，或预览并确认（`/agent/confirm`）→ Trace 与售后记录。登录 Token 保存在当前 Streamlit 会话中，后续 API 请求自动携带 Bearer Token。退出登录会清除会话 Token 和聊天记录。

页面只通过 `app.py` 左侧的“页面”单选项切换。`pages/` 中的 Python 文件是供 `app.py` 调用的模块；`.streamlit/config.toml` 关闭 Streamlit 自动发现的重复页面导航，避免直接运行这些模块时出现空白页。切换单选项仍使用同一个 `st.session_state`，Token 不会被重新初始化。

当前 `/auth/login` 是项目已有的用户 ID 模拟登录接口，只适合开发演示；生产环境该接口被禁用。前端没有绕开或替换后端认证。

如果登录返回 HTTP 404，请查看响应内容：`{"detail":"用户不存在"}` 表示接口存在，但开发库没有对应用户。Compose 的 `migrate` 只负责建表，不自动插入演示数据。仅在本地开发环境需要演示数据时，可运行 `docker compose exec backend python -c "from db.init_db import insert_demo_data; insert_demo_data()"`；该函数会插入一个演示用户及其订单，不应在生产环境执行。前端请求失败时，容器日志会记录 URL、HTTP 状态码和截断后的响应内容，不记录 Bearer Token。

前端 API 客户端访问配置的内部后端地址时不使用宿主机的 HTTP 代理，以免本机 `localhost:8000` 被代理转发成 502。需要接入外部代理的部署应单独配置网络出口，不要把内部 Compose 服务名交给外部代理。

售后记录经已有 `/users/{user_id}/after-sales` API 获取。Trace 页面通过 Bearer Token 调用后端只读接口 `GET /agent/tasks` 和 `GET /agent/trace/{task_id}`；后端以 Token 用户 ID 查询任务，并在读取步骤前校验任务归属。前端不再连接 MySQL，也不需要 `TRACE_DB_USER` 等数据库配置。

政策回答默认只展示自然语言解释，点击“查看政策依据”才展开来源与摘录。售后预览以商品、申请类型和原因展示；原始 Preview 仍保留在后端响应中，但页面不直接呈现 JSON。确认成功后页面展示申请编号，不展示内部确认 ID 或状态码。

创建售后时若缺少类型，对话页会提供“退货退款 / 换货 / 维修”选择框。选中后，前端仍通过原有 `/agent/chat` 的 `message` 字段发送选择，不增加新的后端参数。Trace 默认按用户能理解的处理阶段显示，仅在“查看详细 Trace”中展示原始步骤；售后记录以商品、编号、状态、金额和时间卡片展示。
