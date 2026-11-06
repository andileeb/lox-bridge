"""Headless commands: serve, once, list, restore, verify, gc, check, reset-admin."""

import argparse
import getpass
import json
import os
import sys
from pathlib import Path

from . import logs


def drop_privileges(dirs: list[Path]) -> None:
    """Started as root (the image default): create the data folders, hand them to PUID:PGID (568 = TrueNAS apps)
    and continue as that user. Also covers `docker exec` commands, so they never leave root-owned files behind."""
    if os.getuid() != 0:
        return
    uid, gid = int(os.environ.get("PUID", "568")), int(os.environ.get("PGID", "568"))
    for d in dirs:
        d.mkdir(parents=True, exist_ok=True)
        st = d.stat()
        if (st.st_uid, st.st_gid) != (uid, gid):
            # ponytail: recursive only when the top folder is wrong (first start / data copied in as root)
            for root, subdirs, files in os.walk(d):
                for name in [root, *(os.path.join(root, x) for x in subdirs + files)]:
                    os.lchown(name, uid, gid)
    os.setgroups([])
    os.setgid(gid)
    os.setuid(uid)


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


def _dump(ms, out: Path) -> int:
    """Raw answers of the endpoints LoxBridge parses, to turn into test fixtures."""
    from .backup.runner import ms_client

    out.mkdir(parents=True, exist_ok=True)
    rc = 0
    with ms_client(ms) as c:
        for name, path in (("fslist_root.txt", "dev/fslist/"), ("fslist_prog.txt", "dev/fslist/prog/"),
                           ("fslist_stats.txt", "dev/fslist/stats/"), ("cfg_version.json", "jdev/cfg/version"),
                           ("cfg_api.json", "jdev/cfg/api")):
            try:
                (out / name).write_bytes(c.get(path))
                print(f"{ms.id}: saved {out / name}")
            except Exception as e:  # noqa: BLE001
                print(f"{ms.id}: ERR {path}: {e}")
                rc = 1
        stats = [e for e in c.fslist("stats") if not e.is_dir][:1] if (out / "fslist_stats.txt").exists() else []
        for e in stats:  # one statistics file, to check the binary format
            c.fsget(f"stats/{e.name}", out / e.name)
            print(f"{ms.id}: saved {out / e.name}")
    return rc


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
    p = sub.add_parser("check", help="test Miniserver and destination connections")
    p.add_argument("--dump", type=Path, help="save raw Miniserver answers here (for test fixtures)")
    p = sub.add_parser("reset-admin", help="set the admin user and password")
    p.add_argument("--user", default="admin")
    p.add_argument("--password")
    a = ap.parse_args(argv)
    logs.setup(os.environ.get("LOG_LEVEL", "INFO"))
    env = os.environ.get
    drop_privileges([Path(env("CONFIG_DIR", "/config")), _work(), Path(env("BACKUP_DIR", "/backups"))])

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
            if a.dump:
                rc |= _dump(ms, a.dump / ms.id)
        for d in cfg.destinations:
            try:
                print(f"{d.id}: ok  {make_store(d, _work()).test()}")
            except Exception as e:  # noqa: BLE001
                print(f"{d.id}: ERR {e}")
                rc = 1
        return rc
    return 2
