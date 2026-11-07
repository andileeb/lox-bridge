"""HTML pages (Jinja2 + HTMX)."""

import asyncio
import json
import time
from datetime import datetime
from pathlib import Path

from argon2.exceptions import VerifyMismatchError
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, StreamingResponse
from pydantic import ValidationError

from .. import logs
from ..app import HASHER
from ..backup.manifest import diff
from ..backup.runner import ms_client, restore
from ..miniserver.ws import probe_sync
from ..settings.models import (
    S3_PRESETS,
    BackupSettings,
    InfluxSink,
    LocalDestination,
    Miniserver,
    MqttSink,
    Notifications,
    PrometheusSink,
    S3Destination,
    Security,
    WebhookSink,
)
from . import forms
from .common import LIMITER, csrf_guard, redirect, render, svc
from .routes_api import all_snapshots, export_config, import_config

router = APIRouter(dependencies=[Depends(csrf_guard)], include_in_schema=False)
DEST_TYPES = {"local": LocalDestination, "s3": S3Destination}
SINK_TYPES = {"influx": InfluxSink, "prometheus": PrometheusSink, "mqtt": MqttSink, "webhook": WebhookSink}
SINK_SKIP = {"id", "type", "selection"}
SINK_NAMES = {"influx": "InfluxDB", "prometheus": "Prometheus", "mqtt": "MQTT", "webhook": "Webhook"}
# fields shown up front on the edit pages; the rest is folded into "Advanced"
SECTIONS: dict[type, list[tuple[str, list[str]]]] = {
    Miniserver: [("Connection", ["name", "url"]),
                 ("Backup", ["backup_enabled", "backup_user", "backup_password"]),
                 ("Live data", ["bridge_enabled", "bridge_user", "bridge_password"])],
    LocalDestination: [("", ["name", "path", "keep", "enabled"])],
    S3Destination: [("", ["name", "endpoint", "bucket", "access_key", "secret_key", "keep", "mode", "enabled"])],
    InfluxSink: [("", ["name", "version", "url", "org", "bucket", "token", "enabled"])],
    PrometheusSink: [("", ["name", "enabled"])],
    MqttSink: [("", ["name", "host", "port", "username", "password", "ha_discovery", "enabled"])],
    WebhookSink: [("", ["name", "url", "secret", "enabled"])],
    Notifications: [("", ["on"]), ("ntfy", ["ntfy_url", "ntfy_token"]), ("Gotify", ["gotify_url", "gotify_token"]),
                    ("Email", ["smtp_host", "smtp_port", "smtp_starttls", "smtp_user", "smtp_password",
                               "smtp_from", "smtp_to"]),
                    ("Webhook and healthchecks", ["webhook_url", "healthcheck_success_url",
                                                  "healthcheck_fail_url"])],
}
SCHEDULES = [("03:00", "Every night at 03:00"), ("00:00, 06:00, 12:00, 18:00", "Every 6 hours"),
             ("0 3 * * 0", "Weekly, Sunday 03:00")]


def _flash(request: Request) -> str | None:
    return request.session.pop("flash", None)


def _page(request: Request, name: str, **ctx):
    return render(request, name, flash=_flash(request), **ctx)


async def _form(request: Request) -> dict:
    return dict(await request.form())


def _locked_names(request: Request, path: str) -> set[str]:
    return {p[len(path) + 1:] for p in svc(request).store.locked if p.startswith(path + ".")}


def _sections(model, path: str, locked: set[str], skip: set[str] = frozenset()):  # type: ignore[assignment]
    return forms.sections(forms.fields(model, path, locked, skip), SECTIONS.get(type(model), []))


def _save(request: Request, cfg) -> str | None:
    try:
        svc(request).store.save(cfg)
        return None
    except ValidationError as e:
        return "; ".join(forms.errors(e))


# --- auth -----------------------------------------------------------------------------------------
@router.get("/login")
def login_page(request: Request, next: str = "/"):
    return _page(request, "login.html", next=next, no_user=not svc(request).store.has_user())


@router.post("/login")
async def login(request: Request):
    f = await _form(request)
    ip = request.client.host if request.client else ""
    s = svc(request)
    nxt = f.get("next", "/")
    if not nxt.startswith("/") or nxt.startswith("//"):
        nxt = "/"
    if LIMITER.blocked(ip):
        return _page(request, "login.html", next=nxt, error="Too many failed logins, try again in 5 minutes.")
    h = s.store.user_hash(f.get("username", ""))
    try:
        if not h:
            raise VerifyMismatchError
        HASHER.verify(h, f.get("password", ""))
    except VerifyMismatchError:
        LIMITER.fail(ip)
        return _page(request, "login.html", next=nxt, error="Wrong user name or password.")
    request.session.clear()
    request.session["user"] = f["username"]
    return redirect(request, nxt)


