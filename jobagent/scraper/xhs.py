"""Xiaohongshu (小红书) referral-post scraper using Playwright.

Searches referral notes, fetches note details + comments, and fetches the
author's profile page (bio, IP location, recent note titles) which feeds the
account-authenticity analysis (docs/referral-design.md §4.5.2).

Risk control notes (measured, see design doc §2):
- XHS blocks even *read* access after a few rapid requests -> all page loads
  are paced with minute-level random delays (``xhs_scrape_delay_*``).
- The scraper aborts on risk-control/captcha pages instead of hammering on.
"""

from __future__ import annotations

import asyncio
import logging
import random
import re
from datetime import datetime
from typing import Self
from urllib.parse import quote

from playwright.async_api import Browser, ElementHandle, Page, Playwright, async_playwright

from jobagent.auth.cookie_manager import inject_cookies
from jobagent.config import Settings
from jobagent.domain import AuthorProfile, ReferralPost

logger = logging.getLogger(__name__)

_XHS_BASE = "https://www.xiaohongshu.com"

_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
)

# Risk-control / error markers shown by XHS when access is restricted.
_RISK_CONTROL_MARKERS = [
    "滑动验证",
    "请完成安全验证",
    "访问异常",
    "当前笔记暂时无法浏览",
    "账号存在异常",
    "登录继续访问",
]

# Search result note cards (multiple fallbacks - XHS DOM drifts often)
_CARD_SELECTORS = ["section.note-item", ".note-item", "div.note-item"]

# Note detail page (search_result/explore pages share the detail DOM)
_NOTE_TITLE = ["#detail-title", ".note-content .title", "div.title"]
_NOTE_CONTENT = ["#detail-desc", ".note-content", ".desc"]
_NOTE_AUTHOR = [".username", ".author-wrapper .name", ".author .name"]
_NOTE_AUTHOR_LINK = ["a[href*='/user/profile/']", ".author .avatar a"]
_NOTE_DATE = [".bottom-container .date", ".date", "div.date"]
_NOTE_TAGS = [".note-content .tag", ".tag"]
_NOTE_LIKES = [".engage-bar .count", ".like-wrapper .count", ".count"]

# Comments
_COMMENT_ITEMS = [".comment-item", ".comments-container .comment-item", "div.comment-item"]
_COMMENT_AUTHOR = [".author .name", ".name"]
_COMMENT_CONTENT = [".content", ".note-text"]

# Author profile page
_AUTHOR_DESC = [".user-desc", ".desc", "div.user-desc"]
_AUTHOR_IP = [".ip-location", ".user-info .location", ".info-list .ip"]
_AUTHOR_NOTE_TITLES = [".note-item .title", ".notes-container .title", "section.note-item .title"]

_NOTE_ID_RE = re.compile(r"/(?:search_result|explore|discovery/item)/([0-9a-f]+)")
_AUTHOR_ID_RE = re.compile(r"/user/profile/([0-9a-f]+)")
_IP_TAIL_RE = re.compile(r"(?:20\d{2}-\d{2}-\d{2})?(.+)$")


class XhsRiskControlError(RuntimeError):
    """Raised when XHS shows a risk-control / captcha / block page."""


