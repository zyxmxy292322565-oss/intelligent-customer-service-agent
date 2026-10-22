import streamlit as st

from api.client import APIError, BackendAPI


AFTER_SALE_STAGES = (
    ("理解需求", ("input_analysis", "context_merge")),
    ("查询订单", ("resolve_order",)),
    ("生成售后方案", ("build_preview", "create_pending_action")),
    ("用户确认", ("confirm",)),
    ("完成", ("execute",)),
)


def build_timeline(steps: list[dict]) -> list[tuple[str, str]]:
    """Present the latest result of each business stage, not raw trace rows."""
    latest = {step.get("step_name"): step.get("status") for step in steps}
    has_after_sale_steps = any(
        name in latest for _, names in AFTER_SALE_STAGES[1:] for name in names
    )
    stages = AFTER_SALE_STAGES if has_after_sale_steps else AFTER_SALE_STAGES[:1]
    timeline = []
    for label, names in stages:
        statuses = [latest[name] for name in names if name in latest]
        if "FAILED" in statuses:
            state = "未完成"
        elif "SUCCESS" in statuses:
            state = "已完成"
        elif statuses:
            state = "进行中"
        else:
            state = "尚未进行"
        timeline.append((label, state))
    return timeline


def render():
    st.title("服务进度")
    st.caption("仅显示当前登录用户的记录。")
    api = BackendAPI(st.session_state.token)
    try:
        tasks = (api.tasks(st.session_state.conversation_id).get("data") or {}).get("tasks") or []
    except APIError:
        st.error("任务记录读取失败，请稍后重试。")
        return
    if not tasks:
        st.info("暂无任务记录")
        return
    labels = {
        task["id"]: f"第 {index + 1} 条 · {str(task.get('created_at') or '时间未知').replace('T', ' ')}"
        for index, task in enumerate(tasks)
    }
    task_id = st.selectbox(
        "选择服务记录", list(labels), format_func=labels.get,
        key=f"trace_task_{st.session_state.conversation_id}",
    )
    try:
        steps = (api.trace(task_id, st.session_state.conversation_id).get("data") or {}).get("steps") or []
    except APIError:
        st.error("步骤记录读取失败，请稍后重试。")
        return
    if not steps:
        st.info("这条记录暂无处理步骤。")
        return

    timeline = build_timeline(steps)
    st.subheader("处理进度")
    icons = {"已完成": "✅", "未完成": "❌", "进行中": "⏳", "尚未进行": "○"}
    for index, (label, state) in enumerate(timeline):
        with st.container(border=True):
            st.markdown(f"{icons[state]} **{label}**")
            st.caption(state)
        if index < len(timeline) - 1:
            st.markdown("│")

    if len(timeline) == 1:
        st.caption("本次记录未进入售后申请阶段。")
    elif timeline[-1][1] == "未完成":
        st.warning("这次售后申请未创建成功，请查看售后记录或详细 Trace。")

    with st.expander("查看详细 Trace"):
        st.dataframe(
            [{key: row.get(key) for key in (
                "task_id", "trace_id", "step_name", "status", "latency_ms", "created_at"
            )} for row in steps],
            width="stretch",
            hide_index=True,
        )
