"""Read-only WebSocket client: token auth, structure file, binary state updates."""

import asyncio
import json
import logging
import ssl
import struct
import time
from collections.abc import Awaitable, Callable
from pathlib import Path

import httpx
import websockets
from websockets.asyncio.client import ClientConnection, connect

from . import auth
from .http import MiniserverError, explain
from .structure import Structure, parse_structure

log = logging.getLogger(__name__)

EPOCH_2009 = auth.EPOCH_2009
KEEPALIVE_S = 60

# message header identifiers
TEXT, BINFILE, VALUES, TEXTS, DAYTIMER, OOS, KEEPALIVE, WEATHER = range(8)


def lox_uuid(b: bytes) -> str:
    d1, d2, d3 = struct.unpack_from("<IHH", b)
    return f"{d1:08x}-{d2:04x}-{d3:04x}-{b[8:16].hex()}"


def lox_uuid_bytes(u: str) -> bytes:
    p = u.split("-")
    return struct.pack("<IHH", int(p[0], 16), int(p[1], 16), int(p[2], 16)) + bytes.fromhex(p[3])


def decode_values(payload: bytes) -> list[tuple[str, float]]:
    return [
        (lox_uuid(payload[i : i + 16]), struct.unpack_from("<d", payload, i + 16)[0])
        for i in range(0, len(payload) - len(payload) % 24, 24)
    ]


def decode_texts(payload: bytes) -> list[tuple[str, str]]:
    out, i = [], 0
    while i + 36 <= len(payload):
        uid = lox_uuid(payload[i : i + 16])
        (n,) = struct.unpack_from("<I", payload, i + 32)
        text = payload[i + 36 : i + 36 + n].decode(errors="replace")
        out.append((uid, text))
        i += 36 + n + (-n % 4)
    return out


def ws_url(url: str) -> str:
    return url.rstrip("/").replace("https://", "wss://", 1).replace("http://", "ws://", 1) + "/ws/rfc6455"


def _ssl(url: str, verify: bool):
    if not url.startswith("https"):
        return None
    ctx = ssl.create_default_context()
    if not verify:
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
    return ctx


def _ll(text: str) -> dict:
    ll = json.loads(text).get("LL", {})
    code = int(str(ll.get("Code", ll.get("code", "200"))))
    if code != 200:
        raise MiniserverError(explain(code) + f" ({ll.get('control', '')})")
    return ll


class Conn:
    """One authenticated WebSocket session. A single reader task owns recv()."""

    def __init__(self, ws: ClientConnection):
        self.ws = ws
        self.responses: asyncio.Queue[str] = asyncio.Queue()
        self.session: auth.Session | None = None

    async def recv_message(self) -> tuple[int, bytes | str]:
        while True:
            header = await self.ws.recv()
            if not isinstance(header, bytes) or len(header) != 8 or header[0] != 3:
                continue  # stray frame
            ident, flags = header[1], header[2]
            if flags & 0x80:  # estimated size header, the real one follows (measured on 17.3; not bit 0)
                continue
            if ident == KEEPALIVE or ident == OOS:
                return ident, b""
            return ident, await self.ws.recv()

    async def cmd(self, command: str, encrypt: bool = False) -> dict:
        if encrypt:
            assert self.session
            command = self.session.encrypt_command(command)
        await self.ws.send(command)
        return _ll(await asyncio.wait_for(self.responses.get(), 15))


async def authenticate(conn: Conn, url: str, user: str, password: str, verify: bool) -> dict:
    async with httpx.AsyncClient(verify=verify, timeout=10) as c:
        r = await c.get(url.rstrip("/") + "/jdev/sys/getPublicKey")
        r.raise_for_status()
    conn.session = auth.Session(auth.load_public_key(_ll(r.text)["value"]))
    await conn.cmd(f"jdev/sys/keyexchange/{conn.session.session_key}")
    k = (await conn.cmd(f"jdev/sys/getkey2/{user}"))["value"]
    alg = k.get("hashAlg", "SHA1")
    h = auth.password_hash(user, password, k["key"], k["salt"], alg)
    tok = (
        await conn.cmd(
            f"jdev/sys/getjwt/{h}/{user}/{auth.PERMISSION_WEB}/{auth.client_uuid(url, user)}/loxbridge-bridge",
            encrypt=True,
        )
    )["value"]
    tok["hashAlg"] = alg
    return tok


async def _reader(conn: Conn, on_message: Callable[[int, bytes | str], None] | None) -> None:
    while True:
        ident, payload = await conn.recv_message()
        if ident in (TEXT, BINFILE) and isinstance(payload, str):
            await conn.responses.put(payload)
        elif on_message:
            on_message(ident, payload)


