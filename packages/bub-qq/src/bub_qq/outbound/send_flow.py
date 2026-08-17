"""Passive-reply send flow shared by the C2C and group send services.

The flow is identical for both targets: look up the inbound message to
reply to, check the passive reply window, deduplicate by content hash,
allocate a ``msg_seq``, send (with markdown fallback), and record the
outcome. Only target resolution and the OpenAPI calls differ, so the
services inject those as callables.
"""

from __future__ import annotations

import hashlib
import re
from datetime import datetime
from datetime import timedelta
from datetime import timezone

from loguru import logger

from ..protocol.errors import QQOpenAPIError
from ..session import QQSendRecord
from ..session import QQSessionState
from .markdown import MarkdownSender
from .markdown import send_with_markdown_fallback
from .send_errors import is_duplicate_send_error
from .send_errors import log_send_duplicate_error
from .send_errors import log_send_error

DEFAULT_PASSIVE_REPLY_WINDOW_SECONDS = 3600.0


async def run_send_flow(
    *,
    state: QQSessionState,
    receive_mode: str,
    session_id: str,
    target_openid: str,
    content: str,
    send_text: MarkdownSender,
    send_markdown: MarkdownSender,
    passive_reply_window_seconds: float = DEFAULT_PASSIVE_REPLY_WINDOW_SECONDS,
) -> dict[str, object] | None:
    msg_id = state.latest_message_id_by_session.get(session_id)
    if not msg_id:
        logger.warning(
            "qq.send missing_msg_id session_id={} reason=active_push_not_supported",
            session_id,
        )
        return None

    if not is_passive_reply_window_open(
        state, session_id, window_seconds=passive_reply_window_seconds
    ):
        logger.warning(
            "qq.send passive_reply_window_expired session_id={} msg_id={}",
            session_id,
            msg_id,
        )
        return None

    content_hash = hash_outbound_content(content)
    record_key = (session_id, msg_id, content_hash)
    existing = state.send_records.get(record_key)
    if existing is not None:
        logger.info(
            "qq.send duplicate session_id={} openid={} msg_id={} reason=already_sent source=local_dedup_hit msg_seq={} content_hash={}",
            session_id,
            target_openid,
            msg_id,
            existing.msg_seq,
            content_hash,
        )
        return build_already_sent_result(existing)

    msg_seq = next_msg_seq(state, session_id, msg_id)
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
                openid=target_openid,
                msg_id=msg_id,
                msg_seq=msg_seq,
                content_hash=content_hash,
            )
            duplicate_record = QQSendRecord(
                content=content,
                content_hash=content_hash,
                msg_seq=msg_seq,
                result={},
            )
            state.send_records[record_key] = duplicate_record
            return build_already_sent_result(duplicate_record)
        log_send_error(
            exc,
            session_id=session_id,
            openid=target_openid,
            msg_id=msg_id,
            msg_seq=msg_seq,
            receive_mode=receive_mode,
        )
        return None

    state.send_records[record_key] = QQSendRecord(
        content=content,
        content_hash=content_hash,
        msg_seq=msg_seq,
        result=dict(result),
    )
    logger.info(
        "qq.send success session_id={} openid={} msg_id={} msg_seq={} response_id={}",
        session_id,
        target_openid,
        msg_id,
        msg_seq,
        result.get("id"),
    )
    return result


def next_msg_seq(state: QQSessionState, session_id: str, msg_id: str) -> int:
    key = (session_id, msg_id)
    current = state.latest_sequence_by_session_and_msg_id.get(key, 0) + 1
    state.latest_sequence_by_session_and_msg_id[key] = current
    return current


def build_already_sent_result(send_record: QQSendRecord) -> dict[str, object]:
    result = dict(send_record.result)
    result["status"] = "already_sent"
    return result


def hash_outbound_content(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()[:16]


def normalize_outbound_content(content: str) -> str:
    normalized = content.strip()
    normalized = re.sub(r"^\$qq\s*→\s*", "", normalized, count=1, flags=re.IGNORECASE)
    return normalized.strip()


def is_passive_reply_window_open(
    state: QQSessionState,
    session_id: str,
    *,
    window_seconds: float = DEFAULT_PASSIVE_REPLY_WINDOW_SECONDS,
) -> bool:
    # Fail open on a missing or unparsable timestamp: blocking the reply
    # locally would be worse than letting QQ reject an expired one, and QQ
    # events are not guaranteed to carry a timestamp.
    timestamp = state.latest_timestamp_by_session.get(session_id)
    if not timestamp:
        return True
    try:
        sent_at = datetime.fromisoformat(timestamp)
    except ValueError:
        return True
    if sent_at.tzinfo is None:
        sent_at = sent_at.replace(tzinfo=timezone.utc)
    return datetime.now(sent_at.tzinfo) - sent_at <= timedelta(seconds=window_seconds)
