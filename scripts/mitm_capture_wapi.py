"""mitmproxy addon: record every Boss wapi round trip to .ua/wapi_capture.jsonl.

Run with ``mitmdump -s scripts/mitm_capture_wapi.py`` from the repo root.
Used to mine write endpoints (e.g. conversation delete) from one manual
web-UI session: request body + response land per line, never printed.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

from mitmproxy import http

_OUTPUT = Path(".ua/wapi_capture.jsonl")
_BODY_LIMIT = 2000


class WapiCapture:
    def response(self, flow: http.HTTPFlow) -> None:
        request = flow.request
        if "zhipin.com" not in request.pretty_host or "/wapi/" not in request.path:
            return
        response = flow.response
        payload = {
            "captured_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            "method": request.method,
            "url": request.url[:600],
            "request_body": (request.get_text(strict=False) or "")[:_BODY_LIMIT],
            "response_status": response.status_code if response else None,
            "response_body": (
                (response.get_text(strict=False) or "")[:_BODY_LIMIT] if response else ""
            ),
        }
        _OUTPUT.parent.mkdir(parents=True, exist_ok=True)
        with _OUTPUT.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(payload, ensure_ascii=False) + "\n")


addons = [WapiCapture()]
