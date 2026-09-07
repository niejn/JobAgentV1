from __future__ import annotations

from jobagent.cli import _format_hitl_review


def test_hitl_review_renders_action_requests_with_description_and_details() -> None:
    payload = {
        "action_requests": [
            {
                "name": "boss_greet_jobs",
                "description": "向 3 个 Boss 岗位发送招呼并建立 HR 会话",
                "args": {
                    "jobs": [
                        {
                            "company": "启链云智能",
                            "title": "AI Agent 全栈开发",
                            "greeting": "您好，期待交流。",
                        }
                    ],
                    "max_greetings": 1,
                },
            }
        ],
        "review_configs": [{"action_name": "boss_greet_jobs"}],
    }

    rendered = _format_hitl_review(payload)

    assert "[1] boss_greet_jobs" in rendered
    assert "向 3 个 Boss 岗位发送招呼并建立 HR 会话" in rendered
    assert "启链云智能" in rendered
    assert "您好，期待交流。" in rendered
