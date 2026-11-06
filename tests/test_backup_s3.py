from datetime import timedelta

import boto3
import pytest
from moto import mock_aws

from loxbridge.backup import runner
from loxbridge.backup.destinations import s3 as s3mod
from loxbridge.backup.destinations.local import LocalStore
from loxbridge.backup.runner import make_store, restore, run_backup
from loxbridge.settings.models import Config, LocalDestination, Miniserver, S3Destination


@pytest.fixture
def bucket():
    with mock_aws():
        boto3.client("s3", region_name="us-east-1").create_bucket(Bucket="backups")
        yield


def cfg(url, tmp_path, mode="dedup", local=True, keep=5):
    dests = [S3Destination(id="s3", bucket="backups", access_key="a", secret_key="b", mode=mode, keep=keep)]
    if local:
        dests.insert(0, LocalDestination(id="local", path=str(tmp_path / "backups")))
    return Config(miniservers=[Miniserver(id="ms1", name="MS", url=url, backup_user="admin",
                                          backup_password="secret")], destinations=dests)


def test_dedup_flow(mock_ms, tmp_path, bucket, monkeypatch):
    url, state = mock_ms
    c = cfg(url, tmp_path, keep=1)
    work = tmp_path / "work"
    r1 = run_backup(c, "ms1", work)
    assert r1["destinations"]["s3"]["ok"] and r1["destinations"]["s3"]["new_blobs"] > 0
    r2 = run_backup(c, "ms1", work)
    assert r2["destinations"]["s3"]["new_blobs"] == 0  # unchanged run uploads nothing

    s3 = make_store(c.destinations[1], work)
    snap = s3.names("ms1")[0]
    assert len(s3.names("ms1")) == 1  # keep=1
    restore(s3, "ms1", snap, tmp_path / "r")
    local = LocalStore("local", tmp_path / "backups")
    for p in local.manifest("ms1", snap)["files"]:
        assert (tmp_path / "r" / p).read_bytes() == (local.snapshot_dir("ms1", snap) / p).read_bytes()
    assert s3.verify("ms1", snap, deep=True) == []

    # a changed file makes its old blob garbage; shared blobs survive GC
    state.files["log/def.log"] = b"new content"
    state.stamps["log/def.log"] = "Oct 03 10:00"
    run_backup(c, "ms1", work)
    monkeypatch.setattr(s3mod, "GC_MIN_AGE", timedelta(0))
    g = s3.gc("ms1", print)
    assert g["deleted_blobs"] == 1
    assert s3.verify("ms1", s3.names("ms1")[0], deep=True) == []


def test_interrupted_run_invisible(mock_ms, tmp_path, bucket, monkeypatch):
    url, _ = mock_ms
    c = cfg(url, tmp_path)
    calls = {"n": 0}
    real = s3mod.S3Store.push

    def boom(self, *a, **kw):
        orig = self.s3.upload_file

        def flaky(*x, **y):
            calls["n"] += 1
            if calls["n"] == 3:
                raise RuntimeError("killed")
            return orig(*x, **y)

        self.s3.upload_file = flaky
        return real(self, *a, **kw)

    monkeypatch.setattr(s3mod.S3Store, "push", boom)
    r = run_backup(c, "ms1", tmp_path / "work")
    assert not r["destinations"]["s3"]["ok"] and r["destinations"]["local"]["ok"]
    assert make_store(c.destinations[1], tmp_path / "work").names("ms1") == []


def test_archive_mode_without_local(mock_ms, tmp_path, bucket):
    url, state = mock_ms
    c = cfg(url, tmp_path, mode="archive", local=False)
    r = run_backup(c, "ms1", tmp_path / "work")
    assert r["destinations"]["s3"]["ok"]
    s3 = make_store(c.destinations[0], tmp_path / "work")
    snap = s3.names("ms1")[0]
    assert s3.verify("ms1", snap, deep=True) == []
    restore(s3, "ms1", snap, tmp_path / "r")
    assert (tmp_path / "r/log/def.log").read_bytes() == state.files["log/def.log"]
    # second run still reads only changed files thanks to the /work staging copy
    reads = sum(state.reads.values())
    run_backup(c, "ms1", tmp_path / "work")
    assert sum(state.reads.values()) == reads


def test_s3_down_local_ok(mock_ms, tmp_path):
    url, _ = mock_ms
    c = cfg(url, tmp_path)
    c.destinations[1].endpoint = "http://127.0.0.1:1"
    r = runner.run_backup(c, "ms1", tmp_path / "work")
    assert r["destinations"]["local"]["ok"] and not r["destinations"]["s3"]["ok"]
    assert r["status"] == "partial"


def test_two_s3_targets_nas_and_cloud(mock_ms, tmp_path, bucket):
    url, _ = mock_ms
    boto3.client("s3", region_name="us-east-1").create_bucket(Bucket="cloud")
    c = Config(miniservers=[Miniserver(id="ms1", name="MS", url=url, backup_user="admin", backup_password="secret")],
               destinations=[S3Destination(id="nas", bucket="backups", access_key="a", secret_key="b", keep=3),
                             S3Destination(id="cloud", bucket="cloud", access_key="a", secret_key="b", mode="archive",
                                           keep=1)])
    work = tmp_path / "work"
    r1 = run_backup(c, "ms1", work)
    r2 = run_backup(c, "ms1", work)
    assert r1["destinations"]["nas"]["new_blobs"] > 0 and r2["destinations"]["nas"]["new_blobs"] == 0
    assert r2["destinations"]["cloud"]["ok"]
    nas, cloud = (make_store(d, work) for d in c.destinations)
    assert len(nas.names("ms1")) == 2 and len(cloud.names("ms1")) == 1
    assert (work / "s3index" / "nas_ms1.txt").exists() and not (work / "s3index" / "cloud_ms1.txt").exists()
