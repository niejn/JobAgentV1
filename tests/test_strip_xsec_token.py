"""xsec_token must never appear in model-facing tool results (review M2)."""

from __future__ import annotations

from jobagent.scraper.xhs_backend import strip_xsec_token


def test_strip_removes_only_xsec_token() -> None:
    url = "https://www.xiaohongshu.com/explore/abc123?xsec_token=SECRET&xsec_source=pc"
    out = strip_xsec_token(url)
    assert "SECRET" not in out
    assert "xsec_token" not in out
    assert "xsec_source=pc" in out
    assert out.startswith("https://www.xiaohongshu.com/explore/abc123")


def test_strip_keeps_other_query_params_and_plain_urls() -> None:
    assert strip_xsec_token("https://x.test/n?id=5&other=1") == "https://x.test/n?id=5&other=1"
    assert strip_xsec_token("https://x.test/n") == "https://x.test/n"
    assert strip_xsec_token("") == ""
