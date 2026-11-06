"""Backup run: pull once into staging -> hash -> push to each destination -> retention -> report."""

import fcntl
import fnmatch
import hashlib
import json
import logging
import os
import time
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

from ..miniserver.http import MiniserverError, MiniserverHTTP
from ..settings.models import Config, LocalDestination, Miniserver, S3Destination
from .destinations.base import Destination, Progress
from .destinations.local import LocalStore
from .destinations.s3 import S3Store
from .manifest import CONFIG_PATH, sha256_file

log = logging.getLogger(__name__)


class BackupBusy(Exception):
    pass


def make_store(d: LocalDestination | S3Destination, work: Path) -> Destination:
    if d.type == "local":
        return LocalStore(d.id, Path(d.path))
    return S3Store(d, work)


def ms_client(ms: Miniserver, delay: float = 0.0) -> MiniserverHTTP:
    return MiniserverHTTP(ms.url, ms.backup_user, ms.backup_password.get_secret_value(), ms.verify_tls,
                          delay=delay, auth_mode=ms.auth_mode)


@contextmanager
def ms_lock(work: Path, ms_id: str):
    """One backup per Miniserver at a time, also across CLI and server processes."""
    work.mkdir(parents=True, exist_ok=True)
    with open(work / f".lock_{ms_id}", "w") as f:
        try:
            fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as e:
            raise BackupBusy(f"a backup of {ms_id} is already running") from e
        yield


def _safe(path: str) -> bool:
    parts = path.split("/")
    return bool(path) and not path.startswith("/") and ".." not in parts and "" not in parts


def run_backup(cfg: Config, ms_id: str, work: Path, progress: Progress = log.info,
               config_export: dict | None = None) -> dict:
    ms = cfg.miniserver(ms_id)
    with ms_lock(work, ms_id):
        return _run(cfg, ms, work, progress, config_export)


def _run(cfg: Config, ms: Miniserver, work: Path, progress: Progress, config_export: dict | None) -> dict:
    t0 = time.monotonic()
    snapshot = f"Backup_{ms.id}_{datetime.now().strftime('%Y%m%d%H%M%S')}"
    dests = [d for d in cfg.destinations if d.enabled]
    local_cfg = next((d for d in dests if d.type == "local"), None)
    # staging is the local snapshot if a local destination exists, otherwise /work (keeps one copy)
    staging_store = LocalStore(local_cfg.id if local_cfg else "work", Path(local_cfg.path) if local_cfg
                               else work / "staging")
    prev_name = next(iter(staging_store.names(ms.id)), None)
    prev = staging_store.manifest(ms.id, prev_name) if prev_name else None
    prev_dir = staging_store.snapshot_dir(ms.id, prev_name) if prev_name else None

    staging = staging_store.begin(ms.id, snapshot)
    result: dict = {"snapshot": snapshot, "miniserver": ms.id, "destinations": {}}
    files: dict[str, dict] = {}
    errors: list[dict] = []
    downloaded = reused = 0
    try:
        excludes = cfg.backup.excludes
        with ms_client(ms, cfg.backup.request_delay) as client:
            progress(f"listing {ms.name} ...")
            for path, entry in client.walk("/", lambda p: any(fnmatch.fnmatch(p, x) for x in excludes)):
                if not _safe(path):
                    errors.append({"path": path, "error": "unsafe path skipped"})
                    continue
                target = staging / path
                target.parent.mkdir(parents=True, exist_ok=True)
                old = (prev or {}).get("files", {}).get(path)
                if (old and prev_dir and not cfg.backup.full_download and old["size"] == entry.size
                        and old["stamp"] == entry.stamp and (prev_dir / path).is_file()):
                    os.link(prev_dir / path, target)
                    files[path] = old
                    reused += 1
                    continue
                try:
                    client.fsget(path, target)
                except MiniserverError as e:
                    target.unlink(missing_ok=True)
                    errors.append({"path": path, "error": str(e)})
                    log.warning("%s: %s", path, e)
                    continue
                files[path] = {"size": target.stat().st_size, "stamp": entry.stamp, "sha256": sha256_file(target)}
                downloaded += 1
                if downloaded % 25 == 0:
                    progress(f"downloaded {downloaded} files ({reused} unchanged)")
        if not files:
            raise MiniserverError("Miniserver returned no files (empty listing?)")
        if config_export is not None:
            cp = staging / CONFIG_PATH
            cp.parent.mkdir(parents=True, exist_ok=True)
            cp.write_text(json.dumps(config_export, indent=1))
            files[CONFIG_PATH] = {"size": cp.stat().st_size, "stamp": "", "sha256": sha256_file(cp)}
    except BaseException:
        staging_store.abort(staging)
        raise

    manifest = {
        "snapshot": snapshot,
        "miniserver": ms.id,
        "created": datetime.now().astimezone().isoformat(timespec="seconds"),
        "files": files,
        "errors": errors,
    }
    final = staging_store.commit(staging, manifest)
    size = sum(f["size"] for f in files.values())
    progress(f"pulled {len(files)} files ({downloaded} downloaded, {reused} unchanged), {len(errors)} errors")

    for d in dests:
        r: dict = {"ok": True}
        try:
            store = staging_store if d.type == "local" else make_store(d, work)
            if d.type != "local":
                progress(f"pushing to {d.name} ...")
                r.update(store.push(ms.id, final, manifest, progress))
            r["retention_deleted"] = store.retention(ms.id, d.keep)
        except Exception as e:  # noqa: BLE001 - one destination failing must not stop the others
            log.exception("destination %s failed", d.id)
            r = {"ok": False, "error": str(e) or type(e).__name__}
        result["destinations"][d.id] = r
    if not local_cfg:
        staging_store.retention(ms.id, 1)

    ok_dests = [r["ok"] for r in result["destinations"].values()]
    result.update(
        status="ok" if all(ok_dests) and not errors else ("failed" if not any(ok_dests) else "partial"),
        files=len(files), downloaded=downloaded, reused=reused, size=size, errors=errors,
        duration=round(time.monotonic() - t0, 1),
    )
    return result


def restore(store: Destination, ms: str, snapshot: str, target: Path, progress: Progress = log.info) -> int:
    """Copy a snapshot's files into target/ (verifying hashes). Returns file count."""
    m = store.manifest(ms, snapshot)
    target.mkdir(parents=True, exist_ok=True)
    for i, (p, f) in enumerate(m["files"].items(), 1):
        out = target / p
        out.parent.mkdir(parents=True, exist_ok=True)
        h = hashlib.sha256()
        with store.open(ms, snapshot, p, m) as src, open(out, "wb") as dst:
            while chunk := src.read(1 << 20):
                h.update(chunk)
                dst.write(chunk)
        if h.hexdigest() != f["sha256"]:
            raise OSError(f"hash mismatch while restoring {p}")
        if i % 50 == 0:
            progress(f"restored {i}/{len(m['files'])} files")
    return len(m["files"])
