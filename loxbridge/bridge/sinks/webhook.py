"""POST JSON batches with an HMAC-SHA256 signature header."""

import hashlib
import hmac
import json

import httpx

from .base import Point, Sink

SIGNATURE_HEADER = "X-LoxBridge-Signature"


def payload(batch: list[Point]) -> bytes:
    return json.dumps({"points": [
        {"miniserver": p.miniserver, **p.info.dict(), "value": p.value, "text": p.text, "ts": p.ts} for p in batch
    ]}).encode()


def sign(secret: str, body: bytes) -> str:
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


class WebhookSink(Sink):
    def __init__(self, cfg):
        super().__init__(cfg)
        self.client = httpx.AsyncClient(timeout=15)

    async def write(self, batch: list[Point]) -> None:
        body = payload(batch)
        headers = {"Content-Type": "application/json"}
        if self.cfg.secret.get_secret_value():
            headers[SIGNATURE_HEADER] = sign(self.cfg.secret.get_secret_value(), body)
        r = await self.client.post(self.cfg.url, content=body, headers=headers)
        if r.status_code >= 300:
            raise RuntimeError(f"webhook answered {r.status_code}")

    async def test(self) -> str:
        await self.write([])
        return "ok"

    async def close(self) -> None:
        await self.client.aclose()
