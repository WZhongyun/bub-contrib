from __future__ import annotations

import pytest

from bub_qq import admins, approval, security


@pytest.fixture(autouse=True)
def _default_command_prefix(monkeypatch):
    # Many tests replace bub.ensure_config with one returning a QQConfig for
    # any class, which would also answer Bub's AgentSettings lookup behind
    # command_prefix(). Pin Bub's default prefix; tests of other prefixes
    # override it.
    for module in (security, approval, admins):
        monkeypatch.setattr(module, "command_prefix", lambda: ",")
