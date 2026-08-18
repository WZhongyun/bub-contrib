from __future__ import annotations

from typing import Any

from loguru import logger

INTERACTION_QUERY = 2001
INTERACTION_UPDATE = 2002


def parse_interaction_event(payload: dict[str, Any]) -> dict[str, Any] | None:
    data = payload.get("d")
    if not isinstance(data, dict):
        logger.warning("qq.interaction.invalid_payload reason=missing_d")
        return None
    interaction_id = str(data.get("id") or "").strip()
    if not interaction_id:
        logger.warning("qq.interaction.invalid_payload reason=missing_id")
        return None
    inner = data.get("data")
    inner_type = inner.get("type") if isinstance(inner, dict) else None
    resolved = inner.get("resolved") if isinstance(inner, dict) else None
    return {
        "id": interaction_id,
        "type": inner_type,
        "group_openid": str(data.get("group_openid") or "").strip(),
        "resolved": resolved if isinstance(resolved, dict) else {},
    }


def build_claw_cfg() -> dict[str, object]:
    return {
        "channel_type": "qq",
        "claw_type": "bub",
        "require_mention": "always",
        "group_policy": "open",
        "online_state": "online",
    }
