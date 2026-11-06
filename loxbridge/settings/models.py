"""Settings model. Everything the UI edits lives here; SecretStr fields are encrypted at rest."""

import re
from typing import Annotated, Literal

from apscheduler.triggers.cron import CronTrigger
from pydantic import BaseModel, Field, SecretStr, field_validator, model_validator

Slug = Annotated[str, Field(pattern=r"^[a-z0-9][a-z0-9_-]{0,39}$")]
_TIME = re.compile(r"^\d{1,2}:\d{2}$")
_DOW_NUM = re.compile(r"(?<![/\d])\d+")
_DAYS = ["sun", "mon", "tue", "wed", "thu", "fri", "sat"]


def empty_secret() -> SecretStr:
    return SecretStr("")


class Miniserver(BaseModel):
    id: Slug
    name: str
    url: str = Field(pattern=r"^https?://[^/\s]+/?$")
    verify_tls: bool = Field(False, description=(
        "Gen 2 certificates are only valid for the CloudDNS name "
        "(https://<ip-with-dashes>.<serial>.dyndns.loxonecloud.com); leave off when using the IP."))
    backup_enabled: bool = True
    auth_mode: Literal["token", "basic"] = Field(
        "token", description="Backup requests: token auth (default) or Basic auth as a fallback.")
    backup_user: str = ""
    backup_password: SecretStr = Field(default_factory=empty_secret)
    bridge_enabled: bool = False
    bridge_user: str = ""
    bridge_password: SecretStr = Field(default_factory=empty_secret)


def cron_triggers(schedule: str, tz: str | None = None) -> list[CronTrigger]:
    """'03:00, 15:30' (daily times) or a 5-field cron expression."""
    s = schedule.strip()
    if not s:
        return []
    parts = [p.strip() for p in s.split(",")]
    if all(_TIME.match(p) for p in parts):
        return [CronTrigger(hour=int(p.split(":")[0]), minute=int(p.split(":")[1]), timezone=tz) for p in parts]
    f = s.split()
    if len(f) != 5:
        raise ValueError("cron expressions need 5 fields")
    # APScheduler 3 numbers weekdays from Monday=0; standard cron uses Sunday=0. Use names to avoid the shift.
    dow = _DOW_NUM.sub(lambda m: _DAYS[int(m[0]) % 7], f[4])
    return [CronTrigger(minute=f[0], hour=f[1], day=f[2], month=f[3], day_of_week=dow, timezone=tz)]


class BackupSettings(BaseModel):
    schedule: str = "03:00"
    excludes: list[str] = []  # fnmatch patterns on the SD card path, e.g. "log/*"
    request_delay: float = Field(0.05, ge=0, le=5)
    full_download: bool = False
    auto_import_history: bool = False
    history_sink: str = ""  # influx sink id used for auto import

    @field_validator("schedule")
    @classmethod
    def _valid_schedule(cls, v: str) -> str:
        try:
            cron_triggers(v)
        except ValueError as e:
            raise ValueError(f"not a list of HH:MM times or a cron expression: {e}") from e
        return v


class LocalDestination(BaseModel):
    type: Literal["local"] = "local"
    id: Slug
    name: str = "Local"
    enabled: bool = True
    keep: int = Field(14, ge=1)
    path: str = "/backups"


S3_PRESETS = {
    "aws": {"endpoint": "", "region": "eu-central-1", "addressing": "virtual"},
    "b2": {"endpoint": "https://s3.eu-central-003.backblazeb2.com", "region": "eu-central-003",
           "addressing": "virtual"},
    "wasabi": {"endpoint": "https://s3.eu-central-1.wasabisys.com", "region": "eu-central-1", "addressing": "virtual"},
    "hetzner": {"endpoint": "https://fsn1.your-objectstorage.com", "region": "fsn1", "addressing": "virtual"},
    "r2": {"endpoint": "https://<account-id>.r2.cloudflarestorage.com", "region": "auto", "addressing": "path"},
    "minio": {"endpoint": "http://minio:9000", "region": "us-east-1", "addressing": "path"},
}


class S3Destination(BaseModel):
    type: Literal["s3"] = "s3"
    id: Slug
    name: str = "S3"
    enabled: bool = True
    keep: int = Field(30, ge=1)
    endpoint: str = ""  # empty = AWS
    bucket: str = ""
    prefix: str = "loxbridge"
    region: str = "us-east-1"
    access_key: str = ""
    secret_key: SecretStr = Field(default_factory=empty_secret)
    addressing: Literal["auto", "path", "virtual"] = "auto"
    mode: Literal["dedup", "archive"] = "dedup"
    storage_class: str = ""
    sse: Literal["", "AES256", "aws:kms"] = ""
    concurrency: int = Field(8, ge=1, le=64)


