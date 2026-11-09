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


SHOTS = os.environ.get("E2E_SCREENSHOTS")  # folder for screenshots of every page at phone/tablet/desktop size
VIEWPORTS = {"phone": (390, 844), "tablet": (820, 1180), "desktop": (1280, 800)}
PAGES = ["/", "/snapshots", "/backup", "/destinations", "/bridge", "/miniservers", "/miniservers/ms1", "/setup",
         "/settings", "/notifications", "/logs", "/history", "/sinks/new?type=influx"]


def test_ui_flow(tmp_path):
    from playwright.sync_api import expect, sync_playwright

    with sync_playwright() as p:
        b = p.chromium.launch()
        page = b.new_page(accept_downloads=True)
        page.goto(f"{BASE}/")  # fresh dev-data: first visit sets the password
        expect(page.get_by_role("button", name="Set password")).to_be_visible()
        page.fill("input[name=password]", "devpassword1")
        page.fill("input[name=repeat]", "devpassword1")
        page.click("button[type=submit]")

        # setup wizard: empty overview → find the mock by name → log in → first backup
        page.click("text=Get started")
        expect(page.get_by_role("heading", name="Connect a Miniserver")).to_be_visible()
        expect(page.locator("#found")).not_to_contain_text("Searching your network", timeout=30000)
        if not page.is_visible("input[name=network]"):  # open by itself when nothing was found
            page.click("summary:has-text('Search another network')")
        page.fill("input[name=network]", "mock-ms")
        page.click("#found button:has-text('Search')")
        page.click(".choice:has-text('504F94A0FFEE')")
        expect(page.locator("#url")).to_have_value("https://mock-ms")
        page.fill("input[name=user]", "admin")
        page.fill("input[name=password]", "nope")
        page.click("#go")
        expect(page.locator("#setup-result")).to_contain_text("Could not log in", timeout=15000)
        page.fill("input[name=password]", "secret")
        page.check("input[name=live]")
        page.click("#go")
        expect(page.locator("#status")).to_contain_text(re.compile("ok|partial"), timeout=60000)
        expect(page.get_by_role("heading", name="Backup MockMS")).to_be_visible()

        # S3 destination (s3mock in the dev stack), tested before saving
        page.goto(f"{BASE}/destinations/new?type=s3&preset=minio")
        page.click("text=Advanced")
        page.fill("input[name=id]", "s3")
        page.fill("input[name=endpoint]", "http://s3:9090")
        page.fill("input[name=bucket]", "loxbridge")
        page.fill("input[name=access_key]", "any")
        page.fill("input[name=secret_key]", "any")
        page.click("button:has-text('Test')")
        expect(page.locator("#test-result")).to_contain_text("✔", timeout=15000)
        page.click("button:has-text('Save')")
        expect(page.locator(".flash")).to_contain_text("Saved")

        # back up from the overview and see the snapshot on S3
        page.goto(f"{BASE}/")
        page.click("text=Back up now")
        expect(page.locator("#status")).to_contain_text(re.compile("ok|partial"), timeout=60000)
        page.goto(f"{BASE}/snapshots?dest=s3")
        expect(page.locator("td").filter(has_text="s3").first).to_be_visible()
        with page.expect_download() as dl:
            page.locator("a:has-text('ZIP')").first.click()
        path = tmp_path / "snap.zip"
        dl.value.save_as(path)
        assert path.stat().st_size > 1000

        # live view works before any output exists: connection status + values from the (mock) Miniserver
        page.goto(f"{BASE}/bridge")
        expect(page.locator("details.card")).to_contain_text("connected", timeout=30000)
        expect(page.locator("details.card td").first).to_be_visible(timeout=15000)

        # outputs: MQTT (+ HA discovery) and InfluxDB, export everything in the living room
        page.goto(f"{BASE}/sinks/new?type=mqtt")
        page.click("text=Advanced")
        page.fill("input[name=id]", "mqtt")
        page.check("input[name=ha_discovery]")
        page.click("button:has-text('Save')")
        page.goto(f"{BASE}/sinks/new?type=influx")
        page.click("text=Advanced")
        page.fill("input[name=id]", "influx")
        page.fill("input[name=org]", "home")
        page.fill("input[name=token]", "dev-influx-token")
        page.click("button:has-text('Save')")
        for sink in ("mqtt", "influx"):
            page.goto(f"{BASE}/bridge?sink={sink}&ms=ms1")
            page.select_option("#f-room", "Living Room")
            page.check("#all")
            page.click("text=Save selection")
            expect(page.locator(".flash")).to_contain_text("controls exported")

        # every page fits phone, tablet and desktop screens without sideways scrolling
        for name, (w, h) in VIEWPORTS.items():
            page.set_viewport_size({"width": w, "height": h})
            for url in PAGES:
                page.goto(f"{BASE}{url}")
                overflow = page.evaluate("document.documentElement.scrollWidth - window.innerWidth")
                assert overflow <= 1, f"{url} overflows by {overflow}px at {name} width"
                expect(page.locator(".mainnav")).to_be_visible()
                if SHOTS:
                    slug = url.strip("/").replace("/", "_").replace("?", "_").replace("=", "-") or "overview"
                    page.screenshot(path=f"{SHOTS}/{name}-{slug}.png", full_page=True)
        b.close()

    msg = mqtt_message("loxbridge/ms1/living_room/temperature/value")
    assert msg and '"value"' in msg
    assert mqtt_message("homeassistant/sensor/+/config")

    flux = 'from(bucket:"loxone") |> range(start: -5m) |> filter(fn: (r) => r.control == "Temperature") |> count()'
    deadline = time.time() + 15
    while True:
        r = httpx.post(f"{INFLUX}/api/v2/query", params={"org": "home"}, content=flux,
                       headers={"Authorization": "Token dev-influx-token", "Content-Type": "application/vnd.flux"})
        if ",value,loxone,Climate,Temperature,ms1,Living Room" in r.text:
            break
        assert time.time() < deadline, r.text
        time.sleep(1)
