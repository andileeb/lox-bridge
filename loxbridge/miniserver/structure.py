"""LoxAPP3.json -> flat state map (state UUID -> names)."""

import re
from dataclasses import asdict, dataclass

_UNIT = re.compile(r"%[-+ #0]*\d*(?:\.\d+)?[a-zA-Z]%?|<v[^>]*>")


@dataclass(frozen=True)
class StateInfo:
    uuid: str
    control_uuid: str
    control: str
    state: str
    room: str
    category: str
    type: str
    unit: str

    def dict(self) -> dict:
        return asdict(self)


@dataclass
class Structure:
    last_modified: str
    ms_info: dict
    controls: dict[str, dict]  # control uuid -> {name, type, room, category, unit, states: {name: uuid}}
    states: dict[str, StateInfo]


def _unit(control: dict) -> str:
    fmt = (control.get("details") or {}).get("format", "")
    return _UNIT.sub("", fmt).replace("%%", "%").strip()


def parse_structure(data: dict) -> Structure:
    rooms = {k: v.get("name", "") for k, v in (data.get("rooms") or {}).items()}
    cats = {k: v.get("name", "") for k, v in (data.get("cats") or {}).items()}
    controls: dict[str, dict] = {}
    states: dict[str, StateInfo] = {}

    def add(uuid: str, c: dict, parent: dict | None) -> None:
        room = rooms.get(c.get("room") or (parent or {}).get("room", ""), "")
        cat = cats.get(c.get("cat") or (parent or {}).get("cat", ""), "")
        name = c.get("name", uuid)
        if parent:
            name = f"{parent.get('name', '')} / {name}"
        info = {
            "uuid": uuid,
            "name": name,
            "type": c.get("type", ""),
            "room": room,
            "category": cat,
            "unit": _unit(c),
            "states": {},
            "statistic": c.get("statistic"),
        }
        for sname, suuid in (c.get("states") or {}).items():
            for i, u in enumerate(suuid if isinstance(suuid, list) else [suuid]):
                label = sname if not isinstance(suuid, list) else f"{sname}{i}"
                info["states"][label] = u
                states[u] = StateInfo(u, uuid, name, label, room, cat, info["type"], info["unit"])
        controls[uuid] = info
        for su, sc in (c.get("subControls") or {}).items():
            add(su, sc, c)

    for uuid, c in (data.get("controls") or {}).items():
        add(uuid, c, None)
    return Structure(str(data.get("lastModified", "")), data.get("msInfo") or {}, controls, states)
