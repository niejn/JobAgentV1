"""WeChat iLink Bot channel for JobAgent (text-only)."""

from jobagent.wechat.ilink import (
    ACCOUNT_FILE,
    CONTEXT_TOKENS_FILE,
    ContextTokenStore,
    ILinkApiError,
    InboundMessage,
    MessageDeduplicator,
    QRCodeLogin,
    QRLoginState,
    QRLoginStatus,
    WeixinAccount,
    WeixinAccountStore,
    WeixinBotClient,
    WeixinSessionExpired,
)

__all__ = [
    "ACCOUNT_FILE",
    "CONTEXT_TOKENS_FILE",
    "ContextTokenStore",
    "ILinkApiError",
    "InboundMessage",
    "MessageDeduplicator",
    "QRCodeLogin",
    "QRLoginState",
    "QRLoginStatus",
    "WeixinAccount",
    "WeixinAccountStore",
    "WeixinBotClient",
    "WeixinSessionExpired",
]