@router.post("/logout")
def logout(request: Request):
    request.session.clear()
    return redirect(request, "/login")


# --- dashboard ------------------------------------------------------------------------------------
def _dashboard_rows(s) -> list[dict]:
    rows = []
    for m in s.cfg.miniservers:
        last = next(iter(s.store.jobs(kind="backup", target=m.id, limit=1)), None)
        st = s.hub.structures.get(m.id)
        rows.append({
            "ms": m, "last": last, "next": s.scheduler.next_backup(m.id),
            "running": s.jobs.running("backup", m.id),
            "bridge": s.hub.status.get(m.id), "firmware": (st.ms_info.get("swVersion") if st else None),
        })
    return rows


@router.get("/")
def dashboard(request: Request):
    s = svc(request)
    return _page(request, "dashboard.html", rows=_dashboard_rows(s), sinks=s.hub.sink_status(),
                 destinations=s.cfg.destinations)


@router.get("/partials/dashboard")
def dashboard_partial(request: Request):
    s = svc(request)
    return render(request, "_dashboard_rows.html", rows=_dashboard_rows(s), sinks=s.hub.sink_status(),
                  destinations=s.cfg.destinations)


# --- Miniservers ----------------------------------------------------------------------------------
@router.get("/miniservers")
def miniservers(request: Request):
    return _page(request, "miniservers.html", items=svc(request).cfg.miniservers)


def _ms_index(cfg, ms_id: str) -> int:
    return next((i for i, m in enumerate(cfg.miniservers) if m.id == ms_id), -1)


@router.get("/miniservers/{ms_id}")
def miniserver_edit(request: Request, ms_id: str):
    cfg = svc(request).cfg
    i = _ms_index(cfg, ms_id)
    if i < 0:
        return redirect(request, "/setup")
    m = cfg.miniservers[i]
    return _page(request, "edit.html", title=m.name,
                 sections=_sections(m, f"miniservers.{i}", svc(request).store.locked, {"id"}),
                 action=f"/miniservers/{ms_id}", back="/miniservers", test_url=f"/miniservers/{ms_id}/test",
                 delete_url=f"/miniservers/{ms_id}/delete")


@router.post("/miniservers/{ms_id}")
async def miniserver_save(request: Request, ms_id: str):
    s = svc(request)
    cfg = s.cfg.model_copy(deep=True)
    i = _ms_index(cfg, ms_id)
    f = await _form(request)
    try:
        m = forms.parse(Miniserver, f, cfg.miniservers[i] if i >= 0 else None,
                        ({"id"} if i >= 0 else set()) | _locked_names(request, f"miniservers.{i}"))
    except ValidationError as e:
        request.session["flash"] = "Error: " + "; ".join(forms.errors(e))
        return redirect(request, f"/miniservers/{ms_id}")
    if i >= 0:
        cfg.miniservers[i] = m
    else:
        cfg.miniservers.append(m)
    err = _save(request, cfg)
    if err:
        request.session["flash"] = "Error: " + err
        return redirect(request, f"/miniservers/{ms_id}")
    return redirect(request, "/miniservers", f"Saved {m.name}.")


@router.post("/miniservers/{ms_id}/delete")
def miniserver_delete(request: Request, ms_id: str):
    cfg = svc(request).cfg.model_copy(deep=True)
    cfg.miniservers = [m for m in cfg.miniservers if m.id != ms_id]
    return redirect(request, "/miniservers", _save(request, cfg) or "Deleted. Existing snapshots were kept.")


def _test_miniserver(m: Miniserver) -> list[tuple[bool, str]]:
    out: list[tuple[bool, str]] = []
    if m.backup_user:
        try:
            with ms_client(m) as c:
                entries = c.fslist("/")
                out.append((True, f"backup user: login ok, root has {len(entries)} entries "
                                  f"({', '.join(e.name for e in entries[:8])})"))
                try:
                    out.append((True, f"firmware {c.jdev('cfg/version')}"))
                except Exception as e:  # noqa: BLE001
                    out.append((False, f"firmware: {e}"))
        except Exception as e:  # noqa: BLE001
            out.append((False, f"backup user: {e}"))
    else:
        out.append((False, "no backup user set"))
    if m.bridge_user:
        try:
            st = probe_sync(m.url, m.bridge_user, m.bridge_password.get_secret_value(), m.verify_tls)
            info = st.ms_info
            out.append((True, f"bridge user: token ok, structure '{info.get('projectName', '')}' with "
                              f"{len(st.controls)} controls, serial {info.get('serialNr', '?')}, "
                              f"firmware {info.get('swVersion', '?')}"))
        except Exception as e:  # noqa: BLE001
            out.append((False, f"bridge user: {e or type(e).__name__}"))
    return out


