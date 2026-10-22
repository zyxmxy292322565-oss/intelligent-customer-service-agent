from pydantic import (
    BaseModel,
    Field
)


class PolicyEvidence(BaseModel):

    source: str

    file_hash: str | None = None

    chunk_id: (
        str
        | int
        | None
    ) = None

    version: str | None = None

    rerank_score: (
        float
        | None
    ) = None

    faiss_distance: (
        float
        | None
    ) = None

    content: str = ""


class PolicyRAGRequest(BaseModel):
    """
    售后 Agent → RAG Service
    """

    question: str = Field(
        min_length=1
    )

    top_k: int = Field(
        default=5,
        ge=1,
        le=20
    )


class PolicyRAGResponse(BaseModel):
    """
    RAG Service → 售后 Agent
    """

    success: bool

    code: str

    message: str

    # 是否因为相关度不足而拒答
    refused: bool = False

    # Optional for old servers; ranking completion is not evidence sufficiency.
    # Produced by the RAG service, never accepted from a frontend approval flag.
    evidence_assessment: dict | None = None

    # RAG 生成的解释文本
    # 只能用于解释，不能作为业务真值
    answer: str | None = None

    # 检索证据
    evidence: list[
        PolicyEvidence
    ] = Field(
        default_factory=list
    )

    timing: dict[
        str,
        float
    ] = Field(
        default_factory=dict
    )