Destination = Annotated[LocalDestination | S3Destination, Field(discriminator="type")]


class SinkBase(BaseModel):
    id: Slug
    name: str = ""
    enabled: bool = True
    miniservers: list[str] = []  # empty = all
    selection: list[str] = []  # control UUIDs to export
    include_text: bool = False
    deadband: float = Field(0.0, ge=0)  # forward only if |delta| >= deadband ...
    heartbeat: int = Field(0, ge=0)  # ... or every n seconds (0 = off)
    batch_seconds: float = Field(5.0, ge=0.1, le=300)
    batch_size: int = Field(5000, ge=1)
    queue_size: int = Field(10000, ge=100)


class InfluxSink(SinkBase):
    type: Literal["influx"] = "influx"
    url: str = "http://influxdb:8086"
    version: Literal["v2", "v1", "vm"] = "v2"
    org: str = ""
    bucket: str = "loxone"  # v2 bucket, v1/vm database
    token: SecretStr = Field(default_factory=empty_secret)
    username: str = ""
    password: SecretStr = Field(default_factory=empty_secret)
    measurement: str = "loxone"


class PrometheusSink(SinkBase):
    type: Literal["prometheus"] = "prometheus"


class MqttSink(SinkBase):
    type: Literal["mqtt"] = "mqtt"
    batch_seconds: float = 0.2
    host: str = "mosquitto"
    port: int = 1883
    tls: bool = False
    username: str = ""
    password: SecretStr = Field(default_factory=empty_secret)
    prefix: str = "loxbridge"
    retain: bool = True
    payload: Literal["json", "plain"] = "json"
    ha_discovery: bool = False
    ha_prefix: str = "homeassistant"
    include_text: bool = True


class WebhookSink(SinkBase):
    type: Literal["webhook"] = "webhook"
    url: str = ""
    secret: SecretStr = Field(default_factory=empty_secret)


Sink = Annotated[InfluxSink | PrometheusSink | MqttSink | WebhookSink, Field(discriminator="type")]


class Notifications(BaseModel):
    on: Literal["failure", "always"] = "failure"
    healthcheck_success_url: str = ""
    healthcheck_fail_url: str = ""
    webhook_url: str = ""
    ntfy_url: str = ""  # https://ntfy.sh/<topic>
    ntfy_token: SecretStr = Field(default_factory=empty_secret)
    gotify_url: str = ""
    gotify_token: SecretStr = Field(default_factory=empty_secret)
    smtp_host: str = ""
    smtp_port: int = 587
    smtp_starttls: bool = True
    smtp_user: str = ""
    smtp_password: SecretStr = Field(default_factory=empty_secret)
    smtp_from: str = ""
    smtp_to: str = ""


class Security(BaseModel):
    proxy_header: str = ""  # e.g. Remote-User (Authelia / Authentik)
    trusted_proxies: list[str] = []  # client IPs allowed to set proxy_header
    metrics_token: SecretStr = Field(default_factory=empty_secret)


class Config(BaseModel):
    miniservers: list[Miniserver] = []
    backup: BackupSettings = BackupSettings()
    destinations: list[Destination] = [LocalDestination(id="local")]
    sinks: list[Sink] = []
    notifications: Notifications = Notifications()
    security: Security = Security()
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"

    @model_validator(mode="after")
    def _unique(self) -> "Config":
        for name, items in (("Miniserver", self.miniservers), ("destination", self.destinations),
                            ("sink", self.sinks)):
            ids = [i.id for i in items]
            if len(ids) != len(set(ids)):
                raise ValueError(f"duplicate {name} id")
        if sum(1 for d in self.destinations if d.type == "local") > 1:
            raise ValueError("only one local destination is supported")
        if sum(1 for s in self.sinks if s.type == "prometheus") > 1:
            raise ValueError("only one Prometheus sink is supported")
        return self

    def miniserver(self, ms_id: str) -> Miniserver:
        for m in self.miniservers:
            if m.id == ms_id:
                return m
        raise KeyError(ms_id)

    def destination(self, dest_id: str):
        for d in self.destinations:
            if d.id == dest_id:
                return d
        raise KeyError(dest_id)

    def sink(self, sink_id: str):
        for s in self.sinks:
            if s.id == sink_id:
                return s
        raise KeyError(sink_id)