@router.post("/miniservers/{ms_id}/test")
async def miniserver_test(request: Request, ms_id: str):
    m = svc(request).cfg.miniserver(ms_id)
    f = await _form(request)
    if "url" in f:  # test what's in the form, before saving
        try:
            skip = {"id"} | _locked_names(request, f"miniservers.{_ms_index(svc(request).cfg, ms_id)}")
            m = forms.parse(Miniserver, f, m, skip)  # type: ignore[assignment]
        except ValidationError as e:
            return render(request, "_results.html", results=[(False, x) for x in forms.errors(e)])
    results = await asyncio.to_thread(_test_miniserver, m)
    return render(request, "_results.html", results=results)


@router.post("/miniservers/{ms_id}/backup")
def miniserver_backup(request: Request, ms_id: str):
    job = svc(request).start_backup(ms_id)
    return redirect(request, f"/jobs/{job.id}")


# --- setup wizard: find → log in → schedule → first backup ----------------------------------------
@router.get("/setup")
def setup_page(request: Request):
    s = svc(request)
    return _page(request, "setup.html", schedules=SCHEDULES, schedule=s.cfg.backup.schedule,
                 locked_schedule="backup.schedule" in s.store.locked,
                 dest=next((d for d in s.cfg.destinations if d.enabled), None))


@router.post("/setup/scan")
async def setup_scan(request: Request):
    from ..miniserver import discover

    spec = str((await _form(request)).get("network", "")).strip()
    host = (request.url.hostname or "").strip("[]")
    client = request.client.host if request.client else ""
    nets = [spec] if spec else discover.networks(host, client)
    try:
        found = await discover.scan(nets) if nets else []
    except ValueError as e:
        return HTMLResponse(f"<p class='error'>{e}</p>")
    known = {m.url.rstrip("/") for m in svc(request).cfg.miniservers}
    return render(request, "_found.html", found=found, nets=nets, known=known)


def _first_contact(m: Miniserver) -> str:
    """Log in with the backup user (raises with a readable message) and read the Miniserver's name."""
    with ms_client(m) as c:
        c.fslist("/")
        try:
            return json.loads(c.get("data/LoxAPP3.json"))["msInfo"].get("msName") or "Miniserver"
        except Exception:  # noqa: BLE001 - the name is a nicety
            return "Miniserver"


@router.post("/setup")
async def setup_save(request: Request):
    f = await _form(request)
    s = svc(request)
    url = str(f.get("url", "")).strip().rstrip("/")
    if url and "://" not in url:
        url = "https://" + url
    user, pw, live = str(f.get("user", "")).strip(), str(f.get("password", "")), "live" in f
    cfg = s.cfg.model_copy(deep=True)
    taken = {m.id for m in cfg.miniservers}
    ms_id = next(f"ms{n}" for n in range(1, 1000) if f"ms{n}" not in taken)
    try:
        m = Miniserver(id=ms_id, name="Miniserver", url=url, backup_user=user, backup_password=pw,  # type: ignore[arg-type]
                       bridge_enabled=live, bridge_user=user if live else "",
                       bridge_password=pw if live else "")  # type: ignore[arg-type]
        if not user:
            raise ValueError("enter the Loxone user name")
        m.name = await asyncio.to_thread(_first_contact, m)
    except ValidationError:
        return render(request, "_results.html", results=[(False, "Address must look like https://192.168.1.77")])
    except Exception as e:  # noqa: BLE001
        return render(request, "_results.html", results=[(False, f"Could not log in: {e or type(e).__name__}")])
    cfg.miniservers.append(m)
    if f.get("schedule") and "backup.schedule" not in s.store.locked:
        cfg.backup.schedule = str(f["schedule"])
    if not any(d.enabled for d in cfg.destinations):  # a backup needs somewhere to go: the /backups folder
        local = next((d for d in cfg.destinations if d.type == "local"), None)
        if local:
            local.enabled = True
        else:
            cfg.destinations.append(LocalDestination(id="local"))
    if err := _save(request, cfg):
        return render(request, "_results.html", results=[(False, err)])
    job = s.start_backup(ms_id)
    request.session["flash"] = f"{m.name} connected. The first backup is running."
    return HTMLResponse("", headers={"HX-Redirect": f"{request.scope.get('root_path', '')}/jobs/{job.id}"})


