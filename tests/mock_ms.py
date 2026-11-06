"""Fake Miniserver: HTTP (/dev/fslist, /dev/fsget, jdev) and WebSocket (token auth, structure,
binary value events) on one port. Basic auth admin:secret; HTTP 500 on log/broken.log.

  python tests/mock_ms.py --port 8081 [--extra-controls 50] [--interval 1.0]
"""

import argparse
import asyncio
import base64
import hashlib
import hmac
import json
import random
import secrets
import struct
import time
import urllib.parse
from dataclasses import dataclass, field
from http import HTTPStatus
from pathlib import Path

import websockets
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from websockets.asyncio.server import ServerConnection, serve
from websockets.datastructures import Headers
from websockets.http11 import Request, Response

from loxbridge.miniserver.auth import decrypt_command
from loxbridge.miniserver.statsfile import build_stats
from loxbridge.miniserver.ws import EPOCH_2009, lox_uuid_bytes

USER, PASSWORD = "admin", "secret"
FIXTURE = Path(__file__).parent / "fixtures" / "LoxAPP3.json"
TEMP_UUID = "30000000-0000-0001-ffff000000000000"


def default_files() -> dict[str, bytes]:
    rows = [(int(time.time()) - EPOCH_2009 - 3600 * (48 - i), [20.0 + i / 10]) for i in range(48)]
    return {
        "prog/sps_0001_20261001120000.zip": b"PK\x03\x04 fake project " * 50,
        "prog/sps_0002_20261002120000.zip": b"PK\x03\x04 another project " * 50,
        "log/def.log": b"log line\n" * 100,
        "log/broken.log": b"you will never read this",
        "web/LoxAPP3.json": FIXTURE.read_bytes(),
        f"stats/{TEMP_UUID}.{time.strftime('%Y%m')}": build_stats("Temperature", TEMP_UUID, rows),
        "sys/empty.bin": b"",
    }


@dataclass
class MockState:
    files: dict[str, bytes] = field(default_factory=default_files)
    stamps: dict[str, str] = field(default_factory=dict)
    structure: dict = field(default_factory=lambda: json.loads(FIXTURE.read_text()))
    interval: float = 1.0
    reads: dict[str, int] = field(default_factory=dict)  # fsget counter per path
    tokens: set[str] = field(default_factory=set)
    salt: str = field(default_factory=lambda: secrets.token_hex(8))
    http_key: str | None = None  # single-use getkey2 key for HTTP requests
    basic_allowed: bool = True
    key: rsa.RSAPrivateKey = field(default_factory=lambda: rsa.generate_private_key(public_exponent=65537,
                                                                                    key_size=2048))

    def add_controls(self, n: int) -> None:
        for i in range(n):
            u = f"40000000-0000-{i:04x}-ffff000000000000"
            self.structure["controls"][u] = {
                "name": f"Sensor {i}", "type": "InfoOnlyAnalog",
                "room": "10000000-0000-0001-ffff000000000000", "cat": "20000000-0000-0001-ffff000000000000",
                "states": {"value": u[:-1] + "1"},
            }

    def value_states(self) -> list[str]:
        out = []
        for c in self.structure["controls"].values():
            if c["type"] != "TextState":
                out += list(c["states"].values())
        return out

    def stamp(self, path: str) -> str:
        return self.stamps.get(path, "Oct 01 12:00")


def listing(state: MockState, d: str) -> str:
    d = d.strip("/")
    entries: dict[str, tuple[bool, int, str]] = {}
    for p, data in state.files.items():
        if d and not p.startswith(d + "/"):
            continue
        rest = p[len(d) + 1 :] if d else p
        name, _, more = rest.partition("/")
        entries[name] = (True, 0, "Jan 01 2026") if more else (False, len(data), state.stamp(p))
    return "".join(f"{'d' if isd else '-'} {size:>10} {stamp} {name}\n"
                   for name, (isd, size, stamp) in sorted(entries.items()))


def _resp(status: int, body: bytes | str, ctype: str = "text/plain") -> Response:
    b = body.encode() if isinstance(body, str) else body
    return Response(status, HTTPStatus(status).phrase, Headers([("Content-Type", ctype),
                                                                ("Content-Length", str(len(b)))]), b)


