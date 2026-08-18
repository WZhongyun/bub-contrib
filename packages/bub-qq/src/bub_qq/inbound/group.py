from __future__ import annotations

import json
import re
from typing import Any

from bub.channels.message import ChannelMessage
from loguru import logger

from ..protocol.models import QQGroupMessage
from ..protocol.models import QQMention
from .c2c import QQC2CDeduper
from .c2c import QQC2CSessionState
from .c2c import exclude_none
from .c2c import remember_c2c_session

GROUP_AT_EVENT = "GROUP_AT_MESSAGE_CREATE"
GROUP_MESSAGE_EVENT = "GROUP_MESSAGE_CREATE"
GROUP_EVENTS = {GROUP_AT_EVENT, GROUP_MESSAGE_EVENT}

_AT_RE = re.compile(r"<@!?([^>]+)>")


class QQGroupInboundService:
    def __init__(
        self,
        *,
        channel_name: str,
        deduper: QQC2CDeduper,
        state: QQC2CSessionState,
    ) -> None:
        self._channel_name = channel_name
        self._deduper = deduper
        self._state = state

    def parse_inbound(
        self, payload: dict[str, Any]
    ) -> tuple[QQGroupMessage, ChannelMessage] | None:
        try:
            message = QQGroupMessage.from_event(payload)
        except ValueError as exc:
            logger.warning("qq.group.invalid_payload error={}", exc)
            return None

        if self._deduper.seen(message.message_id):
            logger.info("qq.group.duplicate message_id={}", message.message_id)
            return None

        channel_message = build_group_channel_message(self._channel_name, message)
        remember_c2c_session(
            self._state,
            session_id=channel_message.session_id,
            message_id=message.message_id,
            timestamp=message.timestamp,
            sequence=message.sequence,
        )
        return message, channel_message


def build_group_channel_message(
    channel_name: str,
    message: QQGroupMessage,
) -> ChannelMessage:
    session_id = f"{channel_name}:group:{message.group_openid}"
    chat_id = f"group:{message.group_openid}"
    text = strip_mention_text(message.content, message.mentions)
    was_mentioned = group_was_mentioned(message)

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
        "message": text,
        "message_id": message.message_id,
        "type": "text" if not message.attachments else "attachment",
        "sender_id": message.member_openid,
        "sender_name": message.sender_name,
        "group_openid": message.group_openid,
        "chat_type": "group",
        "was_mentioned": was_mentioned,
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


def group_was_mentioned(message: QQGroupMessage) -> bool:
    if any(mention.is_you for mention in message.mentions):
        return True
    return message.event_type == GROUP_AT_EVENT


def strip_mention_text(text: str, mentions: tuple[QQMention, ...]) -> str:
    if not text or not mentions:
        return text.strip()

    by_openid = {
        mention.member_openid: mention for mention in mentions if mention.member_openid
    }

    def _replace(match: re.Match[str]) -> str:
        mention = by_openid.get(match.group(1))
        if mention is None:
            return match.group(0)
        if mention.is_you:
            return ""
        if mention.nickname:
            return f"@{mention.nickname}"
        return match.group(0)

    cleaned = _AT_RE.sub(_replace, text)
    return re.sub(r"\s+", " ", cleaned).strip()


def resolve_group_openid(
    *, channel_name: str, session_id: str, chat_id: str
) -> str | None:
    if chat_id.startswith("group:"):
        openid = chat_id.removeprefix("group:").strip()
        return openid or None
    prefix = f"{channel_name}:group:"
    if session_id.startswith(prefix):
        openid = session_id.removeprefix(prefix).strip()
        return openid or None
    return None
