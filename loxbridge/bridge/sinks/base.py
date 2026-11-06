import asyncio
import logging
import time
from collections import deque
from dataclasses import dataclass

from ... import metrics
from ...miniserver.structure import StateInfo, Structure

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class Point:
    miniserver: str
    info: StateInfo
    value: float | None
    text: str | None
    ts: float  # unix seconds


class Sink:
    """Base sink: own bounded queue (drop oldest), batching by time/size, retry with buffer."""

    def __init__(self, cfg):
        self.cfg = cfg
        self.id = cfg.id
        self.queue: deque[Point] = deque(maxlen=cfg.queue_size)
        self.wake = asyncio.Event()
        self.task: asyncio.Task | None = None
        self.error = ""
        self.last_ok: float | None = None

    def enqueue(self, p: Point) -> None:
        if len(self.queue) == self.queue.maxlen:
            metrics.sink_dropped.labels(self.id).inc()
        self.queue.append(p)
        if len(self.queue) >= self.cfg.batch_size:
            self.wake.set()

    def start(self) -> None:
        self.task = asyncio.create_task(self._loop())

    async def stop(self) -> None:
        if self.task:
            self.task.cancel()
            try:
                await self.task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
        await self.close()

    async def _loop(self) -> None:
        backoff = 1.0
        while True:
            try:
                await asyncio.wait_for(self.wake.wait(), self.cfg.batch_seconds)
            except TimeoutError:
                pass
            self.wake.clear()
            while self.queue:
                n = min(len(self.queue), self.cfg.batch_size)
                batch = [self.queue.popleft() for _ in range(n)]
                try:
                    await self.write(batch)
                except asyncio.CancelledError:
                    raise
                except Exception as e:  # noqa: BLE001
                    metrics.sink_errors.labels(self.id).inc()
                    if self.error != str(e):
                        log.warning("sink %s: %s", self.id, e)
                    self.error = str(e) or type(e).__name__
                    for p in reversed(batch):  # put back, oldest dropped first if full
                        if len(self.queue) == self.queue.maxlen:
                            metrics.sink_dropped.labels(self.id).inc()
                            break
                        self.queue.appendleft(p)
                    await asyncio.sleep(backoff)
                    backoff = min(backoff * 2, 60)
                    break
                metrics.sink_sent.labels(self.id).inc(len(batch))
                if self.error:
                    log.info("sink %s recovered", self.id)
                self.error, self.last_ok, backoff = "", time.time(), 1.0
            metrics.sink_queue.labels(self.id).set(len(self.queue))

    async def write(self, batch: list[Point]) -> None:
        raise NotImplementedError

    async def on_structure(self, ms: str, structure: Structure) -> None:
        pass

    async def close(self) -> None:
        pass

    async def test(self) -> str:
        return "ok"
