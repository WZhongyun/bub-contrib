from __future__ import annotations

import asyncio
import json

from bub.channels.message import ChannelMessage

from bub_qq.inbound.group import QQGroupInboundService
from bub_qq.inbound.group import build_group_channel_message
from bub_qq.inbound.group import strip_mention_text
from bub_qq.inbound.interaction import build_claw_cfg
from bub_qq.inbound.interaction import parse_interaction_event
from bub_qq.outbound.group import QQGroupSendService
from bub_qq.protocol.models import QQGroupMessage
from bub_qq.protocol.models import QQMention
from bub_qq.session import QQInboundDeduper
from bub_qq.session import QQSessionState


class GroupOpenAPIStub:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    async def post_group_text_message(
        self,
        *,
        group_openid: str,
        content: str,
        msg_id: str,
        msg_seq: int,
    ) -> dict[str, object]:
        self.calls.append(
            {
                "group_openid": group_openid,
                "content": content,
                "msg_id": msg_id,
                "msg_seq": msg_seq,
            }
        )
        return {"id": "group-reply-1"}

    async def post_group_markdown_message(
        self,
        *,
        group_openid: str,
        content: str,
        msg_id: str,
        msg_seq: int,
    ) -> dict[str, object]:
        self.calls.append(
            {
                "group_openid": group_openid,
                "content": content,
                "msg_id": msg_id,
                "msg_seq": msg_seq,
                "msg_type": 2,
            }
        )
        return {"id": "group-reply-1"}


def _state() -> QQSessionState:
    return QQSessionState()


def _payload(
    *,
    event_type: str = "GROUP_AT_MESSAGE_CREATE",
    message_id: str = "group-message-1",
    content: str = "<@bot-openid> hello",
    mentions: list[dict[str, object]] | None = None,
) -> dict[str, object]:
    return {
        "id": "event-1",
        "op": 0,
        "s": 8,
        "t": event_type,
        "d": {
            "author": {
                "member_openid": "member-openid",
                "username": "Alice",
            },
            "content": content,
            "id": message_id,
            "group_openid": "group-openid",
            "timestamp": "2099-01-01T00:00:00+00:00",
            "mentions": mentions
            if mentions is not None
            else [
                {
                    "member_openid": "bot-openid",
                    "nickname": "Bot",
                    "is_you": True,
                }
            ],
        },
    }


def test_group_inbound_parses_at_message_as_active() -> None:
    state = _state()
    service = QQGroupInboundService(
        channel_name="qq",
        deduper=QQInboundDeduper(16),
        state=state,
    )

    parsed = service.parse_inbound(_payload())

    assert parsed is not None
    message, channel_message = parsed
    assert message.group_openid == "group-openid"
    assert channel_message.session_id == "qq:group:group-openid"
    assert channel_message.chat_id == "group:group-openid"
    assert channel_message.is_active is True
    payload = json.loads(channel_message.content)
    assert payload["message"] == "hello"
    assert payload["sender_id"] == "member-openid"
    assert payload["sender_name"] == "Alice"
    assert payload["was_mentioned"] is True
    assert state.latest_message_id_by_session["qq:group:group-openid"] == (
        "group-message-1"
    )


def test_group_inbound_unmentioned_message_is_still_active() -> None:
    state = _state()
    service = QQGroupInboundService(
        channel_name="qq",
        deduper=QQInboundDeduper(16),
        state=state,
    )

    parsed = service.parse_inbound(
        _payload(
            event_type="GROUP_MESSAGE_CREATE",
            content="just chatting",
            mentions=[],
        )
    )

    assert parsed is not None
    _, channel_message = parsed
    assert channel_message.is_active is True
    payload = json.loads(channel_message.content)
    assert payload["message"] == "just chatting"
    assert payload["was_mentioned"] is False


def test_group_inbound_dedupes_repeated_messages() -> None:
    service = QQGroupInboundService(
        channel_name="qq",
        deduper=QQInboundDeduper(16),
        state=_state(),
    )

    assert service.parse_inbound(_payload(message_id="group-message-1")) is not None
    assert service.parse_inbound(_payload(message_id="group-message-1")) is None


def test_strip_mention_text_removes_bot_and_keeps_others() -> None:
    mentions = (
        QQMention(
            member_openid="bot-openid",
            nickname="Bot",
            is_you=True,
            scope="single",
        ),
        QQMention(
            member_openid="user-2",
            nickname="Bob",
            is_you=False,
            scope="single",
        ),
    )

    cleaned = strip_mention_text("<@bot-openid> see <@user-2> later", mentions)

    assert cleaned == "see @Bob later"


def test_group_send_service_sends_using_session_context() -> None:
    async def _run() -> None:
        state = _state()
        state.latest_message_id_by_session["qq:group:group-openid"] = "group-message-1"
        state.latest_timestamp_by_session["qq:group:group-openid"] = (
            "2099-01-01T00:00:00+00:00"
        )
        openapi = GroupOpenAPIStub()
        service = QQGroupSendService(
            channel_name="qq",
            receive_mode="websocket",
            state=state,
            openapi=openapi,
        )

        result = await service.send(
            ChannelMessage(
                session_id="qq:group:group-openid",
                chat_id="group:group-openid",
                content="hello group",
                channel="qq",
            )
        )

        assert result == {"id": "group-reply-1"}
        assert openapi.calls == [
            {
                "group_openid": "group-openid",
                "content": "hello group",
                "msg_id": "group-message-1",
                "msg_seq": 1,
            }
        ]

    asyncio.run(_run())


def test_group_send_service_sends_markdown_for_formatted_content() -> None:
    async def _run() -> None:
        state = _state()
        state.latest_message_id_by_session["qq:group:group-openid"] = "group-message-1"
        state.latest_timestamp_by_session["qq:group:group-openid"] = (
            "2099-01-01T00:00:00+00:00"
        )
        openapi = GroupOpenAPIStub()
        service = QQGroupSendService(
            channel_name="qq",
            receive_mode="websocket",
            state=state,
            openapi=openapi,
        )

        result = await service.send(
            ChannelMessage(
                session_id="qq:group:group-openid",
                chat_id="group:group-openid",
                content="这是 **重点**",
                channel="qq",
            )
        )

        assert result == {"id": "group-reply-1"}
        assert openapi.calls == [
            {
                "group_openid": "group-openid",
                "content": "这是 **重点**",
                "msg_id": "group-message-1",
                "msg_seq": 1,
                "msg_type": 2,
            }
        ]

    asyncio.run(_run())


def test_group_channel_message_command_is_always_active() -> None:
    message = QQGroupMessage(
        message_id="group-message-1",
        group_openid="group-openid",
        member_openid="member-openid",
        sender_name="Alice",
        content=",status",
        timestamp="2099-01-01T00:00:00+00:00",
        attachments=(),
        mentions=(),
        event_id="event-1",
        sequence=1,
        event_type="GROUP_MESSAGE_CREATE",
    )

    channel_message = build_group_channel_message("qq", message)

    assert channel_message.kind == "command"
    assert channel_message.is_active is True


def test_interaction_query_payload_and_claw_cfg() -> None:
    event = parse_interaction_event(
        {
            "op": 0,
            "t": "INTERACTION_CREATE",
            "d": {
                "id": "interaction-1",
                "group_openid": "group-openid",
                "data": {"type": 2001, "resolved": {}},
            },
        }
    )

    assert event is not None
    assert event["id"] == "interaction-1"
    assert event["type"] == 2001
    assert build_claw_cfg()["require_mention"] == "always"
