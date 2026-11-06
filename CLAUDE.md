# CLAUDE.md — LoxBridge

Python service (one container, one asyncio process) for **Loxone Miniserver Gen 2**: scheduled SD-card backups to a
local dataset and any number of S3 targets, a read-only live data bridge (WebSocket → InfluxDB / Prometheus / MQTT /
webhook / JSON API), and a history import of statistics files from backups. Web UI + API on one port (8080), deployed
as a TrueNAS custom app. User docs: README.md. Design decisions and open validation items: PLAN.md. This file is for
working on the code.

## Commands (uv, Python 3.13)

```bash
uv sync -p 3.13                     # venv + deps (uv.lock is committed; CI uses --locked)
uv run pytest -q                    # ~90 s: unit, moto S3, TLS mock Miniserver (HTTPS + WSS), web
uv run ruff check loxbridge tests   # line length 120
uv run mypy loxbridge/settings loxbridge/backup loxbridge/miniserver loxbridge/bridge
uv run python -m loxbridge serve    # needs CONFIG_DIR / WORK_DIR (default /config, /work) and ADMIN_PASSWORD
uv run python tests/mock_ms.py --port 8443 --extra-controls 50   # fake Miniserver (admin:secret)

docker compose -f compose.dev.yaml up -d --build   # app :8080 (admin/devpassword1), mock, s3mock, mosquitto,
                                                   # influxdb, prometheus, grafana :3000
E2E_BASE_URL=http://localhost:8080 uv run pytest tests/e2e   # Playwright; `uv run playwright install chromium`
```

The dev stack bind-mounts `./dev-data` (runs as root there). Deleting `dev-data` while the container runs kills it →
`docker compose -f compose.dev.yaml up -d --force-recreate loxbridge`.

Release: bump the version in **three places** (`pyproject.toml`, `loxbridge/__init__.py`, image tag in `compose.yaml`),
commit, push a `vX.Y.Z` tag → `.github/workflows/release.yml` checks they match, tests, pushes
`ghcr.io/andileeb/loxbridge:X.Y.Z` / `X.Y` / `latest` (amd64 + arm64) and drafts a GitHub release with `compose.yaml`
attached. Review the notes (summary + Install section, like v0.1.0) and publish.

## Architecture

- `app.py` — `Services` (store, jobs, hub, scheduler) + `create_app()`. Long actions go through
  `start_backup` / `start_gc` / `start_history_import` → `JobManager`.
- `settings/models.py` — the whole config as one pydantic `Config` (miniservers, backup, destinations, sinks,
  notifications, security). `SecretStr` fields are the secrets. `cron_triggers()` parses schedules.
- `settings/store.py` — SQLite `/config/loxbridge.db`: config stored as one JSON row (secrets `enc:<fernet>`),
  users, API tokens (sha256), job history (last 100), history-import progress. Migrations = SQL list +
  `PRAGMA user_version` (append, never edit). `LOXBRIDGE_A__B__0__C` env overrides are applied on load and
  **never persisted** (save restores the stored value at locked paths). `Store.save()` notifies listeners →
  scheduler re-applies, hub reconciles connections/sinks (no restart needed).
- `miniserver/` — `auth.py` (hashing, RSA/AES for WS commands, in-memory HTTP token cache), `http.py` (sync httpx:
  fslist/fsget/jdev with token or Basic auth, retries), `listing.py` (fslist parser), `ws.py` (async WebSocket:
  auth, structure file, binary event decode, keepalive, refresh, reconnect), `structure.py` (LoxAPP3.json → flat
  `StateInfo` per state uuid), `statsfile.py` (statistics files, see Known bugs).
- `backup/` — `runner.py` (sync; runs in a worker thread: pull → hash → push per destination → retention),
  `destinations/local.py` (`LocalStore`, also used as the `/work/staging` copy when no local destination exists),
  `destinations/s3.py` (`S3Store`, dedup or archive), `manifest.py`.
- `bridge/` — `hub.py` (state cache, per-sink selection/deadband/heartbeat, owns the `MiniserverWS` tasks),
  `sinks/` (one class per output, each with its own bounded deque + batching loop in `base.py`), `history.py`.
- `jobs/` — `manager.py` (jobs run in threads, progress polled by SSE), `scheduler.py` (APScheduler 3).
- `web/` — `common.py` (auth middleware, CSRF dependency, templates, streaming ZIP), `forms.py` (generic
  pydantic ↔ form fields: one edit page renders every model), `routes_ui.py`, `routes_api.py` (`/api/v1`,
  `/metrics`). Jinja2 + HTMX + Pico.css vendored in `web/static/` — no JS build step.
- `cli.py` — `serve` (default), `once`, `list`, `restore`, `verify`, `gc`, `check [--dump DIR]`, `reset-admin`.

## Domain knowledge (hard-won, don't rediscover)

- **Gen 2 only**: Miniserver URLs must be `https://` (validated); the bridge derives `wss://`. Gen 2 certificates
  are only valid for `https://<ip-with-dashes>.<serial>.dyndns.loxonecloud.com`, so `verify_tls` defaults to off.
- **Remote Connect / `dns.loxonecloud.com/<serial>`** answers with an HTTP 307 to
  `https://<ip-with-dashes>.<serial>.dyndns.loxonecloud.com:<webPort>/`. We deliberately don't follow redirects
  (the signed `autht` query must not go to another host); `explain()` tells the user to use the LAN address.
