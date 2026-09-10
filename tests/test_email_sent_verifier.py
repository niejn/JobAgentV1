import imaplib

import pytest

from jobagent.applier.email_sent_verifier import EmailSentFolderVerifier
from jobagent.config import Settings


def test_unconfigured_sent_verifier_is_unverified():
    result = EmailSentFolderVerifier("", 993, "", "").find("<id@example>")
    assert result.status == "unverified"
    assert result.reason == "imap_not_configured"


def test_sent_verifier_reuses_smtp_credentials():
    verifier = EmailSentFolderVerifier.from_smtp_settings(
        Settings(
            _env_file=None,
            jobagent_email_smtp_host="smtp.qq.com",
            jobagent_email_smtp_user="u",
        )
    )
    assert verifier._host == "imap.qq.com"
    assert verifier._port == 993
    assert verifier._user == "u"


@pytest.mark.parametrize(
    "mode,expected",
    [
        ("hit", "submitted"),
        ("miss", "unverified"),
        ("substring", "unverified"),
        ("folder_missing", "unverified"),
        ("search_error", "unverified"),
        ("fetch_error", "unverified"),
        ("network_error", "unverified"),
    ],
)
def test_sent_lookup_requires_exact_message_id_and_closes_connection(monkeypatch, mode, expected):
    calls = []

    class Imap:
        def login(self, user, password):
            calls.append("login")
            if mode == "network_error":
                raise imaplib.IMAP4.abort("disconnected")

        def select(self, folder, readonly):
            assert folder == "CustomSent"
            assert readonly is True
            return ("NO" if mode == "folder_missing" else "OK"), []

        def uid(self, command, *args):
            calls.append(command)
            if command == "search":
                assert args == ("HEADER", "Message-ID", '"<id@example>"')
                return ("NO" if mode == "search_error" else "OK"), [
                    b"" if mode == "miss" else b"12"
                ]
            assert args == (b"12", "(BODY.PEEK[HEADER.FIELDS (MESSAGE-ID)])")
            value = b"prefix<id@example>suffix" if mode == "substring" else b"<id@example>"
            return ("NO" if mode == "fetch_error" else "OK"), [
                (b"header", b"Message-ID: " + value + b"\r\n\r\n")
            ]

        def logout(self):
            calls.append("logout")

    monkeypatch.setattr(imaplib, "IMAP4_SSL", lambda *args, **kwargs: Imap())
    result = EmailSentFolderVerifier("imap.example", 993, "user", "token", "CustomSent").find(
        "<id@example>"
    )
    assert result.status == expected
    assert calls[-1] == "logout"
    if mode == "folder_missing":
        assert "search" not in calls


def test_explicit_imap_configuration():
    verifier = EmailSentFolderVerifier.from_smtp_settings(
        Settings(
            _env_file=None,
            jobagent_email_imap_host="mail.example",
            jobagent_email_imap_port=1993,
            jobagent_email_sent_folder="Sent Items",
        )
    )
    assert (verifier._host, verifier._port, verifier._folder) == (
        "mail.example",
        1993,
        "Sent Items",
    )
