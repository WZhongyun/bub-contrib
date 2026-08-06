from __future__ import annotations

import bub
from pydantic import Field
from pydantic_settings import SettingsConfigDict


@bub.config(name="acp-server")
class ACPServerSettings(bub.Settings):
    model_config = SettingsConfigDict(env_prefix="BUB_ACP_SERVER_", extra="ignore")

    channel_name: str = "acp-server"
    send_user_message_updates: bool = False
    context_window_size: int = Field(default=128_000, gt=0)
