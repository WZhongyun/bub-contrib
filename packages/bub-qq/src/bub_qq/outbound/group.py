from __future__ import annotations

from typing import Protocol

from bub.channels.message import ChannelMessage
from loguru import logger

from ..inbound.c2c import QQC2CSendRecord
from ..inbound.c2c import QQC2CSessionState
from ..inbound.group import resolve_group_openid
from ..protocol.errors import QQOpenAPIError
from .c2c import build_already_sent_result
from .c2c import hash_c2c_content
from .c2c import is_passive_reply_window_open
from .c2c import next_c2c_msg_seq
from .c2c import normalize_c2c_outbound_content
from .markdown import send_with_markdown_fallback
from .send_errors import is_duplicate_send_error
from .send_errors import log_send_duplicate_error
from .send_errors import log_send_error


class QQGroupOpenAPI(Protocol):
    async def post_group_text_message(
        self,
        *,
        group_openid: str,
        content: str,
        msg_id: str,
        msg_seq: int,
    ) -> dict[str, object]: ...

    async def post_group_markdown_message(
        self,
        *,
        group_openid: str,
        content: str,
        msg_id: str,
        msg_seq: int,
    ) -> dict[str, object]: ...


class QQGroupSendService:
    def __init__(
        self,
        *,
        channel_name: str,
        receive_mode: str,
        state: QQC2CSessionState,
        openapi: QQGroupOpenAPI,
    ) -> None:
        self._channel_name = channel_name
        self._receive_mode = receive_mode
        self._state = state
        self._openapi = openapi

    async def send(self, message: ChannelMessage) -> dict[str, object] | None:
        content = normalize_c2c_outbound_content(message.content or "")
        if not content:
            logger.warning("qq.send skip_empty session_id={}", message.session_id)
            return None

        session_id = message.session_id or ""
        chat_id = message.chat_id or ""
        group_openid = resolve_group_openid(
            channel_name=self._channel_name,
            session_id=session_id,
            chat_id=chat_id,
        )
        if not group_openid:
            logger.warning(
                "qq.send unresolved_group_openid session_id={} chat_id={}",
                message.session_id,
                message.chat_id,
            )
            return None

        msg_id = self._state.latest_message_id_by_session.get(session_id)
        if not msg_id:
            logger.warning(
                "qq.send missing_msg_id session_id={} reason=active_push_not_supported",
                session_id,
            )
            return None

        if not is_passive_reply_window_open(self._state, session_id):
            logger.warning(
                "qq.send passive_reply_window_expired session_id={} msg_id={}",
                session_id,
                msg_id,
            )
            return None

        content_hash = hash_c2c_content(content)
        msg_seq = next_c2c_msg_seq(self._state, session_id, msg_id)
        send_record = self._state.send_record_by_session_msg_id_and_seq.get(
            (session_id, msg_id, msg_seq)
        )
        if send_record is not None:
            if send_record.content_hash == content_hash:
                logger.info(
                    "qq.send duplicate session_id={} openid={} msg_id={} reason=already_sent source=local_dedup_hit msg_seq={} content_hash={}",
                    session_id,
                    group_openid,
                    msg_id,
                    send_record.msg_seq,
                    content_hash,
                )
                return build_already_sent_result(send_record)
            logger.warning(
                "qq.send duplicate session_id={} openid={} msg_id={} reason=duplicate_msg_seq_blocked source=local_dedup_hit msg_seq={} previous_content_hash={} content_hash={}",
                session_id,
                group_openid,
                msg_id,
                msg_seq,
                send_record.content_hash,
                content_hash,
            )
            return {"status": "duplicate_msg_seq_blocked"}
        async def send_text(
            *, content: str, msg_id: str, msg_seq: int
        ) -> dict[str, object]:
            return await self._openapi.post_group_text_message(
                group_openid=group_openid,
                content=content,
                msg_id=msg_id,
                msg_seq=msg_seq,
            )

        async def send_markdown(
            *, content: str, msg_id: str, msg_seq: int
        ) -> dict[str, object]:
            return await self._openapi.post_group_markdown_message(
                group_openid=group_openid,
                content=content,
                msg_id=msg_id,
                msg_seq=msg_seq,
            )

        try:
            result = await send_with_markdown_fallback(
                content=content,
                msg_id=msg_id,
                msg_seq=msg_seq,
                send_text=send_text,
                send_markdown=send_markdown,
            )
        except QQOpenAPIError as exc:
            if is_duplicate_send_error(exc):
                log_send_duplicate_error(
                    exc,
                    session_id=session_id,
                    openid=group_openid,
                    msg_id=msg_id,
                    msg_seq=msg_seq,
                    content_hash=content_hash,
                )
                duplicate_record = QQC2CSendRecord(
                    content=content,
                    content_hash=content_hash,
                    msg_seq=msg_seq,
                    result={},
                )
                self._state.send_record_by_session_msg_id_and_seq[
                    (session_id, msg_id, msg_seq)
                ] = duplicate_record
                return build_already_sent_result(duplicate_record)
            log_send_error(
                exc,
                session_id=session_id,
                openid=group_openid,
                msg_id=msg_id,
                msg_seq=msg_seq,
                receive_mode=self._receive_mode,
            )
            return None

        send_record = QQC2CSendRecord(
            content=content,
            content_hash=content_hash,
            msg_seq=msg_seq,
            result=dict(result),
        )
        self._state.send_record_by_session_msg_id_and_seq[
            (session_id, msg_id, msg_seq)
        ] = send_record
        logger.info(
            "qq.send success session_id={} openid={} msg_id={} msg_seq={} response_id={}",
            session_id,
            group_openid,
            msg_id,
            msg_seq,
            result.get("id"),
        )
        return result
