"""Deliver a selected Boss resume after an HR has replied in chat.

The adapter keeps platform credentials and opaque resume identifiers inside a
short-lived prepared delivery.  Agent-facing callers see only the HR/job
context and readable resume filenames.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from typing import Any
from uuid import uuid4

from jobagent.config import Settings
from jobagent.crawl import CrawlGate
from jobagent.scraper.boss import get_boss_cooldown
from jobagent.scraper.cdp_tab_pool import CdpTabPool

logger = logging.getLogger(__name__)

_CHAT_URL = "https://www.zhipin.com/web/geek/chat"
_PREPARED_TTL_SECONDS = 10 * 60

_PREPARE_JS = r"""
async (payload) => {
  const bstMatch = document.cookie.match(/(?:^|;\s*)bst=([^;]+)/);
  const zpToken = bstMatch ? decodeURIComponent(bstMatch[1]) : "";
  const traceId = "F-" + Math.random().toString(36).slice(2, 8)
    + Date.now().toString(36);
  const base = {traceId};
  if (zpToken) base["zp_token"] = zpToken;
  const withCacheBuster = (path) => path + (path.includes("?") ? "&" : "?")
    + "_=" + Date.now();
  const json = async (path, options) => fetch(withCacheBuster(path), {
    credentials: "include", ...options,
  }).then((r) => r.json()).catch((e) => ({code: -1, message: String(e)}));

  const list = await json("/wapi/zprelation/friend/geekFilterByLabel?labelId=0", {
    method: "GET", headers: {...base, "X-Requested-With": "XMLHttpRequest"},
  });
  if (list.code !== 0) return {step: "list", code: list.code, message: list.message};
  const friends = ((list.zpData || {}).friendList) || [];
  const target = friends.find((f) => String(f.friendId) === String(payload.friendId));
  if (!target) return {step: "match", code: 0, message: "conversation not found"};

  const formBody = String(target.friendSource) === "1"
    ? "dzFriendIds=" + encodeURIComponent(target.friendId)
    : "friendIds=" + encodeURIComponent(target.friendId);
  const creds = await json("/wapi/zprelation/friend/getGeekFriendList.json", {
    method: "POST", headers: {...base, "Content-Type": "application/x-www-form-urlencoded"},
    body: formBody,
  });
  if (creds.code !== 0) return {step: "creds", code: creds.code, message: creds.message};
  const full = (((creds.zpData || {}).result) || []).find(
    (f) => String(f.friendId || f.uid) === String(target.friendId));
  const securityId = (full && full.securityId) || target.securityId || "";
  const bossId = (full && full.encryptBossId) || target.encryptBossId
    || target.encryptFriendId || "";
  if (!securityId || !bossId) {
    return {step: "creds", code: 0, message: "chat credentials unavailable"};
  }

  const history = await json("/wapi/zpchat/geek/historyMsg?bossId="
    + encodeURIComponent(bossId) + "&maxMsgId=0&c=20&page=1&src=0"
    + "&securityId=" + encodeURIComponent(securityId), {
    method: "GET", headers: {...base, "X-Requested-With": "XMLHttpRequest"},
  });
  if (history.code !== 0) return {step: "history", code: history.code, message: history.message};
  const messages = ((history.zpData || {}).messages) || [];
  const selfId = String(((window._PAGE || {}).uid) || "");
  const inbound = messages.filter((m) => String(m.fromId || (m.from || {}).uid || "") !== selfId);
  const latest = inbound.sort((a, b) => Number(b.time || b.createTime || 0)
    - Number(a.time || a.createTime || 0))[0];
  if (!latest) return {step: "eligibility", code: 0, message: "HR has not replied"};
  const mid = String(latest.mid || latest.msgId || latest.cmid || "");
  const type = String(latest.type != null ? latest.type : latest.messageType || "");
  if (!mid || !type) return {step: "eligibility", code: 0, message: "reply message is incomplete"};

  const test = await json("/wapi/zpchat/exchange/testAccept", {
    method: "POST", headers: {...base, "X-Requested-With": "XMLHttpRequest",
      "Content-Type": "application/x-www-form-urlencoded"},
    body: new URLSearchParams({mid, type, securityId}).toString(),
  });
  if (test.code !== 0) return {step: "eligibility", code: test.code, message: test.message};
  const choices = await json("/wapi/zpgeek/resume/attachment/checkbox.json?from=3", {
    method: "GET", headers: {...base, "X-Requested-With": "XMLHttpRequest"},
  });
  if (choices.code !== 0) return {step: "resumes", code: choices.code, message: choices.message};
  const resumeList = ((choices.zpData || {}).resumeList) || [];
  const supportCommonResume = Boolean((choices.zpData || {}).supportCommonResume);
  return {
    step: "ready", friendId: String(target.friendId), hrName: target.name || "",
    company: target.brandName || "", jobTitle: target.jobName || target.positionName || "",
    securityId, bossId, mid, type, supportCommonResume,
    resumes: resumeList.map((r, index) => ({
      optionId: "resume_" + index,
      encryptResumeId: String(r.resumeId || ""),
      fileName: String(r.showName || "") + String(r.suffixName || ""),
      annexType: r.annexType, uploadTime: r.uploadTime || 0,
      restricted: Boolean(r.restricted), restrictedLabel: r.restrictedLabel || "",
      securityStatus: r.securityStatus || "",
    })),
  };
}
"""

_SEND_JS = r"""
async (payload) => {
  const bstMatch = document.cookie.match(/(?:^|;\s*)bst=([^;]+)/);
  const zpToken = bstMatch ? decodeURIComponent(bstMatch[1]) : "";
  const traceId = "F-" + Math.random().toString(36).slice(2, 8)
    + Date.now().toString(36);
  const headers = {traceId, "X-Requested-With": "XMLHttpRequest",
    "Content-Type": "application/x-www-form-urlencoded"};
  if (zpToken) headers["zp_token"] = zpToken;
  const accept = await fetch("/wapi/zpchat/exchange/accept?_=" + Date.now(), {
    method: "POST", credentials: "include", headers,
    body: new URLSearchParams({securityId: payload.securityId, type: payload.type,
      mid: payload.mid, scene: "", encryptResumeId: payload.encryptResumeId}).toString(),
  }).then((r) => r.json()).catch((e) => ({code: -1, message: String(e)}));
  if (accept.code !== 0) return {step: "accept", code: accept.code, message: accept.message};
  const refresh = await fetch("/wapi/zpchat/message/refresh?messageId="
    + encodeURIComponent(payload.mid) + "&_=" + Date.now(), {
    method: "GET", credentials: "include",
    headers: {traceId, "X-Requested-With": "XMLHttpRequest",
      ...(zpToken ? {"zp_token": zpToken} : {})},
  }).then((r) => r.json()).catch((e) => ({code: -1, message: String(e)}));
  const history = await fetch("/wapi/zpchat/geek/historyMsg?bossId="
    + encodeURIComponent(payload.bossId) + "&maxMsgId=0&c=20&page=1&src=0"
    + "&securityId=" + encodeURIComponent(payload.securityId) + "&_=" + Date.now(), {
    method: "GET", credentials: "include",
    headers: {traceId, "X-Requested-With": "XMLHttpRequest",
      ...(zpToken ? {"zp_token": zpToken} : {})},
  }).then((r) => r.json()).catch((e) => ({code: -1, message: String(e)}));
  return {step: "done", acceptCode: accept.code, acceptStatus: ((accept.zpData || {}).status || ""),
    refreshCode: refresh.code, historyCode: history.code, refreshMessage: refresh.message || ""};
}
"""


@dataclass(frozen=True, slots=True)
class _PreparedResumeDelivery:
    delivery_id: str
    friend_id: str
    security_id: str
    boss_id: str
    mid: str
    message_type: str
    hr_name: str
    company: str
    job_title: str
    created_at: float
    options: dict[str, dict[str, Any]]


class BossResumeDelivery:
    """Prepare and send a selected Boss resume in one HR conversation."""

    def __init__(self, settings: Settings, *, crawl_gate: CrawlGate | None = None) -> None:
        self._settings = settings
        self._crawl_gate = crawl_gate
        self._prepared: dict[str, _PreparedResumeDelivery] = {}

    async def prepare(self, friend_id: str) -> dict[str, Any]:
        """Return selectable resume filenames after checking an HR reply."""

        raw = await self._evaluate(_PREPARE_JS, {"friendId": friend_id})
        if not isinstance(raw, dict) or raw.get("step") != "ready":
            return self._failure(raw, "prepare_failed")
        options: dict[str, dict[str, Any]] = {}
        visible: list[dict[str, Any]] = []
        for item in raw.get("resumes", []):
            if not isinstance(item, dict):
                continue
            option_id = str(item.get("optionId") or "")
            filename = str(item.get("fileName") or "").strip()
            resume_id = str(item.get("encryptResumeId") or "")
            selectable = bool(option_id and filename and resume_id and not item.get("restricted"))
            options[option_id] = {**item, "selectable": selectable}
            visible.append(
                {
                    "resume_option_id": option_id,
                    "file_name": filename,
                    "resume_type": "附件简历" if str(item.get("annexType")) == "0" else "在线简历",
                    "uploaded_at": item.get("uploadTime"),
                    "restricted": bool(item.get("restricted")),
                    "restricted_reason": str(item.get("restrictedLabel") or ""),
                    "selectable": selectable,
                }
            )
        if not any(option["selectable"] for option in options.values()):
            return {"status": "blocked", "error_type": "no_sendable_resume", "resumes": visible}
        delivery_id = f"boss-resume-{uuid4().hex}"
        self._prepared[delivery_id] = _PreparedResumeDelivery(
            delivery_id=delivery_id,
            friend_id=str(raw.get("friendId") or friend_id),
            security_id=str(raw.get("securityId") or ""),
            boss_id=str(raw.get("bossId") or ""),
            mid=str(raw.get("mid") or ""),
            message_type=str(raw.get("type") or ""),
            hr_name=str(raw.get("hrName") or ""),
            company=str(raw.get("company") or ""),
            job_title=str(raw.get("jobTitle") or ""),
            created_at=time.monotonic(),
            options=options,
        )
        return {
            "status": "ready",
            "delivery_id": delivery_id,
            "conversation_id": str(raw.get("friendId") or friend_id),
            "hr_name": str(raw.get("hrName") or ""),
            "company": str(raw.get("company") or ""),
            "job_title": str(raw.get("jobTitle") or ""),
            "support_common_resume": bool(raw.get("supportCommonResume")),
            "resume_options": visible,
        }

    async def send(
        self,
        *,
        delivery_id: str,
        resume_option_id: str,
        resume_file_name: str,
        hr_name: str,
        company: str,
        job_title: str,
    ) -> dict[str, Any]:
        """Send exactly one user-selected prepared resume."""

        prepared = self._prepared.get(delivery_id)
        if prepared is None:
            return {"status": "failed", "error_type": "unknown_delivery"}
        reprepared_after_expiry = False
        if time.monotonic() - prepared.created_at > _PREPARED_TTL_SECONDS:
            # HITL approval between prepare and send routinely outlives the
            # 10-minute preflight TTL. Re-run the read-only preflight and keep
            # going when the user-selected resume is still selectable, instead
            # of failing (the old hard failure drove agents to script
            # prepare+send as one bypass).
            self._prepared.pop(delivery_id, None)
            refreshed = await self.prepare(prepared.friend_id)
            if refreshed.get("status") != "ready":
                return {
                    **refreshed,
                    "error_type": refreshed.get("error_type") or "delivery_expired",
                    "message": "原预检已过期，重新预检未就绪；请按返回状态处理。",
                }
            new_delivery_id = str(refreshed.get("delivery_id") or "")
            prepared = self._prepared.get(new_delivery_id)
            if prepared is None:
                return {"status": "failed", "error_type": "delivery_expired"}
            reprepared_after_expiry = True
            option = prepared.options.get(resume_option_id)
            if option is None or not option.get("selectable"):
                # Option ids may rotate between preflights; fall back to the
                # exact filename the user approved.
                option = next(
                    (
                        item
                        for item in prepared.options.values()
                        if item.get("selectable")
                        and str(item.get("fileName") or "") == resume_file_name
                    ),
                    None,
                )
            if option is None:
                return {
                    "status": "failed",
                    "error_type": "delivery_expired_options_changed",
                    "message": "原预检已过期，重新预检后所选简历已不可发送。",
                    "resume_options": refreshed.get("resume_options"),
                }
        else:
            option = prepared.options.get(resume_option_id)
            if option is None or not option.get("selectable"):
                return {"status": "failed", "error_type": "resume_not_selectable"}
        if str(option.get("fileName") or "") != resume_file_name:
            return {"status": "failed", "error_type": "resume_filename_mismatch"}
        if (hr_name, company, job_title) != (
            prepared.hr_name,
            prepared.company,
            prepared.job_title,
        ):
            return {"status": "failed", "error_type": "delivery_context_mismatch"}
        from jobagent.journey.resume_deliveries import ResumeDeliveryRegistry

        with ResumeDeliveryRegistry(self._settings.jobagent_state_db) as registry:
            if registry.already_confirmed(
                conversation_id=prepared.friend_id,
                source_mid=prepared.mid,
                resume_file_name=resume_file_name,
            ):
                return {"status": "blocked", "error_type": "resume_already_delivered"}
        raw = await self._evaluate(
            _SEND_JS,
            {
                "securityId": prepared.security_id,
                "bossId": prepared.boss_id,
                "mid": prepared.mid,
                "type": prepared.message_type,
                "encryptResumeId": str(option["encryptResumeId"]),
            },
        )
        if not isinstance(raw, dict) or raw.get("step") != "done":
            return self._failure(raw, "send_failed")
        self._prepared.pop(delivery_id, None)
        delivery_status = (
            "confirmed"
            if raw.get("refreshCode") == 0 and raw.get("historyCode") == 0
            else "unverified"
        )
        receipt = {
            "accept_status": raw.get("acceptStatus"),
            "refresh_code": raw.get("refreshCode"),
            "history_code": raw.get("historyCode"),
        }
        with ResumeDeliveryRegistry(self._settings.jobagent_state_db) as registry:
            registry.record(
                conversation_id=prepared.friend_id,
                source_mid=prepared.mid,
                resume_file_name=resume_file_name,
                hr_name=prepared.hr_name,
                company=prepared.company,
                job_title=prepared.job_title,
                status=delivery_status,
                receipt=receipt,
            )
        if delivery_status != "confirmed":
            return {
                "status": "unverified",
                "error_type": "refresh_unconfirmed",
                "hr_name": prepared.hr_name,
                "company": prepared.company,
                "job_title": prepared.job_title,
                "resume_file_name": resume_file_name,
            }
        result: dict[str, Any] = {
            "status": "confirmed",
            "hr_name": prepared.hr_name,
            "company": prepared.company,
            "job_title": prepared.job_title,
            "resume_file_name": resume_file_name,
            "receipt": receipt,
        }
        if reprepared_after_expiry:
            result["reprepared_after_expiry"] = True
        return result

    async def _evaluate(self, script: str, payload: dict[str, str]) -> Any:
        allowed, remaining_min, _ = get_boss_cooldown().check()
        if not allowed:
            return {"step": "cooldown", "message": f"Boss 冷却中（约 {remaining_min} 分钟）"}
        if self._crawl_gate is not None:
            await self._crawl_gate.acquire("boss-cdp")
        from playwright.async_api import async_playwright

        from jobagent.applier.boss_chat_session import get_chat_page

        playwright = await async_playwright().start()
        pool: CdpTabPool | None = None
        try:
            browser = await playwright.chromium.connect_over_cdp(
                self._settings.debug_chrome_cdp_endpoint, timeout=10_000
            )
            context = next((item for item in browser.contexts if item.pages), None)
            if context is None:
                return {"step": "browser", "message": "Chrome 中没有可用的登录上下文"}
            pool = CdpTabPool(context)
            page, fresh = await get_chat_page(pool)
            if fresh:
                await page.goto(_CHAT_URL, wait_until="domcontentloaded", timeout=30_000)
                await asyncio.sleep(1.0)
            return await asyncio.wait_for(page.evaluate(script, payload), timeout=45.0)
        except Exception as exc:
            logger.info("Boss resume delivery page evaluation failed", exc_info=True)
            return {"step": "page", "message": type(exc).__name__}
        finally:
            if pool is not None:
                await pool.close()
            await playwright.stop()

    @staticmethod
    def _failure(raw: Any, fallback: str) -> dict[str, Any]:
        if isinstance(raw, dict):
            return {
                "status": "failed",
                "error_type": str(raw.get("step") or fallback),
                "message": str(raw.get("message") or "Boss 简历发送预检失败"),
            }
        return {"status": "failed", "error_type": fallback}
