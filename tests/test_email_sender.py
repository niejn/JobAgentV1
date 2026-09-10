import smtplib

import pytest

from jobagent.applier.email_sender import EmailSender
from jobagent.config import Settings


@pytest.mark.parametrize("message_id", ["", "<persisted@example>"])
def test_email_sender_returns_generated_message_id(monkeypatch, message_id):
    sent = []

    class FakeSmtp:
        def login(self, user, password):
            return None

        def send_message(self, message):
            sent.append(message)

        def __enter__(self):
            return self

        def __exit__(self, *_):
            return None

    monkeypatch.setattr("jobagent.applier.email_sender._smtp_connection", lambda *_: FakeSmtp())
    settings = Settings(
        _env_file=None,
        jobagent_email_smtp_host="smtp.example.com",
        jobagent_email_smtp_user="sender@example.com",
        jobagent_email_smtp_password="token",
    )
    result = EmailSender(settings).send(
        to="hr@example.com", subject="测试", body_html="<p>正文</p>", message_id=message_id
    )
    assert result["status"] == "ok"
    assert result["message_id"] == sent[0]["Message-ID"]
    if message_id:
        assert result["message_id"] == message_id


@pytest.mark.parametrize(
    "failure,expected",
    [
        ("disconnect", "unverified"),
        ("rejected", "failed"),
        ("quit", "unverified"),
    ],
)
def test_smtp_uncertainty_is_not_retryable_failure(monkeypatch, failure, expected):
    class Smtp:
        def login(self, *args):
            pass

        def send_message(self, message):
            if failure == "disconnect":
                raise smtplib.SMTPServerDisconnected("disconnected during DATA")
            if failure == "rejected":
                raise smtplib.SMTPDataError(550, b"rejected")

        def __enter__(self):
            return self

        def __exit__(self, *args):
            if failure == "quit":
                raise smtplib.SMTPResponseException(500, b"QUIT error after accepted DATA")

    monkeypatch.setattr("jobagent.applier.email_sender._smtp_connection", lambda *_: Smtp())
    result = EmailSender(
        Settings(
            _env_file=None,
            jobagent_email_smtp_password="test-token",
        )
    ).send(to="hr@example.com", subject="test", body_html="test", message_id="<saved@example>")
    assert result["status"] == expected
    assert result["message_id"] == "<saved@example>"
