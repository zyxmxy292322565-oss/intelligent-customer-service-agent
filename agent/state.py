from typing import TypedDict, Any
from uuid import uuid4
from schemas.plan_schema import AgentPlan
class AgentState(TypedDict):
    # 请求链路追踪ID
    trace_id: str

    # 一次 Agent 任务唯一标识
    task_id: str

    # 用户原始输入
    user_text: str

    # 可信用户 ID
    current_user_id: int

    # Input Analyzer 得到的意图
    intent: str | None

    # Parameter Extractor 提取出的参数
    params: dict[str, Any]

    # MySQL agent_tasks.context 中恢复的跨轮业务上下文。
    # Graph checkpoint 只负责编排状态，不作为业务参数的唯一来源。
    task_context: dict[str, Any]

    # waiting_input 任务续轮时由服务端恢复的可信意图。
    continuation_intent: str | None
    continuation_goal: str | None

    # Context Resolver 从真实业务系统补充的上下文
    resolved_context: dict[str, Any]

    # Planner 后面生成的执行计划
    plan: AgentPlan | None

    # Tool 执行结果
    tool_results: list[dict[str, Any]]

    # 操作风险等级
    risk_level: str | None

    # 写操作确认 ID
    confirmation_id: str | None

    # 已通过政策核验、与 Pending Action 一致的用户预览
    action_preview: dict[str, Any] | None

    # 最终返回用户的回答
    final_answer: str | None

    # Agent 执行过程中的错误信息
    error: dict[str, Any] | None

def create_agent_state(
    user_text: str,
    current_user_id: int,
    trace_id: str,
    task_id: str | int | None = None,
    task_context: dict[str, Any] | None = None,
    continuation_intent: str | None = None,
) -> AgentState:

    return {
        "trace_id": trace_id,
        "task_id": str(task_id) if task_id is not None else f"TASK_{uuid4().hex[:16].upper()}",
        "user_text": user_text,
        "current_user_id": current_user_id,

        "intent": None,
        "params": {},
        "task_context": dict(task_context or {}),
        "continuation_intent": continuation_intent,
        "continuation_goal": None,

        "resolved_context": {},
        "plan": None,

        "tool_results": [],
        "risk_level": None,

        "confirmation_id": None,
        "action_preview": None,
        "final_answer": None,
        "error": None
    }
