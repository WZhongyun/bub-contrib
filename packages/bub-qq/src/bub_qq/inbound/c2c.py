from __future__ import annotations

import json
from collections import deque
from dataclasses import dataclass
from typing import Any

from bub.channels.message import ChannelMessage
from loguru import logger

from ..protocol.models import QQC2CMessage


@dataclass
class QQC2CSessionState:
    latest_message_id_by_session: dict[str, str]
    latest_sequence_by_session_and_msg_id: dict[tuple[str, str], int]
    latest_timestamp_by_session: dict[str, str]
    send_record_by_session_msg_id_and_seq: dict[tuple[str, str, int], "QQC2CSendRecord"]


@dataclass
class QQC2CSendRecord:
    content: str
    content_hash: str
    msg_seq: int
    result: dict[str, object]


class QQC2CDeduper:
    """Bounded recent-message cache for duplicate QQ deliveries."""

    def __init__(self, size: int) -> None:
        self._ids: deque[str] = deque(maxlen=size)
        self._id_set: set[str] = set()

    def seen(self, message_id: str) -> bool:
        if message_id in self._id_set:
            return True
        evicted: str | None = None
        if len(self._ids) == self._ids.maxlen:
            evicted = self._ids[0]
        self._ids.append(message_id)
        self._id_set.add(message_id)
        if evicted is not None and evicted not in self._ids:
            self._id_set.discard(evicted)
        return False


class QQC2CInboundService:
    def __init__(
        self, *, channel_name: str, deduper: QQC2CDeduper, state: QQC2CSessionState
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
        remember_c2c_session(
            self._state,
            session_id=channel_message.session_id,
            message_id=message.message_id,
            timestamp=message.timestamp,
            sequence=message.sequence,
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
        "attachments": [
            {
                "content_type": attachment.content_type,
                "filename": attachment.filename,
                "height": attachment.height,
                "width": attachment.width,
                "size": attachment.size,
                "url": attachment.url,
                "voice_wav_url": attachment.voice_wav_url,
                "asr_refer_text": attachment.asr_refer_text,
            }
            for attachment in message.attachments
        ]
        or None,
    }
    return ChannelMessage(
        session_id=session_id,
        content=json.dumps(exclude_none(payload), ensure_ascii=False),
        channel=channel_name,
        chat_id=chat_id,
        is_active=True,
    )


def remember_c2c_session(
    state: QQC2CSessionState,
    *,
    session_id: str,
    message_id: str,
    timestamp: str | None,
    sequence: int | None,
) -> None:
    state.latest_message_id_by_session[session_id] = message_id
    if timestamp is not None:
        state.latest_timestamp_by_session[session_id] = timestamp


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


def exclude_none(payload: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in payload.items() if value is not None}
