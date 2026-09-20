"""mitmproxy addon capturing Boss resume-popup traffic (online resume design G1-G4).

Run with ``mitmdump -s scripts/mitm_capture_resume_flow.py``. Appends each
matched flow (request body + response body) as one JSON line to the
git-ignored ``.ua/resume_flow_capture.jsonl``; nothing is printed.

Captures (see docs/boss-online-resume-delivery-design.md):
- exchange/testAccept      -> response body = G1/G3 hypothesis (checkInfo,
                              secureExchange, maybe the online-resume entry)
- resume/attachment/checkbox.json -> corroborates capability flags
- exchange/accept          -> request body = G2 (attachment path)
- exchange/auth/accept     -> request body = G2/G4 (online-resume auth path?)
- zpgeek resume / webresume endpoints -> G1 fallback (online resume source)
"""

from __future__ import annotations

import json
import time
from pathlib import Path

from mitmproxy import http

_PATTERNS = (
    "/wapi/zpchat/exchange/",
    "/wapi/zpgeek/resume/",
    "/wflow/zpgeek/webresume",
    "/wapi/zpgeek/common/data/header",
    "/wapi/zpchat/geek/getBossData",
)

_OUTPUT = Path(".ua/resume_flow_capture.jsonl")


def _match(path: str) -> str | None:
    for pattern in _PATTERNS:
        if pattern in path:
            return pattern
    return None


class ResumeFlowCapture:
    def response(self, flow: http.HTTPFlow) -> None:
        request = flow.request
        pattern = _match(request.path)
        if pattern is None:
            return
        entry = {
            "captured_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            "pattern": pattern,
            "method": request.method,
            "url": request.url.split("?", 1)[0],
            "request_body": request.get_text(strict=False) or "",
            "response_status": flow.response.status_code if flow.response else None,
            "response_body": (
                flow.response.get_text(strict=False) or ""
                if flow.response is not None
                else ""
            )[:20000],
        }
        _OUTPUT.parent.mkdir(parents=True, exist_ok=True)
        with _OUTPUT.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(entry, ensure_ascii=False) + "\n")


addons = [ResumeFlowCapture()]
