from __future__ import annotations

import json
from pathlib import Path

import pytest

from jobagent.auth.cookie_manager import CookieNotFoundError, load_cookie_export


def test_load_complete_boss_cookie_export(tmp_path: Path) -> None:
    path = tmp_path / "cookies.json"
    path.write_text(
        json.dumps(
            {
                "cookies": [
                    {"name": "wt2", "value": "a", "domain": ".zhipin.com"},
                    {"name": "bst", "value": "b", "domain": ".zhipin.com"},
                    {"name": "__zp_stoken__", "value": "c", "domain": ".zhipin.com"},
                ]
            }
        ),
        encoding="utf-8",
    )

    cookies = load_cookie_export(path)

    assert {cookie["name"] for cookie in cookies} == {"wt2", "bst", "__zp_stoken__"}


def test_reject_incomplete_boss_cookie_export(tmp_path: Path) -> None:
    path = tmp_path / "cookies.json"
    path.write_text(json.dumps({"cookies": [{"name": "wt2", "value": "a"}]}))

    with pytest.raises(CookieNotFoundError, match="incomplete"):
        load_cookie_export(path)
