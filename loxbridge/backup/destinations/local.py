"""Local snapshots: <root>/<ms>/Backup_<ms>_<ts>/, built in .inprogress_* and renamed when complete.
Unchanged files are hard-linked to the previous snapshot."""

import json
import os
import shutil
import uuid
from pathlib import Path
from typing import BinaryIO

from ..manifest import MANIFEST, sha256_file, summary
from .base import Progress

PREFIX = "Backup_"
INPROGRESS = ".inprogress_"


class LocalStore:
    def __init__(self, id: str, root: Path):
        self.id = id
        self.root = root

    def _dir(self, ms: str) -> Path:
        return self.root / ms

    def snapshot_dir(self, ms: str, snapshot: str) -> Path:
        if "/" in snapshot or snapshot.startswith(".") or not snapshot.startswith(PREFIX):
            raise KeyError(snapshot)
        return self._dir(ms) / snapshot

    # --- staging (used by the runner) -------------------------------------------------------------
    def begin(self, ms: str, snapshot: str) -> Path:
        d = self._dir(ms)
        d.mkdir(parents=True, exist_ok=True)
        for stale in d.glob(INPROGRESS + "*"):  # leftovers of a killed run
            shutil.rmtree(stale, ignore_errors=True)
        staging = d / (INPROGRESS + snapshot)
        staging.mkdir()
        return staging

    def commit(self, staging: Path, manifest: dict) -> Path:
        (staging / MANIFEST).write_text(json.dumps(manifest, indent=1))
        final = staging.parent / manifest["snapshot"]
        os.replace(staging, final)
        return final

    def abort(self, staging: Path) -> None:
        shutil.rmtree(staging, ignore_errors=True)

    # --- Destination interface --------------------------------------------------------------------
    def push(self, ms: str, staging: Path, manifest: dict, progress: Progress) -> dict:
        return {}  # the runner stages directly into this store

    def miniservers(self):
        if self.root.exists():
            yield from sorted(p.name for p in self.root.iterdir() if p.is_dir() and not p.name.startswith("."))

    def names(self, ms: str) -> list[str]:
        d = self._dir(ms)
        if not d.exists():
            return []
        return sorted((p.name for p in d.iterdir() if p.name.startswith(PREFIX) and (p / MANIFEST).exists()),
                      reverse=True)

    def manifest(self, ms: str, snapshot: str) -> dict:
        return json.loads((self.snapshot_dir(ms, snapshot) / MANIFEST).read_text())

    def snapshots(self, ms: str) -> list[dict]:
        return [summary(self.manifest(ms, n)) for n in self.names(ms)]

    def open(self, ms: str, snapshot: str, path: str, manifest: dict) -> BinaryIO:
        if path not in manifest["files"]:
            raise KeyError(path)
        return open(self.snapshot_dir(ms, snapshot) / path, "rb")

    def delete(self, ms: str, snapshot: str) -> None:
        shutil.rmtree(self.snapshot_dir(ms, snapshot))

    def retention(self, ms: str, keep: int) -> list[str]:
        old = self.names(ms)[keep:]
        for n in old:
            self.delete(ms, n)
        return old

    def gc(self, ms: str, progress: Progress) -> dict:
        return {"deleted_blobs": 0}  # hard links free space by themselves

    def verify(self, ms: str, snapshot: str, deep: bool) -> list[str]:
        m = self.manifest(ms, snapshot)
        d = self.snapshot_dir(ms, snapshot)
        problems = []
        for p, f in m["files"].items():
            fp = d / p
            if not fp.is_file():
                problems.append(f"missing: {p}")
            elif fp.stat().st_size != f["size"]:
                problems.append(f"size mismatch: {p}")
            elif deep and sha256_file(fp) != f["sha256"]:
                problems.append(f"hash mismatch: {p}")
        return problems

    def test(self) -> str:
        self.root.mkdir(parents=True, exist_ok=True)
        probe = self.root / f".probe_{uuid.uuid4().hex}"
        probe.write_bytes(b"loxbridge")
        ok = probe.read_bytes() == b"loxbridge"
        probe.unlink()
        if not ok:
            raise OSError("probe read back differs")
        return f"ok, {shutil.disk_usage(self.root).free / 2**30:.1f} GiB free"
