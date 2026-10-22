"""Format policy evidence and its grounded explanation for the Agent API."""

import logging

from agent.policy_response_generator import generate_policy_answer


logger = logging.getLogger("agent.policy_response")

# A refusal is a completed read, not a failed dependency call. Unknown errors
# deliberately stay failures; never infer success merely from a nonempty answer.
POLICY_BUSINESS_RESULTS = frozenset({
    "POLICY_NOT_FOUND", "POLICY_EVIDENCE_INSUFFICIENT", "NEEDS_REVIEW", "INELIGIBLE",
})


def read_result_failed(result) -> bool:
    return (isinstance(result, dict) and result.get("success") is False
            and result.get("code") not in POLICY_BUSINESS_RESULTS)


def format_policy_result(result: dict, question: str) -> dict:
    evidence = result.get("evidence") or []
    sources = list(dict.fromkeys(
        item["source"] for item in evidence if item.get("source")
    ))
    data = {"answer": None, "evidence": evidence, "sources": sources}
    assessment = result.get("evidence_assessment")
    if assessment is not None:
        data["evidence_assessment"] = assessment

    if not result.get("success"):
        return {
            "success": False,
            "code": result.get("code") or "POLICY_QUERY_FAILED",
            "message": result.get("message") or "政策查询失败",
            "data": data,
        }

    if (result.get("refused") or not any(item.get("content") for item in evidence)
            or (assessment is not None and assessment.get("sufficient_for_answer") is not True)):
        return {
            "success": False,
            "code": (result.get("code") if result.get("code") in POLICY_BUSINESS_RESULTS
                     else "POLICY_EVIDENCE_INSUFFICIENT"),
            "message": result.get("message") or "未找到足够的政策依据",
            "data": data,
        }

    try:
        answer = generate_policy_answer(question, evidence)
    except Exception:
        logger.exception("policy answer generation failed")
        return {
            "success": False,
            "code": "POLICY_ANSWER_GENERATION_FAILED",
            "message": "政策依据已找到，但暂时无法生成回答，请稍后重试",
            "data": data,
        }

    data["answer"] = answer
    return {
        "success": True,
        "code": "POLICY_RESULT_READY",
        "message": answer,
        "data": data,
    }
