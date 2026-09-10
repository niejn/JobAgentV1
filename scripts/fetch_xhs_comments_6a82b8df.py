# -*- coding: utf-8 -*-
"""只读脚本：抓取指定小红书笔记的全部评论（一级 + 二级）并保存快照。

用法（在 D:\\mashibing\\jobclaw 下）：
    D:\\mashibing\\jobclaw\\.venv\\Scripts\\python.exe scripts\\fetch_xhs_comments_6a82b8df.py

- Cookie 来源：jobagent auth cookie_manager（xhs），与 jobagent 平台读取链路一致。
- 仅调用 GET 评论接口（comment/page, comment/sub/page），不做任何写操作。
- 任何失败（风控/登录失效/接口异常）都会原样记录到 snapshot 元数据中，不重试绕过。
"""

import io
import json
import re
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, r"D:\mashibing\jobclaw")
sys.path.insert(0, r"D:\mashibing\jobclaw\spiders\Spider_XHS")

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")

from xhs_utils.xhs_pc import XHSPcAuth  # noqa: E402
from apis.xhs_pc_apis import XHS_Apis  # noqa: E402

NOTE_ID = "6a82b8df0000000025014840"
XSEC_TOKEN = "CB41eF9yXAZNEzvsfBedrZhtIe_CS075dyToNqc4DqZlw="
NOTE_URL = f"https://www.xiaohongshu.com/explore/{NOTE_ID}?xsec_token={XSEC_TOKEN}&xsec_source=app_share"
AUTHOR_ID = "5b31e6b2e8ac2b1d8c943c44"
AUTHOR_NAME = "冇名有姓"
NOTE_DIR = Path(
    r"D:\mashibing\jobclaw\data\xhs\冇名有姓_5b31e6b2e8ac2b1d8c943c44"
    r"\Ai初创团队招人啦！研发&增长运营～_6a82b8df0000000025014840"
)
COMMENTS_DIR = NOTE_DIR / "comments"
COMMENTS_DIR.mkdir(parents=True, exist_ok=True)

KEYWORDS = [
    "@", "邮箱", "email", "e-mail", "mail", ".com", ".cn", ".net", ".org", ".io",
    "微信", "weixin", "wechat", "vx", "v信", "私信", "内推", "投递", "投简历", "简历",
    "job@", "hr@", "招募", "联系方式", "telegram", "qq", "QQ", "官网", "网站",
    "app", "App", "APP", "公司", "产品", "demo", "链接", "link", "投喂", "DM", "dm",
    "联系我", "求联系", "戳我", "发送",
]
EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")
WX_RE = re.compile(r"(?:微信|weixin|wechat|vx|v信)[号:：\s]*[A-Za-z0-9_-]{4,}")
URL_RE = re.compile(
    r"(?:https?://|www\.)[^\s，。！？）)】\"]+"
    r"|[A-Za-z0-9-]+\.(?:com|cn|net|org|io|ai|app|me|co|xyz|tech)(?:/[^\s，。！？）)】\"]*)?"
)


def cookie_header() -> str:
    import json

    cookie_file = Path(r"C:\Users\julien\.jobagent\cookies\www.xiaohongshu.com_23-08-2026.json")
    payload = json.loads(cookie_file.read_text(encoding="utf-8-sig"))
    items = payload.get("cookies") if isinstance(payload, dict) else payload
    return "; ".join(
        f"{c.get('name')}={c.get('value')}" for c in items if c.get("name") and c.get("value")
    )


