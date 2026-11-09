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
    assert "Wrong password" in bad.text
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


def test_register_on_first_visit(tmp_path, monkeypatch):
    """No ADMIN_PASSWORD: the first visitor sets the password, after that /register is closed."""
    monkeypatch.delenv("ADMIN_PASSWORD", raising=False)
    monkeypatch.delenv("SECRET_KEY", raising=False)
    with TestClient(create_app(tmp_path / "config", tmp_path / "work")) as c:
        assert c.get("/", follow_redirects=False).headers["location"].endswith("/register")
        assert c.get("/login", follow_redirects=False).headers["location"].endswith("/register")
        t = csrf(c, "/register")
        assert "at least 8" in c.post("/register", data={"csrf": t, "password": "short", "repeat": "short"}).text
        r = c.post("/register", data={"csrf": t, "password": "firstpass1", "repeat": "firstpass1"},
                   follow_redirects=False)
        assert r.status_code == 303 and c.get("/").status_code == 200  # logged in right away
        c.post("/logout", data={"csrf": csrf(c)})
        r = c.post("/register", data={"csrf": csrf(c, "/login"), "password": "hijack123", "repeat": "hijack123"},
                   follow_redirects=False)
        assert r.headers["location"].endswith("/login")
        assert "Wrong password" in c.post("/login", data={"csrf": csrf(c, "/login"), "password": "hijack123"}).text
        r = c.post("/login", data={"csrf": csrf(c, "/login"), "password": "firstpass1"}, follow_redirects=False)
        assert r.status_code == 303


def test_setup_wizard(client, mock_ms, tmp_path):
    """Find the mock by address, log in, pick a schedule: one POST saves it and starts the first backup."""
    url, _ = mock_ms
    login(client)
    t = csrf(client, "/setup")
    client.post("/destinations/local", data={"csrf": t, "name": "Local", "enabled": "on", "keep": "3",
                                              "path": str(tmp_path / "backups")})
    hostport = url.removeprefix("https://")
    found = client.post("/setup/scan", data={"network": hostport}, headers={"X-CSRF-Token": t}).text
    assert "504F94A0FFEE" in found and "15.3.1.12" in found
    bad = client.post("/setup", data={"url": hostport, "user": "admin", "password": "wrong"},
                      headers={"X-CSRF-Token": t})
    assert "Could not log in" in bad.text and "HX-Redirect" not in bad.headers
    r = client.post("/setup", data={"url": hostport, "user": "admin", "password": "secret", "live": "on",
                                    "schedule": "0 3 * * 0"}, headers={"X-CSRF-Token": t})
    job = r.headers["HX-Redirect"].rsplit("/", 1)[1]
    assert wait_job(client, job)["status"] in ("ok", "partial")
    cfg = client.get("/api/v1/config").json()
    ms = cfg["miniservers"][0]
    assert (ms["name"], ms["url"], ms["bridge_enabled"]) == ("MockMS", url, True)
    assert cfg["backup"]["schedule"] == "0 3 * * 0"
    assert "MockMS" in client.get("/").text


def test_test_button_uses_unsaved_form(client, mock_ms):
    url, _ = mock_ms
    login(client)
    t = csrf(client, "/setup")
    client.post("/setup", data={"url": url, "user": "admin", "password": "secret"}, headers={"X-CSRF-Token": t})
    form = {"name": "x", "url": url, "backup_enabled": "on", "backup_user": "admin", "backup_password": "nope"}
    assert "✘" in client.post("/miniservers/ms1/test", data=form, headers={"X-CSRF-Token": t}).text
    form["backup_password"] = ""  # empty = the stored (correct) one
    assert "login ok" in client.post("/miniservers/ms1/test", data=form, headers={"X-CSRF-Token": t}).text


def test_activity_history(client, mock_ms, tmp_path):
    """Logins, settings changes (names only, never secrets) and backups end up in Settings → Activity."""
    url, _ = mock_ms
    client.post("/login", data={"password": "wrong-one", "csrf": csrf(client, "/login")})
    login(client)
    t = csrf(client, "/setup")
    client.post("/destinations/local", data={"csrf": t, "name": "Local", "enabled": "on", "keep": "3",
                                             "path": str(tmp_path / "b")})
    r = client.post("/setup", data={"csrf": t, "url": url, "user": "admin", "password": "secret",
                                    "schedule": "03:00"})
    job = r.headers["HX-Redirect"].rsplit("/", 1)[1]
    wait_job(client, job)
    for _ in range(50):  # the event is written right after the job reports finished
        page = client.get("/logs").text
        if "Backup MockMS" in page:
            break
        time.sleep(0.1)
    assert "Failed login" in page and "Login from" in page
    assert "Destination Local: keep, path" in page
    assert "Miniserver MockMS added" in page
    messages = " ".join(e["message"] for e in client.app.state.svc.store.events(limit=1000))
    assert "secret" not in messages  # the Loxone password is never part of a message
    assert f'/jobs/{job}"' in page and "Backup MockMS: partial" in page  # mock has one unreadable file
    backups = client.get("/logs?kind=backup").text
    assert "Backup MockMS" in backups and "Failed login" not in backups
