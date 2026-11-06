"""Content-addressed S3 storage.

  <prefix>/<ms>/blobs/sha256/ab/cd/<hash>    immutable content, stored once
  <prefix>/<ms>/snapshots/<snapshot>.json    manifest, uploaded last
  <prefix>/<ms>/latest.json
  <prefix>/<ms>/archives/<snapshot>.tar.gz   (mode=archive only)
"""

import hashlib
import json
import logging
import tarfile
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import BinaryIO

import boto3
from botocore.config import Config as BotoConfig
from botocore.exceptions import ClientError

from ...settings.models import S3Destination
from ..manifest import summary
from .base import Progress

log = logging.getLogger(__name__)
GC_MIN_AGE = timedelta(hours=24)


class S3Store:
    def __init__(self, cfg: S3Destination, work: Path):
        self.id = cfg.id
        self.cfg = cfg
        self.work = work
        self.prefix = cfg.prefix.strip("/")
        self.s3 = boto3.client(
            "s3",
            endpoint_url=cfg.endpoint or None,
            region_name=cfg.region or None,
            aws_access_key_id=cfg.access_key or None,
            aws_secret_access_key=cfg.secret_key.get_secret_value() or None,
            config=BotoConfig(
                s3={"addressing_style": cfg.addressing},
                retries={"max_attempts": 5, "mode": "standard"},
                max_pool_connections=cfg.concurrency + 2,
            ),
        )
        self._manifests: dict[str, dict] = {}  # manifests are immutable: cache by key
        self._lock = threading.Lock()

    # --- keys -------------------------------------------------------------------------------------
    def _k(self, ms: str, *parts: str) -> str:
        return "/".join(p for p in (self.prefix, ms, *parts) if p)

    def blob_key(self, ms: str, sha: str) -> str:
        return self._k(ms, "blobs", "sha256", sha[:2], sha[2:4], sha)

    def _extra(self) -> dict:
        extra = {}
        if self.cfg.storage_class:
            extra["StorageClass"] = self.cfg.storage_class
        if self.cfg.sse:
            extra["ServerSideEncryption"] = self.cfg.sse
        return extra

    def _list(self, prefix: str):
        for page in self.s3.get_paginator("list_objects_v2").paginate(Bucket=self.cfg.bucket, Prefix=prefix):
            yield from page.get("Contents", [])

    # --- local index cache (no HEAD storm) --------------------------------------------------------
    def _index_file(self, ms: str) -> Path:
        return self.work / "s3index" / f"{self.id}_{ms}.txt"

    def _index(self, ms: str) -> set[str]:
        f = self._index_file(ms)
        if f.exists() and self._exists(self._k(ms, "latest.json")):
            return set(f.read_text().split())
        # cache missing or bucket was reset: rebuild from one listing
        return {o["Key"].rsplit("/", 1)[1] for o in self._list(self._k(ms, "blobs/"))}

    def _save_index(self, ms: str, idx: set[str]) -> None:
        f = self._index_file(ms)
        f.parent.mkdir(parents=True, exist_ok=True)
        tmp = f.with_suffix(".tmp")
        tmp.write_text("\n".join(sorted(idx)))
        tmp.replace(f)

    def _exists(self, key: str) -> bool:
        try:
            self.s3.head_object(Bucket=self.cfg.bucket, Key=key)
            return True
        except ClientError as e:
            if e.response["Error"]["Code"] in ("404", "NoSuchKey", "NotFound"):
                return False
            raise

    # --- Destination interface --------------------------------------------------------------------
    def push(self, ms: str, staging: Path, manifest: dict, progress: Progress) -> dict:
        if self.cfg.mode == "archive":
            return self._push_archive(ms, staging, manifest, progress)
        idx = self._index(ms)
        todo: dict[str, str] = {}
        for p, f in manifest["files"].items():
            if f["sha256"] not in idx:
                todo.setdefault(f["sha256"], p)
        done = 0
        uploaded = 0

        def up(item: tuple[str, str]) -> int:
            sha, p = item
            self.s3.upload_file(str(staging / p), self.cfg.bucket, self.blob_key(ms, sha), ExtraArgs=self._extra())
            return (staging / p).stat().st_size

        with ThreadPoolExecutor(self.cfg.concurrency) as pool:
            for size in pool.map(up, todo.items()):
                done += 1
                uploaded += size
                if done % 20 == 0:
                    progress(f"{self.id}: uploaded {done}/{len(todo)} blobs")
        idx |= set(todo)
        self._put_manifest(ms, manifest)
        self._save_index(ms, idx)
        return {"new_blobs": len(todo), "uploaded_bytes": uploaded}

    def _push_archive(self, ms: str, staging: Path, manifest: dict, progress: Progress) -> dict:
        tmp = self.work / f"{manifest['snapshot']}.tar.gz"
        tmp.parent.mkdir(parents=True, exist_ok=True)
        try:
            with tarfile.open(tmp, "w:gz") as tar:
                for p in manifest["files"]:
                    tar.add(staging / p, arcname=p)
            progress(f"{self.id}: uploading archive ({tmp.stat().st_size} bytes)")
            key = self._k(ms, "archives", f"{manifest['snapshot']}.tar.gz")
            self.s3.upload_file(str(tmp), self.cfg.bucket, key, ExtraArgs=self._extra())
            size = tmp.stat().st_size
        finally:
            tmp.unlink(missing_ok=True)
        self._put_manifest(ms, {**manifest, "archive": key})
        return {"uploaded_bytes": size}

    def _put_manifest(self, ms: str, manifest: dict) -> None:
        body = json.dumps(manifest).encode()
        snap = manifest["snapshot"]
        self.s3.put_object(Bucket=self.cfg.bucket, Key=self._k(ms, "snapshots", f"{snap}.json"), Body=body,
                           **self._put_extra())
        self.s3.put_object(Bucket=self.cfg.bucket, Key=self._k(ms, "latest.json"),
                           Body=json.dumps({"snapshot": snap}).encode(), **self._put_extra())

    def _put_extra(self) -> dict:
        return {k: v for k, v in self._extra().items() if k == "ServerSideEncryption"}

    def miniservers(self):
        r = self.s3.list_objects_v2(Bucket=self.cfg.bucket, Prefix=self.prefix + "/" if self.prefix else "",
                                    Delimiter="/")
        for cp in r.get("CommonPrefixes", []):
            yield cp["Prefix"].rstrip("/").rsplit("/", 1)[-1]

    def names(self, ms: str) -> list[str]:
        keys = [o["Key"] for o in self._list(self._k(ms, "snapshots/")) if o["Key"].endswith(".json")]
        return sorted((k.rsplit("/", 1)[1][:-5] for k in keys), reverse=True)

    def manifest(self, ms: str, snapshot: str) -> dict:
        key = self._k(ms, "snapshots", f"{snapshot}.json")
        with self._lock:
            if key in self._manifests:
                return self._manifests[key]
        try:
            m = json.loads(self.s3.get_object(Bucket=self.cfg.bucket, Key=key)["Body"].read())
        except ClientError as e:
            if e.response["Error"]["Code"] in ("NoSuchKey", "404"):
                raise KeyError(snapshot) from e
            raise
        with self._lock:
            self._manifests[key] = m
        return m

    def snapshots(self, ms: str) -> list[dict]:
        return [summary(self.manifest(ms, n)) for n in self.names(ms)]

    def _archive_path(self, ms: str, manifest: dict) -> Path:
        local = self.work / "s3cache" / self.id / ms / f"{manifest['snapshot']}.tar.gz"
        if not local.exists():
            local.parent.mkdir(parents=True, exist_ok=True)
            tmp = local.with_suffix(".part")
            self.s3.download_file(self.cfg.bucket, manifest["archive"], str(tmp))
            tmp.replace(local)
        return local

    def open(self, ms: str, snapshot: str, path: str, manifest: dict) -> BinaryIO:
        f = manifest["files"][path]
        if "archive" in manifest:
            tar = tarfile.open(self._archive_path(ms, manifest), "r:gz")
            member = tar.extractfile(path)
            if member is None:
                raise KeyError(path)
            return member  # type: ignore[return-value]
        return self.s3.get_object(Bucket=self.cfg.bucket, Key=self.blob_key(ms, f["sha256"]))["Body"]

    def delete(self, ms: str, snapshot: str) -> None:
        m = self.manifest(ms, snapshot)
        keys = [self._k(ms, "snapshots", f"{snapshot}.json")] + ([m["archive"]] if "archive" in m else [])
        self._delete_keys(keys)
        with self._lock:
            self._manifests.pop(keys[0], None)

    def _delete_keys(self, keys: list[str]) -> tuple[int, list[str]]:
        deleted, warnings = 0, []
        for i in range(0, len(keys), 1000):
            r = self.s3.delete_objects(
                Bucket=self.cfg.bucket, Delete={"Objects": [{"Key": k} for k in keys[i : i + 1000]], "Quiet": True}
            )
            errs = r.get("Errors", [])
            deleted += len(keys[i : i + 1000]) - len(errs)
            for e in errs:  # Object Lock / retention: warn, don't fail
                warnings.append(f"{e.get('Key')}: {e.get('Code')} {e.get('Message', '')}".strip())
        for w in warnings[:10]:
            log.warning("%s: delete blocked: %s", self.id, w)
        return deleted, warnings

    def retention(self, ms: str, keep: int) -> list[str]:
        old = self.names(ms)[keep:]
        for n in old:
            self.delete(ms, n)
        return old

    def gc(self, ms: str, progress: Progress) -> dict:
        referenced = set()
        for n in self.names(ms):
            referenced |= {f["sha256"] for f in self.manifest(ms, n)["files"].values()}
        cutoff = datetime.now(UTC) - GC_MIN_AGE
        idx = self._index(ms)
        garbage = []
        for o in self._list(self._k(ms, "blobs/")):
            sha = o["Key"].rsplit("/", 1)[1]
            if sha not in referenced and o["LastModified"] < cutoff:
                garbage.append(o["Key"])
        progress(f"{self.id}: GC found {len(garbage)} unreferenced blobs")
        deleted, warnings = self._delete_keys(garbage)
        blocked = {w.split(":", 1)[0] for w in warnings}
        idx -= {k.rsplit("/", 1)[1] for k in garbage if k not in blocked}
        self._save_index(ms, idx)
        return {"deleted_blobs": deleted, "warnings": warnings}

    def verify(self, ms: str, snapshot: str, deep: bool) -> list[str]:
        m = self.manifest(ms, snapshot)
        problems = []
        if "archive" in m:
            if not self._exists(m["archive"]):
                return [f"missing archive {m['archive']}"]
            if deep:
                with tarfile.open(self._archive_path(ms, m), "r:gz") as tar:
                    for p, f in m["files"].items():
                        member = tar.extractfile(p)
                        if member is None or hashlib.file_digest(member, "sha256").hexdigest() != f["sha256"]:  # type: ignore[arg-type]
                            problems.append(f"hash mismatch: {p}")
            return problems
        present = {o["Key"].rsplit("/", 1)[1] for o in self._list(self._k(ms, "blobs/"))}
        for p, f in m["files"].items():
            if f["sha256"] not in present:
                problems.append(f"missing blob for {p}")
            elif deep:
                body = self.open(ms, snapshot, p, m)
                if hashlib.file_digest(body, "sha256").hexdigest() != f["sha256"]:  # type: ignore[arg-type]
                    problems.append(f"hash mismatch: {p}")
        return problems

    def test(self) -> str:
        key = self._k("", f".probe_{uuid.uuid4().hex}")
        t = time.monotonic()
        self.s3.put_object(Bucket=self.cfg.bucket, Key=key, Body=b"loxbridge", **self._put_extra())
        data = self.s3.get_object(Bucket=self.cfg.bucket, Key=key)["Body"].read()
        self.s3.delete_object(Bucket=self.cfg.bucket, Key=key)
        if data != b"loxbridge":
            raise OSError("probe read back differs")
        return f"ok, write/read/delete in {time.monotonic() - t:.2f} s"

