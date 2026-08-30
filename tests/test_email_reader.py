"""Tests for read-only inbox helpers (thread grouping, address split)."""

from __future__ import annotations

from email.message import Message

from jobagent.applier.email_reader import (
    _decode_part,
    _split_address,
    thread_key,
)


class TestThreadKey:
    def test_reply_prefixes_group_together(self) -> None:
        base = thread_key("应聘全栈工程师", "hr@example.com")
        reply = thread_key("Re: 应聘全栈工程师", "hr@example.com")
        forward = thread_key("Fwd: 应聘全栈工程师", "hr@example.com")
        assert base == reply == forward

    def test_different_sender_splits(self) -> None:
        assert thread_key("同一主题", "a@x.com") != thread_key("同一主题", "b@x.com")

    def test_case_insensitive_subject(self) -> None:
        assert thread_key("Re: Agent Engineer", "a@x.com") == thread_key(
            "agent engineer", "a@x.com"
        )


class TestSplitAddress:
    def test_named_address(self) -> None:
        addr, name = _split_address('陈女士 <tongzh@qinchengsoft.com>')
        assert addr == "tongzh@qinchengsoft.com"
        assert name == "陈女士"

    def test_bare_address(self) -> None:
        assert _split_address("tongzh@qinchengsoft.com") == (
            "tongzh@qinchengsoft.com",
            "",
        )


class TestDecodePart:
    def test_plain_bytes_decoded(self) -> None:
        message = Message()
        message.set_payload("面试链接：https://meet.example.com/x".encode())
        assert "meet.example.com" in (_decode_part(message) or "")

    def test_missing_payload_returns_none(self) -> None:
        message = Message()
        assert _decode_part(message) is None
