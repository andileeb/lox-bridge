"""loxone_state gauge on /metrics."""

from ... import metrics
from .base import Point, Sink


def labels(p: Point) -> tuple[str, ...]:
    i = p.info
    return (p.miniserver, i.room, i.category, i.control, i.state, i.type)


class PrometheusSink(Sink):
    async def write(self, batch: list[Point]) -> None:
        for p in batch:
            if p.value is not None:
                metrics.loxone_state.labels(*labels(p)).set(p.value)

    async def close(self) -> None:
        metrics.loxone_state.clear()  # selection may have changed
