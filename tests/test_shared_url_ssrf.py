"""SSRF regression tests for shared-URL validation (code review MEDIUM M1)."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

import jobagent.tools.shared_url as shared_url_module
from jobagent.tools.shared_url import SharedUrlSaveRequest


@pytest.fixture(autouse=True)
def _stub_resolver(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        shared_url_module, "_RESOLVER", lambda host: ["93.184.216.34"]
    )


@pytest.mark.parametrize(
    "bad_url",
    [
        "http://127.0.0.1:8080/admin",
        "http://10.0.0.5/",
        "http://192.168.1.1/",
        "http://169.254.169.254/latest/meta-data/",  # cloud metadata
        "http://0x7f000001/",  # hex loopback
        "http://2130706433/",  # decimal loopback
        "http://0177.0.0.1/",  # octal loopback
        "http://localhost/x",
        "http://printer.local/",
        "http://[::1]/",
        "http://[fe80::1]/",  # link-local v6
    ],
)
def test_literal_and_obfuscated_private_targets_are_rejected(bad_url: str) -> None:
    with pytest.raises(ValidationError):
        SharedUrlSaveRequest(url=bad_url)


def test_dns_rebinding_to_private_space_is_rejected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        shared_url_module, "_RESOLVER", lambda host: ["169.254.169.254"]
    )
    with pytest.raises(ValidationError, match="resolves to a private"):
        SharedUrlSaveRequest(url="https://rebind.attacker.example/latest")


def test_docker_internal_alias_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        shared_url_module, "_RESOLVER", lambda host: ["192.168.65.2"]
    )
    with pytest.raises(ValidationError, match="resolves to a private"):
        SharedUrlSaveRequest(url="http://host.docker.internal:2375")


def test_unresolvable_host_fails_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(host: str) -> list[str]:
        raise OSError("no DNS")

    monkeypatch.setattr(shared_url_module, "_RESOLVER", boom)
    with pytest.raises(ValidationError, match="cannot be resolved"):
        SharedUrlSaveRequest(url="https://nonexistent.example.invalid/x")


def test_public_host_still_passes() -> None:
    assert SharedUrlSaveRequest(url="https://example.com/x").url == (
        "https://example.com/x"
    )
