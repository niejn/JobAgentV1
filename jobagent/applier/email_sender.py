"""Send application emails over SMTP (QQ/Foxmail by default, any SMTP works).

Attachment-heavy MIME building is kept boring on purpose: HTML + plain-text
alternative bodies and one optional PDF resume. Authentication is the
provider's authorization code (never the account password); port 465 uses
implicit SSL, anything else assumes STARTTLS.
"""

from __future__ import annotations

import logging
import smtplib
from email.message import EmailMessage
from email.utils import formataddr, make_msgid
from pathlib import Path
from typing import Any

from jobagent.config import Settings

logger = logging.getLogger(__name__)

_MAX_ATTACHMENT_BYTES = 10 * 1024 * 1024


class EmailConfigError(RuntimeError):
    """Raised when SMTP settings are missing or obviously invalid."""


class EmailSender:
    """One-shot SMTP application email sender."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings

    @property
    def configured(self) -> bool:
        return bool(
            self._settings.jobagent_email_smtp_host.strip()
            and self._settings.jobagent_email_smtp_user.strip()
            and self._settings.jobagent_email_smtp_password.strip()
        )

    def send(
        self,
        *,
        to: str,
        subject: str,
        body_html: str,
        body_text: str = "",
        attachment: Path | None = None,
        attachment_name: str = "",
    ) -> dict[str, Any]:
        """Send one email; returns a structured result (never raises for
        delivery problems - the caller surfaces them to the agent)."""

        if not self.configured:
            raise EmailConfigError(
                "SMTP 未配置：请在 .env 设置 JOBAGENT_EMAIL_SMTP_PASSWORD"
                "（QQ 邮箱授权码，设置→账户→POP3/IMAP/SMTP→生成授权码）。"
            )
        to = to.strip()
        if "@" not in to:
            return {"status": "failed", "error_type": "invalid_recipient", "to": to}

        message = EmailMessage()
        message["From"] = formataddr(
            (self._settings.jobagent_email_sender_name, self._settings.jobagent_email_smtp_user)
        )
        message["To"] = to
        message["Subject"] = subject
        message_id = make_msgid()
        message["Message-ID"] = message_id
        message.set_content(body_text or _html_to_text(body_html))
        message.add_alternative(body_html, subtype="html")

        if attachment is not None:
            path = attachment.expanduser().resolve()
            if not path.is_file():
                return {
                    "status": "failed",
                    "error_type": "attachment_missing",
                    "attachment": str(path),
                }
            if path.stat().st_size > _MAX_ATTACHMENT_BYTES:
                return {
                    "status": "failed",
                    "error_type": "attachment_too_large",
                    "attachment": str(path),
                }
            message.add_attachment(
                path.read_bytes(),
                maintype="application",
                subtype="pdf",
                filename=attachment_name or path.name,
            )

        host = self._settings.jobagent_email_smtp_host.strip()
        port = self._settings.jobagent_email_smtp_port
        user = self._settings.jobagent_email_smtp_user.strip()
        try:
            with _smtp_connection(host, port) as smtp:
                smtp.login(user, self._settings.jobagent_email_smtp_password)
                smtp.send_message(message)
        except smtplib.SMTPAuthenticationError as exc:
            logger.warning("smtp auth failed for %s: %s", user, exc)
            return {
                "status": "failed",
                "error_type": "auth_failed",
                "message": "SMTP 认证失败：请检查授权码是否正确/是否最新生成。",
            }
        except smtplib.SMTPException as exc:
            logger.warning("smtp send failed: %s", exc)
            return {
                "status": "failed",
                "error_type": "smtp_error",
                "message": f"发送失败：{exc}",
            }
        except OSError as exc:
            logger.warning("smtp network error: %s", exc)
            return {
                "status": "unverified",
                "error_type": "network_uncertain",
                "message": f"网络状态不确定：{exc}",
                "message_id": message_id,
            }
        return {
            "status": "ok",
            "to": to,
            "subject": subject,
            "attachment": str(attachment) if attachment else "",
            "message_id": message_id,
        }


def _smtp_connection(host: str, port: int) -> smtplib.SMTP:
    """Port 465 -> implicit SSL; otherwise STARTTLS on plain SMTP."""

    if port == 465:
        return smtplib.SMTP_SSL(host, port, timeout=30)
    smtp = smtplib.SMTP(host, port, timeout=30)
    smtp.starttls()
    return smtp


def _html_to_text(html: str) -> str:
    """Degenerate HTML->text fallback for the alternative plain part."""

    import re

    text = re.sub(r"<br\s*/?>", "\n", html, flags=re.IGNORECASE)
    text = re.sub(r"</p>", "\n\n", text, flags=re.IGNORECASE)
    text = re.sub(r"<[^>]+>", "", text)
    return text.strip()
