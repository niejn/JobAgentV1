"""Read-only inbox tools: list recent mail and read one message.

The agent composes HR-reply tracking itself: list_job_records(status=
applied) -> list_recent_emails(from_contains=<hr mailbox>) -> read_email
-> update_job_progress(hr_replied, note=<interview link/time>). Tools
stay stateless and read-only; no delete/move/mark capability exists.
"""

from __future__ import annotations

import asyncio
from typing import Any

from langchain_core.tools import BaseTool, StructuredTool
from pydantic import BaseModel, Field

from jobagent.config import Settings


class ListRecentEmailsRequest(BaseModel):
    """Inbox scan criteria."""

    limit: int = Field(default=20, ge=1, le=50)
    since_days: int = Field(default=30, ge=0, le=365)
    from_contains: str = Field(
        default="",
        max_length=120,
        description="按发件人地址子串过滤（如 HR 邮箱 tongzh@qinchengsoft.com）",
    )


class ReadEmailRequest(BaseModel):
    """Fetch one message's full text by IMAP uid."""

    uid: str = Field(min_length=1, max_length=20, description="list 返回的 uid")


def build_list_recent_emails_tool(settings: Settings) -> BaseTool:
    reader_root = settings

    async def _run(
        limit: int = 20,
        since_days: int = 30,
        from_contains: str = "",
    ) -> dict[str, Any]:
        from jobagent.applier.email_reader import EmailReader

        def _scan() -> dict[str, Any]:
            reader = EmailReader(reader_root)
            items = reader.list_recent(
                limit=limit,
                since_days=since_days,
                from_contains=from_contains,
            )
            return {
                "status": "ok",
                "count": len(items),
                "emails": [
                    {
                        "uid": e.uid,
                        "from": e.from_addr,
                        "from_name": e.from_name,
                        "subject": e.subject,
                        "date": e.date,
                        "snippet": e.snippet[:200],
                        "thread_key": e.thread_key,
                    }
                    for e in items
                ],
            }

        try:
            return await asyncio.to_thread(_scan)
        except RuntimeError as exc:
            return {"status": "failed", "error_type": "imap_error", "message": str(exc)}
        except Exception as exc:  # noqa: BLE001
            return {
                "status": "failed",
                "error_type": "imap_error",
                "message": f"收件箱读取失败：{exc}",
            }

    return StructuredTool.from_function(
        coroutine=_run,
        name="list_recent_emails",
        description=(
            "读取自己邮箱的最近邮件（只读 IMAP，不发件、不删件）。"
            "追踪 HR 回复：先用 from_contains 过滤投递过的 HR 邮箱，"
            "再用 read_email 读正文提取面试链接/时间。"
        ),
        args_schema=ListRecentEmailsRequest,
    )


def build_read_email_tool(settings: Settings) -> BaseTool:
    reader_root = settings

    async def _run(uid: str) -> dict[str, Any]:
        from jobagent.applier.email_reader import EmailReader

        def _read() -> dict[str, Any]:
            return EmailReader(reader_root).read_body(uid)

        try:
            return await asyncio.to_thread(_read)
        except RuntimeError as exc:
            return {"status": "failed", "error_type": "imap_error", "message": str(exc)}
        except Exception as exc:  # noqa: BLE001
            return {
                "status": "failed",
                "error_type": "imap_error",
                "message": f"邮件读取失败：{exc}",
            }

    return StructuredTool.from_function(
        coroutine=_run,
        name="read_email",
        description="按 uid 读取一封邮件的完整正文（只读）。uid 来自 list_recent_emails。",
        args_schema=ReadEmailRequest,
    )
