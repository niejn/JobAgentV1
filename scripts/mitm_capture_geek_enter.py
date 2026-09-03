"""mitmproxy addon for one local-only Boss geekEnter capture.

Run with ``mitmdump -s scripts/mitm_capture_geek_enter.py``.  The captured
form body is written to an ignored .ua file and is never printed.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

from mitmproxy import http

_ENDPOINT = "/wapi/zpchat/session/geekEnter"
_OUTPUT = Path(".ua/geek_enter_capture.json")


class GeekEnterCapture:
    def request(self, flow: http.HTTPFlow) -> None:
        request = flow.request
        if request.method != "POST" or _ENDPOINT not in request.path:
            return
        _OUTPUT.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "captured_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            "method": request.method,
            "url_path": request.path.split("?", 1)[0],
            "post_data": request.get_text(strict=False) or "",
            "post_data_length": len(request.raw_content or b""),
            "request_header_names": sorted(request.headers.keys()),
            "response_status": None,
        }
        flow.metadata["geek_enter_capture"] = payload
        _OUTPUT.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    def response(self, flow: http.HTTPFlow) -> None:
        payload = flow.metadata.get("geek_enter_capture")
        if not isinstance(payload, dict):
            return
        payload["response_status"] = flow.response.status_code if flow.response else None
        _OUTPUT.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


addons = [GeekEnterCapture()]
