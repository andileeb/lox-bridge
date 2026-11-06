"""InfluxDB v2 / v1 / VictoriaMetrics via line protocol over HTTP."""

import math

import httpx

from ...settings.models import InfluxSink as InfluxCfg
from .base import Point, Sink


def _esc_tag(s: str) -> str:
    return s.replace("\\", "\\\\").replace(",", "\\,").replace("=", "\\=").replace(" ", "\\ ").replace("\n", "\\n")


def _esc_meas(s: str) -> str:
    return s.replace("\\", "\\\\").replace(",", "\\,").replace(" ", "\\ ")


def _esc_str(s: str) -> str:
    return '"' + s.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n") + '"'


def line(measurement: str, tags: dict[str, str], value: float | None, text: str | None, ts_ms: int) -> str:
    if value is not None and not math.isfinite(value):
        return ""  # NaN / inf can't be stored
    tag_s = "".join(f",{k}={_esc_tag(v)}" for k, v in tags.items() if v)  # empty tag values are invalid
    field = f"value={float(value)!r}" if value is not None else f"text={_esc_str(text or '')}"
    return f"{_esc_meas(measurement)}{tag_s} {field} {ts_ms}"


def point_tags(p: Point) -> dict[str, str]:
    i = p.info
    return {"miniserver": p.miniserver, "room": i.room, "category": i.category, "control": i.control,
            "state": i.state, "type": i.type, "uuid": i.uuid}


def write_request(cfg: InfluxCfg) -> tuple[str, dict, dict]:
    """(url, params, headers) for a write with ms precision."""
    base = cfg.url.rstrip("/")
    if cfg.version == "v2":
        return (f"{base}/api/v2/write", {"org": cfg.org, "bucket": cfg.bucket, "precision": "ms"},
                {"Authorization": f"Token {cfg.token.get_secret_value()}"})
    params = {"db": cfg.bucket, "precision": "ms"}
    if cfg.username:
        params |= {"u": cfg.username, "p": cfg.password.get_secret_value()}
    headers = {"Authorization": f"Bearer {cfg.token.get_secret_value()}"} if cfg.token.get_secret_value() else {}
    return f"{base}/write", params, headers


def _check(r: httpx.Response) -> None:
    if r.status_code >= 300:
        raise RuntimeError(f"InfluxDB answered {r.status_code}: {r.text[:200]}")


def write_lines_sync(cfg: InfluxCfg, lines: list[str]) -> None:
    url, params, headers = write_request(cfg)
    with httpx.Client(timeout=30) as c:
        for i in range(0, len(lines), 5000):
            _check(c.post(url, params=params, headers=headers, content="\n".join(lines[i : i + 5000]).encode()))


class InfluxSink(Sink):
    def __init__(self, cfg: InfluxCfg):
        super().__init__(cfg)
        self.client = httpx.AsyncClient(timeout=15)

    async def write(self, batch: list[Point]) -> None:
        lines = (line(self.cfg.measurement, point_tags(p), p.value, p.text, int(p.ts * 1000)) for p in batch)
        body = "\n".join(x for x in lines if x)
        url, params, headers = write_request(self.cfg)
        _check(await self.client.post(url, params=params, headers=headers, content=body.encode()))

    async def test(self) -> str:
        url, params, headers = write_request(self.cfg)
        _check(await self.client.post(url, params=params, headers=headers, content=b""))
        return "ok, write accepted"

    async def close(self) -> None:
        await self.client.aclose()
