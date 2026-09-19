"""Hard screen for outbound Boss text: the user speaks, or nothing is sent.

The job seeker's own Boss account sends every greeting/reply, so outbound
text must read as the candidate speaking in first person. Two families of
content are refused outright (send nothing, report why):

1. meta/test phrasing — ``链路测试`` / ``测试消息`` / ``请忽略`` style plumbing
   chatter that exposes automation and burns the first impression;
2. AI/automation self-disclosure — ``我是AI`` / ``AI助手`` / ``自动发送`` ...

False-positive budget is deliberate: bare ``测试``, ``人工智能`` and
``大语言模型`` are NOT blocked, because a candidate may legitimately write
``五年测试工程师经验`` or ``有人工智能项目经验``. Only self-referential or
meta phrasings are listed.
"""

from __future__ import annotations

_TEST_PATTERNS: tuple[str, ...] = (
    "链路测试",
    "测试消息",
    "测试发送",
    "冒烟测试",
    "试发一条",
    "这条是测试",
    "这条消息是测试",
    "这是一条测试",
    "test message",
    "this is a test",
    "just a test",
    "smoke test",
)

_IGNORE_PATTERNS: tuple[str, ...] = (
    "请忽略",
    "请无视",
    "忽略此消息",
    "忽略本条",
    "忽略这条",
    "收到请忽略",
    "可忽略本条",
    "please ignore",
    "disregard this",
)

_AI_PATTERNS: tuple[str, ...] = (
    "我是ai",
    "我是一个ai",
    "作为ai",
    "由ai",
    "ai生成",
    "ai助手",
    "ai撰写",
    "ai代写",
    "人工智能助手",
    "我是一个人工智能",
    "作为人工智能",
    "我是机器人",
    "这是一个机器人",
    "机器人自动",
    "自动发送",
    "自动回复",
    "自动化发送",
    "我是一个智能体",
    "智能体发送",
    "gpt生成",
    "由gpt",
    "chatgpt",
    "语言模型生成",
    "as an ai",
    "i am an ai",
    "i'm an ai",
    "ai agent",
    "language model",
    "artificial intelligence",
)


def screen_outbound_text(text: str) -> list[str]:
    """Return violation labels for disallowed outbound content; empty = send."""

    lowered = (text or "").lower()
    violations: list[str] = []
    for patterns, label in (
        (_TEST_PATTERNS, "测试性话术（链路测试/测试消息类）"),
        (_IGNORE_PATTERNS, "请忽略类话术"),
        (_AI_PATTERNS, "AI/自动化身份暴露"),
    ):
        if any(pattern in lowered for pattern in patterns):
            violations.append(label)
    return violations


def guard_refusal(violations: list[str]) -> dict[str, object]:
    """Uniform refusal payload for blocked outbound Boss text."""

    return {
        "status": "refused",
        "error_type": "outbound_guard",
        "guard_violations": violations,
        "message": (
            "出站护栏拒发：消息不得包含测试性/请忽略话术，也不得暴露 AI 或自动化身份。"
            "请以求职者本人第一人称重写后再试——要么以用户本人身份合格发送，要么不发。"
        ),
    }
