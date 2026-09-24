"""LLM question classifier for HR auto-replies (classification only).

The model NEVER writes reply text: it labels the intent, confidence and
whether the message is a negotiation follow-up. Reply drafts are assembled
deterministically from the user's own fact talking points (see policy.py) -
so cross-session insight leakage and off-persona text are impossible by
construction, and the outbound guard is a second net.

LLM failures degrade to ``Classification.degraded()``: the policy engine
then routes everything to awaiting_human (design P0-2) instead of guessing.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from langchain_core.language_models.chat_models import BaseChatModel
from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)

INTENTS = (
    "interview_mode",       # 能否先线上沟通
    "interview_time",       # 约具体时间/改期 - always human
    "outsourcing",          # 是不是外包/用工主体
    "salary_expectation",   # 期望薪资（首轮 vs 谈判由 followup 区分）
    "availability",         # 到岗时间
    "employment_status",    # 在职/离职状态
    "city",                 # 所在城市/是否异地
    "basic_interest",       # 表达兴趣/继续沟通
    "resume_request",       # HR 索要简历/附件
    "multi_intent",         # 一条消息多个问题 - human
    "unknown",
)

#: Deterministic mapping: which user fact categories an intent needs before
#: an automatic answer may be assembled.
INTENT_FACT_REQUIREMENTS: dict[str, tuple[str, ...]] = {
    "interview_mode": (),
    "interview_time": (),
    "outsourcing": ("outsourcing_stance",),
    "salary_expectation": ("salary_expectation",),
    "availability": ("availability",),
    "employment_status": ("employment_status",),
    "city": ("city",),
    "basic_interest": (),
    "resume_request": (),
    "multi_intent": (),
    "unknown": (),
}


class ClassifySchema(BaseModel):
    """Structured output contract for one HR message group."""

    intent: str = Field(description=f"One of: {', '.join(INTENTS)}")
    confidence: float = Field(ge=0.0, le=1.0)
    negotiation_followup: bool = Field(
        default=False,
        description="True when the HR message pushes on a previous answer "
        "(压价/追问底线/给出数字要承诺) - negotiation round, human only.",
    )
    question_summary: str = Field(default="", max_length=200)


@dataclass(frozen=True, slots=True)
class Classification:
    """Classified HR message group plus degradation state."""

    intent: str
    confidence: float
    negotiation_followup: bool
    question_summary: str
    degraded_reason: str = ""
    required_facts: tuple[str, ...] = field(default_factory=tuple)

    @property
    def degraded(self) -> bool:
        return bool(self.degraded_reason)

    @classmethod
    def degraded_classification(cls, reason: str) -> Classification:
        return cls(
            intent="unknown",
            confidence=0.0,
            negotiation_followup=False,
            question_summary="",
            degraded_reason=reason,
            required_facts=(),
        )

    @property
    def requires_human(self) -> bool:
        """Intents that are human-only regardless of facts."""

        return (
            self.degraded
            or self.intent in {"interview_time", "multi_intent", "unknown"}
            or self.negotiation_followup
        )


class HrQuestionClassifier:
    """Classify merged HR message groups via the fallback-chained model."""

    def __init__(self, model: BaseChatModel | None = None) -> None:
        self._model = model

    def _invoke(self, prompt: str) -> dict[str, Any]:
        if self._model is None:
            raise RuntimeError("classifier model unavailable")
        result = self._model.with_structured_output(ClassifySchema).invoke(prompt)
        if isinstance(result, ClassifySchema):
            return result.model_dump()
        if isinstance(result, dict):
            return result
        raise RuntimeError(f"unexpected classifier output: {type(result).__name__}")

    def classify(
        self,
        *,
        hr_messages: list[str],
        conversation_context: str = "",
    ) -> Classification:
        """Classify one merged group of HR messages; never raises."""

        body = "\n".join(f"HR：{text}" for text in hr_messages)
        context = f"\n会话上下文：{conversation_context}" if conversation_context else ""
        prompt = (
            "你是招聘沟通分类器。判断求职者（ geek 侧）收到的 HR 消息的意图类别。\n"
            f"{body}{context}\n"
            "注意：若消息是在追问/压价此前给出的答案（例如对方回应了薪资数字、"
            "要求确认底线、对已说过的时间反复施压），negotiation_followup=true。"
        )
        try:
            raw = self._invoke(prompt)
        except Exception as exc:  # noqa: BLE001 - degradation is the contract
            logger.warning("hr classifier degraded: %s", type(exc).__name__)
            return Classification.degraded_classification(
                f"llm_unavailable:{type(exc).__name__}"
            )
        # Some LangChain adapters return a plain dict even when structured
        # output was requested.  Validate it again at this trust boundary:
        # malformed values (including NaN confidence) must degrade to human
        # review instead of accidentally passing a numeric policy gate.
        try:
            parsed = ClassifySchema.model_validate(raw)
        except Exception as exc:  # noqa: BLE001 - same safe degradation contract
            logger.warning("hr classifier returned invalid output: %s", type(exc).__name__)
            return Classification.degraded_classification(
                f"invalid_llm_output:{type(exc).__name__}"
            )
        intent = parsed.intent
        if intent not in INTENTS:
            intent = "unknown"
        return Classification(
            intent=intent,
            confidence=parsed.confidence,
            negotiation_followup=parsed.negotiation_followup,
            question_summary=parsed.question_summary[:200],
            required_facts=INTENT_FACT_REQUIREMENTS.get(intent, ()),
        )
