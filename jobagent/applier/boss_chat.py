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
from typing import Any
from urllib.parse import urlsplit

from jobagent.config import Settings
from jobagent.scraper.cdp_tab_pool import CdpTabPool

logger = logging.getLogger(__name__)


async def _get_parked_page(pool: Any) -> tuple[Any, bool]:
    from jobagent.applier.boss_chat_session import get_chat_page

    return await get_chat_page(pool)


_CHAT_PAGE_PATH = "/web/geek/chat"
_LABEL_IDS = {"全部": 0}  # calibration point: remaining tabs' ids unknown
_NAV_TIMEOUT_MS = 30_000
_LIST_TIMEOUT_S = 45.0
_MAX_FRIENDS = 100

_FETCH_LIST_JS = """
async (payload) => {
  const pageToken = ((window._PAGE || {}).token || "").split("|")[0];
  const base = {
    "X-Requested-With": "XMLHttpRequest",
    "Content-Type": "application/x-www-form-urlencoded",
    "traceId": "F-" + Math.random().toString(36).slice(2, 8)
      + Date.now().toString(36),
  };
  if (pageToken) base.token = pageToken;
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
_FETCH_CONVERSATION_JS = """
async (payload) => {
  const pageToken = ((window._PAGE || {}).token || "").split("|")[0];
  const base = {
    "X-Requested-With": "XMLHttpRequest",
    "Content-Type": "application/json;charset=UTF-8",
    "traceId": "F-" + Math.random().toString(36).slice(2, 8)
      + Date.now().toString(36),
  };
  if (pageToken) base.token = pageToken;
  const jq = (p) => "&_=" + Date.now();

  // 1) label list -> find the friend by name
  const list = await fetch(
    "/wapi/zprelation/friend/geekFilterByLabel?labelId=0" + jq(),
    {method: "GET", credentials: "include", headers: base},
  ).then((r) => r.json()).catch((e) => ({code: -1, message: String(e)}));
  if (list.code !== 0) return {step: "list", code: list.code, message: list.message};
  const friends = ((list.zpData || {}).friendList) || [];
  const target = friends.find(
    (f) => f.name === payload.hrName && Number(f.friendId) > 1000);
  if (!target) return {step: "match", code: 0, message: "conversation not found"};

  // 2) credentials (securityId rides in getGeekFriendList's result)
  let securityId = target.securityId || "";
  let bossId = target.encryptBossId || target.encryptFriendId || "";
  if (!securityId) {
    const body = String(target.friendSource) === "1"
      ? {dzFriendIds: String(target.friendId)}
      : {friendIds: String(target.friendId)};
    const creds = await fetch(
      "/wapi/zprelation/friend/getGeekFriendList.json" + jq(),
      {method: "POST", credentials: "include", headers: base,
       body: JSON.stringify(body)},
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
  }
  if (!bossId) return {step: "creds", code: 0, message: "no encryptBossId"};

  // 3) history
  const hist = await fetch(
    "/wapi/zpchat/geek/historyMsg?bossId=" + encodeURIComponent(bossId)
      + "&maxMsgId=0&c=20&page=" + payload.page + "&src=0"
      + "&securityId=" + encodeURIComponent(securityId) + jq(),
    {method: "GET", credentials: "include", headers: base},
  ).then((r) => r.json()).catch((e) => ({code: -1, message: String(e)}));
  if (hist.code !== 0) return {step: "history", code: hist.code, message: hist.message};
  const msgs = ((hist.zpData || {}).messages) || [];
  return {
    step: "done",
    messages: msgs.map((m) => {
      const b = m.body || {};
      return {
        fromId: m.fromId || (m.from && m.from.uid) || 0,
        toId: m.toId || (m.to && m.to.uid) || 0,
        time: m.time || m.createTime || 0,
        type: m.type != null ? m.type : m.messageType,
        text: String(b.text || m.text || b.content || m.content || "")
          .slice(0, 500),
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
        from playwright.async_api import async_playwright

        self._playwright = await async_playwright().start()
        try:
            browser = await self._playwright.chromium.connect_over_cdp(
                self._settings.xhs_cdp_endpoint,
                timeout=10_000,
            )
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
        self, *, hr_name: str, page: int = 1
    ) -> dict[str, Any]:
        """Read the chat history with one HR in a SINGLE page evaluate.

        Three page-internal fetches (label list -> credentials -> history)
        with one CDP round-trip: warlock tolerates only ~2-3 automated
        fetch rounds per page load, so the previous multi-stage Python
        flow burned the budget and the page died mid-chain.
        """

        assert self._tab_pool is not None
        try:
            page_obj, fresh = await _get_parked_page(self._tab_pool)
            if fresh:
                prepared = await self._prepare_parked_page(page_obj)
                if prepared is not None:
                    return prepared
            raw = await asyncio.wait_for(
                page_obj.evaluate(
                    _FETCH_CONVERSATION_JS, {"hrName": hr_name, "page": page}
                ),
                timeout=_LIST_TIMEOUT_S,
            )
        except TimeoutError:
            return {
                "status": "failed",
                "error_type": "tab_pool_timeout",
                "message": "浏览器 tab 池或请求超时。",
            }
        except Exception as exc:
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
            logger.info(
                "Boss chat history: %d messages with %s", len(messages), hr_name
            )
            return {
                "status": "ok",
                "hr_name": hr_name,
                "page": page,
                "count": len(messages),
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
        logger.info("Boss chat list: %d friends (labelId=%s)", len(real), label_id)
        return {
            "status": "ok",
            "filter_label_id": label_id,
            "count": len(real),
            "greetings": real,
        }