- **HTTP token auth** (backups; from LoxBerry `libs/pythonlib/loxberry/auth.py`, measured on 17.1.7.3):
  - `jdev/sys/getkey2/{user}` needs no auth and returns a **single-use** key: every signed request needs a fresh one.
  - `jdev/sys/getjwt/...` works over HTTPS without the RSA/AES layer.
  - Requests are signed with `?autht=HMAC(token, key)&user=`.
  - The JSON `LL.code` can be 401 inside an HTTP 200, so always check both (`MiniserverHTTP._ll`).
  - `killtoken` works only with Basic auth.
- **WebSocket auth** (bridge): `getjwt` *must* be encrypted (`keyexchange` + `jdev/sys/enc/`). Web-permission
  token, refreshed with `getkey` + `refreshjwt` at half its lifetime. Keepalive every 60 s; the Miniserver drops
  idle sockets after 5 min.
- Message framing: an 8-byte header `03 <id> <flags> 00 <u32 len>` precedes every payload. Flag bit 0 = estimated
  header, the real one follows. Value events are 24 bytes (uuid + f64). Text events are uuid + icon uuid + u32 len +
  text padded to 4.
- **Loxone UUIDs** are `8-4-4-16` hex. Binary layout: `u32 LE, u16 LE, u16 LE, 8 bytes` (`ws.lox_uuid`).
- Loxone epoch = 2009-01-01 (`auth.EPOCH_2009 = 1230768000`).
- **APScheduler 3 numbers weekdays Monday=0**; `cron_triggers()` converts cron numbers to names so `1-5` means
  Mon–Fri like standard cron. Dependabot ignores APScheduler ≥ 4 (different API).
- Backups read each file at most once per run: unchanged size+stamp → hard link from the previous snapshot. The
  fslist stamp switches from `HH:MM` to a year after ~6 months; that only causes one extra download.
- S3 dedup keeps a local blob index in `/work/s3index/<dest>_<ms>.txt` to avoid HEAD requests. It's rebuilt from one
  listing if `latest.json` is missing. GC only deletes unreferenced blobs older than 24 h.
- Snapshots are invisible until complete: local `.inprogress_*` → rename; S3 manifest + `latest.json` last.
- httpx/httpcore loggers are forced to WARNING (`logs.py`): at INFO they log request URLs, which carry `autht`.

## Known bugs / open items

- `statsfile.py` is ported from `../lox-datamanager` (`src/main/statfile.ts`; LoxStatEdit + sarnau layout,
  stride detected per file, `<uuid>[_N].<YYYYMM>` names) but only tested with synthetic files built by
  `tests/build_statfile.py`. Drop real files into `tests/fixtures/` (`test_real_fixture_files` picks them up).
  statisticV2 suffixes are mapped for Meter only (`history.METER_V2`); others get `stat<N>_<i>` state names.
- Validate on a real Miniserver (PLAN.md): fslist format, autht on `/dev/fsget`, user rights, `wss://`, event padding,
  restore via Loxone Config. `check --dump DIR` collects the raw answers → add them to `tests/fixtures/`.
- HTTP tokens are cached per process only (`auth.TOKENS`); persist them if the Miniserver's token list fills up.

## Constraints & gotchas

- Read-only bridge: never send commands to the Miniserver other than auth, structure, `enablebinstatusupdate`,
  keepalive and token refresh.
- Secrets: never log, never render. Forms show `••• set`; an empty or `••• set` value keeps the stored secret
  (`forms.parse`, `routes_api._merge_secrets`). Fernet key from `SECRET_KEY` or the generated `/config/secret.key`.
- Snapshot file paths come from the Miniserver: `runner._safe()` rejects `..`/absolute paths, and `LocalStore`
  checks snapshot names. Keep those guards.
- Jobs and config listeners can fire from worker threads (sync FastAPI endpoints, APScheduler executors). Use
  `JobManager.spawn()` to get onto the app loop, never `asyncio.get_running_loop()` there.
- UI POSTs need the CSRF token (form field `csrf` or `X-CSRF-Token`; HTMX sends it from `<body hx-headers>`).
  Bearer-token API calls are exempt.
- Locked (env-overridden) form fields are disabled → not submitted → pass their names in `skip` to `forms.parse`,
  or booleans would read as false.
- Container runs as UID/GID 568 (TrueNAS apps). `Dockerfile` copies README.md + LICENSE (pyproject metadata needs
  them).
- `astral-sh/setup-uv` has no moving major tag: pin the full version in workflows.
- MinIO images are no longer published; the dev stack uses `adobe/s3mock` (internal only, no host port).

## Testing

- `tests/mock_ms.py` is a fake Gen 2 Miniserver on one TLS port (self-signed):
  - HTTP side: fslist/fsget, single-use getkey2, getjwt (LL code 401 inside HTTP 200), autht HMAC check, Basic
    auth (`basic_allowed`), and HTTP 500 on `log/broken.log`.
  - WebSocket side: real RSA keyexchange + AES `enc`, getjwt, structure file, value/text event tables.
  - `state.reads` counts fsget calls per path.

  The `mock_ms` fixture (conftest) yields `(url, state)`.
- S3 tests use moto (`mock_aws`); bucket names need ≥ 3 chars.
- `tests/fixtures/LoxAPP3.json` is synthetic. Replace or extend it with an anonymised real one when available.
- Bridge tests drive `Hub` directly with `asyncio.run`; a tiny `http.server` recorder stands in for InfluxDB/webhooks.
