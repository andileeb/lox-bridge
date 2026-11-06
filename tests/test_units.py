import json
import struct
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from pydantic import ValidationError

from loxbridge.backup.manifest import diff
from loxbridge.bridge.history import output_states
from loxbridge.bridge.sinks.base import Point
from loxbridge.bridge.sinks.influx import line
from loxbridge.bridge.sinks.mqtt import discovery, slug, topic
from loxbridge.bridge.sinks.webhook import sign
from loxbridge.miniserver import auth
from loxbridge.miniserver.listing import parse_listing
from loxbridge.miniserver.statsfile import UnsupportedStatsFile, build_stats, parse_stats
from loxbridge.miniserver.structure import parse_structure
from loxbridge.miniserver.ws import EPOCH_2009, decode_texts, decode_values, lox_uuid, lox_uuid_bytes
from loxbridge.settings.models import Config, MqttSink, cron_triggers
from loxbridge.settings.store import Store

FIXTURE = json.loads((Path(__file__).parent / "fixtures" / "LoxAPP3.json").read_text())


def test_listing_formats():
    text = (
        "d          0 Jan 01 2026 log\n"
        "-      12345 Mar  4 10:22 sps 0001.zip\n"
        "-rw-r--r-- 1 root root 77 Oct 1 2025 ls style.txt\n"
        "drwxr-xr-x 2 root root 0 Oct 1 12:00 stats\n"
        "garbage line\n"
        "d 0 Jan 01 2026 .\n"
    )
    e = parse_listing(text)
    assert [(x.name, x.is_dir, x.size) for x in e] == [
        ("log", True, 0), ("sps 0001.zip", False, 12345), ("ls style.txt", False, 77), ("stats", True, 0)]
    assert e[1].stamp == "Mar 4 10:22"


def test_manifest_diff():
    a = {"files": {"x": {"sha256": "1"}, "y": {"sha256": "2"}}}
    b = {"files": {"y": {"sha256": "3"}, "z": {"sha256": "4"}}}
    assert diff(a, b) == {"added": ["z"], "removed": ["x"], "changed": ["y"]}


def test_schedule():
    t = cron_triggers("03:00, 15:30", "UTC")
    now = datetime(2026, 1, 1, 10, 0, tzinfo=ZoneInfo("UTC"))
    assert min(x.get_next_fire_time(None, now) for x in t) == datetime(2026, 1, 1, 15, 30, tzinfo=ZoneInfo("UTC"))
    [c] = cron_triggers("0 4 * * 1", "UTC")  # Mondays 04:00
    assert c.get_next_fire_time(None, now).weekday() == 0
    with pytest.raises(ValidationError):
        Config.model_validate({"backup": {"schedule": "25:99 nope"}})


def test_settings_validation_and_secrets(tmp_path, monkeypatch):
    monkeypatch.delenv("SECRET_KEY", raising=False)
    with pytest.raises(ValidationError):
        Config.model_validate({"miniservers": [{"id": "Bad Id", "name": "x", "url": "https://a"}]})
    with pytest.raises(ValidationError):
        Config.model_validate({"miniservers": [{"id": "a", "name": "x", "url": "ftp://a"}]})
    with pytest.raises(ValidationError, match="duplicate"):
        Config.model_validate({"sinks": [{"type": "mqtt", "id": "a"}, {"type": "influx", "id": "a"}]})

    s = Store(tmp_path)
    assert s.generated_key and oct((tmp_path / "secret.key").stat().st_mode)[-3:] == "600"
    cfg = Config.model_validate({"miniservers": [{"id": "ms", "name": "x", "url": "https://a",
                                                  "backup_password": "pw1"}]})
    s.save(cfg)
    raw = (tmp_path / "loxbridge.db").read_bytes()
    assert b"pw1" not in raw
    s2 = Store(tmp_path)
    assert not s2.generated_key
    assert s2.config.miniservers[0].backup_password.get_secret_value() == "pw1"


def test_env_override_locks(tmp_path, monkeypatch):
    monkeypatch.setenv("LOXBRIDGE_BACKUP__SCHEDULE", "05:15")
    s = Store(tmp_path)
    assert s.config.backup.schedule == "05:15" and "backup.schedule" in s.locked
    cfg = s.config.model_copy(deep=True)
    cfg.backup.excludes = ["log/*"]
    s.save(cfg)
    monkeypatch.delenv("LOXBRIDGE_BACKUP__SCHEDULE")
    s2 = Store(tmp_path)
    assert s2.config.backup.schedule == "03:00"  # env value was never persisted
    assert s2.config.backup.excludes == ["log/*"]


def test_structure_mapping():
    st = parse_structure(FIXTURE)
    i = st.states["30000000-0000-0001-ffff000000000001"]
    assert (i.control, i.state, i.room, i.category, i.type, i.unit) == (
        "Temperature", "value", "Living Room", "Climate", "InfoOnlyAnalog", "°C")
    assert st.states["30000000-0000-0003-ffff000000000002"].state == "total"
    assert st.ms_info["serialNr"] == "504F94A0FFEE"


