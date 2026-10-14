import asyncio

from app.realtime.errors import Backpressure
from app.realtime.schemas import ServerEvent


class OutboundQueue:
    """Bound critical delivery waits; admit at most one transient partial."""

    def __init__(self, capacity: int = 32, put_timeout: float = 1) -> None:
        if capacity < 1 or put_timeout <= 0:
            raise ValueError("Queue bounds must be positive")
        self.items: asyncio.Queue[ServerEvent] = asyncio.Queue(maxsize=capacity)
        self.put_timeout = put_timeout
        self.pressure_count = 0
        self.partial_drops = 0

    async def put(self, event: ServerEvent) -> bool:
        if event.event_type == "STT_PARTIAL":
            # At most one partial can be pending. Reserve all remaining capacity for
            # critical events; partial producers never wait behind critical emitters.
            if not self.items.empty() or self.items.maxsize < 2:
                self.partial_drops += 1
                return False
            self.items.put_nowait(event)
            return True
        if self.items.full():
            self.pressure_count += 1
        try:
            async with asyncio.timeout(self.put_timeout):
                await self.items.put(event)
            return True
        except TimeoutError:
            raise Backpressure() from None

    def discard_on_close(self) -> int:
        count = 0
        while not self.items.empty():
            self.items.get_nowait()
            self.items.task_done()
            count += 1
        return count
