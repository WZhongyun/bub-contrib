from __future__ import annotations

from pydantic import ValidationError

from bub_qq.config import QQConfig


def test_inbound_dedupe_size_must_be_positive() -> None:
    try:
        QQConfig(inbound_dedupe_size=0)
    except ValidationError as exc:
        assert "inbound_dedupe_size" in str(exc)
    else:
        raise AssertionError("expected inbound_dedupe_size=0 to be rejected")
