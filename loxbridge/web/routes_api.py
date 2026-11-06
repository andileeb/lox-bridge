"""JSON API under /api/v1 (OpenAPI docs at /api/docs) plus /metrics."""

import re
import time

import httpx
from fastapi import APIRouter, Body, Depends, HTTPException, Query, Request
from fastapi.responses import PlainTextResponse, StreamingResponse
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest
from pydantic import SecretStr, ValidationError

from .. import __version__, metrics
from ..settings.models import Config, InfluxSink
from ..settings.store import decrypt_tree
from .common import csrf_guard, svc, zip_stream
from .forms import SECRET_SET

router = APIRouter()
api = APIRouter(prefix="/api/v1", dependencies=[Depends(csrf_guard)], tags=["api"])


@router.get("/metrics", include_in_schema=False)
def prom(request: Request):
    tok = svc(request).cfg.security.metrics_token.get_secret_value()
    if tok and request.headers.get("Authorization", "") != f"Bearer {tok}":
        raise HTTPException(401, "metrics token required")
    return PlainTextResponse(generate_latest(metrics.REGISTRY), media_type=CONTENT_TYPE_LATEST)


@api.get("/health")
def health(request: Request):
    s = svc(request)
    return {"status": "ok", "version": __version__, "uptime": round(time.time() - s.started),
            "bridge": {ms: st["connected"] for ms, st in s.hub.status.items()}}


@api.get("/miniservers")
def miniservers(request: Request):
    s = svc(request)
    out = []
    for m in s.cfg.miniservers:
        last = next(iter(s.store.jobs(kind="backup", target=m.id, limit=1)), None)
        nxt = s.scheduler.next_backup(m.id)
        out.append({"id": m.id, "name": m.name, "url": m.url, "bridge": s.hub.status.get(m.id),
                    "last_backup": last, "next_backup": nxt.isoformat() if nxt else None})
    return out


@api.post("/miniservers/{ms_id}/backup", status_code=202)
def backup(request: Request, ms_id: str):
    s = svc(request)
    try:
        s.cfg.miniserver(ms_id)
    except KeyError as e:
        raise HTTPException(404, "unknown Miniserver") from e
    return s.start_backup(ms_id).dict()


@api.get("/jobs")
def jobs(request: Request, kind: str | None = None, limit: int = 100):
    s = svc(request)
    running = [j.dict() for j in s.jobs.jobs.values() if not j.finished and (not kind or j.kind == kind)]
    return running + s.store.jobs(kind=kind, limit=limit)


@api.get("/jobs/{job_id}")
def job(request: Request, job_id: str):
    j = svc(request).jobs.get(job_id)
    if not j:
        raise HTTPException(404, "unknown job")
    return j


def all_snapshots(s, ms: str | None = None, dest: str | None = None) -> list[dict]:
    out = []
    for d in s.cfg.destinations:
        if not d.enabled or (dest and d.id != dest):
            continue
        store = s.store_for(d.id)
        try:
            for m in s.cfg.miniservers:
                if ms and m.id != ms:
                    continue
                out += [{**x, "destination": d.id} for x in store.snapshots(m.id)]
        except Exception as e:  # noqa: BLE001
            out.append({"destination": d.id, "error": str(e)})
    return out


@api.get("/snapshots")
def snapshots(request: Request, miniserver: str | None = None, destination: str | None = None):
    return all_snapshots(svc(request), miniserver, destination)


def _manifest(s, dest: str, ms: str, snap: str):
    try:
        store = s.store_for(dest)
        return store, store.manifest(ms, snap)
    except KeyError as e:
        raise HTTPException(404, "unknown snapshot") from e


@api.get("/snapshots/{dest}/{ms}/{snap}")
def snapshot(request: Request, dest: str, ms: str, snap: str):
    return _manifest(svc(request), dest, ms, snap)[1]


@api.get("/snapshots/{dest}/{ms}/{snap}/download")
def download(request: Request, dest: str, ms: str, snap: str, path: str | None = None):
    store, m = _manifest(svc(request), dest, ms, snap)
    if path:
        if path not in m["files"]:
            raise HTTPException(404, "file not in snapshot")
        name = path.rsplit("/", 1)[-1]
        f = store.open(ms, snap, path, m)
        return StreamingResponse(iter(lambda: f.read(1 << 20), b""), media_type="application/octet-stream",
                                 headers={"Content-Disposition": f'attachment; filename="{name}"'})
    files = ((f"{snap}/{p}", store.open(ms, snap, p, m)) for p in sorted(m["files"]))
    return StreamingResponse(zip_stream(files), media_type="application/zip",  # type: ignore[arg-type]
                             headers={"Content-Disposition": f'attachment; filename="{snap}.zip"'})


