from __future__ import annotations

import asyncio
from collections import defaultdict, deque
from collections.abc import Hashable
from dataclasses import dataclass

from bub.envelope import Envelope
from bub.turn import TurnState


@dataclass(frozen=True, slots=True)
class SteeringReceipt:
    """Tracks whether one queued steering message reached a model step."""

    key: Hashable
    token: object
    delivered: asyncio.Future[None]


@dataclass(slots=True)
class _QueuedSteering:
    message: Envelope
    token: object | None = None
    delivered: asyncio.Future[None] | None = None


class ACPSteeringInbox:
    """Session-scoped steering inbox with atomic delivery receipts.

    Bub's built-in agent drains this object at model-step boundaries. Receipts
    let the ACP adapter distinguish a message consumed by the active turn from
    one that arrived after its final drain and must start a new turn.
    """

    def __init__(self) -> None:
        self._messages: defaultdict[Hashable, deque[_QueuedSteering]] = defaultdict(
            deque
        )
        self._lock = asyncio.Lock()

    async def enqueue_message(self, message: Envelope, state: TurnState) -> None:
        async with self._lock:
            self._messages[self._key(state)].append(_QueuedSteering(message))

    async def enqueue_with_receipt(
        self, message: Envelope, state: TurnState
    ) -> SteeringReceipt:
        key = self._key(state)
        token = object()
        delivered = asyncio.get_running_loop().create_future()
        async with self._lock:
            self._messages[key].append(
                _QueuedSteering(message, token=token, delivered=delivered)
            )
        return SteeringReceipt(key=key, token=token, delivered=delivered)

    async def drain_messages(self, state: TurnState) -> list[Envelope]:
        key = self._key(state)
        async with self._lock:
            queued = list(self._messages.pop(key, ()))
            for item in queued:
                if item.delivered is not None and not item.delivered.done():
                    item.delivered.set_result(None)
        return [item.message for item in queued]

    async def claim_pending(self, receipt: SteeringReceipt) -> Envelope | None:
        """Remove and return a receipt's message if no model step consumed it."""

        async with self._lock:
            queued = self._messages.get(receipt.key)
            if queued is None:
                return None
            for index, item in enumerate(queued):
                if item.token is receipt.token:
                    del queued[index]
                    if not queued:
                        self._messages.pop(receipt.key, None)
                    return item.message
        return None

    def message_count(self, state: TurnState) -> int:
        return len(self._messages.get(self._key(state), ()))

    @staticmethod
    def _key(state: TurnState) -> Hashable:
        thread_id = state.get("_runtime_thread_id")
        if isinstance(thread_id, Hashable) and thread_id:
            return thread_id
        session_id = state.get("session_id")
        if isinstance(session_id, Hashable) and session_id:
            return session_id
        return "default"
