"""Headless commands: serve, once, list, restore, verify, gc, check, reset-admin."""

import argparse
import getpass
import json
import os
import sys
from pathlib import Path

from . import logs


def _store():
    from .settings.store import Store

    return Store(Path(os.environ.get("CONFIG_DIR", "/config")))


def _work() -> Path:
    return Path(os.environ.get("WORK_DIR", "/work"))


def _dest(cfg, dest_id: str | None):
    from .backup.destinations.local import LocalStore
    from .backup.runner import make_store

    if dest_id == "work":
        return LocalStore("work", _work() / "staging")
    d = cfg.destination(dest_id) if dest_id else next(d for d in cfg.destinations if d.enabled)
    return make_store(d, _work())


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="loxbridge")
    sub = ap.add_subparsers(dest="cmd")
    sub.add_parser("serve", help="web UI + scheduler + bridge (default)")
    p = sub.add_parser("once", help="back up now")
    p.add_argument("miniserver", nargs="?")
    p = sub.add_parser("list", help="list snapshots")
    p.add_argument("--dest")
    p.add_argument("miniserver", nargs="?")
    p = sub.add_parser("restore", help="restore a snapshot into a folder")
    p.add_argument("miniserver")
    p.add_argument("snapshot")
    p.add_argument("target", type=Path)
    p.add_argument("--dest")
    p = sub.add_parser("verify", help="check a snapshot is complete")
    p.add_argument("miniserver")
    p.add_argument("snapshot")
    p.add_argument("--dest")
    p.add_argument("--deep", action="store_true", help="re-hash every file")
    p = sub.add_parser("gc", help="delete unreferenced S3 blobs")
    p.add_argument("--dest")
    sub.add_parser("check", help="test Miniserver and destination connections")
    p = sub.add_parser("reset-admin", help="set the admin user and password")
    p.add_argument("--user", default="admin")
    p.add_argument("--password")
    a = ap.parse_args(argv)
    logs.setup(os.environ.get("LOG_LEVEL", "INFO"))

    if a.cmd in (None, "serve"):
        import uvicorn

        from .app import create_app

        uvicorn.run(create_app(), host="0.0.0.0", port=int(os.environ.get("PORT", "8080")), log_config=None,
                    proxy_headers=True)
        return 0

    store = _store()
    cfg = store.config
    if a.cmd == "reset-admin":
        from .app import HASHER

        pw = a.password or getpass.getpass("New admin password: ")
        if len(pw) < 8:
            print("password must have at least 8 characters", file=sys.stderr)
            return 2
        store.set_user(a.user, HASHER.hash(pw))
        print(f"admin account '{a.user}' set")
        return 0

    if a.cmd == "once":
        from .backup.runner import run_backup
        from .settings.store import dump_config

        rc = 0
        for ms in cfg.miniservers:
            if a.miniserver and ms.id != a.miniserver:
                continue
            try:
                r = run_backup(cfg, ms.id, _work(), print,
                               {"config": dump_config(cfg, None, include_secrets=False)})
            except Exception as e:  # noqa: BLE001
                print(f"{ms.id}: FAILED: {e}", file=sys.stderr)
                rc = 1
                continue
            print(json.dumps({k: v for k, v in r.items() if k != "errors"}, indent=1))
            rc |= r["status"] != "ok"
        return rc

    if a.cmd == "list":
        store_ = _dest(cfg, a.dest)
        for ms in [a.miniserver] if a.miniserver else list(store_.miniservers()):
            for s in store_.snapshots(ms):
                print(f"{s['snapshot']}  {s['files']:6d} files  {s['size'] / 2**20:9.1f} MiB  {s['errors']} errors")
        return 0

    if a.cmd == "restore":
        from .backup.runner import restore

        n = restore(_dest(cfg, a.dest), a.miniserver, a.snapshot, a.target, print)
        print(f"restored {n} files to {a.target}")
        return 0

    if a.cmd == "verify":
        problems = _dest(cfg, a.dest).verify(a.miniserver, a.snapshot, a.deep)
        for p in problems:
            print(p)
        print("ok" if not problems else f"{len(problems)} problems")
        return 1 if problems else 0

    if a.cmd == "gc":
        s = _dest(cfg, a.dest)
        for ms in s.miniservers():
            print(ms, s.gc(ms, print))
        return 0

    if a.cmd == "check":
        from .backup.runner import make_store
        from .web.routes_ui import _test_miniserver

        rc = 0
        for ms in cfg.miniservers:
            for ok, msg in _test_miniserver(ms):
                print(f"{ms.id}: {'ok ' if ok else 'ERR'} {msg}")
                rc |= not ok
        for d in cfg.destinations:
            try:
                print(f"{d.id}: ok  {make_store(d, _work()).test()}")
            except Exception as e:  # noqa: BLE001
                print(f"{d.id}: ERR {e}")
                rc = 1
        return rc
    return 2