# --- backup settings ------------------------------------------------------------------------------
@router.get("/backup")
def backup_page(request: Request):
    s = svc(request)
    fields = forms.fields(s.cfg.backup, "backup", s.store.locked)
    return _page(request, "backup.html", fields={f["name"]: f for f in fields},
                 schedules=SCHEDULES,
                 miniservers=s.cfg.miniservers,
                 influx=[x for x in s.cfg.sinks if x.type == "influx"],
                 next={m.id: s.scheduler.next_backup(m.id) for m in s.cfg.miniservers})


@router.post("/backup")
async def backup_save(request: Request):
    s = svc(request)
    cfg = s.cfg.model_copy(deep=True)
    try:
        cfg.backup = forms.parse(BackupSettings, await _form(request), cfg.backup,  # type: ignore[assignment]
                                 _locked_names(request, "backup"))
    except ValidationError as e:
        return redirect(request, "/backup", "Error: " + "; ".join(forms.errors(e)))
    return redirect(request, "/backup", _save(request, cfg) or "Saved.")


@router.get("/backup/tree")
async def backup_tree(request: Request, ms: str, path: str = ""):
    m = svc(request).cfg.miniserver(ms)

    def ls():
        with ms_client(m) as c:
            return c.fslist(path)

    try:
        entries = await asyncio.to_thread(ls)
    except Exception as e:  # noqa: BLE001
        return HTMLResponse(f"<p class='error'>{e}</p>")
    return render(request, "_tree.html", entries=entries, path=path.strip("/"), ms=ms)


# --- destinations ---------------------------------------------------------------------------------
@router.get("/destinations")
def destinations(request: Request):
    return _page(request, "destinations.html", items=svc(request).cfg.destinations, presets=S3_PRESETS)


def _dest_index(cfg, dest_id: str) -> int:
    return next((i for i, d in enumerate(cfg.destinations) if d.id == dest_id), -1)


@router.get("/destinations/{dest_id}")
def destination_edit(request: Request, dest_id: str, type: str = "s3", preset: str = ""):
    s = svc(request)
    i = _dest_index(s.cfg, dest_id)
    if i >= 0:
        d = s.cfg.destinations[i]
    elif dest_id == "new" and type in DEST_TYPES:
        taken = {x.id for x in s.cfg.destinations}
        new_id = next(f"{preset or type}{n}" for n in range(1, 100) if f"{preset or type}{n}" not in taken)
        d = DEST_TYPES[type](id=new_id, **S3_PRESETS.get(preset, {}))  # type: ignore[arg-type]
    else:
        raise HTTPException(404)
    title = d.name if i >= 0 else ("Add local folder" if type == "local" else "Add S3")
    return _page(request, "edit.html", title=title,
                 sections=_sections(d, f"destinations.{i}", s.store.locked, {"type"} | ({"id"} if i >= 0 else set())),
                 action=f"/destinations/{dest_id}?type={d.type}", back="/destinations", is_new=i < 0,
                 test_url=f"/destinations/{dest_id}/test?type={d.type}",
                 delete_url=f"/destinations/{dest_id}/delete" if i >= 0 else None)


@router.post("/destinations/{dest_id}")
async def destination_save(request: Request, dest_id: str, type: str = "s3"):
    s = svc(request)
    cfg = s.cfg.model_copy(deep=True)
    i = _dest_index(cfg, dest_id)
    existing = cfg.destinations[i] if i >= 0 else None
    model = DEST_TYPES[existing.type if existing else type]
    try:
        d = forms.parse(model, await _form(request), existing,
                        {"type"} | ({"id"} if existing else set()) | _locked_names(request, f"destinations.{i}"))
    except ValidationError as e:
        return redirect(request, f"/destinations/{dest_id}?type={type}", "Error: " + "; ".join(forms.errors(e)))
    if i >= 0:
        cfg.destinations[i] = d  # type: ignore[call-overload]
    else:
        cfg.destinations.append(d)  # type: ignore[arg-type]
    err = _save(request, cfg)
    if err:
        return redirect(request, f"/destinations/{dest_id}?type={type}", "Error: " + err)
    return redirect(request, "/destinations", "Saved.")


@router.post("/destinations/{dest_id}/delete")
def destination_delete(request: Request, dest_id: str):
    cfg = svc(request).cfg.model_copy(deep=True)
    cfg.destinations = [d for d in cfg.destinations if d.id != dest_id]
    return redirect(request, "/destinations", _save(request, cfg) or "Deleted. Stored data was not touched.")


