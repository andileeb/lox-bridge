"""Backfill: statistics files from a backup snapshot -> InfluxDB (same tags as live data) or CSV."""

import csv
import json
import logging
from collections.abc import Callable
from pathlib import Path
from zoneinfo import ZoneInfo

from ..backup.destinations.base import Destination
from ..miniserver.statsfile import NAME_RE, UnsupportedStatsFile, parse_stats
from ..miniserver.structure import StateInfo, Structure, parse_structure
from ..settings.models import InfluxSink
from .sinks.influx import line, write_lines_sync

log = logging.getLogger(__name__)


def snapshot_structure(store: Destination, ms: str, snapshot: str, manifest: dict, fallback: Path) -> Structure:
    """LoxAPP3.json from the snapshot if it has one, else the cached live structure."""
    for p in manifest["files"]:
        if p.rsplit("/", 1)[-1] == "LoxAPP3.json":
            with store.open(ms, snapshot, p, manifest) as f:
                return parse_structure(json.loads(f.read()))
    if fallback.exists():
        return parse_structure(json.loads(fallback.read_text()))
    raise FileNotFoundError("no LoxAPP3.json in the snapshot and no cached structure: enable the bridge once")


def stats_files(manifest: dict) -> list[tuple[str, str]]:
    """[(path, control uuid)] of statistics files in the snapshot."""
    out = []
    for p in manifest["files"]:
        m = NAME_RE.match(p.rsplit("/", 1)[-1])
        if m and p.startswith("stats/"):
            out.append((p, m["uuid"]))
    return sorted(out)


def output_states(control: dict, count: int) -> list[str]:
    """Name each value column after the matching live state so history and live data form one series."""
    states = list(control["states"])
    outputs = [o.get("name", "") for o in ((control.get("statistic") or {}).get("outputs") or [])]
    names = []
    for i in range(count):
        out = outputs[i] if i < len(outputs) else ""
        match = next((s for s in states if s.lower() == out.lower()), None)
        if match:
            names.append(match)
        elif count == 1 and len(states) == 1:
            names.append(states[0])
        elif count == 1 and "value" in states:
            names.append("value")
        else:
            names.append(out.lower() or f"value{i}")
    return names


def run_import(
    store: Destination, ms: str, snapshot: str, structure_cache: Path, tz: ZoneInfo, *,
    controls: set[str] | None = None, start: int | None = None, end: int | None = None,
    influx: InfluxSink | None = None, csv_path: Path | None = None,
    progress_get: Callable[[str], int] | None = None, progress_set: Callable[[str, int], None] | None = None,
    progress: Callable[[str, float], None] = lambda m, p: None,
) -> dict:
    manifest = store.manifest(ms, snapshot)
    structure = snapshot_structure(store, ms, snapshot, manifest, structure_cache)
    files = [(p, u) for p, u in stats_files(manifest) if controls is None or u in controls]
    skipped, points = [], 0
    writer = None
    fh = open(csv_path, "w", newline="") if csv_path else None
    try:
        if fh:
            writer = csv.writer(fh)
            writer.writerow(["time", "miniserver", "room", "category", "control", "state", "uuid", "value"])
        for n, (path, cuuid) in enumerate(files, 1):
            control = structure.controls.get(cuuid)
            if control is None:
                skipped.append(f"{path}: control not in structure")
                continue
            with store.open(ms, snapshot, path, manifest) as f:
                data = f.read()
            try:
                sf = parse_stats(data, tz)
            except UnsupportedStatsFile as e:
                log.warning("skipping %s: %s", path, e)
                skipped.append(f"{path}: {e}")
                continue
            names = output_states(control, sf.value_count)
            done_until = progress_get(path) if progress_get else 0
            lines: list[str] = []
            last = done_until
            for ts, vals in sf.rows:
                if ts <= done_until or (start and ts < start) or (end and ts > end):
                    continue
                last = max(last, ts)
                for name, v in zip(names, vals, strict=True):
                    st = control["states"].get(name, cuuid)
                    info = StateInfo(st, cuuid, control["name"], name, control["room"], control["category"],
                                     control["type"], control["unit"])
                    if writer:
                        writer.writerow([ts, ms, info.room, info.category, info.control, name, st, v])
                    if influx:
                        tags = {"miniserver": ms, "room": info.room, "category": info.category,
                                "control": info.control, "state": name, "type": info.type, "uuid": st}
                        if ln := line(influx.measurement, tags, v, None, ts * 1000):
                            lines.append(ln)
                    points += 1
            if influx and lines:
                write_lines_sync(influx, lines)  # same timestamps overwrite: re-imports are idempotent
            if progress_set and last > done_until:
                progress_set(path, last)
            progress(f"{path}: {len(sf.rows)} rows", 100 * n / max(len(files), 1))
    finally:
        if fh:
            fh.close()
    return {"status": "ok" if not skipped else "partial", "files": len(files), "points": points,
            "skipped": skipped}
