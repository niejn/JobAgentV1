from jobagent.applier.email_sender import EmailSender
from jobagent.config import Settings


def test_email_sender_returns_generated_message_id(monkeypatch):
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
        to="hr@example.com", subject="测试", body_html="<p>正文</p>"
    )
    assert result["status"] == "ok"
    assert result["message_id"] == sent[0]["Message-ID"]
