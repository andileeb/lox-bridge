import asyncio
import sys
import threading
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))
from mock_ms import MockState, start  # noqa: E402


@pytest.fixture
def mock_ms():
    """Mock Miniserver in a background thread. Yields (url, state)."""
    state = MockState(interval=0.2)
    loop = asyncio.new_event_loop()
    ready = threading.Event()
    box: dict = {}

    async def boot():
        box["server"], box["port"] = await start(state)
        ready.set()

    threading.Thread(target=lambda: (loop.run_until_complete(boot()), loop.run_forever()), daemon=True).start()
    ready.wait(5)
    yield f"https://127.0.0.1:{box['port']}", state

    async def stop():
        box["server"].close()
        await box["server"].wait_closed()

    try:
        asyncio.run_coroutine_threadsafe(stop(), loop).result(10)
    except TimeoutError:
        pass  # a client didn't finish the close handshake; the daemon thread goes away with the test run
    loop.call_soon_threadsafe(loop.stop)
