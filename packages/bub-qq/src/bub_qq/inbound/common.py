"""Helpers shared by C2C and group inbound adaptation."""

from __future__ import annotations

from typing import Any

from ..protocol.models import QQAttachment


def exclude_none(payload: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in payload.items() if value is not None}


def attachment_payloads(
    attachments: tuple[QQAttachment, ...],
) -> list[dict[str, Any]] | None:
    if not attachments:
        return None
    return [
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
        for attachment in attachments
    ]