@api.get("/controls")
def controls(request: Request, miniserver: str | None = None):
    s = svc(request)
    return [{"miniserver": ms, **{k: v for k, v in c.items() if k != "statistic"}}
            for ms, st in s.hub.structures.items() if not miniserver or ms == miniserver
            for c in st.controls.values()]


@api.get("/states")
def states(request: Request, miniserver: str | None = None):
    return svc(request).hub.states(miniserver)


@api.get("/states/{uuid}/history")
def history(request: Request, uuid: str, from_: str = Query("-24h", alias="from"), to: str = "now()",
            sink: str | None = None):
    """Raw points from the first InfluxDB v2 sink (Flux). from/to: Flux times, e.g. -7d or 2026-01-01T00:00:00Z."""
    s = svc(request)
    sinks = [x for x in s.cfg.sinks if isinstance(x, InfluxSink) and (not sink or x.id == sink)]
    if not sinks:
        raise HTTPException(404, "no InfluxDB sink configured")
    c = sinks[0]
    if not re.fullmatch(r"[0-9a-f-]{20,40}", uuid):
        raise HTTPException(400, "invalid uuid")
    for v in (from_, to):
        if not all(ch.isalnum() or ch in "-:.()TZ+" for ch in v):
            raise HTTPException(400, "invalid time")
    if c.version != "v2":
        q = f"SELECT value FROM {c.measurement} WHERE uuid = '{uuid}'"
        r = httpx.get(c.url.rstrip("/") + "/query", params={"db": c.bucket, "q": q, "epoch": "s"}, timeout=30)
        return r.json()
    flux = (f'from(bucket: "{c.bucket}") |> range(start: {from_}, stop: {to}) '
            f'|> filter(fn: (r) => r._measurement == "{c.measurement}" and r.uuid == "{uuid}")'
            ' |> keep(columns: ["_time", "_value", "state", "control"])')
    r = httpx.post(c.url.rstrip("/") + "/api/v2/query", params={"org": c.org}, timeout=30,
                   headers={"Authorization": f"Token {c.token.get_secret_value()}", "Accept": "application/csv",
                            "Content-Type": "application/vnd.flux"}, content=flux)
    if r.status_code != 200:
        raise HTTPException(502, f"InfluxDB answered {r.status_code}: {r.text[:200]}")
    out = []
    lines = r.text.splitlines()
    header = None
    for ln in lines:
        if not ln.strip():
            header = None
            continue
        cols = ln.split(",")
        if header is None:
            header = cols
            continue
        row = dict(zip(header, cols, strict=False))
        out.append({"time": row.get("_time"), "value": float(row["_value"]) if row.get("_value") else None,
                    "state": row.get("state"), "control": row.get("control")})
    return out


def _mask(data):
    """Config for the API: secrets as '••• set' / ''."""
    if isinstance(data, dict):
        return {k: _mask(v) for k, v in data.items()}
    if isinstance(data, list):
        return [_mask(v) for v in data]
    if isinstance(data, SecretStr):
        return SECRET_SET if data.get_secret_value() else ""
    return data


@api.get("/config")
def get_config(request: Request):
    return _mask(svc(request).cfg.model_dump())


def _merge_secrets(new, old):
    """Empty or '••• set' secrets in a submitted config keep the stored secret (matched by list item id)."""
    if isinstance(new, dict) and isinstance(old, dict):
        return {k: _merge_secrets(v, old.get(k)) for k, v in new.items()}
    if isinstance(new, list) and isinstance(old, list):
        by_id = {o.get("id"): o for o in old if isinstance(o, dict)}
        return [_merge_secrets(v, by_id.get(v.get("id")) if isinstance(v, dict) else None) for v in new]
    if isinstance(old, SecretStr) and new in ("", SECRET_SET, None):
        return old  # empty or '••• set' keeps the stored secret
    return new


@api.put("/config")
def put_config(request: Request, body: dict = Body(...)):
    s = svc(request)
    try:
        cfg = Config.model_validate(_merge_secrets(body, s.cfg.model_dump()))
    except ValidationError as e:
        raise HTTPException(422, e.errors(include_url=False, include_context=False)) from e
    return _mask(s.store.save(cfg).model_dump())


@api.get("/config/export")
def export_config(request: Request, secrets: bool = False):
    return svc(request).config_export(include_secrets=secrets)


@api.post("/config/import")
def import_config(request: Request, body: dict = Body(...)):
    s = svc(request)
    data = body.get("config", body)
    try:
        cfg = Config.model_validate(_merge_secrets(decrypt_tree(data, s.store.fernet), s.cfg.model_dump()))
    except ValidationError as e:
        raise HTTPException(422, e.errors(include_url=False, include_context=False)) from e
    s.store.save(cfg)
    return {"status": "ok"}

router.include_router(api)
