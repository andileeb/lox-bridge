import os

import pytest

from loxbridge.backup.destinations.local import LocalStore
from loxbridge.backup.runner import BackupBusy, ms_lock, restore, run_backup
from loxbridge.miniserver.http import MiniserverError
from loxbridge.settings.models import Config, LocalDestination, Miniserver


def cfg(url, tmp_path, password="secret", **kw):
    return Config(
        miniservers=[Miniserver(id="ms1", name="MS", url=url, backup_user="admin", backup_password=password, **kw)],
        destinations=[LocalDestination(id="local", path=str(tmp_path / "backups"), keep=2)],
    )


def test_backup_hardlinks_and_retention(mock_ms, tmp_path):
    url, state = mock_ms
    c = cfg(url, tmp_path)
    r1 = run_backup(c, "ms1", tmp_path / "work", config_export={"x": 1})
    assert r1["status"] == "partial"  # broken.log -> HTTP 500 is recorded, not fatal
    assert [e["path"] for e in r1["errors"]] == ["log/broken.log"]
    assert r1["downloaded"] == len(state.files) - 1
    store = LocalStore("local", tmp_path / "backups")
    snap1 = store.names("ms1")[0]
    assert "_loxbridge/config.json" in store.manifest("ms1", snap1)["files"]

    reads = dict(state.reads)
    state.stamps["log/def.log"] = "Oct 02 08:00"  # one changed file
    r2 = run_backup(c, "ms1", tmp_path / "work")
    assert r2["downloaded"] == 1 and r2["reused"] == len(state.files) - 2
    assert state.reads["log/def.log"] == reads["log/def.log"] + 1
    snap2 = store.names("ms1")[0]
    a = store.snapshot_dir("ms1", snap1) / "prog/sps_0001_20261001120000.zip"
    b = store.snapshot_dir("ms1", snap2) / "prog/sps_0001_20261001120000.zip"
    assert os.stat(a).st_ino == os.stat(b).st_ino

    run_backup(c, "ms1", tmp_path / "work")
    assert len(store.names("ms1")) == 2  # keep=2
    assert store.verify("ms1", store.names("ms1")[0], deep=True) == []

    n = restore(store, "ms1", snap2, tmp_path / "restore")
    assert n == len(state.files) - 1
    assert (tmp_path / "restore/log/def.log").read_bytes() == state.files["log/def.log"]


def test_wrong_password_leaves_nothing(mock_ms, tmp_path):
    url, _ = mock_ms
    with pytest.raises(MiniserverError, match="401"):
        run_backup(cfg(url, tmp_path, password="nope"), "ms1", tmp_path / "work")
    assert not any((tmp_path / "backups" / "ms1").iterdir())


def test_offline(tmp_path):
    with pytest.raises(MiniserverError, match="connection"):
        c = cfg("http://127.0.0.1:1", tmp_path)
        run_backup(c, "ms1", tmp_path / "work")


def test_lock(tmp_path):
    with ms_lock(tmp_path, "ms1"):
        with pytest.raises(BackupBusy):
            with ms_lock(tmp_path, "ms1"):
                pass


def test_token_auth_is_default_and_basic_fallback(mock_ms, tmp_path):
    from loxbridge.miniserver import auth

    url, state = mock_ms
    state.basic_allowed = False  # current firmware may refuse Basic auth
    auth.TOKENS.clear()
    r = run_backup(cfg(url, tmp_path), "ms1", tmp_path / "work")
    assert r["files"] > 3 and len(state.tokens) == 1
    run_backup(cfg(url, tmp_path), "ms1", tmp_path / "work")
    assert len(state.tokens) == 1  # token reused across runs
    state.tokens.clear()  # token revoked on the Miniserver -> re-acquired once
    assert run_backup(cfg(url, tmp_path), "ms1", tmp_path / "work")["files"] > 3
    with pytest.raises(MiniserverError, match="401"):
        run_backup(cfg(url, tmp_path, auth_mode="basic"), "ms1", tmp_path / "work")
    state.basic_allowed = True
    assert run_backup(cfg(url, tmp_path, auth_mode="basic"), "ms1", tmp_path / "work")["files"] > 3