def main() -> int:
    meta: dict = {
        "note_id": NOTE_ID,
        "note_url": NOTE_URL,
        "author": {"user_id": AUTHOR_ID, "nickname": AUTHOR_NAME},
        "fetched_at": datetime.now().isoformat(timespec="seconds"),
        "purpose": "read-only comment research; no interaction of any kind",
    }
    failures: list[str] = []

    header = cookie_header()
    if not header:
        print("NO_COOKIE")
        meta["cookie_status"] = "missing"
        meta["fetch_status"] = "failed"
        (COMMENTS_DIR / "comments_snapshot.json").write_text(
            json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        return 2
    meta["cookie_status"] = "loaded"

    auth = XHSPcAuth.from_cookie(header)
    api = XHS_Apis(auth).bootstrap()

    out_comments: list[dict] = []
    cursor = ""
    page = 0
    while True:
        page += 1
        success, msg, res_json = api.get_note_out_comment(NOTE_ID, cursor, XSEC_TOKEN)
        if not success:
            failures.append(f"page {page} failed: {msg}")
            meta["fetch_status"] = "failed"
            meta["failures"] = failures
            meta["pages_fetched"] = page - 1
            meta["top_level_comments_partial"] = out_comments
            (COMMENTS_DIR / "comments_snapshot.json").write_text(
                json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            print(f"FAIL page {page}: {msg}")
            return 1
        data = res_json.get("data", {}) if isinstance(res_json, dict) else {}
        comments = data.get("comments", []) or []
        out_comments.extend(comments)
        has_more = data.get("has_more")
        new_cursor = data.get("cursor")
        print(f"page {page}: +{len(comments)} (total {len(out_comments)}) has_more={has_more}")
        if not has_more:
            break
        if new_cursor in (None, "", cursor):
            break
        cursor = str(new_cursor)
        if page >= 40:
            failures.append("safety stop after 40 top-level pages")
            break

    sub_total = 0
    for c in list(out_comments):
        try:
            if not c.get("sub_comment_has_more"):
                continue
            sub_cursor = c.get("sub_comment_cursor", "")
            guard = 0
            while True:
                guard += 1
                if guard > 20:
                    failures.append(f"sub-comment safety stop for root {c.get('id')}")
                    break
                s2, m2, r2 = api.get_note_inner_comment(
                    {"note_id": NOTE_ID, "id": c.get("id")},
                    str(sub_cursor),
                    XSEC_TOKEN,
                )
                if not s2:
                    failures.append(f"sub comments for root {c.get('id')} failed: {m2}")
                    break
                d2 = r2.get("data", {}) if isinstance(r2, dict) else {}
                subs = d2.get("comments", []) or []
                c.setdefault("sub_comments", []).extend(subs)
                sub_total += len(subs)
                if not d2.get("has_more"):
                    break
                sub_cursor = d2.get("cursor", "")
                if sub_cursor in (None, ""):
                    break
        except Exception as exc:  # noqa: BLE001
            failures.append(f"sub-comment exception root {c.get('id')}: {exc}")

    meta["fetch_status"] = "ok" if not failures else "partial"
    meta["failures"] = failures
    meta["pages_fetched"] = page
    meta["top_level_count"] = len(out_comments)
    meta["sub_comment_count"] = sub_total

    def flatten(items: list, level: int = 1, parent=None) -> list:
        rows: list[dict] = []
        for it in items:
            u = it.get("user_info") or {}
            target = it.get("target_comment")
            rows.append(
                {
                    "comment_id": it.get("id"),
                    "parent_id": parent,
                    "level": level,
                    "user_id": u.get("user_id"),
                    "nickname": u.get("nickname"),
                    "is_note_author": u.get("user_id") == AUTHOR_ID,
                    "content": it.get("content"),
                    "like_count": it.get("like_count"),
                    "ip_location": it.get("ip_location"),
                    "create_time": it.get("create_time"),
                    "sub_comment_count": it.get("sub_comment_count"),
                    "target_comment_id": target.get("id") if isinstance(target, dict) else None,
                }
            )
            subs = it.get("sub_comments") or []
            if subs:
                rows.extend(flatten(subs, level + 1, it.get("id")))
        return rows

    flat = flatten(out_comments)
    meta["total_flat_comments"] = len(flat)
    meta["author_comment_count"] = sum(1 for r in flat if r["is_note_author"])

    (COMMENTS_DIR / "comments_raw.json").write_text(
        json.dumps(out_comments, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    (COMMENTS_DIR / "comments_flat.json").write_text(
        json.dumps(flat, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    author_rows = [r for r in flat if r["is_note_author"]]
    (COMMENTS_DIR / "author_comments.txt").write_text(
        "\n\n".join(
            f"[{r['level']}级] id={r['comment_id']} parent={r['parent_id']} like={r['like_count']} "
            f"time={r['create_time']} ip={r['ip_location']}\n{r['content']}"
            for r in author_rows
        ),
        encoding="utf-8",
    )

    hits = []
    for r in flat:
        text = r["content"] or ""
        matched = sorted({k for k in KEYWORDS if k in text})
        emails = EMAIL_RE.findall(text)
        wxs = WX_RE.findall(text)
        urls = URL_RE.findall(text)
        if matched or emails or wxs or urls:
            hits.append(
                {
                    "comment_id": r["comment_id"],
                    "nickname": r["nickname"],
                    "user_id": r["user_id"],
                    "level": r["level"],
                    "is_note_author": r["is_note_author"],
                    "content": text,
                    "matched_keywords": matched,
                    "emails": emails,
                    "wechat_likes": wxs,
                    "urls": urls,
                }
            )
    (COMMENTS_DIR / "keyword_hits.json").write_text(
        json.dumps(hits, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    meta["keyword_hit_count"] = len(hits)
    (COMMENTS_DIR / "comments_snapshot.json").write_text(
        json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    print(f"DONE top={len(out_comments)} flat={len(flat)} author={meta['author_comment_count']} hits={len(hits)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