@router.post("/destinations/{dest_id}/test")
async def destination_test(request: Request, dest_id: str, type: str = "s3"):
    from ..backup.runner import make_store

    s = svc(request)
    f = await _form(request)
    try:
        if "name" in f:  # test what's in the form, before saving
            i = _dest_index(s.cfg, dest_id)
            existing = s.cfg.destinations[i] if i >= 0 else None
            d = forms.parse(DEST_TYPES[existing.type if existing else type], f, existing,
                            {"type"} | ({"id"} if existing else set()))
            target = make_store(d, s.work)  # type: ignore[arg-type]
        else:
            target = s.store_for(dest_id)
        msg = await asyncio.to_thread(target.test)
        results = [(True, msg)]
    except ValidationError as e:
        results = [(False, x) for x in forms.errors(e)]
    except Exception as e:  # noqa: BLE001
        results = [(False, str(e) or e.__class__.__name__)]
    return render(request, "_results.html", results=results)


@router.post("/destinations/{dest_id}/gc")
def destination_gc(request: Request, dest_id: str):
    return redirect(request, f"/jobs/{svc(request).start_gc(dest_id).id}")


# --- snapshots ------------------------------------------------------------------------------------
@router.get("/snapshots")
async def snapshots(request: Request, ms: str = "", dest: str = ""):
    s = svc(request)
    items = await asyncio.to_thread(all_snapshots, s, ms or None, dest or None)
    return _page(request, "snapshots.html", items=items, ms=ms, dest=dest, miniservers=s.cfg.miniservers,
                 destinations=s.cfg.destinations)


@router.get("/snapshots/{dest}/{ms}/{snap}")
async def snapshot_view(request: Request, dest: str, ms: str, snap: str, compare: str = ""):
    s = svc(request)
    store = s.store_for(dest)
    try:
        m = await asyncio.to_thread(store.manifest, ms, snap)
    except KeyError as e:
        raise HTTPException(404) from e
    d = None
    if compare:
        d = diff(await asyncio.to_thread(store.manifest, ms, compare), m)
    others = [x["snapshot"] for x in await asyncio.to_thread(store.snapshots, ms) if x["snapshot"] != snap]
    return _page(request, "snapshot.html", m=m, dest=dest, ms=ms, snap=snap, diff=d, compare=compare,
                 others=others)


@router.post("/snapshots/{dest}/{ms}/{snap}/restore")
async def snapshot_restore(request: Request, dest: str, ms: str, snap: str):
    target = Path(str((await _form(request)).get("target", "")).strip())
    if not target.is_absolute():
        return redirect(request, f"/snapshots/{dest}/{ms}/{snap}", "Error: target must be an absolute path")
    if target.exists() and any(target.iterdir()):
        return redirect(request, f"/snapshots/{dest}/{ms}/{snap}", "Error: target folder is not empty")
    s = svc(request)
    store = s.store_for(dest)
    job = s.jobs.start("restore", ms, f"Restore {snap} to {target}",
                       lambda j: {"status": "ok", "files": restore(store, ms, snap, target, j.progress),
                                  "target": str(target)})
    return redirect(request, f"/jobs/{job.id}")


@router.post("/snapshots/{dest}/{ms}/{snap}/verify")
async def snapshot_verify(request: Request, dest: str, ms: str, snap: str):
    deep = (await _form(request)).get("deep") is not None
    s = svc(request)
    store = s.store_for(dest)

    def work(j):
        problems = store.verify(ms, snap, deep)
        for p in problems[:200]:
            j.progress(p)
        return {"status": "ok" if not problems else "failed", "problems": len(problems), "deep": deep}

    return redirect(request, f"/jobs/{s.jobs.start('verify', ms, f'Verify {snap}', work).id}")


@router.post("/snapshots/{dest}/{ms}/{snap}/delete")
async def snapshot_delete(request: Request, dest: str, ms: str, snap: str):
    await asyncio.to_thread(svc(request).store_for(dest).delete, ms, snap)
    return redirect(request, f"/snapshots?ms={ms}", f"Deleted {snap}.")