def ll(control: str, value, code: int = 200) -> str:
    return json.dumps({"LL": {"control": control, "value": value, "Code": str(code)}})


def make_handlers(state: MockState):
    def pw_hash(key: str) -> str:
        pw = hashlib.sha256(f"{PASSWORD}:{state.salt}".encode()).hexdigest().upper()
        return hmac.new(bytes.fromhex(key), f"{USER}:{pw}".encode(), hashlib.sha256).hexdigest()

    def take_key() -> str | None:
        k, state.http_key = state.http_key, None
        return k

    def authed(request: Request, query: dict) -> bool:
        if "autht" in query:
            key = take_key()
            want = {hmac.new(bytes.fromhex(key), t.encode(), hashlib.sha256).hexdigest() for t in state.tokens} \
                if key else set()
            return query.get("user", [""])[0] == USER and query["autht"][0] in want
        h = request.headers.get("Authorization", "")
        return state.basic_allowed and h == "Basic " + base64.b64encode(f"{USER}:{PASSWORD}".encode()).decode()

    def process_request(conn: ServerConnection, request: Request):
        url = urllib.parse.urlsplit(request.path)
        path = urllib.parse.unquote(url.path)
        query = urllib.parse.parse_qs(url.query)
        if path == "/ws/rfc6455":
            return None  # upgrade to WebSocket
        if path == "/jdev/sys/getPublicKey":
            pem = state.key.public_key().public_bytes(serialization.Encoding.PEM,
                                                      serialization.PublicFormat.SubjectPublicKeyInfo).decode()
            return _resp(200, ll("dev/sys/getPublicKey", pem.replace("PUBLIC KEY", "CERTIFICATE")))
        if path.startswith("/jdev/sys/getkey2/"):
            state.http_key = secrets.token_hex(20)
            return _resp(200, ll(path[1:], {"key": state.http_key, "salt": state.salt, "hashAlg": "SHA256"}))
        if path.startswith("/jdev/sys/getjwt/"):
            _, _, _, _, h, user = path.split("/")[:6]
            key = take_key()
            if user != USER or not key or h != pw_hash(key):
                return _resp(200, ll(path[1:], "", 401))  # LL code 401 inside HTTP 200, like the real one
            tok = secrets.token_urlsafe(24)
            state.tokens.add(tok)
            valid = int(time.time()) - EPOCH_2009 + 28 * 86400
            return _resp(200, ll(path[1:], {"token": tok, "validUntil": valid, "tokenRights": 4}))
        if not authed(request, query):
            return _resp(401, "Unauthorized")
        if path == "/jdev/cfg/version":
            return _resp(200, ll("dev/cfg/version", state.structure["msInfo"]["swVersion"]))
        if path.startswith("/dev/fslist"):
            return _resp(200, listing(state, path[len("/dev/fslist") :]))
        if path.startswith("/dev/fsget/"):
            p = path[len("/dev/fsget/") :]
            if p == "log/broken.log":
                return _resp(500, "Internal Server Error")
            if p not in state.files:
                return _resp(404, "Not Found")
            state.reads[p] = state.reads.get(p, 0) + 1
            return _resp(200, state.files[p], "application/octet-stream")
        return _resp(404, "Not Found")

    async def send(ws: ServerConnection, ident: int, payload: bytes | str | None) -> None:
        body = payload.encode() if isinstance(payload, str) else (payload or b"")
        await ws.send(struct.pack("<BBBBI", 3, ident, 0, 0, len(body)))
        if payload is not None:
            await ws.send(payload)

    async def handler(ws: ServerConnection) -> None:
        aes: tuple[bytes, bytes] | None = None
        key2 = secrets.token_hex(20)
        authed_user = None
        pusher = None
        try:
            async for msg in ws:
                assert isinstance(msg, str)
                cmd = msg
                if cmd.startswith("jdev/sys/enc/"):
                    assert aes, "keyexchange first"
                    cmd = decrypt_command(aes[0], aes[1], cmd[len("jdev/sys/enc/") :])
                parts = cmd.split("/")
                if cmd.startswith("jdev/sys/keyexchange/"):
                    blob = base64.b64decode(cmd[len("jdev/sys/keyexchange/") :])
                    raw = state.key.decrypt(blob, padding.PKCS1v15()).decode()
                    k, iv = raw.split(":")
                    aes = (bytes.fromhex(k), bytes.fromhex(iv))
                    await send(ws, 0, ll(cmd, "ok"))
                elif cmd.startswith("jdev/sys/getkey2/"):
                    await send(ws, 0, ll(cmd, {"key": key2, "salt": state.salt, "hashAlg": "SHA256"}))
                elif cmd.startswith("jdev/sys/getjwt/"):
                    assert msg.startswith("jdev/sys/enc/"), "getjwt must be encrypted"
                    _, _, _, h, user = parts[:5]
                    if user != USER or h != pw_hash(key2):
                        await send(ws, 0, ll(cmd, "", 401))
                        continue
                    tok = secrets.token_urlsafe(24)
                    state.tokens.add(tok)
                    authed_user = user
                    valid = int(time.time()) - EPOCH_2009 + 3600
                    await send(ws, 0, ll(cmd, {"token": tok, "key": key2, "validUntil": valid, "tokenRights": 4}))
                elif not authed_user and cmd != "keepalive":
                    await send(ws, 0, ll(cmd, "", 401))
                elif cmd == "jdev/sps/LoxAPPversion3":
                    await send(ws, 0, ll(cmd, state.structure["lastModified"]))
                elif cmd == "data/LoxAPP3.json":
                    await send(ws, 1, json.dumps(state.structure))
                elif cmd == "jdev/sps/enablebinstatusupdate":
                    await send(ws, 0, ll(cmd, "1"))
                    pusher = asyncio.create_task(push_events(ws))
                elif cmd == "keepalive":
                    await send(ws, 6, None)
                elif cmd == "jdev/sys/getkey":
                    await send(ws, 0, ll(cmd, key2))
                elif cmd.startswith("jdev/sys/refreshjwt/"):
                    await send(ws, 0, ll(cmd, {"validUntil": int(time.time()) - EPOCH_2009 + 3600}))
                else:
                    await send(ws, 0, ll(cmd, "", 404))
        except websockets.ConnectionClosed:
            pass
        finally:
            if pusher:
                pusher.cancel()

    async def push_events(ws: ServerConnection) -> None:
        values = state.value_states()
        text_uuid = "30000000-0000-0004-ffff000000000001"
        # initial table with all states, then random changes
        batch = values
        while True:
            payload = b"".join(lox_uuid_bytes(u) + struct.pack("<d", round(random.uniform(0, 30), 2))
                               for u in batch)
            await send(ws, 2, payload)
            txt = f"ok {time.strftime('%H:%M:%S')}".encode()
            tpayload = lox_uuid_bytes(text_uuid) + b"\0" * 16 + struct.pack("<I", len(txt)) + txt
            await send(ws, 3, tpayload + b"\0" * (-len(txt) % 4))
            await asyncio.sleep(state.interval)
            batch = random.sample(values, k=max(1, len(values) // 2))

    return process_request, handler


async def start(state: MockState, host: str = "127.0.0.1", port: int = 0):
    process_request, handler = make_handlers(state)
    server = await serve(handler, host, port, process_request=process_request,
                         subprotocols=["remotecontrol"])  # type: ignore[list-item]
    return server, server.sockets[0].getsockname()[1]


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="0.0.0.0")
    ap.add_argument("--port", type=int, default=8081)
    ap.add_argument("--extra-controls", type=int, default=0)
    ap.add_argument("--interval", type=float, default=1.0)
    a = ap.parse_args()
    state = MockState(interval=a.interval)
    state.add_controls(a.extra_controls)

    async def run() -> None:
        _, port = await start(state, a.host, a.port)
        print(f"mock Miniserver on {a.host}:{port} ({USER}:{PASSWORD})", flush=True)
        await asyncio.Future()

    asyncio.run(run())


if __name__ == "__main__":
    main()

