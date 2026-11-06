"""State cache + fan-out to sinks. Owns the Miniserver WebSocket connections."""

import asyncio
import json
import logging
import time
from pathlib import Path

from .. import metrics
from ..miniserver.structure import Structure, parse_structure
from ..miniserver.ws import MiniserverWS
from ..settings.models import Config, Miniserver
from .sinks.base import Point, Sink
from .sinks.influx import InfluxSink
from .sinks.mqtt import MqttSink
from .sinks.prometheus import PrometheusSink
from .sinks.webhook import WebhookSink

log = logging.getLogger(__name__)
SINKS = {"influx": InfluxSink, "prometheus": PrometheusSink, "mqtt": MqttSink, "webhook": WebhookSink}


def make_sink(cfg) -> Sink:
    return SINKS[cfg.type](cfg)


class Hub:
    def __init__(self, work: Path):
        self.work = work
        self.values: dict[str, dict[str, dict]] = {}  # ms -> state uuid -> {value, text, ts}
        self.structures: dict[str, Structure] = {}
        self.status: dict[str, dict] = {}  # ms -> {connected, error, since, events, rate}
        self.sinks: dict[str, Sink] = {}
        self.ms_names: dict[str, str] = {}
        self._conns: dict[str, tuple[str, asyncio.Task]] = {}
        self._sent: dict[str, dict[tuple[str, str], tuple[float | str | None, float]]] = {}
        self._tick_task: asyncio.Task | None = None
        for f in (work / "structure").glob("*.json"):  # cached structures: browse controls while offline
            try:
                self.structures[f.stem] = parse_structure(json.loads(f.read_text()))
            except Exception:  # noqa: BLE001
                log.warning("ignoring unreadable cached structure %s", f)

    # --- lifecycle --------------------------------------------------------------------------------
    async def apply(self, cfg: Config) -> None:
        self.ms_names = {m.id: m.name for m in cfg.miniservers}
        wanted = {m.id: m for m in cfg.miniservers if m.bridge_enabled and m.bridge_user}
        for ms_id in list(self._conns):
            key = wanted[ms_id].model_dump_json() if ms_id in wanted else None
            if key != self._conns[ms_id][0]:
                self._conns.pop(ms_id)[1].cancel()
                self.status.pop(ms_id, None)
                metrics.ws_connected.labels(ms_id).set(0)
        for ms_id, m in wanted.items():
            if ms_id not in self._conns:
                self._conns[ms_id] = (m.model_dump_json(), asyncio.create_task(self._connect(m)))

        new = {s.id: s for s in cfg.sinks if s.enabled}
        for sid in list(self.sinks):
            old = self.sinks[sid]
            if sid in new and old.cfg.model_dump(exclude={"selection"}) == new[sid].model_dump(exclude={"selection"}):
                old.cfg = new[sid]  # selection change only: keep queue and connection
                continue
            await self.sinks.pop(sid).stop()
            self._sent.pop(sid, None)
        for sid, scfg in new.items():
            if sid not in self.sinks:
                s = make_sink(scfg)
                if isinstance(s, MqttSink):
                    s.ms_names = self.ms_names
                    for ms, st in self.structures.items():
                        await s.on_structure(ms, st)
                s.start()
                self.sinks[sid] = s
                self._sent[sid] = {}
                self._replay(s)
        if not self._tick_task:
            self._tick_task = asyncio.create_task(self._tick())

    async def stop(self) -> None:
        for ms_id, (_, t) in self._conns.items():
            t.cancel()
            metrics.ws_connected.labels(ms_id).set(0)
        self._conns.clear()
        for s in self.sinks.values():
            await s.stop()
        self.sinks.clear()
        if self._tick_task:
            self._tick_task.cancel()

    async def _connect(self, m: Miniserver) -> None:
        self.status[m.id] = {"connected": False, "error": "connecting", "since": time.time(), "events": 0, "rate": 0}

        async def on_structure(st: Structure) -> None:
            self.structures[m.id] = st
            for s in self.sinks.values():
                await s.on_structure(m.id, st)

        def on_connected(ok: bool, err: str) -> None:
            self.status[m.id].update(connected=ok, error=err, since=time.time())
            metrics.ws_connected.labels(m.id).set(1 if ok else 0)

        ws = MiniserverWS(
            m.url, m.bridge_user, m.bridge_password.get_secret_value(), m.verify_tls,
            self.work / "structure" / f"{m.id}.json", on_structure,
            lambda items: self.on_values(m.id, items), lambda items: self.on_texts(m.id, items), on_connected,
        )
        await ws.run()

    # --- events -----------------------------------------------------------------------------------
    def on_values(self, ms: str, items: list[tuple[str, float]]) -> None:
        now = time.time()
        cache = self.values.setdefault(ms, {})
        for uuid, v in items:
            cache[uuid] = {"value": v, "text": None, "ts": now}
            self._dispatch(ms, uuid, v, None, now)
        self._count(ms, len(items))

    def on_texts(self, ms: str, items: list[tuple[str, str]]) -> None:
        now = time.time()
        cache = self.values.setdefault(ms, {})
        for uuid, t in items:
            cache[uuid] = {"value": None, "text": t, "ts": now}
            self._dispatch(ms, uuid, None, t, now)
        self._count(ms, len(items))

    def _count(self, ms: str, n: int) -> None:
        metrics.bridge_events.labels(ms).inc(n)
        if ms in self.status:
            self.status[ms]["events"] += n

    def _accepts(self, s: Sink, ms: str, info, is_text: bool) -> bool:
        c = s.cfg
        if c.miniservers and ms not in c.miniservers:
            return False
        if is_text and (not c.include_text or c.type == "prometheus"):
            return False
        return info.control_uuid in c.selection

    def _dispatch(self, ms: str, uuid: str, value: float | None, text: str | None, ts: float,
                  only: Sink | None = None, force: bool = False) -> None:
        st = self.structures.get(ms)
        info = st.states.get(uuid) if st else None
        if info is None:
            return
        for s in [only] if only else self.sinks.values():
            if not self._accepts(s, ms, info, text is not None):
                continue
            sent = self._sent[s.id]
            last = sent.get((ms, uuid))
            if not force and last is not None:
                if text is not None or s.cfg.deadband == 0:
                    if last[0] == (text if text is not None else value):
                        continue
                elif isinstance(last[0], float) and value is not None and abs(value - last[0]) < s.cfg.deadband:
                    continue
            sent[(ms, uuid)] = (text if text is not None else value, ts)
            s.enqueue(Point(ms, info, value, text, ts))

    def _replay(self, s: Sink) -> None:
        """A (re)started sink gets the current values right away."""
        for ms, cache in self.values.items():
            for uuid, v in cache.items():
                self._dispatch(ms, uuid, v["value"], v["text"], v["ts"], only=s, force=True)

    async def _tick(self) -> None:
        last_events: dict[str, int] = {}
        n = 0
        while True:
            await asyncio.sleep(1)
            n += 1
            now = time.time()
            for s in self.sinks.values():
                hb = s.cfg.heartbeat
                if not hb:
                    continue
                for (ms, uuid), (_, t) in list(self._sent[s.id].items()):
                    if now - t >= hb and uuid in self.values.get(ms, {}):
                        v = self.values[ms][uuid]
                        self._dispatch(ms, uuid, v["value"], v["text"], now, only=s, force=True)
            if n % 10 == 0:
                for ms, st in self.status.items():
                    st["rate"] = round((st["events"] - last_events.get(ms, 0)) / 10, 1)
                    last_events[ms] = st["events"]

    # --- queries ----------------------------------------------------------------------------------
    def states(self, ms: str | None = None) -> list[dict]:
        out = []
        for m, cache in self.values.items():
            if ms and m != ms:
                continue
            st = self.structures.get(m)
            for uuid, v in cache.items():
                info = st.states.get(uuid) if st else None
                if info:
                    out.append({"miniserver": m, **info.dict(), **v})
        return out

    def sink_status(self) -> list[dict]:
        return [{"id": s.id, "type": s.cfg.type, "queue": len(s.queue), "error": s.error, "last_ok": s.last_ok}
                for s in self.sinks.values()]
