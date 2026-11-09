"""Miniserver system status (CPU, PLC load, cycles, memory, tasks) as an extra control "Miniserver system".

Read-only HTTP queries, polled by the hub. Measured on a Gen 2 with 17.3: token auth gets 403 for jdev/sys/lastcpu
and numtasks even as admin, Basic auth (over HTTPS) gets them; jdev/sys/heap works with both.
The control is added to every structure, so it shows up in the Live view and the control picker like a real one.
"""

import json
import re

from .http import MiniserverHTTP
from .structure import StateInfo, Structure

CONTROL = "10b51d9e-5359-5354-0000000000000000"  # fixed, made up: "SYST"; real Loxone UUIDs never look like this
NAME, ROOM, CATEGORY, TYPE = "Miniserver system", "Miniserver", "System", "LoxBridgeSystem"
STATES = {  # state name -> (uuid, unit)
    "cpu": ("10b51d9e-5359-5354-0000000000000001", "%"),
    "plcLoad": ("10b51d9e-5359-5354-0000000000000002", "%"),
    "plcCycles": ("10b51d9e-5359-5354-0000000000000003", "/s"),
    "heapUsed": ("10b51d9e-5359-5354-0000000000000004", "kB"),
    "heapTotal": ("10b51d9e-5359-5354-0000000000000005", "kB"),
    "tasks": ("10b51d9e-5359-5354-0000000000000006", ""),
}


def add_to(st: Structure) -> Structure:
    st.controls[CONTROL] = {"uuid": CONTROL, "name": NAME, "type": TYPE, "room": ROOM, "category": CATEGORY,
                            "unit": "", "states": {k: u for k, (u, _) in STATES.items()}, "statistic": None}
    for name, (uuid, unit) in STATES.items():
        st.states[uuid] = StateInfo(uuid, CONTROL, NAME, name, ROOM, CATEGORY, TYPE, unit)
    return st


def _value(client: MiniserverHTTP, cmd: str) -> str:
    ll = json.loads(client._request(cmd)).get("LL", {})
    code = str(ll.get("Code", ll.get("code", "200")))
    if code != "200":
        raise PermissionError(f"{cmd}: LL code {code}")
    return str(ll.get("value", ""))


def parse(lastcpu: str, heap: str, tasks: str) -> dict[str, float]:
    """'CPU:23 SPS:18 Cycles:98', '666392/1014416kB', '54' -> {state name: value}."""
    out: dict[str, float] = {}
    for key, name in (("CPU", "cpu"), ("SPS", "plcLoad"), ("Cycles", "plcCycles")):
        if m := re.search(rf"{key}:\s*(\d+(?:\.\d+)?)", lastcpu):
            out[name] = float(m.group(1))
    if m := re.match(r"\s*(\d+)\s*/\s*(\d+)", heap):
        out["heapUsed"], out["heapTotal"] = float(m.group(1)), float(m.group(2))
    if tasks.strip().isdigit():
        out["tasks"] = float(tasks)
    return out


def read(url: str, user: str, password: str, verify_tls: bool) -> dict[str, float]:
    with MiniserverHTTP(url, user, password, verify_tls, retries=1, timeout=10, auth_mode="basic") as c:
        return parse(_value(c, "jdev/sys/lastcpu"), _value(c, "jdev/sys/heap"), _value(c, "jdev/sys/numtasks"))


if __name__ == "__main__":  # ponytail: self-check of the parser against answers measured on 17.3
    assert parse("CPU:23 SPS:18 Cycles:98", "666392/1014416kB", "54") == {
        "cpu": 23, "plcLoad": 18, "plcCycles": 98, "heapUsed": 666392, "heapTotal": 1014416, "tasks": 54}
    assert parse("", "", "") == {}
    print("ok")
