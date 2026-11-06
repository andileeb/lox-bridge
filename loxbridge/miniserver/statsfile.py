"""Parser for Miniserver statistics files (/stats/<control-uuid>[_N].<YYYYMM>).

Ported from lox-datamanager src/main/statfile.ts (layout from LoxStatEdit and
sarnau/Inside-The-Loxone-Miniserver), little endian:

  header:  u16 valueCount | u16 unknown (often 0x8000) | u32 controlType | i32 textLen | utf8 name | 0x00
           | zero-pad to the record stride
  records: u16 uuidFrag1 (3rd uuid group) | u16 uuidFrag2 (2nd group) | u32 ts (seconds since 2009-01-01,
           Miniserver wall clock) | valueCount × f64 | zero-pad to the stride

The stride is ambiguous for valueCount >= 3 (LoxStatEdit and sarnau disagree), so it is detected per file.
The `_N` suffix marks statisticV2 files (e.g. Meter `_1` power, `_2` meter reading).
"""

import math
import re
import struct
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

NAME_RE = re.compile(
    r"^(?P<uuid>[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{16})(?:_(?P<suffix>[1-9]))?\.(?P<ym>[12]\d{3}[01]\d)$"
)
LOX_EPOCH = datetime(2009, 1, 1)
HEADER_FIXED = 12


class UnsupportedStatsFile(Exception):
    pass


@dataclass
class StatsFile:
    value_count: int
    control_type: int
    name: str
    stride: int
    rows: list[tuple[int, list[float]]] = field(default_factory=list)  # (unix ts UTC, values)
    uuid_mismatches: int = 0


def lox_to_unix(seconds: int, tz: ZoneInfo) -> int:
    """Stored timestamps are the Miniserver's local wall clock."""
    return int((LOX_EPOCH + timedelta(seconds=seconds)).replace(tzinfo=tz).timestamp())


def stride_candidates(value_count: int) -> list[int]:
    lox_stat_edit = 8 + 8 * value_count + (8 if value_count > 1 else 0)
    slots = 1 if value_count <= 1 else 3 if value_count <= 3 else 7 if value_count <= 7 else 10
    sarnau = 8 + slots * 8
    return [lox_stat_edit] if lox_stat_edit == sarnau else [lox_stat_edit, sarnau]


def uuid_frags(uuid: str) -> tuple[int, int]:
    g = uuid.split("-")
    return int(g[2], 16), int(g[1], 16)


def _try(data: bytes, header_end: int, stride: int, frags: tuple[int, int]) -> tuple[int, int, bool] | None:
    """(stride, data start, strict) if the file fits this stride; strict = uuid frags match + ts ascending."""
    start = -(-header_end // stride) * stride
    if len(data) < start:  # empty file never padded up to the first record boundary
        return (stride, len(data), True) if len(data) == header_end else None
    if (len(data) - start) % stride:
        return None
    strict, prev = True, -1
    for off in range(start, len(data), stride):
        f1, f2, ts = struct.unpack_from("<HHI", data, off)
        if (f1, f2) != frags or ts < prev:
            strict = False
        prev = ts
    return stride, start, strict


def parse_stats(data: bytes, file_name: str, tz: ZoneInfo) -> StatsFile:
    m = NAME_RE.match(file_name)
    if not m:
        raise UnsupportedStatsFile(f"not a statistics file name: {file_name}")
    frags = uuid_frags(m["uuid"])
    if len(data) < HEADER_FIXED + 1:
        raise UnsupportedStatsFile("file too small for a statistics header")
    value_count, _, control_type, text_len = struct.unpack_from("<HHIi", data)
    if not 1 <= value_count <= 10:
        raise UnsupportedStatsFile(f"implausible value count {value_count}")
    if text_len < 0 or HEADER_FIXED + text_len + 1 > len(data):
        raise UnsupportedStatsFile(f"implausible name length {text_len}")
    name = data[HEADER_FIXED : HEADER_FIXED + text_len].decode(errors="replace")
    header_end = HEADER_FIXED + text_len + 1

    fits = [c for c in (_try(data, header_end, s, frags) for s in stride_candidates(value_count)) if c]
    if not fits:
        raise UnsupportedStatsFile(f"file size matches no known record layout for {value_count} value(s)")
    # strict wins; candidate order already prefers the LoxStatEdit stride on a tie
    stride, start, _ = next((c for c in fits if c[2]), fits[0])

    out = StatsFile(value_count, control_type, name, stride)
    for off in range(start, len(data), stride):
        f1, f2, ts = struct.unpack_from("<HHI", data, off)
        if (f1, f2) != frags:
            out.uuid_mismatches += 1
        vals = list(struct.unpack_from(f"<{value_count}d", data, off + 8))
        if all(math.isfinite(v) for v in vals):
            out.rows.append((lox_to_unix(ts, tz), vals))
    return out
