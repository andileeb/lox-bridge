"""Background jobs: every long action runs here; progress is polled by the SSE endpoints."""

import asyncio
import logging
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from .. import activity
from ..settings.store import Store

log = logging.getLogger(__name__)


@dataclass
class Job:
    id: str
    kind: str
    target: str
    title: str
    started: float = field(default_factory=time.time)
    finished: float | None = None
    status: str = "running"  # running | ok | partial | failed
    messages: list[str] = field(default_factory=list)
    summary: Any = None
    percent: float | None = None

    def progress(self, msg: str, percent: float | None = None) -> None:
        self.messages.append(f"{time.strftime('%H:%M:%S')} {msg}")
        del self.messages[:-500]
        if percent is not None:
            self.percent = percent

    def dict(self) -> dict:
        return {"id": self.id, "kind": self.kind, "target": self.target, "title": self.title,
                "started": self.started, "finished": self.finished, "status": self.status,
                "summary": self.summary, "percent": self.percent, "messages": self.messages[-50:]}


class JobManager:
    def __init__(self, store: Store):
        self.store = store
        self.jobs: dict[str, Job] = {}
        self.loop: asyncio.AbstractEventLoop | None = None  # set at startup; jobs may start from worker threads

    def start(self, kind: str, target: str, title: str, fn: Callable[[Job], Any],
              done: Callable[[Job], Any] | None = None) -> Job:
        """fn runs in a worker thread and returns the summary. Raising marks the job failed."""
        job = Job(uuid.uuid4().hex[:12], kind, target, title)
        self.jobs[job.id] = job
        for old in sorted(self.jobs.values(), key=lambda j: j.started)[:-50]:
            if old.finished:
                self.jobs.pop(old.id, None)
        self.spawn(self._run(job, fn, done))
        return job

    def spawn(self, coro) -> None:
        """Run a coroutine on the app loop, from the loop or from any thread."""
        assert self.loop
        try:
            if asyncio.get_running_loop() is self.loop:
                self.loop.create_task(coro)
                return
        except RuntimeError:
            pass
        asyncio.run_coroutine_threadsafe(coro, self.loop)

    async def _run(self, job: Job, fn: Callable[[Job], Any], done: Callable[[Job], Any] | None) -> None:
        job.progress(f"{job.title} started")
        try:
            job.summary = await asyncio.to_thread(fn, job)
            status = job.summary.get("status", "ok") if isinstance(job.summary, dict) else "ok"
            job.status = status
            job.progress(f"finished: {status}")
        except Exception as e:  # noqa: BLE001
            log.exception("job %s (%s) failed", job.kind, job.target)
            job.status = "failed"
            job.summary = {"status": "failed", "error": str(e) or type(e).__name__}
            job.progress(f"failed: {job.summary['error']}")
        job.finished = time.time()
        job.percent = 100
        self.store.record_job({**job.dict(), "summary": job.summary})
        activity.record("backup" if job.kind == "backup" else "job", _job_line(job),
                        {"ok": "info", "partial": "warn"}.get(job.status, "error"), f"/jobs/{job.id}")
        if done:
            try:
                r = done(job)
                if asyncio.iscoroutine(r):
                    await r
            except Exception:  # noqa: BLE001
                log.exception("post-job hook failed")

    def running(self, kind: str, target: str) -> Job | None:
        return next((j for j in self.jobs.values() if j.kind == kind and j.target == target and not j.finished),
                    None)

    def get(self, job_id: str) -> dict | None:
        if job_id in self.jobs:
            return self.jobs[job_id].dict()
        rows = [j for j in self.store.jobs() if j["id"] == job_id]
        return {**rows[0], "messages": [], "title": rows[0]["kind"]} if rows else None


def _job_line(job: Job) -> str:
    s = job.summary if isinstance(job.summary, dict) else {}
    line = f"{job.title}: {job.status}"
    if s.get("error"):
        return f"{line}, {s['error']}"
    if "files" in s:
        line += f", {s['files']} files ({s.get('downloaded', 0)} new"
        line += f", {s['skipped']} skipped)" if s.get("skipped") else ")"
        if s.get("errors"):
            line += f", {len(s['errors'])} error" + ("s" if len(s["errors"]) > 1 else "")
    bad = [d for d, r in (s.get("destinations") or {}).items() if not r.get("ok")]
    if bad:
        line += f", failed: {', '.join(bad)}"
    if "duration" in s:
        line += f", {s['duration']} s"
    return line
