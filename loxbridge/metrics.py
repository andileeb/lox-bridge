"""Prometheus metrics, served at /metrics on the web port. App metrics are always available."""

from prometheus_client import CollectorRegistry, Counter, Gauge

REGISTRY = CollectorRegistry()

backup_last_success = Gauge("loxbridge_backup_last_success_timestamp", "Unix time of the last good backup",
                            ["miniserver"], registry=REGISTRY)
backup_duration = Gauge("loxbridge_backup_duration_seconds", "Duration of the last backup", ["miniserver"],
                        registry=REGISTRY)
backup_bytes = Gauge("loxbridge_backup_bytes", "Size of the last snapshot", ["miniserver"], registry=REGISTRY)
backup_errors = Gauge("loxbridge_backup_errors", "File errors in the last backup", ["miniserver"],
                      registry=REGISTRY)
backup_ok = Gauge("loxbridge_backup_last_ok", "1 if the last backup succeeded for all destinations",
                  ["miniserver"], registry=REGISTRY)
destination_ok = Gauge("loxbridge_destination_ok", "1 if the last push to the destination worked",
                       ["miniserver", "destination"], registry=REGISTRY)
ws_connected = Gauge("loxbridge_ws_connected", "1 while the bridge WebSocket is connected", ["miniserver"],
                     registry=REGISTRY)
bridge_events = Counter("loxbridge_bridge_events", "State events received", ["miniserver"], registry=REGISTRY)
sink_sent = Counter("loxbridge_sink_sent", "Points written by a sink", ["sink"], registry=REGISTRY)
sink_dropped = Counter("loxbridge_sink_dropped", "Points dropped because the sink queue overflowed", ["sink"],
                       registry=REGISTRY)
sink_errors = Counter("loxbridge_sink_errors", "Failed sink writes", ["sink"], registry=REGISTRY)
sink_queue = Gauge("loxbridge_sink_queue", "Points waiting in a sink queue", ["sink"], registry=REGISTRY)
loxone_state = Gauge("loxone_state", "Current Loxone state value",
                     ["miniserver", "room", "category", "control", "state", "type"], registry=REGISTRY)


def record_backup(ms: str, summary: dict, finished: float) -> None:
    if not isinstance(summary, dict) or "destinations" not in summary:
        backup_ok.labels(ms).set(0)
        return
    for dest, r in summary["destinations"].items():
        destination_ok.labels(ms, dest).set(1 if r.get("ok") else 0)
    ok = summary.get("status") in ("ok", "partial") and all(r.get("ok") for r in summary["destinations"].values())
    backup_ok.labels(ms).set(1 if summary.get("status") == "ok" else 0)
    backup_duration.labels(ms).set(summary.get("duration", 0))
    backup_bytes.labels(ms).set(summary.get("size", 0))
    backup_errors.labels(ms).set(len(summary.get("errors", [])))
    if ok:
        backup_last_success.labels(ms).set(finished)
