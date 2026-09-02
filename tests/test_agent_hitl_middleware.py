"""HITL middleware coverage: every external-write tool must be interrupted.

The migration from per-tool ``user_confirmed`` parameter gates to the
framework ``HumanInTheLoopMiddleware`` moved approval out of the tools.
A tool that performs an external write but is missing from ``_HITL_TOOLS``
would run unapproved - this file pins the mapping.
"""

from __future__ import annotations

from jobagent.agent import _HITL_TOOLS, build_hitl_middleware


def test_hitl_tools_cover_exactly_the_external_writes() -> None:
    """The external-write surfaces (including Skill installation) -
    nothing less (unapproved write),
    nothing more (pointless interrupts on read/local tools)."""

    assert set(_HITL_TOOLS) == {
        "install_skill",
        "send_application_email",
        "boss_greet_jobs",
        "upload_boss_resume_pdf",
        "reply_boss_greeting",
        "merge_job_identities",
    }


def test_build_hitl_middleware_interrupts_on_every_hitl_tool() -> None:
    middleware = build_hitl_middleware()

    assert set(middleware.interrupt_on) == set(_HITL_TOOLS)
    for name, config in middleware.interrupt_on.items():
        assert config["allowed_decisions"] == ["approve", "reject"], name
        assert config["description"], f"{name} needs a user-facing description"


def test_hitl_tool_schemas_have_no_user_confirmed_field() -> None:
    """The old weak gates are gone from the request schemas, so the model
    can no longer satisfy approval by passing a boolean."""

    from jobagent.tools.boss_chat_send import BossChatReplyRequest
    from jobagent.tools.boss_greet import BossGreetJobsRequest
    from jobagent.tools.job_identity_merge import MergeIdentitiesRequest

    assert "user_confirmed" not in BossGreetJobsRequest.model_fields
    assert "user_confirmed" not in BossChatReplyRequest.model_fields
    assert "user_confirmed" not in MergeIdentitiesRequest.model_fields