# --- bridge ---------------------------------------------------------------------------------------
@router.get("/bridge")
def bridge(request: Request, sink: str = "", ms: str = ""):
    s = svc(request)
    sinks = s.cfg.sinks
    current = next((x for x in sinks if x.id == sink), sinks[0] if sinks else None)
    ms = ms or next(iter(s.hub.structures), "")
    st = s.hub.structures.get(ms)
    controls = sorted(st.controls.values(), key=lambda c: (c["room"], c["name"])) if st else []
    values = s.hub.values.get(ms, {})
    return _page(request, "bridge.html", sinks=sinks, current=current, ms=ms, structures=s.hub.structures,
                 controls=controls, values=values, status=s.hub.status, sink_status=s.hub.sink_status(),
                 selected=set(current.selection) if current else set(), names=s.hub.ms_names)


@router.get("/bridge/live")
def bridge_live(request: Request, ms: str = ""):
    s = svc(request)
    rows = sorted(s.hub.states(ms or None), key=lambda r: -r["ts"])[:200]
    return render(request, "_live.html", rows=rows, now=time.time())


@router.post("/bridge/selection")
async def bridge_selection(request: Request):
    form = await request.form()
    sink_id, ms = str(form.get("sink", "")), str(form.get("ms", ""))
    s = svc(request)
    cfg = s.cfg.model_copy(deep=True)
    sk = cfg.sink(sink_id)
    st = s.hub.structures.get(ms)
    on_page = set(st.controls) if st else set()
    chosen = {str(x) for x in form.getlist("sel")}
    sk.selection = sorted((set(sk.selection) - on_page) | chosen)
    return redirect(request, f"/bridge?sink={sink_id}&ms={ms}",
                    _save(request, cfg) or f"{len(sk.selection)} controls exported to {sk.name or sk.id}.")


def _sink_index(cfg, sink_id: str) -> int:
    return next((i for i, x in enumerate(cfg.sinks) if x.id == sink_id), -1)


@router.get("/sinks/{sink_id}")
def sink_edit(request: Request, sink_id: str, type: str = "influx"):
    s = svc(request)
    i = _sink_index(s.cfg, sink_id)
    if i >= 0:
        sk = s.cfg.sinks[i]
    elif sink_id == "new" and type in SINK_TYPES:
        sk = SINK_TYPES[type](id=f"{type}{len(s.cfg.sinks) + 1}", name=SINK_NAMES[type])
    else:
        raise HTTPException(404)
    skip = SINK_SKIP - ({"id"} if i < 0 else set())
    return _page(request, "edit.html", title=(sk.name or sk.id) if i >= 0 else f"Add {sk.name} output",
                 sections=_sections(sk, f"sinks.{i}", s.store.locked, skip),
                 action=f"/sinks/{sink_id}?type={sk.type}", back="/bridge", is_new=i < 0,
                 test_url=f"/sinks/{sink_id}/test?type={sk.type}",
                 delete_url=f"/sinks/{sink_id}/delete" if i >= 0 else None,
                 note="After saving, tick the controls this output exports on the Live data page.")


@router.post("/sinks/{sink_id}")
async def sink_save(request: Request, sink_id: str, type: str = "influx"):
    s = svc(request)
    cfg = s.cfg.model_copy(deep=True)
    i = _sink_index(cfg, sink_id)
    existing = cfg.sinks[i] if i >= 0 else None
    model = SINK_TYPES[existing.type if existing else type]
    skip = (SINK_SKIP - ({"id"} if i < 0 else set())) | _locked_names(request, f"sinks.{i}")
    try:
        sk = forms.parse(model, await _form(request), existing, skip)
    except ValidationError as e:
        return redirect(request, f"/sinks/{sink_id}?type={type}", "Error: " + "; ".join(forms.errors(e)))
    if i >= 0:
        cfg.sinks[i] = sk  # type: ignore[call-overload]
    else:
        cfg.sinks.append(sk)  # type: ignore[arg-type]
    err = _save(request, cfg)
    if err:
        return redirect(request, f"/sinks/{sink_id}?type={type}", "Error: " + err)
    return redirect(request, f"/bridge?sink={sk.id}", "Saved.")


@router.post("/sinks/{sink_id}/delete")
def sink_delete(request: Request, sink_id: str):
    cfg = svc(request).cfg.model_copy(deep=True)
    cfg.sinks = [x for x in cfg.sinks if x.id != sink_id]
    return redirect(request, "/bridge", _save(request, cfg) or "Deleted.")


