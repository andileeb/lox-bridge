"""Parser for Miniserver statistics files (/stats/<control-uuid>.<YYYYMM>).

Layout as reverse-engineered by the community (little endian):
  header:  uint32 value_count, uint32 control_type, uint32 name_len, name[name_len] (padded to 4)
  entries: uuid[16], uint32 timestamp (seconds since 2009-01-01, Miniserver local time), double[value_count]
ponytail: verified only against synthetic files built to this spec. Anything that doesn't fit the
layout exactly is reported as unsupported (skip with warning), never guessed.
"""

import re
import struct
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from .ws import lox_uuid_bytes

NAME_RE = re.compile(r"^(?P<uuid>[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{16})\.(?P<ym>\d{6})$")
LOX_EPOCH = datetime(2009, 1, 1)


class UnsupportedStatsFile(Exception):
    pass


@dataclass
class StatsFile:
    value_count: int
    control_type: int
    name: str
    rows: list[tuple[int, list[float]]] = field(default_factory=list)  # (unix ts UTC, values)


def lox_to_unix(seconds: int, tz: ZoneInfo) -> int:
    return int((LOX_EPOCH + timedelta(seconds=seconds)).replace(tzinfo=tz).timestamp())


def parse_stats(data: bytes, tz: ZoneInfo) -> StatsFile:
    if len(data) < 12:
        raise UnsupportedStatsFile("file too short")
    count, ctype, name_len = struct.unpack_from("<III", data)
    if not 1 <= count <= 64 or name_len > 1024:
        raise UnsupportedStatsFile(f"unknown header (value_count={count}, name_len={name_len})")
    entry = 20 + 8 * count
    # name padding is not documented; accept whichever start offset makes the body fit exactly
    for start in (12 + name_len + (-name_len % 4), 12 + name_len):
        if start <= len(data) and (len(data) - start) % entry == 0:
            break
    else:
        raise UnsupportedStatsFile("body does not match the expected entry size")
    out = StatsFile(count, ctype, data[12 : 12 + name_len].split(b"\0")[0].decode(errors="replace"))
    for off in range(start, len(data), entry):
        (ts,) = struct.unpack_from("<I", data, off + 16)
        out.rows.append((lox_to_unix(ts, tz), list(struct.unpack_from(f"<{count}d", data, off + 20))))
    return out


def build_stats(name: str, uid: str, rows: list[tuple[int, list[float]]], ctype: int = 0) -> bytes:
    """Write a file to the spec above (rows use Loxone-epoch seconds). Used by tests and the mock."""
    count = len(rows[0][1]) if rows else 1
    nb = name.encode()
    out = struct.pack("<III", count, ctype, len(nb)) + nb + b"\0" * (-len(nb) % 4)
    for ts, vals in rows:
        out += lox_uuid_bytes(uid) + struct.pack(f"<I{count}d", ts, *vals)
    return out

