from __future__ import annotations

import hashlib
import re
from datetime import datetime
from datetime import timedelta
from datetime import timezone
from typing import Protocol

from bub.channels.message import ChannelMessage
from loguru import logger

from ..inbound.c2c import QQC2CSendRecord
from ..inbound.c2c import QQC2CSessionState
from ..inbound.c2c import resolve_c2c_openid
from ..protocol.errors import QQOpenAPIError
from .send_errors import is_duplicate_send_error
from .send_errors import log_send_duplicate_error
from .send_errors import log_send_error


class QQC2COpenAPI(Protocol):
    async def post_c2c_text_message(
        self,
        *,
        openid: str,
        content: str,
        msg_id: str,
        msg_seq: int,
    ) -> dict[str, object]: ...


class QQC2CSendService:
    def __init__(
        self,
        *,
        channel_name: str,
        receive_mode: str,
        state: QQC2CSessionState,
        openapi: QQC2COpenAPI,
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
        openid = resolve_c2c_openid(
            channel_name=self._channel_name,
            session_id=session_id,
            chat_id=chat_id,
        )
        if not openid:
            logger.warning(
                "qq.send unresolved_openid session_id={} chat_id={}",
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
                    openid,
                    msg_id,
                    send_record.msg_seq,
                    content_hash,
                )
                return build_already_sent_result(send_record)
            logger.warning(
                "qq.send duplicate session_id={} openid={} msg_id={} reason=duplicate_msg_seq_blocked source=local_dedup_hit msg_seq={} previous_content_hash={} content_hash={}",
                session_id,
                openid,
                msg_id,
                msg_seq,
                send_record.content_hash,
                content_hash,
            )
            return {"status": "duplicate_msg_seq_blocked"}
        try:
            result = await self._openapi.post_c2c_text_message(
                openid=openid,
                content=content,
                msg_id=msg_id,
                msg_seq=msg_seq,
            )
        except QQOpenAPIError as exc:
            if is_duplicate_send_error(exc):
                log_send_duplicate_error(
                    exc,
                    session_id=session_id,
                    openid=openid,
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
                openid=openid,
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
            openid,
            msg_id,
            msg_seq,
            result.get("id"),
        )
        return result


def next_c2c_msg_seq(state: QQC2CSessionState, session_id: str, msg_id: str) -> int:
    key = (session_id, msg_id)
    current = state.latest_sequence_by_session_and_msg_id.get(key, 0) + 1
    state.latest_sequence_by_session_and_msg_id[key] = current
    return current


def build_already_sent_result(send_record: QQC2CSendRecord) -> dict[str, object]:
    result = dict(send_record.result)
    result["status"] = "already_sent"
    return result


def hash_c2c_content(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()[:16]


def normalize_c2c_outbound_content(content: str) -> str:
    normalized = content.strip()
    normalized = re.sub(r"^\$qq\s*→\s*", "", normalized, count=1, flags=re.IGNORECASE)
    return normalized.strip()


def is_passive_reply_window_open(state: QQC2CSessionState, session_id: str) -> bool:
    timestamp = state.latest_timestamp_by_session.get(session_id)
    if not timestamp:
        return True
    try:
        sent_at = datetime.fromisoformat(timestamp)
    except ValueError:
        return True
    if sent_at.tzinfo is None:
        sent_at = sent_at.replace(tzinfo=timezone.utc)
    return datetime.now(sent_at.tzinfo) - sent_at <= timedelta(minutes=60)
