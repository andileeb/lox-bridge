"""Playwright smoke test against the compose.dev.yaml stack.

  docker compose -f compose.dev.yaml up -d --build
  E2E_BASE_URL=http://localhost:8080 uv run pytest tests/e2e -q
"""

import asyncio
import os
import re
import time

import httpx
import pytest

BASE = os.environ.get("E2E_BASE_URL")
pytestmark = pytest.mark.skipif(not BASE, reason="set E2E_BASE_URL to run the E2E test")
INFLUX = os.environ.get("E2E_INFLUX_URL", "http://localhost:8086")
MQTT_HOST = os.environ.get("E2E_MQTT_HOST", "localhost")


def mqtt_message(topic: str, timeout: float = 10) -> str | None:
    import aiomqtt

    async def go():
        async with aiomqtt.Client(MQTT_HOST) as c:
            await c.subscribe(topic)
            async with asyncio.timeout(timeout):
                async for m in c.messages:
                    return m.payload.decode()

    try:
        return asyncio.run(go())
    except TimeoutError:
        return None


def test_ui_flow(tmp_path):
    from playwright.sync_api import expect, sync_playwright

    with sync_playwright() as p:
        b = p.chromium.launch()
        page = b.new_page(accept_downloads=True)
        page.goto(f"{BASE}/")
        page.fill("input[name=password]", "devpassword1")
        page.click("button[type=submit]")
        expect(page.get_by_role("heading", name="Dashboard")).to_be_visible()

        # add the mock Miniserver (backup + bridge user)
        page.goto(f"{BASE}/miniservers/new")
        page.fill("input[name=id]", "home")
        page.fill("input[name=name]", "Home")
        page.fill("input[name=url]", "https://mock-ms")
        page.fill("input[name=backup_user]", "admin")
        page.fill("input[name=backup_password]", "secret")
        page.check("input[name=bridge_enabled]")
        page.fill("input[name=bridge_user]", "admin")
        page.fill("input[name=bridge_password]", "secret")
        page.click("text=Save")
        expect(page.locator(".flash")).to_contain_text("Saved Home")
        page.goto(f"{BASE}/miniservers/home")
        page.click("button:has-text('Test')")
        expect(page.locator("#test-result")).to_contain_text("token ok", timeout=15000)

        # S3 destination (s3mock in the dev stack)
        page.goto(f"{BASE}/destinations/new?type=s3&preset=minio")
        page.fill("input[name=id]", "s3")
        page.fill("input[name=endpoint]", "http://s3:9090")
        page.fill("input[name=bucket]", "loxbridge")
        page.fill("input[name=access_key]", "any")
        page.fill("input[name=secret_key]", "any")
        page.click("text=Save")
        expect(page.locator(".flash")).to_contain_text("Saved")

        # back up and see the snapshot
        page.goto(f"{BASE}/")
        page.click("text=Back up now")
        expect(page.locator("#status")).to_contain_text(re.compile("ok|partial"), timeout=60000)
        page.goto(f"{BASE}/snapshots")
        expect(page.locator("td").filter(has_text="s3").first).to_be_visible()
        with page.expect_download() as dl:
            page.locator("a:has-text('ZIP')").first.click()
        path = tmp_path / "snap.zip"
        dl.value.save_as(path)
        assert path.stat().st_size > 1000

        # outputs: MQTT (+ HA discovery) and InfluxDB, export everything in the living room
        page.goto(f"{BASE}/sinks/new?type=mqtt")
        page.fill("input[name=id]", "mqtt")
        page.check("input[name=ha_discovery]")
        page.click("text=Save")
        page.goto(f"{BASE}/sinks/new?type=influx")
        page.fill("input[name=id]", "influx")
        page.fill("input[name=org]", "home")
        page.fill("input[name=token]", "dev-influx-token")
        page.click("text=Save")
        for sink in ("mqtt", "influx"):
            page.goto(f"{BASE}/bridge?sink={sink}&ms=home")
            page.select_option("#f-room", "Living Room")
            page.click("text=Select matching")
            page.click("text=Save selection")
            expect(page.locator(".flash")).to_contain_text("controls exported")
        b.close()

    msg = mqtt_message("loxbridge/home/living_room/temperature/value")
    assert msg and '"value"' in msg
    assert mqtt_message("homeassistant/sensor/+/config")

    flux = 'from(bucket:"loxone") |> range(start: -5m) |> filter(fn: (r) => r.control == "Temperature") |> count()'
    deadline = time.time() + 15
    while True:
        r = httpx.post(f"{INFLUX}/api/v2/query", params={"org": "home"}, content=flux,
                       headers={"Authorization": "Token dev-influx-token", "Content-Type": "application/vnd.flux"})
        if ",value,loxone,Climate,Temperature,home,Living Room" in r.text:
            break
        assert time.time() < deadline, r.text
        time.sleep(1)
