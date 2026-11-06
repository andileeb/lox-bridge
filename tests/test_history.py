import csv
from zoneinfo import ZoneInfo

from test_bridge import Recorder, http_recorder

from loxbridge.backup.destinations.local import LocalStore
from loxbridge.backup.runner import run_backup
from loxbridge.bridge.history import run_import
from loxbridge.settings.models import Config, InfluxSink, LocalDestination, Miniserver


def test_import_csv_and_influx_incremental(mock_ms, tmp_path):
    url, _ = mock_ms
    cfg = Config(miniservers=[Miniserver(id="home", name="Home", url=url, backup_user="admin",
                                         backup_password="secret")],
                 destinations=[LocalDestination(id="local", path=str(tmp_path / "b"))])
    snap = run_backup(cfg, "home", tmp_path / "w")["snapshot"]
    store = LocalStore("local", tmp_path / "b")
    tz = ZoneInfo("Europe/Vienna")

    r = run_import(store, "home", snap, tmp_path / "none.json", tz, csv_path=tmp_path / "out.csv")
    assert r["status"] == "ok" and r["points"] == 48, r
    rows = list(csv.DictReader(open(tmp_path / "out.csv")))
    assert rows[0]["control"] == "Temperature" and rows[0]["state"] == "value" and rows[0]["room"] == "Living Room"

    srv, influx_url = http_recorder()
    influx = InfluxSink(id="i", url=influx_url, version="v1", bucket="lox")
    progress: dict[str, int] = {}
    kw = dict(influx=influx, progress_get=lambda f: progress.get(f, 0),
              progress_set=lambda f, t: progress.__setitem__(f, t))
    r1 = run_import(store, "home", snap, tmp_path / "none.json", tz, **kw)
    r2 = run_import(store, "home", snap, tmp_path / "none.json", tz, **kw)
    srv.shutdown()
    assert r1["points"] == 48 and r2["points"] == 0  # incremental: nothing new the second time
    body = b"".join(Recorder.bodies).decode()
    assert body.count("\n") == 47 and "state=value" in body and "uuid=30000000-0000-0001-ffff000000000001" in body
