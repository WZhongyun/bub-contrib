from __future__ import annotations

from bub_qq.session import BoundedDict
from bub_qq.session import QQInboundDeduper
from bub_qq.session import QQSendRecord
from bub_qq.session import QQSessionState
from bub_qq.session import remember_session


def test_bounded_dict_evicts_oldest_entries() -> None:
    bounded: BoundedDict[str, int] = BoundedDict(2)

    bounded["a"] = 1
    bounded["b"] = 2
    bounded["c"] = 3

    assert "a" not in bounded
    assert bounded["b"] == 2
    assert bounded["c"] == 3


def test_bounded_dict_refreshes_recency_on_rewrite() -> None:
    bounded: BoundedDict[str, int] = BoundedDict(2)

    bounded["a"] = 1
    bounded["b"] = 2
    bounded["a"] = 10
    bounded["c"] = 3

    assert "b" not in bounded
    assert bounded["a"] == 10
    assert bounded["c"] == 3


def test_session_state_send_records_are_bounded() -> None:
    state = QQSessionState(max_entries=2)

    for index in range(3):
        state.send_records[("session", f"msg-{index}", "hash")] = QQSendRecord(
            content="hello",
            content_hash="hash",
            msg_seq=1,
            result={},
        )

    assert len(state.send_records) == 2
    assert ("session", "msg-0", "hash") not in state.send_records


def test_inbound_deduper_marks_repeats_and_evicts() -> None:
    deduper = QQInboundDeduper(2)

    assert deduper.seen("m1") is False
    assert deduper.seen("m1") is True
    assert deduper.seen("m2") is False
    assert deduper.seen("m3") is False
    # m1 was evicted by the size-2 bound, so it is treated as new again.
    assert deduper.seen("m1") is False


def test_remember_session_updates_message_id_and_timestamp() -> None:
    state = QQSessionState()

    remember_session(
        state,
        session_id="qq:c2c:user",
        message_id="message-1",
        timestamp="2099-01-01T00:00:00+00:00",
    )
    remember_session(
        state,
        session_id="qq:c2c:user",
        message_id="message-2",
        timestamp=None,
    )

    assert state.latest_message_id_by_session["qq:c2c:user"] == "message-2"
    assert (
        state.latest_timestamp_by_session["qq:c2c:user"] == "2099-01-01T00:00:00+00:00"
    )
