import io
import re
import time
import zipfile

import pytest
from fastapi.testclient import TestClient

from loxbridge.app import create_app


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("ADMIN_PASSWORD", "adminpass1")
    monkeypatch.delenv("SECRET_KEY", raising=False)
    monkeypatch.setenv("LOXBRIDGE_LOG_LEVEL", "DEBUG")  # env override -> locked setting
    app = create_app(tmp_path / "config", tmp_path / "work")
    with TestClient(app) as c:
        yield c


def csrf(c, path="/"):
    return re.search(r'name="csrf" value="([^"]+)"', c.get(path).text)[1]


def login(c):
    c.post("/login", data={"username": "admin", "password": "adminpass1", "csrf": csrf(c, "/login")})


def wait_job(c, job_id):
    for _ in range(300):
        j = c.get(f"/api/v1/jobs/{job_id}").json()
        if j["finished"]:
            return j
        time.sleep(0.1)
    raise AssertionError("job did not finish")


def test_auth_required(client):
    assert client.get("/api/v1/health").status_code == 200
    assert client.get("/api/v1/states").status_code == 401
    r = client.get("/", follow_redirects=False)
    assert r.status_code == 303 and "/login" in r.headers["location"]
    bad = client.post("/login", data={"username": "admin", "password": "nope", "csrf": csrf(client, "/login")})
    assert "Wrong user name" in bad.text
    assert client.post("/login", data={"username": "admin", "password": "x"}).status_code == 403  # no CSRF


def test_full_flow(client, mock_ms, tmp_path):
    url, _ = mock_ms
    login(client)
    t = csrf(client)
    r = client.post("/miniservers/new", data={
        "csrf": t, "id": "home", "name": "Home", "url": url, "backup_enabled": "on", "auth_mode": "basic",
        "backup_user": "admin", "backup_password": "secret", "bridge_user": "admin", "bridge_password": "secret",
    })
    assert "Saved Home" in r.text
    page = client.get("/miniservers/home").text
    assert 'value="secret"' not in page and "••• set" in page  # never shown again
    db = (tmp_path / "config" / "loxbridge.db").read_bytes()
    assert b'"backup_password": "enc:' in db and b'"secret"' not in db

    res = client.post("/miniservers/home/test", headers={"X-CSRF-Token": t}).text
    assert "login ok" in res and "token ok" in res and "504F94A0FFEE" in res

    client.post("/destinations/local", data={"csrf": t, "name": "Local", "enabled": "on", "keep": "3",
                                              "path": str(tmp_path / "backups")})
    job = client.post("/api/v1/miniservers/home/backup", headers={"X-CSRF-Token": t}).json()
    j = wait_job(client, job["id"])
    assert j["status"] == "partial", j

    snaps = client.get("/api/v1/snapshots").json()
    assert len(snaps) == 1 and snaps[0]["files"] > 3
    s = snaps[0]
    z = client.get(f"/api/v1/snapshots/{s['destination']}/home/{s['snapshot']}/download")
    names = zipfile.ZipFile(io.BytesIO(z.content)).namelist()
    assert f"{s['snapshot']}/log/def.log" in names and f"{s['snapshot']}/_loxbridge/config.json" in names
    assert client.get(f"/snapshots/{s['destination']}/home/{s['snapshot']}").status_code == 200
    assert "loxbridge_backup_last_success_timestamp{miniserver=\"home\"}" in client.get("/metrics").text

    cfgj = client.get("/api/v1/config").json()
    assert cfgj["miniservers"][0]["backup_password"] == "••• set"
    assert "Locked by an environment variable" not in client.get("/backup").text
    for page in ["/", "/backup", "/destinations", "/snapshots", "/bridge", "/history", "/notifications",
                 "/logs", "/settings", "/destinations/new?type=s3&preset=b2", "/sinks/new?type=mqtt"]:
        assert client.get(page).status_code == 200, page


def test_api_token(client):
    login(client)
    page = client.post("/settings/tokens", data={"csrf": csrf(client), "name": "grafana"}).text
    tok = re.search(r"<code>(lxb_[^<]+)</code>", page)[1]
    assert tok not in client.get("/settings").text  # shown once
    anon = TestClient(client.app)
    assert anon.get("/api/v1/states", headers={"Authorization": f"Bearer {tok}"}).status_code == 200
    assert anon.get("/api/v1/states", headers={"Authorization": "Bearer lxb_wrong"}).status_code == 401
