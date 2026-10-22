from langchain_openai import ChatOpenAI
from core.settings import get_settings


def get_llm(*, disable_thinking: bool = False, input_analysis: bool = False):
    settings = get_settings()
    llm = ChatOpenAI(
        model=settings.deepseek_model,
        api_key=settings.deepseek_api_key,
        base_url=settings.deepseek_base_url,
        temperature=0,
        # HTTPX limits each network operation, not total wall-clock time.
        # The analyzer also enforces an independent wall-clock deadline.
        **({"timeout": 15.0, "max_retries": 0} if input_analysis else {}),
        extra_body={"thinking": {"type": "disabled"}} if disable_thinking else None,
    )

    return llm