def _short_session(url: str, user: str, password: str, verify: bool, structure: bool) -> dict:
    async def go() -> dict:
        async with connect(ws_url(url), subprotocols=["remotecontrol"], ssl=_ssl(url, verify),  # type: ignore[list-item]
                           max_size=64 * 2**20, open_timeout=10) as ws:
            conn = Conn(ws)
            reader = asyncio.create_task(_reader(conn, None))
            try:
                out = await authenticate(conn, url, user, password, verify)
                if structure:
                    await ws.send("data/LoxAPP3.json")
                    out["structure"] = json.loads(await asyncio.wait_for(conn.responses.get(), 60))
                return out
            finally:
                reader.cancel()

    return asyncio.run(go())


def probe_sync(url: str, user: str, password: str, verify: bool) -> Structure:
    """Log in with the bridge user and read the structure file (for "Test connection")."""
    return parse_structure(_short_session(url, user, password, verify, True)["structure"])


class MiniserverWS:
    """Keeps a live connection; calls back with structure and decoded events. Never sends commands
    other than auth, structure download, enablebinstatusupdate, keepalive and token refresh."""

    def __init__(
        self,
        url: str,
        user: str,
        password: str,
        verify_tls: bool,
        cache: Path,
        on_structure: Callable[[Structure], Awaitable[None] | None],
        on_values: Callable[[list[tuple[str, float]]], None],
        on_texts: Callable[[list[tuple[str, str]]], None],
        on_connected: Callable[[bool, str], None],
    ):
        self.url, self.user, self.password, self.verify = url, user, password, verify_tls
        self.cache = cache
        self.on_structure, self.on_values, self.on_texts = on_structure, on_values, on_texts
        self.on_connected = on_connected

    async def run(self) -> None:
        backoff = 1.0
        while True:
            started = time.monotonic()
            try:
                await self._session()
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001 - any failure means reconnect
                msg = str(e) or type(e).__name__
                if isinstance(e, websockets.exceptions.InvalidStatus):
                    msg = explain(e.response.status_code)
                elif isinstance(e, TimeoutError):
                    msg = "the Miniserver didn't answer in time (reconnecting)"
                log.warning("bridge connection to %s lost: %s", self.url, msg)
                self.on_connected(False, msg)
            if time.monotonic() - started > 60:
                backoff = 1.0
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 300)

    def _on_message(self, ident: int, payload: bytes | str) -> None:
        if not isinstance(payload, bytes):
            return
        if ident == VALUES:
            self.on_values(decode_values(payload))
        elif ident == TEXTS:
            self.on_texts(decode_texts(payload))
        # daytimer / weather tables are ignored in v1

    async def _structure(self, conn: Conn) -> Structure:
        version = str((await conn.cmd("jdev/sps/LoxAPPversion3"))["value"])
        if self.cache.exists():
            data = json.loads(self.cache.read_text())
            if str(data.get("lastModified")) == version:
                return parse_structure(data)
        await conn.ws.send("data/LoxAPP3.json")
        text = await asyncio.wait_for(conn.responses.get(), 60)
        data = json.loads(text)
        self.cache.parent.mkdir(parents=True, exist_ok=True)
        self.cache.write_text(text)
        return parse_structure(data)

    async def _session(self) -> None:
        async with connect(
            ws_url(self.url),
            subprotocols=["remotecontrol"],  # type: ignore[list-item]
            ssl=_ssl(self.url, self.verify),
            max_size=64 * 2**20,
            ping_interval=None,
        ) as ws:
            conn = Conn(ws)
            reader = asyncio.create_task(_reader(conn, self._on_message))
            try:
                tok = await authenticate(conn, self.url, self.user, self.password, self.verify)
                structure = await self._structure(conn)
                res = self.on_structure(structure)
                if res is not None:
                    await res
                await conn.cmd("jdev/sps/enablebinstatusupdate")
                self.on_connected(True, "")
                log.info("bridge connected to %s", self.url)
                await self._keepalive(conn, tok, reader)
            finally:
                reader.cancel()

    async def _keepalive(self, conn: Conn, tok: dict, reader: asyncio.Task) -> None:
        valid_until = EPOCH_2009 + int(tok.get("validUntil", 0))
        refresh_at = time.time() + max(60, (valid_until - time.time()) / 2)
        while not reader.done():
            done, _ = await asyncio.wait({reader}, timeout=KEEPALIVE_S)
            if done:
                reader.result()  # re-raises the connection error
            await conn.ws.send("keepalive")
            if time.time() > refresh_at:
                key = (await conn.cmd("jdev/sys/getkey"))["value"]
                th = auth.token_hash(tok["token"], key, tok["hashAlg"])
                v = (await conn.cmd(f"jdev/sys/refreshjwt/{th}/{self.user}", encrypt=True))["value"]
                valid_until = EPOCH_2009 + int(v.get("validUntil", 0))
                refresh_at = time.time() + max(60, (valid_until - time.time()) / 2)
