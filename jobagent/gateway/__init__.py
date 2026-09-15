"""JobAgent HR Gateway: the always-on process beside ``jobagent chat``.

Runs messaging channels (WeChat iLink bot now, Boss message monitor later)
and answers user commands from the job progress registry. Follows the Hermes
gateway's production patterns - persistent long-poll cursor, session-expiry
pause, consecutive-failure backoff, message dedup, single-poller lock -
reduced to what a single-user local gateway needs.
"""

from jobagent.gateway.wechat_channel import (
    BossReplyCommandHandler,
    CompositeMessageHandler,
    RegistryCommandHandler,
    WeChatChannel,
    build_registry_command_handler,
)

__all__ = [
    "RegistryCommandHandler",
    "BossReplyCommandHandler",
    "CompositeMessageHandler",
    "WeChatChannel",
    "build_registry_command_handler",
]
