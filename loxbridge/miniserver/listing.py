"""Parser for /dev/fslist output.

Accepts the Miniserver format   ``d|-  <size> <Mon> <DD> <HH:MM|YYYY> <name>``
and ``ls -l`` style lines       ``drwxr-xr-x 1 root root <size> <Mon> <DD> <HH:MM|YYYY> <name>``.
"""

import re
from dataclasses import dataclass

_DATE = r"(?P<stamp>[A-Z][a-z]{2}\s+\d{1,2}\s+(?:\d{1,2}:\d{2}|\d{4}))"
_SHORT = re.compile(r"^(?P<kind>[d-])\s+(?P<size>\d+)\s+" + _DATE + r"\s+(?P<name>.+?)\s*$")
_LS = re.compile(
    r"^(?P<kind>[d-])[rwxsStT-]{9}\S*\s+\d+\s+\S+\s+\S+\s+(?P<size>\d+)\s+" + _DATE + r"\s+(?P<name>.+?)\s*$"
)


@dataclass(frozen=True)
class Entry:
    name: str
    is_dir: bool
    size: int
    stamp: str  # raw date text, only used to detect changes


def parse_listing(text: str) -> list[Entry]:
    out = []
    for line in text.splitlines():
        m = _SHORT.match(line) or _LS.match(line)
        if not m or m["name"] in (".", ".."):
            continue
        out.append(Entry(m["name"], m["kind"] == "d", int(m["size"]), " ".join(m["stamp"].split())))
    return out
