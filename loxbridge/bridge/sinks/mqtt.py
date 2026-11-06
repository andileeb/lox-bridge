"""MQTT: loxbridge/<ms>/<room>/<control>/<state>, retained, LWT, optional Home Assistant discovery."""

import json
import re
import ssl

import aiomqtt

from ...miniserver.structure import StateInfo, Structure
from ...settings.models import MqttSink as MqttCfg
from .base import Point, Sink

_SLUG = re.compile(r"[^a-z0-9]+")
_UMLAUT = str.maketrans({"ä": "ae", "ö": "oe", "ü": "ue", "ß": "ss"})

# unit -> (device_class, state_class)
HA_CLASSES = {
    "°C": ("temperature", "measurement"), "°F": ("temperature", "measurement"),
    "W": ("power", "measurement"), "kW": ("power", "measurement"),
    "kWh": ("energy", "total_increasing"), "Wh": ("energy", "total_increasing"),
    "lx": ("illuminance", "measurement"), "V": ("voltage", "measurement"), "A": ("current", "measurement"),
    "ppm": ("carbon_dioxide", "measurement"), "hPa": ("pressure", "measurement"),
}
BINARY_TYPES = {"Switch", "InfoOnlyDigital", "PresenceDetector", "WindowMonitor", "SmokeAlarm"}


def slug(s: str) -> str:
    return _SLUG.sub("_", s.lower().translate(_UMLAUT)).strip("_") or "_"


def topic(prefix: str, ms: str, i: StateInfo) -> str:
    return "/".join([prefix.strip("/"), slug(ms), slug(i.room or "no_room"), slug(i.control), slug(i.state)])


def state_payload(p: Point, fmt: str) -> str:
    if fmt == "plain":
        return str(p.value if p.value is not None else p.text)
    return json.dumps({"value": p.value if p.value is not None else p.text, "unit": p.info.unit, "ts": p.ts,
                       "control": p.info.control, "room": p.info.room})


def discovery(cfg: MqttCfg, ms: str, ms_name: str, i: StateInfo) -> tuple[str, dict]:
    """(config topic, payload) for one state."""
    binary = i.type in BINARY_TYPES
    component = "binary_sensor" if binary else "sensor"
    uid = f"loxbridge_{slug(ms)}_{i.uuid.replace('-', '')}"
    val = "value_json.value" if cfg.payload == "json" else "value"
    conf: dict = {
        "name": f"{i.control} {i.state}" if i.state not in ("value", "active") else i.control,
        "unique_id": uid,
        "object_id": uid,
        "state_topic": topic(cfg.prefix, ms, i),
        "availability_topic": f"{cfg.prefix.strip('/')}/status",
        "device": {"identifiers": [f"loxbridge_{slug(ms)}"], "name": ms_name, "manufacturer": "Loxone"},
    }
    if i.room:
        conf["device"]["suggested_area"] = i.room
    if binary:
        conf["value_template"] = f"{{{{ 'ON' if ({val} | float(0)) > 0 else 'OFF' }}}}"
    else:
        conf["value_template"] = f"{{{{ {val} }}}}"
        if i.unit:
            conf["unit_of_measurement"] = i.unit
        if i.unit in HA_CLASSES:
            conf["device_class"], conf["state_class"] = HA_CLASSES[i.unit]
    return f"{cfg.ha_prefix.strip('/')}/{component}/{uid}/config", conf


class MqttSink(Sink):
    def __init__(self, cfg: MqttCfg):
        super().__init__(cfg)
        self.client: aiomqtt.Client | None = None
        self.ms_names: dict[str, str] = {}
        self.structures: dict[str, Structure] = {}
        self.announced: set[str] = set()

    @property
    def status_topic(self) -> str:
        return f"{self.cfg.prefix.strip('/')}/status"

    async def _connect(self) -> aiomqtt.Client:
        if self.client is None:
            c = aiomqtt.Client(
                self.cfg.host, self.cfg.port,
                username=self.cfg.username or None,
                password=self.cfg.password.get_secret_value() or None,
                will=aiomqtt.Will(self.status_topic, "offline", qos=1, retain=True),
                tls_context=ssl.create_default_context() if self.cfg.tls else None,
                identifier=f"loxbridge-{self.id}",
                timeout=10,
            )
            await c.__aenter__()
            self.client = c
            self.announced.clear()
            await c.publish(self.status_topic, "online", qos=1, retain=True)
        return self.client

    async def _drop(self) -> None:
        if self.client:
            try:
                await self.client.__aexit__(None, None, None)
            except Exception:  # noqa: BLE001
                pass
            self.client = None

    async def write(self, batch: list[Point]) -> None:
        try:
            c = await self._connect()
            if self.cfg.ha_discovery:
                await self._announce(c)
            for p in batch:
                await c.publish(topic(self.cfg.prefix, p.miniserver, p.info), state_payload(p, self.cfg.payload),
                                retain=self.cfg.retain)
        except aiomqtt.MqttError:
            await self._drop()
            raise

    async def on_structure(self, ms: str, structure: Structure) -> None:
        self.structures[ms] = structure
        self.announced = {a for a in self.announced if not a.startswith(ms + ":")}

    async def _announce(self, c: aiomqtt.Client) -> None:
        sel = set(self.cfg.selection)
        for ms, st in self.structures.items():
            for i in st.states.values():
                key = f"{ms}:{i.uuid}"
                if i.control_uuid not in sel or key in self.announced:
                    continue
                if i.type == "TextState" and not self.cfg.include_text:
                    continue
                t, conf = discovery(self.cfg, ms, self.ms_names.get(ms, ms), i)
                await c.publish(t, json.dumps(conf), qos=1, retain=True)
                self.announced.add(key)

    async def test(self) -> str:
        await self._connect()
        return "ok, connected"

    async def close(self) -> None:
        if self.client:
            try:
                await self.client.publish(self.status_topic, "offline", qos=1, retain=True)
            except aiomqtt.MqttError:
                pass
        await self._drop()
