"""Read Boss直聘 chat lists (HR greetings) via the user's Chrome (CDP).

Transport: the same page-scope fetch route proven by BossResumeUploader
(2026-08-28) - Boss's own axios request-interceptor headers are mirrored
(X-Requested-With, Content-Type, traceId, token, `_` cache-buster) and the
`__zp_stoken__` proof rides along in cookies via credentials:"include".

APIs (extracted verbatim from Boss's public geek-chat-core.2.0.3.umd.min.js):

    GET /wapi/zprelation/friend/geekFilterByLabel?labelId=<n>
        -> zpData.friendList[]   (label-filtered conversation list)
    POST /wapi/zprelation/friend/getGeekFriendList.json
        -> paged list (unfiltered)

friendList item shape (from the library's own `formateFriends`):
    friendId, friendSource, encryptFriendId, name (HR), brandName (company),
    jobName / positionName (job), bossTitle, jobCity, updateTime,
    lastMessageInfo (last message), unreadCount (merged from local store).

labelId semantics (partial - calibration point for live verification):
    0  = 全部 (library init calls getFriendsByLabel({labelId: 0}))
    -1 = internal filtered view (getFilteredFriend)
    The Boss App's other filter tabs (未读/新招呼/仅沟通/有交换/有面试/
    不感兴趣) each map to an id defined in the chat-page UI chunk, not in
    the core library - pass label_id explicitly once calibrated.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Mapping
from types import ModuleType
from typing import Any
from urllib.parse import urlsplit

from playwright.async_api import async_playwright

from jobagent.applier import boss_chat_session as session_mod
from jobagent.auth.boss_debug_chrome import BossDebugChromeError, ensure_boss_debug_chrome
from jobagent.auth.browser_login import sync_boss_cookies_from_cdp_context
from jobagent.auth.cookie_manager import get_cookies
from jobagent.config import Settings
from jobagent.journey.job_registry import SQLiteJobRegistry
from jobagent.journey.resume_requests import RESUME_CARD_TYPE
from jobagent.scraper.cdp_tab_pool import CdpTabPool

httpx: ModuleType | None
try:
    import httpx
except ImportError:  # optional transport dependency
    httpx = None

logger = logging.getLogger(__name__)

#: Non-text Boss message labels (chat-core enum; see
#: docs/boss-card-message-ingestion-design.md). Only ACTIONABLE types are
#: written into ``text`` - the sole channel the daemon inbound pipeline and
#: the reply classifier read - so stickers/images cannot flood the human
#: review queue. Every other enumerated type surfaces as ``card_label`` on
#: the tool result (agent-visible, behavior-neutral).
_CARD_LABELS: dict[int, str] = {
    2: "[语音消息]",
    3: "[图片消息]",
    4: "[动作卡片]",
    7: "[对话卡片]",
    8: "[职位卡片]",
    9: "[简历请求卡片]",
    12: "[链接消息]",
    13: "[视频消息]",
    14: "[面试卡片]",
    19: "[简历分享]",
    20: "[表情]",
    25: "[评价]",
}


def _apply_card_semantics(message: dict[str, Any]) -> None:
    """Label one mapped history message that carries no text.

    Card messages (resume requests, job cards...) have no ``body.text`` by
    protocol; without this the TCL 2026-09-23 resume-request card read back
    as an empty message. ``bodyJson`` from the page JS becomes ``body_json``
    for structure mining once a raw-body capture exists.
    """

    body_json = str(message.pop("bodyJson", "") or "")
    if body_json:
        message["body_json"] = body_json
    if str(message.get("text") or ""):
        return
    try:
        message_type = int(message.get("type"))
    except (TypeError, ValueError):
        return
    if message_type == RESUME_CARD_TYPE:
        message["text"] = _CARD_LABELS[RESUME_CARD_TYPE]
    elif message_type in _CARD_LABELS:
        message["card_label"] = _CARD_LABELS[message_type]


async def _get_parked_page(pool: Any) -> tuple[Any, bool]:
    return await session_mod.get_chat_page(pool)


_CHAT_PAGE_PATH = "/web/geek/chat"
_LABEL_IDS = {"全部": 0}  # calibration point: remaining tabs' ids unknown
_NAV_TIMEOUT_MS = 30_000
_LIST_TIMEOUT_S = 45.0
_MAX_FRIENDS = 100


async def _sync_live_boss_cookies(settings: Settings) -> int:
    """Refresh the HTTP session from the same debug Chrome used for login."""

    await ensure_boss_debug_chrome(settings)
    driver = await async_playwright().start()
    browser: Any | None = None
    try:
        browser = await driver.chromium.connect_over_cdp(
            settings.debug_chrome_cdp_endpoint, timeout=10_000
        )
        context = next((item for item in browser.contexts if item.pages), None)
        if context is None:
            raise RuntimeError("Boss debug Chrome has no browser context")
        return await sync_boss_cookies_from_cdp_context(context)
    finally:
        if browser is not None:
            try:
                await browser.close()
            except Exception:
                pass
        await driver.stop()


async def list_boss_greetings_http(
    settings: Settings,
    *,
    label_id: int = 0,
    limit: int = _MAX_FRIENDS,
) -> dict[str, Any]:
    """List Boss conversations through the read-only relation endpoint.

    This adapter does not load or evaluate ``/web/geek/chat``. It only briefly
    attaches to the logged-in CDP context to refresh cookies, then uses the
    relation endpoint; it remains safe when the chat SPA renderer is blocked.
    """

    if httpx is None:
        return {"status": "failed", "error_type": "httpx_unavailable"}
    try:
        saved = await _sync_live_boss_cookies(settings)
        logger.info("Boss HTTP chat list: synchronized %d live cookies", saved)
    except Exception:
        logger.warning("Boss HTTP chat list: live cookie sync unavailable", exc_info=True)
        return {
            "status": "failed",
            "error_type": "boss_login_required",
            "message": "无法从调试 Chrome 同步 Boss Cookie；请在该 Chrome 登录后重试。",
        }
    items = await get_cookies("boss", settings)
    cookies = {
        str(item["name"]): str(item["value"])
        for item in items
        if item.get("name") and item.get("value")
    }
    headers = {
        "User-Agent": "Mozilla/5.0",
        "Accept": "application/json, text/plain, */*",
        "Origin": "https://www.zhipin.com",
        "Referer": "https://www.zhipin.com/web/geek/chat",
        "X-Requested-With": "XMLHttpRequest",
        "zp_token": cookies.get("bst", ""),
    }
    try:
        async with httpx.AsyncClient(
            cookies=cookies, headers=headers, timeout=20, follow_redirects=True
        ) as client:
            response = await client.get(
                "https://www.zhipin.com/wapi/zprelation/friend/geekFilterByLabel",
                params={"labelId": label_id, "_": int(time.time() * 1000)},
            )
        body = response.json()
    except Exception as exc:
        return {
            "status": "failed",
            "error_type": "conversation_list_request_failed",
            "message": type(exc).__name__,
        }
    code = body.get("code") if isinstance(body, dict) else None
    if response.status_code != 200 or code != 0:
        return {
            "status": "failed",
            "error_type": "boss_login_required" if code == 7 else "api_rejected",
            "code": code,
            "message": str(body.get("message") or "Boss conversation list rejected")[:200]
            if isinstance(body, dict)
            else "Boss conversation list rejected",
        }
    friends = ((body.get("zpData") or {}).get("friendList") or []) if isinstance(body, dict) else []
    real = [
        friend
        for friend in friends
        if isinstance(friend, dict) and int(friend.get("friendId") or 0) > 1000
    ]
    greetings: list[dict[str, Any]] = []
    for friend in real[: max(1, min(limit, _MAX_FRIENDS))]:
        metadata = _normalize_job_metadata(
            {
                "jobId": friend.get("encryptJobId") or friend.get("jobId"),
                "jobUrl": friend.get("jobUrl") or friend.get("jobDetailUrl"),
                "title": friend.get("jobName") or friend.get("positionName"),
                "company": friend.get("brandName"),
                "city": friend.get("jobCity"),
                "source": "conversation_friend_http",
            },
            source="conversation_friend_http",
        )
        metadata = metadata or _resolve_registry_job_metadata(
            settings,
            company=str(friend.get("brandName") or ""),
            title=str(friend.get("jobName") or friend.get("positionName") or ""),
        )
        friend = dict(friend)
        friend["job_metadata"] = metadata
        greetings.append(friend)
    logger.info("Boss HTTP chat list: %d friends (labelId=%s)", len(greetings), label_id)
    return {
        "status": "ok",
        "transport": "http",
        "filter_label_id": label_id,
        "count": len(greetings),
        "greetings": greetings,
    }


def _normalize_job_metadata(
    value: Mapping[str, Any] | None,
    *,
    source: str,
) -> dict[str, str] | None:
    """Return a safe, canonical Boss job reference from chat payload metadata."""

    if not value:
        return None
    raw_id = value.get("jobId") or value.get("job_id") or value.get("encryptJobId")
    job_id = str(raw_id or "").strip()
    raw_url = str(value.get("jobUrl") or value.get("job_url") or "").strip()
    if raw_url:
        parsed = urlsplit(raw_url)
        if (
            parsed.hostname
            and (
                parsed.hostname == "zhipin.com"
                or parsed.hostname.endswith(".zhipin.com")
            )
            and "/job_detail/" in parsed.path
        ):
            raw_url = f"https://www.zhipin.com{parsed.path}"
            job_id = job_id or parsed.path.rsplit("/", 1)[-1].removesuffix(".html")
        else:
            raw_url = ""
    if not raw_url and job_id:
        raw_url = f"https://www.zhipin.com/job_detail/{job_id}.html"
    if not job_id and not raw_url:
        return None
    return {
        "job_id": job_id,
        "job_url": raw_url,
        "title": str(value.get("title") or value.get("jobName") or ""),
        "company": str(value.get("company") or value.get("brandName") or ""),
        "city": str(value.get("city") or value.get("jobCity") or ""),
        "source": str(value.get("source") or source),
    }


def _normalized_match_key(value: str) -> str:
    """Normalize display labels for an exact, non-guessing registry match."""

    return "".join(value.casefold().split())


def _resolve_registry_job_metadata(
    settings: Settings, *, company: str, title: str
) -> dict[str, str] | None:
    """Recover one known Boss posting only when company and title are unambiguous."""

    if not company.strip() or not title.strip():
        return None

    with SQLiteJobRegistry(settings.jobagent_state_db) as registry:
        matches = [
            record
            for record in registry.list_records(company=company, limit=20)
            if record.source == "boss"
            and record.url
            and _normalized_match_key(record.company) == _normalized_match_key(company)
            and _normalized_match_key(record.title) == _normalized_match_key(title)
        ]
    if len(matches) != 1:
        return None
    record = matches[0]
    return _normalize_job_metadata(
        {
            "jobId": record.job_id.removeprefix("boss:"),
            "jobUrl": record.url,
            "title": record.title,
            "company": record.company,
            "city": record.location,
            "source": "job_registry_exact_match",
        },
        source="job_registry_exact_match",
    )


_FETCH_LIST_JS = r"""
async (payload) => {
  const jobFrom = (f, source) => {
    const jobId = String(
      f.encryptJobId || f.jobId || f.encryptPositionId || f.positionId || "",
    );
    const rawUrl = String(f.jobUrl || f.jobDetailUrl || f.job_url || f.url || "");
    const match = rawUrl.match(/https?:\/\/[^\s"']*zhipin\.com\/job_detail\/[^\s"']+/i);
    const jobUrl = match
      ? match[0].replace(/[?#].*$/, "")
      : (jobId ? "https://www.zhipin.com/job_detail/" + jobId + ".html" : "");
    return {
      jobId,
      jobUrl,
      title: f.jobName || f.positionName || f.jobTitle || "",
      company: f.brandName || f.companyName || "",
      city: f.jobCity || f.cityName || "",
      source,
    };
  };
  const bstMatch = document.cookie.match(/(?:^|;\s*)bst=([^;]+)/);
  const zpToken = bstMatch ? decodeURIComponent(bstMatch[1]) : "";
  const base = {
    "X-Requested-With": "XMLHttpRequest",
    "traceId": "F-" + Math.random().toString(36).slice(2, 8)
      + Date.now().toString(36),
  };
  if (zpToken) base["zp_token"] = zpToken;
  const qs = "labelId=" + payload.labelId + "&_=" + Date.now();
  const res = await fetch(
    "/wapi/zprelation/friend/geekFilterByLabel?" + qs,
    {method: "GET", credentials: "include", headers: base},
  ).then((r) => r.json()).catch((e) => ({code: -1, message: String(e)}));
  if (res.code !== 0) return {
    code: res.code, message: res.message,
    stokenPresent: document.cookie.indexOf("__zp_stoken__") !== -1,
  };
  const friends = (res.zpData && res.zpData.friendList) || [];
  return {
    code: 0,
    friends: friends.map((f) => ({
      friendId: f.friendId,
      friendSource: f.friendSource,
      encryptFriendId: f.encryptFriendId || "",
      encryptBossId: f.encryptBossId || f.encryptFriendId || "",
      securityId: f.securityId || "",
      name: f.name || "",
      brandName: f.brandName || "",
      jobName: f.jobName || "",
      positionName: f.positionName || "",
      bossTitle: f.bossTitle || "",
      jobCity: f.jobCity || "",
      updateTime: f.updateTime || 0,
      jobMetadata: jobFrom(f, "conversation_friend"),
      lastMessage: (f.lastMessageInfo && {
        text: String(
          f.lastMessageInfo.text
          || f.lastMessageInfo.lastContent
          || f.lastMessageInfo.content
          || "",
        ).slice(0, 120),
        fromId: f.lastMessageInfo.fromId || 0,
      }) || null,
    })),
  };
}
"""


# Full conversation read in ONE evaluate: list -> match -> creds ->
# history, all page-internal fetches with a single CDP round-trip.
# (Live finding: warlock tolerates only ~2-3 automated fetch rounds per
# page load; the previous 3-stage Python flow burned them all at once.)
_FETCH_CONVERSATION_JS = r"""
async (payload) => {
  const jobFrom = (f, source) => {
    const jobId = String(
      f.encryptJobId || f.jobId || f.encryptPositionId || f.positionId || "",
    );
    const rawUrl = String(f.jobUrl || f.jobDetailUrl || f.job_url || f.url || "");
    const match = rawUrl.match(/https?:\/\/[^\s"']*zhipin\.com\/job_detail\/[^\s"']+/i);
    const jobUrl = match
      ? match[0].replace(/[?#].*$/, "")
      : (jobId ? "https://www.zhipin.com/job_detail/" + jobId + ".html" : "");
    return {
      jobId,
      jobUrl,
      title: f.jobName || f.positionName || f.jobTitle || "",
      company: f.brandName || f.companyName || "",
      city: f.jobCity || f.cityName || "",
      source,
    };
  };
  const jobFromMessage = (m) => {
    const b = m.body || {};
    const candidates = [m, b, b.card, b.job, b.jobCard, b.data].filter(Boolean);
    for (const item of candidates) {
      const job = jobFrom(item, "conversation_message");
      if (job.jobUrl || job.jobId) return job;
      const text = JSON.stringify(item);
      const match = text.match(/https?:\/\/[^\s"']*zhipin\.com\/job_detail\/[^\s"']+/i);
      if (match) {
        return {
          ...job,
          jobUrl: match[0].replace(/[?#].*$/, ""),
          source: "conversation_message",
        };
      }
    }
    return null;
  };
  // Header matrix verified against net03 captures of SUCCESSFUL calls:
  //   filterByLabel/historyMsg: zp_token (+XRW for history)
  //   getGeekFriendList: zp_token + form-urlencoded body, NO token/XRW
  // zp_token's value IS cookie "bst" (chat-core reads it via CookieUtil).
  const bstMatch = document.cookie.match(/(?:^|;\s*)bst=([^;]+)/);
  const zpToken = bstMatch ? decodeURIComponent(bstMatch[1]) : "";
  const traceId = "F-" + Math.random().toString(36).slice(2, 8)
    + Date.now().toString(36);
  const jq = (p) => "&_=" + Date.now();
  const base = {"traceId": traceId};
  if (zpToken) base["zp_token"] = zpToken;

  // 1) label list -> find the friend by name
  const listHeaders = {...base, "X-Requested-With": "XMLHttpRequest"};
  const list = await fetch(
    "/wapi/zprelation/friend/geekFilterByLabel?labelId=0" + jq(),
    {method: "GET", credentials: "include", headers: listHeaders},
  ).then((r) => r.json()).catch((e) => ({code: -1, message: String(e)}));
  if (list.code !== 0) return {step: "list", code: list.code, message: list.message};
  const friends = ((list.zpData || {}).friendList) || [];
  const target = friends.find((f) =>
    Number(f.friendId) > 1000 &&
    (payload.friendId
      ? String(f.friendId) === String(payload.friendId)
      : f.name === payload.hrName));
  if (!target) return {step: "match", code: 0, message: "conversation not found"};

  // 2) credentials (securityId rides in getGeekFriendList's result)
  let securityId = target.securityId || "";
  let bossId = target.encryptBossId || target.encryptFriendId || "";
  if (!securityId) {
    const formBody = String(target.friendSource) === "1"
      ? "dzFriendIds=" + encodeURIComponent(target.friendId)
      : "friendIds=" + encodeURIComponent(target.friendId);
    const credsHeaders = {
      ...base, "Content-Type": "application/x-www-form-urlencoded",
    };
    const creds = await fetch(
      "/wapi/zprelation/friend/getGeekFriendList.json?" + jq().slice(1),
      {method: "POST", credentials: "include", headers: credsHeaders,
       body: formBody},
    ).then((r) => r.json()).catch((e) => ({code: -1, message: String(e)}));
    if (creds.code !== 0) {
      return {step: "creds", code: creds.code, message: creds.message};
    }
    const full = ((creds.zpData || {}).result || []).find(
      (f) => String(f.friendId || f.uid) === String(target.friendId));
    if (!full || !full.securityId) {
      return {step: "creds", code: 0, message: "no securityId returned"};
    }
    securityId = full.securityId;
    bossId = full.encryptBossId || bossId;
    Object.assign(target, full);
  }
  if (!bossId) return {step: "creds", code: 0, message: "no encryptBossId"};

  // 3) history
  const histHeaders = {...base, "X-Requested-With": "XMLHttpRequest"};
  const hist = await fetch(
    "/wapi/zpchat/geek/historyMsg?bossId=" + encodeURIComponent(bossId)
      + "&maxMsgId=0&c=20&page=" + payload.page + "&src=0"
      + "&securityId=" + encodeURIComponent(securityId) + jq(),
    {method: "GET", credentials: "include", headers: histHeaders},
  ).then((r) => r.json()).catch((e) => ({code: -1, message: String(e)}));
  if (hist.code !== 0) return {step: "history", code: hist.code, message: hist.message};
  const msgs = ((hist.zpData || {}).messages) || [];
  const friendJob = jobFrom(target, "conversation_friend");
  const messageJobs = msgs.map(jobFromMessage).filter(Boolean);
  const job = friendJob.jobUrl || friendJob.jobId
    ? friendJob : (messageJobs[0] || friendJob);
  return {
    step: "done",
    friendId: target.friendId,
    company: target.brandName || target.companyName || "",
    title: target.jobName || target.positionName || target.jobTitle || "",
    job,
    jobCandidates: messageJobs,
    messages: msgs.map((m) => {
      const b = m.body || {};
      const fromUid = m.fromId || (m.from && m.from.uid) || 0;
      const text = String(b.text || m.text || b.content || m.content || "")
        .slice(0, 500);
      return {
        mid: m.mid || m.msgId || m.cmid || 0,
        direction: String(fromUid) === String(((window._PAGE || {}).uid))
          ? "geek" : "boss",
        fromId: fromUid,
        toId: m.toId || (m.to && m.to.uid) || 0,
        time: m.time || m.createTime || 0,
        type: m.type != null ? m.type : m.messageType,
        job: jobFromMessage(m),
        text,
        // Card payloads (resume requests, job cards...) carry no text; pass
        // the raw body through so Python can label and mine it.
        bodyJson: text ? "" : JSON.stringify(b).slice(0, 400),
      };
    }),
  };
}
"""

class BossChatReader:
    """List Boss chat conversations (who greeted us) - read-only."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._playwright: Any | None = None
        self._context: Any | None = None
        self._tab_pool: CdpTabPool | None = None

    async def __aenter__(self) -> BossChatReader:
        self._playwright = await async_playwright().start()
        try:
            await ensure_boss_debug_chrome(self._settings)
            browser = await self._playwright.chromium.connect_over_cdp(
                self._settings.debug_chrome_cdp_endpoint,
                timeout=10_000,
            )
        except BossDebugChromeError as exc:
            await self._playwright.stop()
            raise ConnectionError(str(exc)) from exc
        except Exception as exc:
            await self._playwright.stop()
            raise ConnectionError(
                "无法连接调试 Chrome（CDP）。请先启动带 --remote-debugging-port 的 Chrome。"
            ) from exc
        self._context = next((c for c in browser.contexts if c.pages), None)
        if self._context is None:
            await self._playwright.stop()
            raise ConnectionError("调试 Chrome 中没有可用的浏览器上下文。")
        self._tab_pool = CdpTabPool(self._context)
        return self

    async def __aexit__(self, *exc: object) -> None:
        if self._tab_pool is not None:
            await self._tab_pool.close()
        self._context = None
        if self._playwright is not None:
            await self._playwright.stop()
            self._playwright = None

    async def list_greetings(
        self,
        *,
        filter_name: str = "全部",
        label_id: int | None = None,
        limit: int = _MAX_FRIENDS,
    ) -> dict[str, Any]:
        """Return the conversation list, optionally label-filtered."""

        if label_id is None:
            if filter_name not in _LABEL_IDS:
                return {
                    "status": "failed",
                    "error_type": "unknown_filter",
                    "message": (
                        f"过滤器「{filter_name}」的 labelId 尚未校准"
                        f"（已知：{sorted(_LABEL_IDS)}）。可用 label_id=<int> 直接指定。"
                    ),
                }
            label_id = _LABEL_IDS[filter_name]
        assert self._tab_pool is not None
        try:
            return await self._list_on_page(
                self._tab_pool, label_id=label_id, limit=limit
            )
        except TimeoutError:
            return {
                "status": "failed",
                "error_type": "tab_pool_timeout",
                "message": "浏览器 tab 池超时。",
            }

    async def read_conversation(
        self, *, hr_name: str, friend_id: int | None = None, page: int = 1
    ) -> dict[str, Any]:
        """Read the chat history with one HR in a SINGLE page evaluate.

        Three page-internal fetches (label list -> credentials -> history)
        with one CDP round-trip: warlock tolerates only ~2-3 automated
        fetch rounds per page load, so the previous multi-stage Python
        flow burned the budget and the page died mid-chain.
        """

        assert self._tab_pool is not None
        for attempt in range(2):
            # attempt 2 only happens after a page_lost: warlock kills the
            # parked tab with a delay AFTER the first automated round, so
            # one fresh-tab retry usually lands inside the new budget.
            try:
                page_obj, fresh = await _get_parked_page(self._tab_pool)
                if fresh:
                    prepared = await self._prepare_parked_page(page_obj)
                    if prepared is not None:
                        return prepared
                payload: dict[str, Any] = {"hrName": hr_name, "page": page}
                if friend_id is not None:
                    payload["friendId"] = friend_id
                raw = await asyncio.wait_for(
                    page_obj.evaluate(
                        _FETCH_CONVERSATION_JS,
                        payload,
                    ),
                    timeout=_LIST_TIMEOUT_S,
                )
                break
            except TimeoutError:
                return {
                    "status": "failed",
                    "error_type": "tab_pool_timeout",
                    "message": "浏览器 tab 池或请求超时。",
                }
            except Exception as exc:
                if attempt == 0:
                    logger.info(
                        "Boss chat read: page died mid-evaluate, retrying "
                        "on a fresh tab",
                        exc_info=True,
                    )
                    # drop the dead parked page so the next loop gets a new tab
                    session_mod._parked = None
                    continue
                return {
                    "status": "failed",
                    "error_type": "page_lost",
                    "message": f"页内读取被中断（页面可能被反爬跳转）: {exc}",
                }
        if not isinstance(raw, dict):
            return {
                "status": "failed",
                "error_type": "api_rejected",
                "message": f"接口返回无法解析: {str(raw)[:150]}",
            }
        step = str(raw.get("step", ""))
        if step == "done":
            messages = list(raw.get("messages", []))
            raw_job = raw.get("job")
            job_metadata = _normalize_job_metadata(
                raw_job if isinstance(raw_job, Mapping) else None,
                source="conversation_friend",
            )
            if job_metadata is None:
                job_metadata = _resolve_registry_job_metadata(
                    self._settings,
                    company=str(raw.get("company") or ""),
                    title=str(raw.get("title") or ""),
                )
            raw_candidates = raw.get("jobCandidates", [])
            job_candidates = (
                [
                    candidate
                    for item in raw_candidates
                    for candidate in [
                        _normalize_job_metadata(
                            item if isinstance(item, Mapping) else None,
                            source="conversation_message",
                        )
                    ]
                    if candidate is not None
                ]
                if isinstance(raw_candidates, list)
                else []
            )
            for message in messages:
                raw_message_job = message.pop("job", None)
                if isinstance(raw_message_job, Mapping):
                    message["job_metadata"] = _normalize_job_metadata(
                        raw_message_job, source="conversation_message"
                    )
                _apply_card_semantics(message)
            logger.info(
                "Boss chat history: %d messages with %s", len(messages), hr_name
            )
            return {
                "status": "ok",
                "hr_name": hr_name,
                "page": page,
                "count": len(messages),
                "friend_id": raw.get("friendId"),
                "job_metadata": job_metadata,
                "job_candidates": job_candidates,
                "messages": messages,
            }
        if step == "match":
            return {
                "status": "failed",
                "error_type": "conversation_not_found",
                "message": f"会话列表中没有找到「{hr_name}」。",
            }
        if step == "creds":
            return {
                "status": "failed",
                "error_type": "security_id_unavailable",
                "message": f"会话凭据获取失败：{str(raw.get('message', ''))[:120]}",
            }
        return {
            "status": "failed",
            "error_type": "api_rejected",
            "api_step": step or "list",
            "code": raw.get("code"),
            "message": str(raw.get("message", ""))[:200],
        }

    async def _prepare_parked_page(self, page: Any) -> dict[str, Any] | None:
        """Navigate+settle a fresh parked page; failure dict or None."""

        try:
            await page.goto(
                f"https://www.zhipin.com{_CHAT_PAGE_PATH}",
                wait_until="domcontentloaded",
                timeout=_NAV_TIMEOUT_MS,
            )
        except Exception:
            logger.info("Boss chat: chat page nav interrupted", exc_info=True)
        deadline = asyncio.get_event_loop().time() + 30.0
        while asyncio.get_event_loop().time() < deadline:
            if urlsplit(str(page.url or "")).path == _CHAT_PAGE_PATH:
                try:
                    settled = await page.evaluate(
                        "document.readyState === 'complete'"
                    )
                except Exception:
                    settled = False
                if settled:
                    return None
            await asyncio.sleep(0.5)
        return {
            "status": "failed",
            "error_type": "chat_page_blocked",
            "message": "聊天页未能稳定加载（页面可能被反爬跳转到空白页）。",
        }

    async def _list_on_page(
        self, pool: Any, *, label_id: int, limit: int
    ) -> dict[str, Any]:
        page, fresh = await _get_parked_page(pool)
        if fresh:
            # First use this process: load the chat page once and park it.
            try:
                await page.goto(
                    f"https://www.zhipin.com{_CHAT_PAGE_PATH}",
                    wait_until="domcontentloaded",
                    timeout=_NAV_TIMEOUT_MS,
                )
            except Exception:
                logger.info("Boss chat: chat page nav interrupted", exc_info=True)
            deadline = asyncio.get_event_loop().time() + 30.0
            while asyncio.get_event_loop().time() < deadline:
                if urlsplit(str(page.url or "")).path == _CHAT_PAGE_PATH:
                    try:
                        settled = await page.evaluate(
                            "document.readyState === 'complete'"
                        )
                    except Exception:
                        settled = False
                    if settled:
                        break
                await asyncio.sleep(0.5)
            else:
                return {
                    "status": "failed",
                    "error_type": "chat_page_blocked",
                    "message": (
                        "聊天页未能稳定加载（页面可能被反爬跳转到空白页）。"
                        "建议稍后重试。"
                    ),
                }
        # Parked page: list refresh is a pure wapi fetch - no reload, ever.

        try:
            raw = await asyncio.wait_for(
                page.evaluate(_FETCH_LIST_JS, {"labelId": label_id}),
                timeout=_LIST_TIMEOUT_S,
            )
        except Exception as exc:
            return {
                "status": "failed",
                "error_type": "page_lost",
                "message": f"页内读取被中断（页面可能被反爬跳转）: {exc}",
            }
        if not isinstance(raw, dict) or raw.get("code") != 0:
            return {
                "status": "failed",
                "error_type": "api_rejected",
                "code": (raw or {}).get("code") if isinstance(raw, dict) else None,
                "message": str((raw or {}).get("message", raw))[:200]
                if isinstance(raw, dict)
                else str(raw)[:200],
                "stoken_present": (raw or {}).get("stokenPresent")
                if isinstance(raw, dict)
                else None,
            }
        friends = list(raw.get("friends", []))[:limit]
        # Boss system accounts (friendId <= 1000, e.g. job assistant) are
        # not HR greetings - the chat core library filters the same way.
        real = [f for f in friends if int(f.get("friendId") or 0) > 1000]
        for friend in real:
            raw_metadata = friend.pop("jobMetadata", None)
            metadata = _normalize_job_metadata(
                raw_metadata if isinstance(raw_metadata, Mapping) else None,
                source="conversation_friend",
            )
            friend["job_metadata"] = metadata or _resolve_registry_job_metadata(
                self._settings,
                company=str(friend.get("brandName") or ""),
                title=str(friend.get("jobName") or friend.get("positionName") or ""),
            )
        logger.info("Boss chat list: %d friends (labelId=%s)", len(real), label_id)
        return {
            "status": "ok",
            "filter_label_id": label_id,
            "count": len(real),
            "greetings": real,
        }
