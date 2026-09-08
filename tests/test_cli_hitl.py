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


def test_hitl_review_shows_resume_recipient_and_filename() -> None:
    rendered = _format_hitl_review(
        {
            "action_requests": [
                {
                    "name": "send_boss_resume_after_hr_reply",
                    "description": "向已回复的 Boss HR 发送用户选定的简历",
                    "args": {
                        "hr_name": "陈女士",
                        "company": "四川影目",
                        "job_title": "AI Agent 工程师",
                        "resume_file_name": "李明_AI Agent工程师.pdf",
                    },
                }
            ]
        }
    )

    assert "陈女士" in rendered
    assert "四川影目" in rendered
    assert "AI Agent 工程师" in rendered
    assert "李明_AI Agent工程师.pdf" in rendered
