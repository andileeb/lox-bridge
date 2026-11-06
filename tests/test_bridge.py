import asyncio
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer

from loxbridge import metrics
from loxbridge.bridge.hub import Hub
from loxbridge.bridge.sinks.base import Sink
from loxbridge.miniserver.structure import parse_structure
from loxbridge.settings.models import Config, InfluxSink, Miniserver, PrometheusSink, WebhookSink

TEMP = "30000000-0000-0001-ffff000000000001"
TEMP_CTL = "30000000-0000-0001-ffff000000000000"


class Recorder(BaseHTTPRequestHandler):
    bodies: list[bytes] = []

    def do_POST(self):  # noqa: N802
        n = int(self.headers.get("Content-Length", 0))
        Recorder.bodies.append(self.rfile.read(n))
        self.send_response(204)
        self.end_headers()

    def log_message(self, *a):
        pass


def http_recorder():
    Recorder.bodies = []
    srv = HTTPServer(("127.0.0.1", 0), Recorder)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, f"http://127.0.0.1:{srv.server_port}"


def test_live_values_reach_sinks(mock_ms, tmp_path):
    url, state = mock_ms
    srv, influx_url = http_recorder()
    all_controls = list(state.structure["controls"])
    cfg = Config(
        miniservers=[Miniserver(id="home", name="Home", url=url, bridge_enabled=True, bridge_user="admin",
                                bridge_password="secret")],
        sinks=[
            InfluxSink(id="influx", url=influx_url, version="v1", bucket="lox", selection=all_controls,
                       batch_seconds=0.2),
            PrometheusSink(id="prom", selection=[TEMP_CTL], batch_seconds=0.2),
            InfluxSink(id="dead", url="http://127.0.0.1:1", version="v1", selection=all_controls,
                       batch_seconds=0.1, queue_size=100),
        ],
    )

    async def go():
        hub = Hub(tmp_path)
        await hub.apply(cfg)
        t0 = time.monotonic()
        while time.monotonic() - t0 < 5 and not any(b"Temperature" in b for b in Recorder.bodies):
            await asyncio.sleep(0.05)
        latency = time.monotonic() - t0
        await asyncio.sleep(0.5)
        status = dict(hub.status["home"])
        states = hub.states("home")
        dead_err = hub.sinks["dead"].error
        await hub.stop()
        return latency, status, states, dead_err

    latency, status, states, dead_err = asyncio.run(go())
    srv.shutdown()
    assert status["connected"], status
    assert latency < 2.0, latency  # includes connect + token auth + structure download
    body = b"\n".join(Recorder.bodies).decode()
    assert "loxone,miniserver=home,room=Living\\ Room,category=Climate,control=Temperature,state=value" in body
    assert "text=" not in body  # text states off for TSDB sinks by default
    assert any(s["uuid"] == TEMP and s["value"] is not None for s in states)
    assert any(s["state"] == "textAndIcon" and s["text"] for s in states)  # cached for API/MQTT
    assert dead_err  # dead sink failed ...
    gauge = metrics.REGISTRY.get_sample_value("loxbridge_ws_connected", {"miniserver": "home"})
    assert gauge == 0  # reset on stop
    assert (tmp_path / "structure" / "home.json").exists()


class FakeSink(Sink):
    def __init__(self, cfg, fail=False):
        super().__init__(cfg)
        self.got = []
        self.fail = fail

    async def write(self, batch):
        if self.fail:
            raise RuntimeError("down")
        self.got += batch


def test_deadband_heartbeat_and_overflow():
    st = parse_structure(__import__("json").loads(
        (__import__("pathlib").Path(__file__).parent / "fixtures" / "LoxAPP3.json").read_text()))

    async def go():
        hub = Hub(__import__("pathlib").Path("/nonexistent"))
        hub.structures["ms"] = st
        good = FakeSink(WebhookSink(id="good", selection=[TEMP_CTL], deadband=0.5, heartbeat=1, batch_seconds=0.1))
        bad = FakeSink(WebhookSink(id="bad", selection=[TEMP_CTL], batch_seconds=0.1, queue_size=100), fail=True)
        for s in (good, bad):
            hub.sinks[s.id] = s
            hub._sent[s.id] = {}
            s.start()
        hub._tick_task = asyncio.create_task(hub._tick())
        for v in (20.0, 20.2, 20.6, 20.7, 22.0):
            hub.on_values("ms", [(TEMP, v)])
        hub.on_values("ms", [("30000000-0000-0002-ffff000000000001", 1.0)])  # not selected
        await asyncio.sleep(0.4)
        first = [p.value for p in good.got]
        await asyncio.sleep(1.5)  # heartbeat re-sends the current value
        for i in range(300):
            hub.on_values("ms", [(TEMP, float(i))])
        await asyncio.sleep(0.2)
        dropped = metrics.REGISTRY.get_sample_value("loxbridge_sink_dropped_total", {"sink": "bad"})
        await hub.stop()
        return first, [p.value for p in good.got], dropped, len(bad.queue)

    first, later, dropped, bad_q = asyncio.run(go())
    assert first == [20.0, 20.6, 22.0]  # 20.2 and 20.7 are inside the deadband
    assert 22.0 in later[3:4]  # heartbeat
    assert dropped and dropped > 0 and bad_q <= 100  # slow sink drops oldest, never blocks the others
    assert len(later) > 100
