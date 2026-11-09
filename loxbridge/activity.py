"""Activity history: what LoxBridge did, one line per event, kept in SQLite (Settings → Logs).

Callers anywhere (worker threads, the hub, sinks) just call record(). Never pass secrets in messages.
"""

import logging
from typing import Literal

log = logging.getLogger(__name__)

Kind = Literal["backup", "job", "live", "output", "settings", "security", "notify", "app"]
KINDS: dict[str, str] = {"backup": "Backups", "job": "Other jobs", "live": "Live data", "output": "Outputs",
                         "settings": "Settings", "security": "Logins", "notify": "Notifications", "app": "App"}

# ponytail: module-level store like logs.BUFFER; one app per process
STORE = None


def record(kind: Kind, message: str, level: Literal["info", "warn", "error"] = "info", link: str = "") -> None:
    if STORE is None:
        return
    try:
        STORE.add_event(kind, level, message, link)
    except Exception:  # noqa: BLE001 - the history must never break the thing it records
        log.exception("could not record activity")
