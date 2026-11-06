# LoxBridge plan: status and decisions

The original implementation plan (phases 1–8) is implemented. This file records design decisions and what still has
to be validated against a real Miniserver.

## Scope

- **Gen 2 Miniservers only.** HTTPS everywhere; no Gen 1 code paths.

## Transport per job

| Job | Transport | Why |
|---|---|---|
| Live bridge | WebSocket `/ws/rfc6455` (`ws://` or `wss://` from the configured URL), token auth, `LoxAPP3.json`, `enablebinstatusupdate`, keepalive every 60 s (Miniserver drops idle sockets after 5 min), reconnect with exponential backoff (1 s → 5 min) | push based, real time, no Loxone Config changes |
| Backups | HTTPS `/dev/fslist/<path>/`, `/dev/fsget/<path>`, sequential, per-file retry | big downloads never delay state events; files can be streamed to disk |

Files are not downloaded over the WebSocket. Loxone documents file access as HTTP web services. A WebSocket download
holds the whole file in one message and blocks the event stream for that time.

Rejected: **FTP** (unencrypted, the 2022 Loxone security change, not available via Remote Connect) and **native
Loxone MQTT** as the main path (max 16 publishes/subscriptions, 2 s throttling, per-value setup in Config). Native
MQTT could later be an optional input for a few values.

## Authentication (`loxbridge/miniserver/auth.py`)

One module with the hashing and crypto, used by both transports:

- Acquire: `jdev/sys/getkey2/{user}` → `{key, salt, hashAlg}`; `pwHash = HASH(password:salt)` upper-case;
  `hash = HMAC-HASH(key, user:pwHash)`; `jdev/sys/getjwt/{hash}/{user}/{perm}/{uuid}/{info}`. Over HTTPS
  (backups) plain, falling back to `gettoken` on old firmware. Over the WebSocket encrypted (`keyexchange` + `jdev/sys/enc/`),
  as the document requires there.
- HTTPS requests: `?autht={HMAC(token, key from a fresh getkey2)}&user={user}`. The getkey2 key is single use, so
  each signed request costs one extra small request. A 401 re-acquires the token once.
- The JSON `LL.code` wins over the HTTP status (200 with LL code 401 happens).
- Permissions: the backup uses an **app token (4)**, cached per process, re-acquired when < 1 day is left. The bridge
  uses a **web token (2)** per WebSocket session, refreshed (`getkey` + `refreshjwt`) at half its lifetime.
- Client UUID stable per Miniserver and user, so the Miniserver doesn't collect new client entries.
- **Two users** (recommended): a backup user with file access and a low-privilege bridge user. Tokens are per user,
  so one token can't serve both jobs unless one user does both.
- Basic auth: per-Miniserver fallback setting for backups only (`auth_mode: basic`).

Sources: Loxone "Communicating with the Miniserver" (Config 10.0 PDF), and LoxBerry's `libs/pythonlib/loxberry/auth.py`
(notes measured on firmware 17.1.7.3: getkey2 needs no auth and gives a one-time key; tokens can be acquired without
application-layer encryption from 11.2.10.22; LL code can differ from the HTTP status; killtoken works only with
Basic auth).

## Risks and mitigations

- **Connection limits / load**: one WebSocket per Miniserver (bridge) plus one sequential HTTPS client during
  backups (optional request delay, default 50 ms). The bridge keeps running during backups.
- **Token storage on the Miniserver**: stable client UUID; one app token per user per process. If the token list grows
  (e.g. many CLI runs), persist tokens encrypted in the DB and kill them on Miniserver removal (needs Basic auth).
- **Certificates**: Gen 2 certificates match only the CloudDNS name; `verify_tls` is off by default for IP URLs.
- **Remote Connect / Cloud DNS** URLs redirect; only direct LAN URLs are tested.

## Still to validate on the real Miniserver

1. The real `/dev/fslist` output format (add as a test fixture).
2. That `autht` signing works for `/dev/fslist` and `/dev/fsget` (LoxBerry verified it for `jdev` commands), and the
   minimum user rights for `/dev/fsget` and for WebSocket states.
3. `wss://` on the user's firmware (the Config 10.0 document still says wss is unsupported; Gen 2 firmware supports it).
4. The binary event tables and text-state padding on the current firmware.
5. The statistics file format and location (blocking only for the history import). Unknown files are skipped.
6. A test restore through Loxone Config, adjusting the snapshot folder naming if Config expects a `_<version>` suffix.

## Later

Write bridge (MQTT → Miniserver with an allow-list), client-side encryption of S3 blobs, multi-user roles, high-availability bridge,
persisted HTTP tokens.
