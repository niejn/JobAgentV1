"""Boss直聘 (zhipin.com) scraper using Playwright."""

from __future__ import annotations

import logging
from hashlib import sha256
from typing import Self
from urllib.parse import urlencode

from playwright.async_api import Browser, Playwright, async_playwright
from pydantic import BaseModel, Field, HttpUrl

from jobagent.models import Job, JobSource, SalaryRange
from jobagent.scraper.base import BaseScraper

logger = logging.getLogger(__name__)

_CITY_CODES = {
    "全国": "100010000",
    "北京": "101010100",
    "上海": "101020100",
    "广州": "101280100",
    "深圳": "101280600",
    "杭州": "101210100",
}


class BossDiscoveryRequest(BaseModel):
    """User-level filters for read-only Boss job discovery."""

    query: str = Field(min_length=1)
    city: str = "全国"
    area: str | None = None
    company_sizes: list[str] = Field(default_factory=list)
    limit: int = Field(default=20, ge=1, le=100)


class BossScraper(BaseScraper):
    """Scrape job listings from Boss直聘."""

    source = JobSource.BOSS

    def __init__(self, settings: object) -> None:
        self._settings = settings
        self._playwright: Playwright | None = None
        self._browser: Browser | None = None

    async def __aenter__(self) -> Self:
        self._playwright = await async_playwright().start()
        self._browser = await self._playwright.chromium.launch(
            headless=getattr(self._settings, "jobagent_headless", True),
        )
        return self

    async def __aexit__(self, *args: object) -> None:
        if self._browser:
            await self._browser.close()
        if self._playwright:
            await self._playwright.stop()

    async def scrape_jobs(
        self,
        query: str,
        location: str | None = None,
        limit: int = 20,
    ) -> list[Job]:
        """Scrape Boss直聘 for jobs matching query.

        Uses cookie-based auth from settings.boss_cookie.
        """
        city = location if location in _CITY_CODES else "全国"
        area = location if location and location not in _CITY_CODES else None
        return await self.discover_jobs(
            BossDiscoveryRequest(query=query, city=city, area=area, limit=limit)
        )

    async def discover_jobs(self, request: BossDiscoveryRequest) -> list[Job]:
        """Discover and post-filter jobs by city, area, and company-size bands."""

        if not self._browser:
            raise RuntimeError("Scraper not initialized. Use 'async with' context.")
        city_code = _CITY_CODES.get(request.city)
        if city_code is None:
            raise ValueError(f"Unsupported Boss city: {request.city}")

        context = await self._browser.new_context()
        try:
            from jobagent.auth.cookie_manager import inject_cookies
            await inject_cookies(context, "boss", self._settings)
        except Exception as e:
            logger.warning("Cookie injection failed (continuing without auth): %s", e)

        page = await context.new_page()
        search_url = "https://www.zhipin.com/web/geek/job?" + urlencode(
            {"query": request.query, "city": city_code}
        )

        jobs: list[Job] = []
        try:
            await page.goto(search_url, wait_until="networkidle", timeout=30000)
            cards = await page.query_selector_all(".job-card-wrapper")

            for card in cards:
                try:
                    title_el = await card.query_selector(".job-name")
                    company_el = await card.query_selector(".company-name a")
                    salary_el = await card.query_selector(".salary")
                    link_el = await card.query_selector(".job-card-left a")
                    area_el = await card.query_selector(".job-area")
                    tags_els = await card.query_selector_all(
                        ".tag-list span, .tag-list li"
                    )
                    company_tags_els = await card.query_selector_all(
                        ".company-tag-list span, .company-tag-list li"
                    )
                    desc_el = await card.query_selector(".job-card-desc")

                    title = await title_el.inner_text() if title_el else "Unknown"
                    company = await company_el.inner_text() if company_el else "Unknown"
                    salary_text = await salary_el.inner_text() if salary_el else ""
                    href = await link_el.get_attribute("href") if link_el else ""
                    tags = [await t.inner_text() for t in tags_els]
                    company_tags = [await t.inner_text() for t in company_tags_els]
                    desc = await desc_el.inner_text() if desc_el else ""
                    job_area = await area_el.inner_text() if area_el else request.city
                    company_size = _extract_company_size(company_tags)

                    if request.area and request.area not in job_area:
                        continue
                    if (
                        request.company_sizes
                        and company_size not in request.company_sizes
                    ):
                        continue

                    url = f"https://www.zhipin.com{href}" if href else "https://www.zhipin.com"

                    salary = _parse_boss_salary(salary_text)

                    jobs.append(Job(
                        id=f"boss:{sha256(url.encode('utf-8')).hexdigest()[:20]}",
                        source=JobSource.BOSS,
                        title=title.strip(),
                        company=company.strip(),
                        location=job_area.strip(),
                        url=HttpUrl(url),
                        description=desc.strip(),
                        salary=salary,
                        tags=tags,
                        metadata={
                            "company_size": company_size,
                            "company_tags": company_tags,
                            "city": request.city,
                            "area_filter": request.area,
                        },
                    ))
                    if len(jobs) >= request.limit:
                        break
                except Exception as e:
                    logger.warning("Failed to parse Boss card: %s", e)
                    continue

        except Exception as e:
            logger.error("Boss scrape failed: %s", e)
        finally:
            await context.close()

        logger.info("Boss: scraped %d jobs for query '%s'", len(jobs), request.query)
        return jobs


def _extract_company_size(company_tags: list[str]) -> str | None:
    for value in company_tags:
        normalized = value.strip().replace(" ", "")
        if "人" in normalized and any(character.isdigit() for character in normalized):
            return normalized
    return None


def _parse_boss_salary(text: str) -> SalaryRange | None:
    """Parse Boss salary text like '25-50K·16薪' into SalaryRange."""
    import re

    match = re.search(r"(\d+)-(\d+)K", text)
    if not match:
        return None

    low = int(match.group(1)) * 1000
    high = int(match.group(2)) * 1000

    # Check for bonus months (e.g. 16薪)
    months = 12
    bonus_match = re.search(r"(\d+)薪", text)
    if bonus_match:
        months = int(bonus_match.group(1))

    return SalaryRange(
        min_annual=low * months,
        max_annual=high * months,
        currency="CNY",
    )
