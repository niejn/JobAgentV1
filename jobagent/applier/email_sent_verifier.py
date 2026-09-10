"""Verify uncertain SMTP deliveries through the sender mailbox Sent folder."""

from __future__ import annotations

import imaplib
import logging
from dataclasses import dataclass
from email import policy
from email.parser import BytesParser

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class SentVerification:
    status: str
    message_id: str
    reason: str = ""


class EmailSentFolderVerifier:
    def __init__(
        self, host: str, port: int, user: str, password: str, folder: str = "Sent"
    ) -> None:
        self._host, self._port = host, port
        self._user, self._password, self._folder = user, password, folder

    @classmethod
    def from_smtp_settings(cls, settings: object) -> EmailSentFolderVerifier:
        smtp_host = str(getattr(settings, "jobagent_email_smtp_host", "") or "")
        imap_host = str(getattr(settings, "jobagent_email_imap_host", "") or "") or (
            smtp_host.replace("smtp.", "imap.", 1) if smtp_host.startswith("smtp.") else ""
        )
        return cls(
            imap_host,
            int(getattr(settings, "jobagent_email_imap_port", 993)),
            str(getattr(settings, "jobagent_email_smtp_user", "") or ""),
            str(getattr(settings, "jobagent_email_smtp_password", "") or ""),
            str(getattr(settings, "jobagent_email_sent_folder", "Sent")),
        )

    def find(self, message_id: str) -> SentVerification:
        if not message_id or not all((self._host, self._user, self._password)):
            return SentVerification("unverified", message_id, "imap_not_configured")
        client: imaplib.IMAP4_SSL | None = None
        try:
            client = imaplib.IMAP4_SSL(self._host, self._port, timeout=30)
            client.login(self._user, self._password)
            status, _ = client.select(self._folder, readonly=True)
            if status != "OK":
                return SentVerification("unverified", message_id, "sent_folder_unavailable")
            # IMAP HEADER searches are substring matches; fetch headers to confirm exact ID.
            quoted_id = '"' + message_id.replace("\\", "\\\\").replace('"', '\\"') + '"'
            status, data = client.uid("search", "HEADER", "Message-ID", quoted_id)
            if status == "OK" and data and data[0]:
                for uid in data[0].split():
                    fetched, headers = client.uid(
                        "fetch", uid, "(BODY.PEEK[HEADER.FIELDS (MESSAGE-ID)])"
                    )
                    if fetched != "OK":
                        continue
                    for item in headers or []:
                        if isinstance(item, tuple) and isinstance(item[1], bytes):
                            header = BytesParser(policy=policy.default).parsebytes(item[1])
                            if str(header.get("Message-ID", "")).strip() == message_id:
                                return SentVerification(
                                    "submitted", message_id, "message_id_found_in_sent"
                                )
            return SentVerification("unverified", message_id, "message_id_not_found_in_sent")
        except (imaplib.IMAP4.error, OSError) as exc:
            logger.warning("sent-folder verification unavailable: %s", exc)
            return SentVerification("unverified", message_id, type(exc).__name__)
        finally:
            if client is not None:
                try:
                    client.logout()
                except (imaplib.IMAP4.error, OSError):
                    pass
