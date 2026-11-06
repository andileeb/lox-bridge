"""Snapshot manifests: {path: {size, stamp, sha256}} plus metadata."""

import hashlib
from pathlib import Path

MANIFEST = "_manifest.json"
CONFIG_PATH = "_loxbridge/config.json"


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while chunk := f.read(1 << 20):
            h.update(chunk)
    return h.hexdigest()


def summary(m: dict) -> dict:
    files = m.get("files", {})
    return {
        "snapshot": m["snapshot"],
        "miniserver": m.get("miniserver"),
        "created": m.get("created"),
        "files": len(files),
        "size": sum(f["size"] for f in files.values()),
        "errors": len(m.get("errors", [])),
    }


def diff(a: dict, b: dict) -> dict[str, list[str]]:
    fa, fb = a.get("files", {}), b.get("files", {})
    return {
        "added": sorted(set(fb) - set(fa)),
        "removed": sorted(set(fa) - set(fb)),
        "changed": sorted(p for p in set(fa) & set(fb) if fa[p]["sha256"] != fb[p]["sha256"]),
    }
