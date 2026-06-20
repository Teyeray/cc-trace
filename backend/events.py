"""In-process pub/sub for live event streaming.

The hook hot path appends to JSONL (durable) and then publishes a lightweight
shaped event here (best-effort, in-memory). SSE subscribers fan out from this.

If there are no subscribers, publishing is a cheap no-op, so this never slows
the hook path.
"""

from __future__ import annotations

import asyncio
from typing import Any

# Bounded so a slow/stuck subscriber can't grow memory without limit.
_QUEUE_MAXSIZE = 1000


class Broadcaster:
    def __init__(self) -> None:
        self._subscribers: set[asyncio.Queue[dict[str, Any]]] = set()

    def subscribe(self) -> asyncio.Queue[dict[str, Any]]:
        queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=_QUEUE_MAXSIZE)
        self._subscribers.add(queue)
        return queue

    def unsubscribe(self, queue: asyncio.Queue[dict[str, Any]]) -> None:
        self._subscribers.discard(queue)

    def publish(self, event: dict[str, Any]) -> None:
        """Fan out to all subscribers. Drops on full queues (never blocks)."""
        for queue in list(self._subscribers):
            try:
                queue.put_nowait(event)
            except asyncio.QueueFull:
                # Subscriber is too slow; skip rather than block the hook path.
                pass


broadcaster = Broadcaster()