def test_binary_events():
    u = "30000000-0000-0001-ffff000000000001"
    assert lox_uuid(lox_uuid_bytes(u)) == u
    payload = lox_uuid_bytes(u) + struct.pack("<d", 21.5) + lox_uuid_bytes(u) + struct.pack("<d", -1.0)
    assert decode_values(payload) == [(u, 21.5), (u, -1.0)]
    t1, t2 = b"hello", b"wxyz"
    tp = (lox_uuid_bytes(u) + b"\0" * 16 + struct.pack("<I", 5) + t1 + b"\0" * 3
          + lox_uuid_bytes(u) + b"\0" * 16 + struct.pack("<I", 4) + t2)
    assert decode_texts(tp) == [(u, "hello"), (u, "wxyz")]


def test_token_hash_and_encryption():
    h = auth.password_hash("admin", "secret", "41424344", "salt", "SHA256")
    assert len(h) == 64
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import padding, rsa

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = key.public_key().public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo)
    sess = auth.Session(auth.load_public_key(pem.decode().replace("PUBLIC KEY", "CERTIFICATE")))
    import base64
    k, iv = key.decrypt(base64.b64decode(sess.session_key), padding.PKCS1v15()).decode().split(":")
    enc = sess.encrypt_command("jdev/sys/getjwt/a/b")
    plain = auth.decrypt_command(bytes.fromhex(k), bytes.fromhex(iv), enc[len("jdev/sys/enc/"):])
    assert plain == "jdev/sys/getjwt/a/b"


def test_stats_parser():
    tz = ZoneInfo("Europe/Vienna")
    u = "30000000-0000-0003-ffff000000000000"
    data = build_stats("Power Meter", u, [(100, [1.5, 2.5]), (200, [3.0, 4.0])])
    sf = parse_stats(data, tz)
    assert sf.name == "Power Meter" and sf.value_count == 2
    assert sf.rows[0][1] == [1.5, 2.5]
    assert sf.rows[1][0] - sf.rows[0][0] == 100
    # Loxone epoch is local time: 2009-01-01 00:01:40 Vienna == 1230764500 UTC
    assert sf.rows[0][0] == EPOCH_2009 - 3600 + 100
    with pytest.raises(UnsupportedStatsFile):
        parse_stats(data[:-3], tz)
    with pytest.raises(UnsupportedStatsFile):
        parse_stats(b"\xff" * 40, tz)


def test_output_states_mapping():
    st = parse_structure(FIXTURE)
    meter = st.controls["30000000-0000-0003-ffff000000000000"]
    assert output_states(meter, 2) == ["total", "actual"]
    temp = st.controls["30000000-0000-0001-ffff000000000000"]
    assert output_states(temp, 1) == ["value"]


def test_line_protocol_escaping():
    tags = {"room": "Living Room", "control": "a,b=c", "empty": ""}
    assert line("loxone", tags, 21.5, None, 1000) == r"loxone,room=Living\ Room,control=a\,b\=c value=21.5 1000"
    assert line("lox one", {}, None, 'say "hi"', 5) == r'lox\ one text="say \"hi\"" 5'
    assert line("loxone", {}, float("nan"), None, 5) == ""


def test_mqtt_topics_and_discovery():
    st = parse_structure(FIXTURE)
    i = st.states["30000000-0000-0001-ffff000000000001"]
    assert slug("Küche / Licht 1") == "kueche_licht_1"
    assert topic("loxbridge", "home", i) == "loxbridge/home/living_room/temperature/value"
    t, conf = discovery(MqttSink(id="m", ha_discovery=True), "home", "Home", i)
    assert t == "homeassistant/sensor/loxbridge_home_3000000000000001ffff000000000001/config"
    assert conf["device_class"] == "temperature" and conf["unit_of_measurement"] == "°C"
    sw = st.states["30000000-0000-0002-ffff000000000001"]
    t, conf = discovery(MqttSink(id="m"), "home", "Home", sw)
    assert t.startswith("homeassistant/binary_sensor/") and "ON" in conf["value_template"]


def test_webhook_signature():
    assert sign("k", b"body") == "sha256=" + __import__("hmac").new(b"k", b"body", "sha256").hexdigest()


def test_point_is_hashable():
    st = parse_structure(FIXTURE)
    Point("ms", next(iter(st.states.values())), 1.0, None, 0.0)


def test_cron_weekdays_like_standard_cron():
    now = datetime(2026, 1, 1, 10, 0, tzinfo=ZoneInfo("UTC"))  # a Thursday
    [c] = cron_triggers("30 2 * * 1-5", "UTC")
    assert c.get_next_fire_time(None, now).weekday() == 4  # Friday
    [c] = cron_triggers("0 3 * * 0", "UTC")
    assert c.get_next_fire_time(None, now).weekday() == 6  # Sunday
    [c] = cron_triggers("0 3 * * */2", "UTC")
    assert c.get_next_fire_time(None, now)


def test_gen2_https_only():
    with pytest.raises(ValidationError):
        Config.model_validate({"miniservers": [{"id": "a", "name": "x", "url": "http://192.168.1.77"}]})
    Config.model_validate({"miniservers": [{"id": "a", "name": "x", "url": "https://192.168.1.77:443"}]})