@router.post("/sinks/{sink_id}/test")
async def sink_test(request: Request, sink_id: str, type: str = "influx"):
    from ..bridge.hub import make_sink

    s = svc(request)
    f = await _form(request)
    i = _sink_index(s.cfg, sink_id)
    existing = s.cfg.sinks[i] if i >= 0 else None
    if "name" in f:  # test what's in the form, before saving
        try:
            model = forms.parse(SINK_TYPES[existing.type if existing else type], f, existing,
                                SINK_SKIP - ({"id"} if i < 0 else set()))
        except ValidationError as e:
            return render(request, "_results.html", results=[(False, x) for x in forms.errors(e)])
    elif existing:
        model = existing
    else:
        raise HTTPException(404)
    sk = make_sink(model)  # type: ignore[arg-type]
    try:
        results = [(True, await asyncio.wait_for(sk.test(), 15))]
    except Exception as e:  # noqa: BLE001
        results = [(False, str(e) or type(e).__name__)]
    finally:
        await sk.close()
    return render(request, "_results.html", results=results)


# --- history import -------------------------------------------------------------------------------
@router.get("/history")
async def history(request: Request, ms: str = ""):
    s = svc(request)
    ms = ms or (s.cfg.miniservers[0].id if s.cfg.miniservers else "")
    snaps = await asyncio.to_thread(all_snapshots, s, ms or None) if ms else []
    st = s.hub.structures.get(ms)
    controls = sorted((c for c in st.controls.values() if c.get("statistic")), key=lambda c: c["name"]) if st else []
    return _page(request, "history.html", ms=ms, miniservers=s.cfg.miniservers,
                 snaps=[x for x in snaps if "snapshot" in x], controls=controls,
                 influx=[x for x in s.cfg.sinks if x.type == "influx"],
                 jobs=s.store.jobs(kind="history", limit=10))


def _ts(v: str) -> int | None:
    return int(datetime.fromisoformat(v).timestamp()) if v else None


@router.post("/history")
async def history_start(request: Request):
    form = await request.form()
    s = svc(request)
    dest, snap = str(form["snapshot"]).split(":", 1)
    target = str(form.get("target", "csv"))
    controls = {str(x) for x in form.getlist("controls")} or None
    try:
        job = s.start_history_import(str(form["ms"]), dest, snap, controls, _ts(str(form.get("from", ""))),
                                     _ts(str(form.get("to", ""))), None if target == "csv" else target,
                                     csv=target == "csv")
    except (KeyError, ValueError) as e:
        return redirect(request, "/history", f"Error: {e}")
    return redirect(request, f"/jobs/{job.id}")


@router.get("/exports/{name}")
def export_download(request: Request, name: str):
    f = svc(request).work / "exports" / name
    if "/" in name or not f.is_file():
        raise HTTPException(404)
    return FileResponse(f, filename=name)


# --- notifications --------------------------------------------------------------------------------
@router.get("/notifications")
def notifications(request: Request):
    s = svc(request)
    return _page(request, "edit.html", title="Notifications",
                 sections=_sections(s.cfg.notifications, "notifications", s.store.locked),
                 action="/notifications", back=None, test_url="/notifications/test",
                 note="Healthcheck URLs are pinged after every backup (success or fail URL). The other channels "
                      "fire on failures, or always if 'On' is set to always.")


@router.post("/notifications")
async def notifications_save(request: Request):
    cfg = svc(request).cfg.model_copy(deep=True)
    try:
        cfg.notifications = forms.parse(Notifications, await _form(request), cfg.notifications,  # type: ignore[assignment]
                                        _locked_names(request, "notifications"))
    except ValidationError as e:
        return redirect(request, "/notifications", "Error: " + "; ".join(forms.errors(e)))
    return redirect(request, "/notifications", _save(request, cfg) or "Saved.")


@router.post("/notifications/test")
async def notifications_test(request: Request):
    from ..notify import notify

    n = svc(request).cfg.notifications
    f = await _form(request)
    if "on" in f:  # test what's in the form, before saving
        try:
            n = forms.parse(Notifications, f, n, _locked_names(request, "notifications"))  # type: ignore[assignment]
        except ValidationError as e:
            return render(request, "_results.html", results=[(False, x) for x in forms.errors(e)])
    n = n.model_copy(update={"on": "always"})
    errs = await asyncio.to_thread(notify, n, False, "LoxBridge test", "This is a test notification.")
    return render(request, "_results.html", results=[(False, e) for e in errs] or [(True, "sent")])


# --- logs and jobs --------------------------------------------------------------------------------
@router.get("/logs")
def logs_page(request: Request):
    s = svc(request)
    return _page(request, "logs.html", jobs=s.store.jobs(limit=100), level=s.cfg.log_level)


