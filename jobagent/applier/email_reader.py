"""Read-only IMAP inbox access for tracking HR replies to applications.

Safety surface: EXAMINE mode only (``select(..., readonly=True)``) - no
seen-flag writes, no delete/move/copy capability exists in this module.
Authentication reuses the SMTP authorization code (QQ/Foxmail: the same
16-char code serves IMAP and SMTP).
"""
import email
import email.message
import imaplib
import logging
import re
from dataclasses import dataclass
from email.header import decode_header, make_header
from email.utils import parsedate_to_datetime
from typing import Any

from jobagent.config import Settings

logger = logging.getLogger(__name__)

_SUBJECT_PREFIX_RE = re.compile(r"^\s*(re|fw|fwd)\s*:\s*", re.IGNORECASE)
_MAX_SNIPPET_CHARS = 400


@dataclass(frozen=True, slots=True)
class InboxEmail:
    """One inbox message summary for agent consumption."""

    uid: str
    from_addr: str
    from_name: str
    subject: str
    date: str
    snippet: str
    thread_key: str


class EmailReader:
    """Read-only IMAP access behind a small synchronous interface."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings

    @property
    def configured(self) -> bool:
        return bool(
            self._settings.jobagent_email_smtp_user.strip()
            and self._settings.jobagent_email_smtp_password.strip()
        )

    def list_recent(
        self,
        *,
        limit: int = 20,
        since_days: int = 30,
        from_contains: str = "",
    ) -> list[InboxEmail]:
        """Newest-first inbox scan; filters: sender substring + recency."""

        if not self.configured:
            raise RuntimeError("IMAP 未配置：缺少邮箱账号/授权码。")
        with self._connect() as imap:
            typ, _ = imap.select("INBOX", readonly=True)
            if typ != "OK":
                raise RuntimeError("无法打开收件箱。")
            criteria: list[str] = []
            if from_contains.strip():
                addr = from_contains.strip().replace('"', "")
                criteria.append(f'HEADER FROM "{addr}"')
            if since_days > 0:
                criteria.append(self._since_criterion(since_days))
            query = " ".join(criteria) if criteria else "ALL"
            typ, data = imap.search(None, query)
            if typ != "OK":
                raise RuntimeError("收件箱搜索失败。")
            uids = data[0].split()[-limit:] if limit else data[0].split()

            results: list[InboxEmail] = []
            for uid in reversed(uids):  # newest first
                item = self._fetch_summary(imap, uid)
                if item is not None:
                    results.append(item)
            return results

    def read_body(self, uid: str) -> dict[str, Any]:
        """Fetch one message's full text (plain part preferred)."""

        if not self.configured:
            raise RuntimeError("IMAP 未配置：缺少邮箱账号/授权码。")
        uid = uid.strip()
        if not uid.isdigit():
            return {"status": "failed", "error_type": "invalid_uid", "uid": uid}
        with self._connect() as imap:
            typ, _ = imap.select("INBOX", readonly=True)
            if typ != "OK":
                raise RuntimeError("无法打开收件箱。")
            typ, data = imap.fetch(uid, "(RFC822)")
            if typ != "OK" or not data or data[0] is None:
                return {"status": "failed", "error_type": "fetch_failed", "uid": uid}
            raw = data[0][1]
            if not isinstance(raw, bytes):
                return {"status": "failed", "error_type": "fetch_failed", "uid": uid}
            message = email.message_from_bytes(raw)
            body = _extract_text(message)
            return {
                "status": "ok",
                "uid": uid,
                "from": str(make_header(decode_header(message.get("From", "")))),
                "subject": str(make_header(decode_header(message.get("Subject", "")))),
                "date": message.get("Date", ""),
                "body": body[:8_000],
            }

    def _connect(self) -> "_ImapContext":
        host = "imap.qq.com"
        if "office365" in self._settings.jobagent_email_smtp_host or "outlook" in (
            self._settings.jobagent_email_smtp_host
        ):
            host = "outlook.office365.com"
        return _ImapContext(
            host,
            self._settings.jobagent_email_smtp_user.strip(),
            self._settings.jobagent_email_smtp_password,
        )

    @staticmethod
    def _since_criterion(days: int) -> str:
        from datetime import datetime, timedelta

        since = (datetime.now() - timedelta(days=max(0, days))).strftime("%d-%b-%Y")
        return f"SINCE {since}"

    def _fetch_summary(self, imap: imaplib.IMAP4, uid: bytes) -> InboxEmail | None:
        query = "(BODY[HEADER.FIELDS (FROM SUBJECT DATE)] BODY.PEEK[1])"
        typ, data = imap.fetch(uid.decode(), query)
        if typ != "OK" or not data or data[0] is None:
            return None
        header_blob = b""
        body_blob = b""
        for part in data:
            if isinstance(part, tuple) and len(part) >= 2:
                payload = part[1]
                if isinstance(payload, bytes):
                    if b"From:" in payload or b"Subject:" in payload or b"Date:" in payload:
                        header_blob = payload
                    else:
                        body_blob = payload
        if not header_blob:
            return None
        message = email.message_from_bytes(header_blob)
        from_raw = str(make_header(decode_header(message.get("From", ""))))
        addr, name = _split_address(from_raw)
        subject = str(make_header(decode_header(message.get("Subject", ""))))
        date_raw = message.get("Date", "")
        try:
            date = parsedate_to_datetime(date_raw).isoformat() if date_raw else ""
        except (TypeError, ValueError):
            date = date_raw
        snippet = body_blob.decode("utf-8", "replace")[:_MAX_SNIPPET_CHARS]
        return InboxEmail(
            uid=uid.decode(),
            from_addr=addr,
            from_name=name,
            subject=subject,
            date=date,
            snippet=snippet,
            thread_key=thread_key(subject, addr),
        )


class _ImapContext:
    """Login/logout around an IMAP4_SSL connection."""

    def __init__(self, host: str, user: str, password: str) -> None:
        self._imap = imaplib.IMAP4_SSL(host, 993, timeout=30)
        self._imap.login(user, password)

    def __enter__(self) -> imaplib.IMAP4:
        return self._imap

    def __exit__(self, *_: object) -> None:
        try:
            self._imap.logout()
        except imaplib.IMAP4.error:  # noqa: PERF203
            logger.debug("imap logout failed", exc_info=True)


def _split_address(from_raw: str) -> tuple[str, str]:
    import re

    match = re.search(r"<([^>]+)>", from_raw)
    if match:
        return match.group(1), from_raw[: match.start()].strip().strip('"')
    return from_raw.strip(), ""


def thread_key(subject: str, from_addr: str) -> str:
    """Group replies: strip Re:/Fwd: prefixes, lowercase the base subject."""

    base = _SUBJECT_PREFIX_RE.sub("", subject or "").strip().lower()
    return f"{from_addr.lower()}|{base}"


def _decode_part(part: email.message.Message) -> str | None:
    payload = part.get_payload(decode=True)
    if isinstance(payload, bytes):
        charset = part.get_content_charset() or "utf-8"
        return payload.decode(charset, "replace")
    return None


def _extract_text(message: email.message.Message) -> str:
    if message.is_multipart():
        for part in message.walk():
            if part.get_content_type() == "text/plain":
                text = _decode_part(part)
                if text:
                    return text
        for part in message.walk():
            if part.get_content_type() == "text/html":
                text = _decode_part(part)
                if text:
                    return text
        return ""
    return _decode_part(message) or ""
