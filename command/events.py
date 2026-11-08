"""In-process event bus. Agents emit events; the dashboard (WebSocket) and the Office page only render them."""
import asyncio, time
from collections import deque


class EventBus:
    def __init__(self, keep: int = 300):
        self.ring = deque(maxlen=keep)
        self.subs = set()
        self.seq = 0

    def publish(self, type: str, **data) -> dict:
        self.seq += 1
        ev = {"id": self.seq, "ts": round(time.time(), 3), "type": type, **data}
        self.ring.append(ev)
        for q in list(self.subs):
            try:
                q.put_nowait(ev)
            except asyncio.QueueFull:          # a stuck viewer must never block the agents
                self.subs.discard(q)
        return ev

    def subscribe(self) -> asyncio.Queue:
        q = asyncio.Queue(maxsize=500)
        self.subs.add(q)
        return q

    def unsubscribe(self, q) -> None:
        self.subs.discard(q)

    def recent(self, n: int = 100) -> list:
        return list(self.ring)[-n:]