@router.get("/logs/stream")
async def logs_stream(request: Request):
    async def gen():
        backlog = list(logs.BUFFER.lines)[-300:]
        seq = backlog[-1][0] if backlog else 0
        for _, line in backlog:
            yield f"data: {json.dumps(line)}\n\n"
        while not await request.is_disconnected():
            new = logs.BUFFER.since(seq)
            for _, line in new:
                yield f"data: {json.dumps(line)}\n\n"
            if new:
                seq = new[-1][0]
            await asyncio.sleep(0.5)

    return StreamingResponse(gen(), media_type="text/event-stream", headers={"Cache-Control": "no-cache"})


@router.post("/logs/level")
async def logs_level(request: Request):
    cfg = svc(request).cfg.model_copy(deep=True)
    cfg.log_level = str((await _form(request)).get("level", "INFO"))  # type: ignore[assignment]
    return redirect(request, "/logs", _save(request, cfg) or f"Log level {cfg.log_level}.")


@router.get("/jobs/{job_id}")
def job_page(request: Request, job_id: str):
    j = svc(request).jobs.get(job_id)
    if not j:
        raise HTTPException(404)
    return _page(request, "job.html", job=j)


@router.get("/jobs/{job_id}/stream")
async def job_stream(request: Request, job_id: str):
    s = svc(request)

    async def gen():
        sent = 0
        while not await request.is_disconnected():
            j = s.jobs.jobs.get(job_id)
            if not j:
                break
            for m in j.messages[sent:]:
                yield f"data: {json.dumps({'message': m, 'percent': j.percent})}\n\n"
            sent = len(j.messages)
            if j.finished:
                yield f"event: done\ndata: {json.dumps({'status': j.status, 'summary': j.summary})}\n\n"
                break
            await asyncio.sleep(0.5)

    return StreamingResponse(gen(), media_type="text/event-stream", headers={"Cache-Control": "no-cache"})


# --- settings -------------------------------------------------------------------------------------
@router.get("/settings")
def settings(request: Request):
    s = svc(request)
    return _page(request, "settings.html", tokens=s.store.tokens(), new_token=request.session.pop("new_token", None),
                 fields=forms.fields(s.cfg.security, "security", s.store.locked), tz=s.tz_name or "UTC",
                 config_dir=s.config_dir, work=s.work)


@router.post("/settings/password")
async def settings_password(request: Request):
    f = await _form(request)
    s = svc(request)
    h = s.store.user_hash(request.session.get("user", ""))
    try:
        if not h:
            raise VerifyMismatchError
        HASHER.verify(h, f.get("current", ""))
    except VerifyMismatchError:
        return redirect(request, "/settings", "Error: current password is wrong")
    if len(f.get("new", "")) < 8 or f.get("new") != f.get("repeat"):
        return redirect(request, "/settings", "Error: new passwords must match and have at least 8 characters")
    s.store.set_user(request.session["user"], HASHER.hash(f["new"]))
    return redirect(request, "/settings", "Password changed.")


@router.post("/settings/tokens")
async def settings_token_create(request: Request):
    name = str((await _form(request)).get("name", "")).strip() or "token"
    request.session["new_token"] = svc(request).store.create_token(name)
    return redirect(request, "/settings")


@router.post("/settings/tokens/{token_id}/delete")
def settings_token_delete(request: Request, token_id: int):
    svc(request).store.delete_token(token_id)
    return redirect(request, "/settings", "Token deleted.")


@router.post("/settings/security")
async def settings_security(request: Request):
    cfg = svc(request).cfg.model_copy(deep=True)
    try:
        cfg.security = forms.parse(Security, await _form(request), cfg.security,  # type: ignore[assignment]
                                   _locked_names(request, "security"))
    except ValidationError as e:
        return redirect(request, "/settings", "Error: " + "; ".join(forms.errors(e)))
    return redirect(request, "/settings", _save(request, cfg) or "Saved.")


@router.get("/settings/export")
def settings_export(request: Request, secrets: bool = False):
    data = export_config(request, secrets)
    name = f"loxbridge-config-{datetime.now():%Y%m%d}.json"
    return HTMLResponse(json.dumps(data, indent=1), media_type="application/json",
                        headers={"Content-Disposition": f'attachment; filename="{name}"'})


@router.post("/settings/import")
async def settings_import(request: Request):
    form = await request.form()
    upload = form.get("file")
    try:
        data = json.loads(await upload.read())  # type: ignore[union-attr]
        import_config(request, data)
    except HTTPException as e:
        return redirect(request, "/settings", f"Error: import failed: {e.detail}")
    except (ValueError, AttributeError) as e:
        return redirect(request, "/settings", f"Error: not a LoxBridge config file ({e})")
    return redirect(request, "/settings", "Configuration imported.")

