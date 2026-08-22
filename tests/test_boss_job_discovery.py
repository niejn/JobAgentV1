"""Behavior tests for read-only Boss job discovery."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from jobagent.config import Settings
from jobagent.scraper.boss import BossDiscoveryRequest, BossScraper


@pytest.mark.asyncio
async def test_discovers_encoded_shanghai_jobs_and_filters_area_and_company_size() -> None:
    matching = _job_card(
        title="AI Agent 工程师",
        company="小而美科技",
        area="上海·杨浦区·五角场",
        company_tags=["人工智能", "未融资", "0-20人"],
    )
    wrong_size = _job_card(
        title="Python 工程师",
        company="大型科技",
        area="上海·杨浦区·五角场",
        company_tags=["互联网", "10000人以上"],
    )
    page = AsyncMock()
    page.goto = AsyncMock()
    page.query_selector_all = AsyncMock(return_value=[matching, wrong_size])
    context = AsyncMock()
    context.new_page = AsyncMock(return_value=page)
    browser = AsyncMock()
    browser.new_context = AsyncMock(return_value=context)
    playwright = MagicMock()
    playwright.chromium.launch = AsyncMock(return_value=browser)
    playwright.stop = AsyncMock()
    manager = MagicMock()
    manager.start = AsyncMock(return_value=playwright)
    settings = Settings(_env_file=None, boss_cookie="test-cookie")

    with patch("jobagent.scraper.boss.async_playwright", return_value=manager):
        async with BossScraper(settings) as scraper:
            jobs = await scraper.discover_jobs(
                BossDiscoveryRequest(
                    query="AI Agent & Python",
                    city="上海",
                    area="五角场",
                    company_sizes=["0-20人", "20-99人"],
                    limit=10,
                )
            )

    requested_url = page.goto.await_args.args[0]
    assert "city=101020100" in requested_url
    assert "query=AI+Agent+%26+Python" in requested_url
    assert len(jobs) == 1
    assert jobs[0].company == "小而美科技"
    assert jobs[0].location == "上海·杨浦区·五角场"
    assert jobs[0].metadata["company_size"] == "0-20人"


def _job_card(
    *,
    title: str,
    company: str,
    area: str,
    company_tags: list[str],
) -> AsyncMock:
    elements = {
        ".job-name": _element(text=title),
        ".company-name a": _element(text=company),
        ".salary": _element(text="20-30K·13薪"),
        ".job-card-left a": _element(href="/job_detail/test.html"),
        ".job-area": _element(text=area),
    }
    card = AsyncMock()
    card.query_selector = AsyncMock(side_effect=lambda selector: elements.get(selector))
    card.query_selector_all = AsyncMock(
        side_effect=lambda selector: (
            [_element(text="Python"), _element(text="Agent")]
            if selector == ".tag-list span, .tag-list li"
            else [_element(text=value) for value in company_tags]
            if selector == ".company-tag-list span, .company-tag-list li"
            else []
        )
    )
    return card


def _element(*, text: str = "", href: str | None = None) -> AsyncMock:
    element = AsyncMock()
    element.inner_text = AsyncMock(return_value=text)
    element.get_attribute = AsyncMock(return_value=href)
    return element
