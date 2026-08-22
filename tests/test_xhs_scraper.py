"""Tests for XhsScraper - mock Playwright-based scraping flow."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from jobagent.config import Settings
from jobagent.domain import AuthorProfile, ReferralPost
from jobagent.scraper.xhs import (
    XhsRiskControlError,
    _parse_count,
    _parse_date,
)


@pytest.fixture()
def settings() -> Settings:
    return Settings(
        xhs_cookie="test-session",
        xhs_referral_max_posts=5,
        xhs_scrape_delay_min=5.0,
        xhs_scrape_delay_max=10.0,
        jobagent_headless=True,
    )


# ---------------------------------------------------------------------------
# Pure helper tests
# ---------------------------------------------------------------------------


class TestHelpers:
    def test_parse_count_plain(self) -> None:
        assert _parse_count("328") == 328

    def test_parse_count_wan(self) -> None:
        assert _parse_count("1.2万") == 12_000

    def test_parse_count_empty(self) -> None:
        assert _parse_count(None) == 0
        assert _parse_count("") == 0

    def test_parse_date_iso(self) -> None:
        dt = _parse_date("编辑于 2025-12-01")
        assert dt is not None
        assert (dt.year, dt.month, dt.day) == (2025, 12, 1)

    def test_parse_date_relative_returns_none(self) -> None:
        assert _parse_date("昨天 12:30") is None
        assert _parse_date(None) is None


# ---------------------------------------------------------------------------
# Model tests
# ---------------------------------------------------------------------------


class TestReferralModels:
    def test_referral_post_defaults(self) -> None:
        post = ReferralPost(id="abc123", title="字节内推")
        assert post.author_id == ""
        assert post.tags == []
        assert post.top_comments == []
        assert post.scraped_at is not None

    def test_author_profile_defaults(self) -> None:
        profile = AuthorProfile(author_id="u1")
        assert profile.recent_note_titles == []
        assert profile.description == ""

    def test_offer_score_bounds(self) -> None:
        from jobagent.domain import ReferralOffer

        offer = ReferralOffer(post_id="p1", company="字节跳动")
        assert offer.scam_risk == 0.0
        assert offer.authenticity_score == 0.0

        with pytest.raises(ValidationError):
            ReferralOffer(post_id="p2", company="x", confidence=1.5)

    def test_clean_ip(self) -> None:
        assert XhsCleanIp("IP属地：北京") == "北京"


def XhsCleanIp(raw: str | None) -> str | None:
    """Small wrapper to access the static method without a scraper instance."""
    from jobagent.scraper.xhs import XhsScraper

    return XhsScraper._clean_ip(raw)


# ---------------------------------------------------------------------------
# Scraper flow tests (mocked Playwright)
# ---------------------------------------------------------------------------


def _make_el(text: str | None = None, href: str | None = None, visible: bool = True):
    from unittest.mock import AsyncMock

    el = AsyncMock()
    el.inner_text = AsyncMock(return_value=text or "")
    el.get_attribute = AsyncMock(return_value=href)
    el.is_visible = AsyncMock(return_value=visible)
    return el


def _make_note_page(
    *,
    title: str = "字节跳动 内推 长期有效",
    content: str = "我司字节商业化招后端，欢迎投递 #内推",
    date: str = "编辑于 2025-12-01",
    likes: str = "328",
    author: str = "字节老王",
    author_href: str = "/user/profile/60e6b306000000000101ef15",
    risk_marker: str | None = None,
):
    from unittest.mock import AsyncMock

    page = AsyncMock()
    page.url = "https://www.xiaohongshu.com/search_result/abc?xsec_token=t"

    body_text = " ".join(filter(None, [title, content, date, author, risk_marker]))
    page.inner_text = AsyncMock(return_value=body_text)
    page.wait_for_timeout = AsyncMock()
    page.mouse = AsyncMock()

    async def query_selector(sel: str):
        if risk_marker and "安全验证" in risk_marker and sel == "body":
            return None
        if sel in ("#detail-title", ".note-content .title", "div.title"):
            return _make_el(title)
        if sel in ("#detail-desc", ".note-content", ".desc"):
            return _make_el(content)
        if sel in (".username", ".author-wrapper .name", ".author .name"):
            return _make_el(author)
        if sel in (".bottom-container .date", ".date", "div.date"):
            return _make_el(date)
        if sel in (".engage-bar .count", ".like-wrapper .count", ".count"):
            return _make_el(likes)
        if sel.startswith("a[href"):
            return _make_el(href=author_href)
        return None

    async def query_selector_all(sel: str):
        if sel in (".note-content .tag", ".tag"):
            return [_make_el("#内推"), _make_el("#社招")]
        if sel in (".comment-item", ".comments-container .comment-item", "div.comment-item"):
            return []
        return []

    page.query_selector = AsyncMock(side_effect=query_selector)
    page.query_selector_all = AsyncMock(side_effect=query_selector_all)
    return page


class TestScrapeNote:
    @pytest.mark.asyncio
    async def test_note_detail_parsed(self, settings: Settings) -> None:
        from jobagent.scraper.xhs import XhsScraper

        scraper = XhsScraper(settings)
        page = _make_note_page()
        card = {
            "note_id": "6a4de02d000000000f01fa94",
            "url": "https://www.xiaohongshu.com/search_result/6a4de02d?xsec_token=t",
            "title": "字节跳动 内推 长期有效",
            "author_name": "字节老王",
        }

        post = await scraper._scrape_note(page, card, comments_per_post=5)

        assert post is not None
        assert post.id == "6a4de02d000000000f01fa94"
        assert post.title == "字节跳动 内推 长期有效"
        assert post.author_name == "字节老王"
        assert post.author_id == "60e6b306000000000101ef15"
        assert post.likes == 328
        assert "内推" in post.tags
        assert post.published_at is not None
        assert post.published_at.year == 2025

    @pytest.mark.asyncio
    async def test_risk_control_raises(self, settings: Settings) -> None:
        from jobagent.scraper.xhs import XhsScraper

        scraper = XhsScraper(settings)
        page = _make_note_page(risk_marker="请完成安全验证")
        card = {"note_id": "x", "url": "https://www.xiaohongshu.com/search_result/x"}

        with pytest.raises(XhsRiskControlError):
            await scraper._scrape_note(page, card, comments_per_post=5)