class XhsScraper:
    """Scrape referral posts (notes) from Xiaohongshu.

    Usage::

        async with XhsScraper(settings) as scraper:
            posts = await scraper.scrape_referral_posts(query="内推 社招")
            author = await scraper.fetch_author_profile(author_id)
    """

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._playwright: Playwright | None = None
        self._browser: Browser | None = None

    # -- lifecycle ----------------------------------------------------------

    async def __aenter__(self) -> Self:
        self._playwright = await async_playwright().start()
        self._browser = await self._playwright.chromium.launch(
            headless=self._settings.jobagent_headless,
            args=["--disable-blink-features=AutomationControlled"],
        )
        return self

    async def __aexit__(self, *args: object) -> None:
        if self._browser:
            await self._browser.close()
        if self._playwright:
            await self._playwright.stop()

    # -- public API ---------------------------------------------------------

    async def new_page(self) -> Page:
        """Create an authenticated page with a realistic UA."""
        if not self._browser:
            raise RuntimeError("XhsScraper not initialised - use 'async with'.")
        context = await self._browser.new_context(user_agent=_USER_AGENT)
        try:
            await inject_cookies(context, "xhs", self._settings)
        except Exception as e:
            logger.warning("Cookie injection failed (continuing without auth): %s", e)
        return await context.new_page()

    async def scrape_referral_posts(
        self,
        query: str,
        *,
        limit: int | None = None,
        comments_per_post: int = 20,
    ) -> list[ReferralPost]:
        """Search referral notes and build ReferralPost objects.

        Args:
            query: Search keywords, e.g. "内推 社招" or "字节 内推".
            limit: Max notes to process (defaults to ``xhs_referral_max_posts``).
            comments_per_post: How many top comments to capture.

        Returns:
            List of ReferralPost. May be short/empty if risk control kicks in;
            in that case an XhsRiskControlError is logged and previously
            scraped posts are returned.
        """
        limit = limit or self._settings.xhs_referral_max_posts
        page = await self.new_page()
        context = page.context
        posts: list[ReferralPost] = []
        try:
            cards = await self._search(page, query)
            logger.info("XHS search '%s': %d cards", query, len(cards))

            for card in cards[:limit]:
                try:
                    post = await self._scrape_note(page, card, comments_per_post)
                    if post:
                        posts.append(post)
                        await self._pacing_delay()
                except XhsRiskControlError:
                    logger.warning("Risk control hit after %d posts - stopping early", len(posts))
                    break
                except Exception as e:
                    logger.warning("Failed to scrape note card: %s", e)
                    continue
        finally:
            await context.close()

        logger.info("XHS: scraped %d referral posts for '%s'", len(posts), query)
        return posts

    async def fetch_author_profile(
        self, author_id: str, *, max_titles: int = 10
    ) -> AuthorProfile | None:
        """Fetch an author's public profile for authenticity analysis (S1-S6).

        Visits ``/user/profile/{author_id}`` and extracts bio, IP location and
        recent note titles.
        """
        page = await self.new_page()
        context = page.context
        try:
            url = f"{_XHS_BASE}/user/profile/{author_id}"
            await page.goto(url, wait_until="domcontentloaded", timeout=30_000)
            await self._check_risk_control(page)
            await page.wait_for_timeout(2000)

            desc = await self._first_text(page, _AUTHOR_DESC)
            ip = await self._first_text(page, _AUTHOR_IP)

            titles: list[str] = []
            for sel in _AUTHOR_NOTE_TITLES:
                els = await page.query_selector_all(sel)
                for el in els[:max_titles]:
                    try:
                        text = (await el.inner_text()).strip()
                        if text:
                            titles.append(text)
                    except Exception:
                        continue
                if titles:
                    break

            return AuthorProfile(
                author_id=author_id,
                nickname="",
                description=desc or "",
                ip_location=self._clean_ip(ip),
                recent_note_titles=titles[:max_titles],
            )
        except XhsRiskControlError:
            logger.warning("Risk control while fetching author %s", author_id)
            return None
        except Exception as e:
            logger.warning("Failed to fetch author profile %s: %s", author_id, e)
            return None
        finally:
            await context.close()

    # -- internal: search ----------------------------------------------------

    async def _search(self, page: Page, query: str) -> list[dict]:
        """Run a keyword search and return raw card info dicts."""
        url = f"{_XHS_BASE}/search_result?keyword={quote(query)}"
        await page.goto(url, wait_until="domcontentloaded", timeout=30_000)
        await self._check_risk_control(page)
        # Search results render async - give the SPA time
        await page.wait_for_timeout(3000)

        cards: list[dict] = []
        for sel in _CARD_SELECTORS:
            els = await page.query_selector_all(sel)
            if not els:
                continue
            for el in els:
                info = await self._parse_card(el)
                if info and info.get("note_id"):
                    cards.append(info)
            if cards:
                break

        # de-dup by note id, keep order
        seen: set[str] = set()
        unique: list[dict] = []
        for c in cards:
            if c["note_id"] not in seen:
                seen.add(c["note_id"])
                unique.append(c)
        return unique

    async def _parse_card(self, el: ElementHandle) -> dict[str, str] | None:
        """Parse one search-result card into {note_id, url, title, author...}."""
        try:
            link = await el.query_selector("a[href]")
            if not link:
                return None
            href = (await link.get_attribute("href")) or ""
            m = _NOTE_ID_RE.search(href)
            if not m:
                return None
            note_id = m.group(1)

            title_el = await el.query_selector(".title") or link
            title = (await title_el.inner_text()).strip() if title_el else ""

            author_el = await el.query_selector(".author .name, .name, .author")
            author = (await author_el.inner_text()).strip() if author_el else ""

            # href may be relative or absolute; keep query string (xsec_token)
            full_url = href if href.startswith("http") else f"{_XHS_BASE}{href}"
            return {
                "note_id": note_id,
                "url": full_url,
                "title": title,
                "author_name": author,
            }
        except Exception as e:
            logger.debug("Card parse failed: %s", e)
            return None

    # -- internal: note detail ------------------------------------------------

    async def _scrape_note(
        self,
        page: Page,
        card: dict[str, str],
        comments_per_post: int,
    ) -> ReferralPost | None:
        """Open one note from search results and extract details + comments."""
        await page.goto(card["url"], wait_until="domcontentloaded", timeout=30_000)
        await self._check_risk_control(page)
        await page.wait_for_timeout(2000)

        title = await self._first_text(page, _NOTE_TITLE) or card.get("title", "")
        content = await self._first_text(page, _NOTE_CONTENT) or ""
        author = await self._first_text(page, _NOTE_AUTHOR) or card.get("author_name", "")
        date_text = await self._first_text(page, _NOTE_DATE)
        likes_text = await self._first_text(page, _NOTE_LIKES)

        tags: list[str] = []
        for sel in _NOTE_TAGS:
            els = await page.query_selector_all(sel)
            for el in els:
                try:
                    t = (await el.inner_text()).strip().lstrip("#")
                    if t:
                        tags.append(t)
                except Exception:
                    continue
            if tags:
                break

        author_id = await self._author_id(page)
        comments = await self._scrape_comments(page, comments_per_post)

        return ReferralPost(
            id=card["note_id"],
            url=card["url"],
            author_name=author,
            author_id=author_id,
            title=title,
            content=content,
            tags=tags,
            likes=_parse_count(likes_text),
            comments_count=len(comments),
            published_at=_parse_date(date_text),
            top_comments=comments,
        )

    async def _scrape_comments(self, page: Page, limit: int) -> list[str]:
        """Capture the top-N visible comments (lazy loaded - scroll once)."""
        try:
            await page.mouse.wheel(0, 2000)
            await page.wait_for_timeout(1500)
        except Exception:
            pass

        comments: list[str] = []
        for sel in _COMMENT_ITEMS:
            els = await page.query_selector_all(sel)
            for el in els[:limit]:
                text = await self._first_text_in(el, _COMMENT_CONTENT)
                if text:
                    comments.append(text.strip())
            if comments:
                break
        return comments

    async def _author_id(self, page: Page) -> str:
        for sel in _NOTE_AUTHOR_LINK:
            try:
                el = await page.query_selector(sel)
                if el:
                    href = (await el.get_attribute("href")) or ""
                    m = _AUTHOR_ID_RE.search(href)
                    if m:
                        return m.group(1)
            except Exception:
                continue
        return ""

    # -- internal: helpers -----------------------------------------------------

    async def _first_text(self, page: Page, selectors: list[str]) -> str | None:
        for sel in selectors:
            try:
                el = await page.query_selector(sel)
                if el:
                    text = (await el.inner_text()).strip()
                    if text:
                        return text
            except Exception:
                continue
        return None

    async def _first_text_in(
        self,
        el: ElementHandle,
        selectors: list[str],
    ) -> str | None:
        for sel in selectors:
            try:
                child = await el.query_selector(sel)
                if child:
                    text = (await child.inner_text()).strip()
                    if text:
                        return text
            except Exception:
                continue
        return None

    async def _check_risk_control(self, page: Page) -> None:
        """Raise XhsRiskControlError if the current page is a block/captcha."""
        try:
            body = await page.inner_text("body")
        except Exception:
            return
        for marker in _RISK_CONTROL_MARKERS:
            if marker in body:
                raise XhsRiskControlError(f"XHS risk control marker on page: {marker}")

    async def _pacing_delay(self) -> None:
        """Minute-level random delay between note fetches (risk-control mitigation)."""
        lo = self._settings.xhs_scrape_delay_min
        hi = max(lo, self._settings.xhs_scrape_delay_max)
        await asyncio.sleep(random.uniform(lo, hi))

    @staticmethod
    def _clean_ip(raw: str | None) -> str | None:
        """Normalize IP location text like 'IP属地：北京' or '2025-12-01北京'."""
        if not raw:
            return None
        m = _IP_TAIL_RE.search(raw.strip())
        text = (m.group(1) if m else raw).replace("IP属地：", "").replace("IP属地:", "").strip()
        return text or None


def _parse_count(text: str | None) -> int:
    """Parse like counts like '328', '1.2万' into int."""
    if not text:
        return 0
    text = text.strip()
    m = re.search(r"([\d.]+)(万)?", text)
    if not m:
        return 0
    value = float(m.group(1))
    if m.group(2):
        value *= 10_000
    return int(value)


def _parse_date(text: str | None) -> datetime | None:
    """Parse XHS date strings like '2025-12-01' or '编辑于 昨天'.

    Currently only supports precise dates; relative dates return None
    (published_at is best-effort only).
    """
    if not text:
        return None
    m = re.search(r"(20\d{2}-\d{2}-\d{2})", text)
    if not m:
        return None
    try:
        from datetime import datetime

        return datetime.strptime(m.group(1), "%Y-%m-%d")
    except ValueError:
        return None
