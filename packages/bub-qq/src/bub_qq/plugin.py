from typing import Any

import bub
from bub import hookimpl
from bub import inquirer as bub_inquirer
from bub.channels import Channel
from bub.channels.contracts import MessageHandler
from bub.envelope import field_of
from bub.hooks.interception import LlmCallDecision
from bub.hooks.interception import LlmCallRequest
from bub.hooks.interception import LlmCallResult
from bub.hooks.interception import ToolCall
from bub.hooks.interception import ToolCallDecision
from bub.hooks.interception import ToolCallResult
from bub.turn import TurnState
from loguru import logger

from .config import QQConfig
from .security import QQ_CONTEXT_KEY
from .security import QQ_STATE_KEY
from .security import SlidingWindowRateLimiter
from .security import evaluate_tool_call

CHANNEL_NAME = "qq"
RECEIVE_MODES = ["webhook", "websocket"]

_rate_limiter: SlidingWindowRateLimiter | None = None


def _qq_state(state: TurnState) -> dict[str, Any] | None:
    if not isinstance(state, dict):
        return None
    qq_state = state.get(QQ_STATE_KEY)
    return qq_state if isinstance(qq_state, dict) else None


def _get_rate_limiter(config: QQConfig) -> SlidingWindowRateLimiter | None:
    if config.llm_rate_limit_per_minute <= 0:
        return None
    global _rate_limiter
    if _rate_limiter is None:
        _rate_limiter = SlidingWindowRateLimiter(
            max_calls=config.llm_rate_limit_per_minute,
            window_seconds=60.0,
        )
    return _rate_limiter


def _channel_enabled(current_config: dict[str, Any]) -> bool:
    enabled_channels = current_config.get("enabled_channels")
    if not isinstance(enabled_channels, str):
        return True
    value = enabled_channels.strip()
    if not value or value.lower() == "all":
        return True
    return CHANNEL_NAME in {item.strip() for item in value.split(",") if item.strip()}


@hookimpl
def provide_channels(message_handler: MessageHandler) -> list[Channel]:
    from .channel import QQChannel

    return [QQChannel(message_handler)]


@hookimpl
def load_state(message: Any, session_id: str) -> TurnState | None:
    """Copy QQ metadata from the inbound message into the turn state.

    Inbound adaptation stores scope/sender/role details on
    ``ChannelMessage.context``; exposing them under ``state["qq"]`` lets the
    interception hooks below enforce per-sender policies.
    """

    context = field_of(message, "context")
    if not isinstance(context, dict):
        return None
    qq_context = context.get(QQ_CONTEXT_KEY)
    if not isinstance(qq_context, dict):
        return None
    return {QQ_STATE_KEY: {**qq_context, "session_id": session_id}}


@hookimpl
def before_llm_call(
    request: LlmCallRequest, state: TurnState
) -> LlmCallDecision | None:
    qq_state = _qq_state(state)
    if qq_state is None:
        return None
    config = bub.ensure_config(QQConfig)
    limiter = _get_rate_limiter(config)
    if limiter is None:
        return None
    key = f"{qq_state.get('session_id')}|{qq_state.get('sender_id')}"
    if limiter.allow(key):
        return None
    logger.warning(
        "qq.security.llm_rate_limited session_id={} sender_id={}",
        qq_state.get("session_id"),
        qq_state.get("sender_id"),
    )
    return LlmCallDecision.finish(config.llm_rate_limit_notice)


@hookimpl
def before_tool_call(call: ToolCall, state: TurnState) -> ToolCallDecision | None:
    qq_state = _qq_state(state)
    if qq_state is None:
        return None
    config = bub.ensure_config(QQConfig)
    reason = evaluate_tool_call(config, qq_state, call.tool)
    if reason is None:
        return None
    logger.warning(
        "qq.security.tool_denied tool={} session_id={} sender_id={} role={}",
        call.tool,
        qq_state.get("session_id"),
        qq_state.get("sender_id"),
        qq_state.get("sender_role"),
    )
    return ToolCallDecision.deny(reason)


@hookimpl
def after_llm_call(
    request: LlmCallRequest, result: LlmCallResult, state: TurnState
) -> None:
    qq_state = _qq_state(state)
    if qq_state is None:
        return
    logger.info(
        "qq.audit.llm session_id={} sender_id={} model={} duration_ms={} error={}",
        qq_state.get("session_id"),
        qq_state.get("sender_id"),
        request.model,
        result.duration_ms,
        type(result.error).__name__ if result.error is not None else "",
    )


@hookimpl
def after_tool_call(
    call: ToolCall, result: ToolCallResult, state: TurnState
) -> None:
    qq_state = _qq_state(state)
    if qq_state is None:
        return
    logger.info(
        "qq.audit.tool session_id={} sender_id={} role={} tool={} duration_ms={} error={}",
        qq_state.get("session_id"),
        qq_state.get("sender_id"),
        qq_state.get("sender_role"),
        call.tool,
        result.duration_ms,
        type(result.error).__name__ if result.error is not None else "",
    )


@hookimpl
def onboard_config(current_config: dict[str, Any]) -> dict[str, Any] | None:
    if not _channel_enabled(current_config):
        return None

    current = current_config.get(CHANNEL_NAME)
    config = current if isinstance(current, dict) else {}
    receive_mode_default = str(config.get("receive_mode") or "webhook")
    if receive_mode_default not in RECEIVE_MODES:
        receive_mode_default = "webhook"

    return {
        CHANNEL_NAME: {
            "appid": bub_inquirer.ask_text(
                "QQ app ID",
                default=str(config.get("appid") or ""),
            ),
            "secret": bub_inquirer.ask_secret("QQ secret"),
            "receive_mode": bub_inquirer.ask_select(
                "QQ receive mode",
                choices=RECEIVE_MODES,
                default=receive_mode_default,
            ),
        }
    }
