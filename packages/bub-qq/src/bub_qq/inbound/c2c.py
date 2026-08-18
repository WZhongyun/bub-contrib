from __future__ import annotations

import json
from typing import Any

from bub.channels.message import ChannelMessage
from loguru import logger

from ..protocol.models import QQC2CMessage
from ..session import QQInboundDeduper
from ..session import QQSessionState
from ..session import remember_session
from .common import attachment_payloads
from .common import exclude_none


class QQC2CInboundService:
    def __init__(
        self,
        *,
        channel_name: str,
        deduper: QQInboundDeduper,
        state: QQSessionState,
    ) -> None:
        self._channel_name = channel_name
        self._deduper = deduper
        self._state = state

    def parse_inbound(
        self, payload: dict[str, Any]
    ) -> tuple[QQC2CMessage, ChannelMessage] | None:
        try:
            message = QQC2CMessage.from_event(payload)
        except ValueError as exc:
            logger.warning("qq.c2c.invalid_payload error={}", exc)
            return None

        if self._deduper.seen(message.message_id):
            logger.info("qq.c2c.duplicate message_id={}", message.message_id)
            return None

        channel_message = build_c2c_channel_message(self._channel_name, message)
        remember_session(
            self._state,
            session_id=channel_message.session_id,
            message_id=message.message_id,
            timestamp=message.timestamp,
        )
        return message, channel_message


def build_c2c_channel_message(
    channel_name: str, message: QQC2CMessage
) -> ChannelMessage:
    session_id = f"{channel_name}:c2c:{message.user_openid}"
    chat_id = f"c2c:{message.user_openid}"
    text = message.content.strip()

    if text.startswith(","):
        return ChannelMessage(
            session_id=session_id,
            content=text,
            channel=channel_name,
            chat_id=chat_id,
            kind="command",
            is_active=True,
        )

    payload = {
        "message": message.content,
        "message_id": message.message_id,
        "type": "text" if not message.attachments else "attachment",
        "sender_id": message.user_openid,
        "date": message.timestamp,
        "attachments": attachment_payloads(message.attachments),
    }
    return ChannelMessage(
        session_id=session_id,
        content=json.dumps(exclude_none(payload), ensure_ascii=False),
        channel=channel_name,
        chat_id=chat_id,
        is_active=True,
    )


def resolve_c2c_openid(
    *, channel_name: str, session_id: str, chat_id: str
) -> str | None:
    if chat_id.startswith("c2c:"):
        openid = chat_id.removeprefix("c2c:").strip()
        return openid or None
    prefix = f"{channel_name}:c2c:"
    if session_id.startswith(prefix):
        openid = session_id.removeprefix(prefix).strip()
        return openid or None
    return None
