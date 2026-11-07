"""App factory: wires settings, scheduler, jobs, bridge and the web UI/API."""

import asyncio
import logging
import os
import time
from contextlib import asynccontextmanager
from pathlib import Path
from zoneinfo import ZoneInfo

from argon2 import PasswordHasher
from argon2.exceptions import VerifyMismatchError
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from starlette.middleware.sessions import SessionMiddleware

from . import __version__, logs, metrics
from .backup.destinations.local import LocalStore
from .backup.runner import BackupBusy, make_store, run_backup
from .bridge.history import run_import
from .bridge.hub import Hub
from .jobs.manager import Job, JobManager
from .jobs.scheduler import Scheduler
from .notify import notify
from .settings.models import Config, InfluxSink
from .settings.store import Store, dump_config

log = logging.getLogger(__name__)
HASHER = PasswordHasher()


class Services:
    def __init__(self, config_dir: Path, work_dir: Path):
        self.config_dir, self.work = config_dir, work_dir
        self.work.mkdir(parents=True, exist_ok=True)
        self.store = Store(config_dir)
        self.tz_name = os.environ.get("TZ") or None
        self.tz = ZoneInfo(self.tz_name) if self.tz_name else ZoneInfo("UTC")
        self.jobs = JobManager(self.store)
        self.hub = Hub(work_dir)
        self.scheduler = Scheduler(self.tz_name, self._scheduled_backup, self._scheduled_gc)
        self.store.listeners.append(self._on_change)
        self.started = time.time()
        for j in self.store.jobs(kind="backup"):  # restore metrics after a restart
            if j["status"] in ("ok", "partial") and j["finished"]:
                metrics.backup_last_success.labels(j["target"]).set(j["finished"])
                break

    @property
    def cfg(self) -> Config:
        return self.store.config

    def bootstrap_admin(self) -> None:
        """ADMIN_PASSWORD wins on every start (like the TrueNAS app form), so editing it in the app YAML works."""
        pw = os.environ.get("ADMIN_PASSWORD")
        if not pw:
            if not self.store.has_user():
                log.warning("no admin account yet: set ADMIN_PASSWORD (or run 'reset-admin')")
            return
        user = os.environ.get("ADMIN_USER", "admin")
        h = self.store.user_hash(user)
        try:
            if h and HASHER.verify(h, pw):
                return
        except VerifyMismatchError:
            pass
        self.store.set_user(user, HASHER.hash(pw))
        log.info("admin password %s from ADMIN_PASSWORD", "updated" if h else "set")

    async def start(self) -> None:
        self.jobs.loop = asyncio.get_running_loop()
        logs.set_level(self.cfg.log_level)
        self.scheduler.start(self.cfg)
        await self.hub.apply(self.cfg)

    async def stop(self) -> None:
        self.scheduler.shutdown()
        await self.hub.stop()

    def _on_change(self, old: Config, new: Config) -> None:
        logs.set_level(new.log_level)
        self.scheduler.apply(new)
        self.jobs.spawn(self.hub.apply(new))

    # --- jobs -------------------------------------------------------------------------------------
    def config_export(self, include_secrets: bool = False) -> dict:
        return {"loxbridge": __version__, "config": dump_config(self.cfg, self.store.fernet, include_secrets)}

    def start_backup(self, ms_id: str) -> Job:
        if j := self.jobs.running("backup", ms_id):
            return j
        cfg = self.cfg
        ms = cfg.miniserver(ms_id)

        def work(job: Job) -> dict:
            try:
                return run_backup(cfg, ms_id, self.work, job.progress, self.config_export())
            except BackupBusy as e:
                return {"status": "failed", "error": str(e)}

        def done(job: Job) -> None:
            metrics.record_backup(ms_id, job.summary, job.finished or time.time())
            s = job.summary or {}
            ok = s.get("status") == "ok"
            text = (f"{ms.name}: {s.get('status')} - {s.get('files', 0)} files, "
                    f"{s.get('size', 0) / 2**20:.1f} MiB in {s.get('duration', 0)} s")
            if s.get("error"):
                text += f"\nerror: {s['error']}"
            for d, r in (s.get("destinations") or {}).items():
                if not r.get("ok"):
                    text += f"\n{d}: {r.get('error')}"
            for e in (s.get("errors") or [])[:10]:
                text += f"\n{e['path']}: {e['error']}"
            asyncio.get_running_loop().run_in_executor(
                None, notify, cfg.notifications, ok, f"LoxBridge backup {s.get('status')}: {ms.name}", text)
            if cfg.backup.auto_import_history and s.get("snapshot") and s.get("status") != "failed":
                self.start_history_import(ms_id, None, s["snapshot"], None, None, None, cfg.backup.history_sink,
                                          incremental=True)

        return self.jobs.start("backup", ms_id, f"Backup {ms.name}", work, done)

    def _scheduled_backup(self, ms_id: str) -> None:
        self.start_backup(ms_id)

    def _scheduled_gc(self) -> None:
        for d in self.cfg.destinations:
            if d.type == "s3" and d.enabled:
                self.start_gc(d.id)

    def store_for(self, dest_id: str):
        if dest_id == "work":
            return LocalStore("work", self.work / "staging")
        return make_store(self.cfg.destination(dest_id), self.work)

    def start_gc(self, dest_id: str) -> Job:
        store = self.store_for(dest_id)

        def work(job: Job) -> dict:
            out = {}
            for ms in store.miniservers():
                out[ms] = store.gc(ms, job.progress)
            return {"status": "ok", "miniservers": out}

        return self.jobs.start("gc", dest_id, f"GC {dest_id}", work)

    def start_history_import(self, ms_id: str, dest_id: str | None, snapshot: str, controls: set[str] | None,
                             start: int | None, end: int | None, sink_id: str | None, csv: bool = False,
                             incremental: bool = False) -> Job:
        cfg = self.cfg
        dest_id = dest_id or next((d.id for d in cfg.destinations if d.enabled and d.type == "local"), "work")
        store = self.store_for(dest_id)
        influx = cfg.sink(sink_id) if sink_id else None
        if influx is not None and not isinstance(influx, InfluxSink):
            raise ValueError("history import needs an InfluxDB sink")
        exports = self.work / "exports"
        exports.mkdir(exist_ok=True)

        def work(job: Job) -> dict:
            csv_path = exports / f"history_{ms_id}_{job.id}.csv" if csv else None
            r = run_import(
                store, ms_id, snapshot, self.work / "structure" / f"{ms_id}.json", self.tz, controls=controls,
                start=start, end=end, influx=influx, csv_path=csv_path,
                progress_get=(lambda f: self.store.history_progress(ms_id).get(f, 0)) if incremental else None,
                progress_set=(lambda f, t: self.store.set_history_progress(ms_id, f, t)) if incremental else None,
                progress=job.progress,
            )
            if csv_path:
                r["download"] = csv_path.name
            return r

        return self.jobs.start("history", ms_id, f"History import {ms_id}", work)


def create_app(config_dir: Path | None = None, work_dir: Path | None = None) -> FastAPI:
    from .web import routes_api, routes_ui
    from .web.common import AuthMiddleware

    logs.setup()
    svc = Services(config_dir or Path(os.environ.get("CONFIG_DIR", "/config")),
                   work_dir or Path(os.environ.get("WORK_DIR", "/work")))
    svc.bootstrap_admin()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        await svc.start()
        yield
        await svc.stop()

    app = FastAPI(title="LoxBridge", version=__version__, lifespan=lifespan, docs_url="/api/docs",
                  openapi_url="/api/openapi.json", redoc_url=None, root_path=os.environ.get("BASE_URL", "").rstrip("/"))
    app.state.svc = svc
    app.add_middleware(AuthMiddleware)
    app.add_middleware(SessionMiddleware, secret_key=svc.store.session_secret,
                        session_cookie="loxbridge_session", max_age=7 * 86400, same_site="lax", https_only=False)
    app.mount("/static", StaticFiles(directory=Path(__file__).parent / "web" / "static"), name="static")
    app.include_router(routes_api.router)
    app.include_router(routes_ui.router)
    return app
