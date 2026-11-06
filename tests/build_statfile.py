"""Builds Loxone stat files byte by byte, independently of the parser (port of lox-datamanager
tests/build-statfile.ts), so parser tests aren't circular. `variant` picks the stride model."""

import struct

LOX_EPOCH_UNIX = 1230768000


def stride_for(value_count: int, variant: str) -> int:
    if variant == "loxstatedit":
        return 8 + 8 * value_count + (8 if value_count > 1 else 0)
    slots = 1 if value_count <= 1 else 3 if value_count <= 3 else 7 if value_count <= 7 else 10
    return 8 + slots * 8


def build_statfile(records: list[tuple[int, list[float]]], value_count: int, *,
                   uuid: str = "10f7ca5e-01e3-15b2-ffff8caa2f4b3d5c", suffix: int | None = None,
                   yyyymm: str = "202401", name: str = "Test statistic", variant: str = "loxstatedit",
                   control_type: int = 42, frags: tuple[int, int] | None = None) -> tuple[str, bytes]:
    """records: (seconds since 2009-01-01, values). Returns (file name, bytes)."""
    file_name = f"{uuid}{f'_{suffix}' if suffix else ''}.{yyyymm}"
    stride = stride_for(value_count, variant)
    g = uuid.split("-")
    f1, f2 = frags or (int(g[2], 16), int(g[1], 16))
    nb = name.encode()
    header = struct.pack("<HHIi", value_count, 0x8000, control_type, len(nb)) + nb + b"\0"
    start = -(-len(header) // stride) * stride
    buf = bytearray(start + len(records) * stride)
    buf[: len(header)] = header
    off = start
    for ts, vals in records:
        struct.pack_into(f"<HHI{len(vals)}d", buf, off, f1, f2, ts, *vals)
        off += stride
    return file_name, bytes(buf)


def lox_ts(unix: int) -> int:
    return unix - LOX_EPOCH_UNIX


JAN_2024 = 473299200  # 2024-01-01 00:00 in seconds since 2009-01-01


def make_records(n: int, value_count: int, start_lox: int = JAN_2024, interval: int = 600):
    """n records every `interval` s from 2024-01-01 (Loxone time)."""
    import math

    return [(start_lox + i * interval, [math.sin(i / 10 + v) * 100 for v in range(value_count)]) for i in range(n)]
