"""QQ channel with auth, OpenAPI and pluggable receive transports."""

from __future__ import annotations

import asyncio
from typing import Any

import bub
from bub.channels import Channel
from bub.channels.message import ChannelMessage
from bub.channels.contracts import MessageHandler
from loguru import logger

from .config import QQConfig
from .gateway.webhook import QQWebhookServer
from .gateway.websocket import QQWebSocketClient
from .inbound.c2c import QQC2CDeduper
from .inbound.c2c import QQC2CInboundService
from .inbound.c2c import QQC2CSessionState
from .inbound.group import GROUP_EVENTS
from .inbound.group import QQGroupInboundService
from .inbound.group import group_was_mentioned
from .inbound.interaction import INTERACTION_QUERY
from .inbound.interaction import INTERACTION_UPDATE
from .inbound.interaction import build_claw_cfg
from .inbound.interaction import parse_interaction_event
from .outbound.c2c import QQC2CSendService
from .outbound.group import QQGroupSendService
from .protocol.auth import QQTokenProvider
from .protocol.errors import QQOpenAPIError
from .protocol.openapi import QQOpenAPI


class QQChannel(Channel):
    """QQ channel registration with reusable auth and OpenAPI client."""

    name = "qq"

    def __init__(self, on_receive: MessageHandler) -> None:
        self._on_receive = on_receive
        self._config = bub.ensure_config(QQConfig)
        self._token_provider = QQTokenProvider(self._config)
        self._openapi = QQOpenAPI(self._config, self._token_provider)
        self._webhook = QQWebhookServer(self._config, self._handle_transport_payload)
        self._websocket = QQWebSocketClient(
            self._config, self._openapi, self._handle_transport_payload
        )
        self._deduper = QQC2CDeduper(self._config.inbound_dedupe_size)
        self._session_state = QQC2CSessionState(
            latest_message_id_by_session={},
            latest_sequence_by_session_and_msg_id={},
            latest_timestamp_by_session={},
            send_record_by_session_msg_id_and_seq={},
        )
        self._c2c_inbound = QQC2CInboundService(
            channel_name=self.name,
            deduper=self._deduper,
            state=self._session_state,
        )
        self._group_inbound = QQGroupInboundService(
            channel_name=self.name,
            deduper=self._deduper,
            state=self._session_state,
        )
        self._c2c_send = QQC2CSendService(
            channel_name=self.name,
            receive_mode=self._config.receive_mode,
            state=self._session_state,
            openapi=self._openapi,
        )
        self._group_send = QQGroupSendService(
            channel_name=self.name,
            receive_mode=self._config.receive_mode,
            state=self._session_state,
            openapi=self._openapi,
        )

    @property
    def _c2c_state(self) -> QQC2CSessionState:
        return self._session_state

    @property
    def needs_debounce(self) -> bool:
        return True

    async def start(self, stop_event: asyncio.Event | None) -> None:
        if not self._config.appid or not self._config.secret:
            raise RuntimeError("qq appid/secret is empty")

        mode = self._normalize_receive_mode()
        if mode == "webhook":
            await self._webhook.start()
            logger.info(
                "qq.start mode=webhook token_url={} openapi_base_url={} webhook=http://{}:{}{} websocket=disabled",
                self._config.token_url,
                self._config.openapi_base_url,
                self._config.webhook_host,
                self._config.webhook_port,
                self._config.webhook_path,
            )
            return

        await self._websocket.start(stop_event)
        logger.info(
            "qq.start mode=websocket token_url={} openapi_base_url={} intents={} webhook=disabled",
            self._config.token_url,
            self._config.openapi_base_url,
            self._config.websocket_intents,
        )

    async def stop(self) -> None:
        await self._webhook.stop()
        await self._websocket.stop()
        await self._openapi.aclose()
        logger.info("qq.stopped")

    async def send(self, message: ChannelMessage) -> None:
        if _is_group_target(self.name, message):
            await self._group_send.send(message)
            return
        await self._c2c_send.send(message)

    async def _handle_transport_payload(self, payload: dict[str, Any]) -> None:
        op = payload.get("op")
        event_type = payload.get("t")
        if op != 0:
            logger.info("qq.transport.ignored op={} t={}", op, event_type)
            return
        if event_type == "READY":
            logger.info("qq.websocket.ready")
            return
        if event_type == "RESUMED":
            logger.info("qq.websocket.resumed")
            return
        if event_type == "C2C_MESSAGE_CREATE":
            await self._handle_c2c_message(payload)
            return
        if event_type in GROUP_EVENTS:
            await self._handle_group_message(payload)
            return
        if event_type == "INTERACTION_CREATE":
            await self._handle_interaction(payload)
            return
        logger.info("qq.transport.unhandled event={} op={}", event_type, op)

    async def _handle_c2c_message(self, payload: dict[str, Any]) -> None:
        parsed = self._c2c_inbound.parse_inbound(payload)
        if parsed is None:
            return
        message, channel_message = parsed
        logger.info(
            "qq.c2c.inbound session_id={} user_openid={} content_len={} attachments={}",
            channel_message.session_id,
            message.user_openid,
            len(message.content),
            len(message.attachments),
        )
        await self._on_receive(channel_message)

    async def _handle_group_message(self, payload: dict[str, Any]) -> None:
        parsed = self._group_inbound.parse_inbound(payload)
        if parsed is None:
            return
        message, channel_message = parsed
        logger.info(
            "qq.group.inbound session_id={} group_openid={} member_openid={} was_mentioned={} is_active={} content_len={}",
            channel_message.session_id,
            message.group_openid,
            message.member_openid,
            group_was_mentioned(message),
            channel_message.is_active,
            len(message.content),
        )
        await self._on_receive(channel_message)

    async def _handle_interaction(self, payload: dict[str, Any]) -> None:
        event = parse_interaction_event(payload)
        if event is None:
            return
        event_type = event["type"]
        if event_type in {INTERACTION_QUERY, INTERACTION_UPDATE}:
            try:
                await self._openapi.put_interaction(
                    interaction_id=event["id"],
                    code=0,
                    data={"claw_cfg": build_claw_cfg()},
                )
            except QQOpenAPIError as exc:
                logger.warning(
                    "qq.interaction.ack_failed id={} code={} error={}",
                    event["id"],
                    exc.error_code,
                    exc.error_message,
                )
            return
        logger.info("qq.interaction.unhandled type={}", event_type)

    def _normalize_receive_mode(self) -> str:
        mode = (self._config.receive_mode or "").strip().lower()
        if mode not in {"webhook", "websocket"}:
            raise RuntimeError(
                f"qq receive_mode must be webhook or websocket, got {self._config.receive_mode!r}"
            )
        return mode


def _is_group_target(channel_name: str, message: ChannelMessage) -> bool:
    chat_id = message.chat_id or ""
    session_id = message.session_id or ""
    return chat_id.startswith("group:") or session_id.startswith(
        f"{channel_name}:group:"
    )
