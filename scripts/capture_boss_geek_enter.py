"""Capture one Boss ``geekEnter`` request from a fresh Chrome tab.

The output is intentionally written to a local ignored file.  It contains
the form body needed to reproduce the request, so it must never be committed
or pasted into chat.  Use a disposable/uncontacted test posting.
"""

from __future__ import annotations

import argparse
import asyncio
import json
from datetime import UTC, datetime
from pathlib import Path

from playwright.async_api import async_playwright

_ENDPOINT = "/wapi/zpchat/session/geekEnter"
_BUTTON_SELECTORS = (
    "a.btn-startchat",
    "a[ka='job_detail_chat']",
    "a:has-text('立即沟通')",
    "button:has-text('立即沟通')",
)


async def capture(job_url: str, output: Path) -> None:
    async with async_playwright() as playwright:
        browser = await playwright.chromium.connect_over_cdp(
            "http://127.0.0.1:9222", timeout=10_000
        )
        context = next((item for item in browser.contexts if item.pages), None)
        if context is None:
            raise RuntimeError("没有找到已登录的 Chrome context")
        page = await context.new_page()
        try:
            async with page.expect_request(
                lambda request: request.method == "POST" and _ENDPOINT in request.url,
                timeout=30_000,
            ) as request_info:
                async with page.expect_response(
                    lambda response: response.request.method == "POST"
                    and _ENDPOINT in response.url,
                    timeout=30_000,
                ) as response_info:
                    await page.goto(job_url, wait_until="domcontentloaded", timeout=30_000)
                    button = None
                    for selector in _BUTTON_SELECTORS:
                        candidate = page.locator(selector).first
                        try:
                            if await candidate.is_visible():
                                button = candidate
                                break
                        except Exception:
                            continue
                    if button is None:
                        raise RuntimeError("未找到“立即沟通”按钮")
                    await button.click()
            request = await request_info.value
            response = await response_info.value
            post_data = request.post_data or ""
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_text(
                json.dumps(
                    {
                        "captured_at": datetime.now(UTC).isoformat(),
                        "method": request.method,
                        "url_path": request.url.split("?", 1)[0],
                        "post_data": post_data,
                        "post_data_length": len(post_data),
                        "response_status": response.status,
                    },
                    ensure_ascii=False,
                    indent=2,
                ),
                encoding="utf-8",
            )
            print(f"captured_file={output}")
            print(f"method={request.method} url_path={request.url.split('?', 1)[0]}")
            print(f"post_data_length={len(post_data)} response_status={response.status}")
            print("请不要打印或粘贴 post_data；告诉我 captured_file 路径即可。")
        finally:
            await page.close()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("job_url", help="未联系过的 Boss 职位详情 URL")
    parser.add_argument(
        "--output",
        type=Path,
        default=Path(".ua/geek_enter_capture.json"),
        help="本地输出文件（默认 .ua/geek_enter_capture.json）",
    )
    args = parser.parse_args()
    asyncio.run(capture(args.job_url, args.output))


if __name__ == "__main__":
    main()
