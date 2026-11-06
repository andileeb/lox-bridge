"""APScheduler wiring: backup per Miniserver, nightly S3 GC."""

import logging
from collections.abc import Callable
from datetime import datetime

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from ..settings.models import Config, cron_triggers

log = logging.getLogger(__name__)


class Scheduler:
    def __init__(self, tz: str | None, backup: Callable[[str], None], gc: Callable[[], None]):
        self.tz = tz
        self.backup, self.gc = backup, gc
        self.sched = AsyncIOScheduler(timezone=tz) if tz else AsyncIOScheduler()

    def start(self, cfg: Config) -> None:
        self.apply(cfg)
        self.sched.start()

    def shutdown(self) -> None:
        if self.sched.running:
            self.sched.shutdown(wait=False)

    def apply(self, cfg: Config) -> None:
        self.sched.remove_all_jobs()
        for ms in cfg.miniservers:
            if not ms.backup_enabled:
                continue
            for i, trig in enumerate(cron_triggers(cfg.backup.schedule, self.tz)):
                self.sched.add_job(self.backup, trig, args=[ms.id], id=f"backup:{ms.id}:{i}",
                                   misfire_grace_time=3600, coalesce=True, max_instances=1)
        if any(d.type == "s3" and d.enabled and d.mode == "dedup" for d in cfg.destinations):
            self.sched.add_job(self.gc, CronTrigger(hour=4, minute=45, timezone=self.tz), id="gc",
                               misfire_grace_time=3600, coalesce=True)

    def next_backup(self, ms_id: str) -> datetime | None:
        times = [j.next_run_time for j in self.sched.get_jobs()
                 if j.id.startswith(f"backup:{ms_id}:") and j.next_run_time]
        return min(times) if times else None
