"""SQLite persistence (/config/loxbridge.db), secret encryption, LOXBRIDGE_* env overrides."""

import base64
import hashlib
import json
import logging
import os
import secrets
import sqlite3
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from cryptography.fernet import Fernet, InvalidToken
from pydantic import SecretStr

from .models import Config

log = logging.getLogger(__name__)

ENC = "enc:"
ENV_PREFIX = "LOXBRIDGE_"

MIGRATIONS = [
    """
    CREATE TABLE config (id INTEGER PRIMARY KEY CHECK (id = 1), data TEXT NOT NULL);
    CREATE TABLE users (username TEXT PRIMARY KEY, password_hash TEXT NOT NULL);
    CREATE TABLE api_tokens (id INTEGER PRIMARY KEY, name TEXT NOT NULL, token_hash TEXT UNIQUE NOT NULL,
                             created REAL NOT NULL, last_used REAL);
    CREATE TABLE jobs (id TEXT PRIMARY KEY, kind TEXT NOT NULL, target TEXT, started REAL NOT NULL,
                       finished REAL, status TEXT NOT NULL, summary TEXT);
    CREATE TABLE history_progress (miniserver TEXT, file TEXT, last_ts INTEGER NOT NULL,
                                   PRIMARY KEY (miniserver, file));
    """,
]


def load_secret_key(config_dir: Path) -> tuple[bytes, bool]:
    """Returns (fernet key, generated). SECRET_KEY / SECRET_KEY_FILE win over /config/secret.key."""
    raw = os.environ.get("SECRET_KEY")
    if not raw and os.environ.get("SECRET_KEY_FILE"):
        raw = Path(os.environ["SECRET_KEY_FILE"]).read_text().strip()
    generated = False
    if not raw:
        f = config_dir / "secret.key"
        if f.exists():
            raw = f.read_text().strip()
        else:
            raw = secrets.token_urlsafe(32)
            f.touch(mode=0o600)
            f.write_text(raw)
            generated = True
    return base64.urlsafe_b64encode(hashlib.sha256(raw.encode()).digest()), generated


def _walk(obj: Any, fn: Callable[[Any], Any]) -> Any:
    if isinstance(obj, dict):
        return {k: _walk(v, fn) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_walk(v, fn) for v in obj]
    return fn(obj)


def dump_config(cfg: Config, fernet: Fernet | None, include_secrets: bool = True) -> dict:
    """Plain dict; secrets are encrypted (fernet given), dropped (include_secrets=False)."""

    def conv(v: Any) -> Any:
        if isinstance(v, SecretStr):
            s = v.get_secret_value()
            if not include_secrets or not s:
                return ""
            assert fernet
            return ENC + fernet.encrypt(s.encode()).decode()
        return v

    return _walk(cfg.model_dump(mode="python"), conv)


def decrypt_tree(data: Any, fernet: Fernet) -> Any:
    def conv(v: Any) -> Any:
        if isinstance(v, str) and v.startswith(ENC):
            try:
                return fernet.decrypt(v[len(ENC) :].encode()).decode()
            except InvalidToken:
                log.error("cannot decrypt a stored secret: SECRET_KEY changed? The secret was cleared.")
                return ""
        return v

    return _walk(data, conv)


def _set_path(data: Any, path: list[str], value: Any) -> None:
    for p in path[:-1]:
        data = data[int(p)] if isinstance(data, list) else data.setdefault(p, {})
    if isinstance(data, list):
        data[int(path[-1])] = value
    else:
        data[path[-1]] = value


def _get_path(data: Any, path: list[str]) -> Any:
    for p in path:
        data = data[int(p)] if isinstance(data, list) else data[p]
    return data


def env_overrides() -> dict[str, Any]:
    """LOXBRIDGE_BACKUP__SCHEDULE=04:00, LOXBRIDGE_MINISERVERS__0__URL=... -> {'backup.schedule': ...}"""
    out = {}
    for k, v in os.environ.items():
        if k.startswith(ENV_PREFIX):
            path = ".".join(k[len(ENV_PREFIX) :].lower().split("__"))
            try:
                out[path] = json.loads(v)
            except json.JSONDecodeError:
                out[path] = v
    return out


class Store:
    def __init__(self, config_dir: Path):
        config_dir.mkdir(parents=True, exist_ok=True)
        key, self.generated_key = load_secret_key(config_dir)
        self.fernet = Fernet(key)
        self.session_secret = hashlib.sha256(b"session:" + key).hexdigest()
        self.db = sqlite3.connect(config_dir / "loxbridge.db", check_same_thread=False, isolation_level=None)
        self.db.row_factory = sqlite3.Row
        self.lock = threading.RLock()
        self._migrate()
        self.overrides = env_overrides()
        self.listeners: list[Callable[[Config, Config], Any]] = []
        self.config = self._load()

    def _migrate(self) -> None:
        version = self.db.execute("PRAGMA user_version").fetchone()[0]
        for i, sql in enumerate(MIGRATIONS[version:], start=version + 1):
            self.db.executescript(f"BEGIN; {sql}; PRAGMA user_version = {i}; COMMIT;")

    def _raw(self) -> dict:
        row = self.db.execute("SELECT data FROM config WHERE id = 1").fetchone()
        return json.loads(row["data"]) if row else dump_config(Config(), self.fernet)

    def _load(self) -> Config:
        data = decrypt_tree(self._raw(), self.fernet)
        for path, value in self.overrides.items():
            try:
                _set_path(data, path.split("."), value)
            except (KeyError, IndexError, ValueError, TypeError):
                log.warning("ignoring env override for unknown setting %s", path)
        return Config.model_validate(data)

    @property
    def locked(self) -> set[str]:
        return set(self.overrides)

    def save(self, cfg: Config) -> Config:
        """Validate, persist and notify listeners. Env-locked values keep their stored value in the DB."""
        with self.lock:
            cfg = Config.model_validate(cfg.model_dump())
            data = dump_config(cfg, self.fernet)
            old_raw = self._raw()
            for path in self.overrides:
                try:
                    _set_path(data, path.split("."), _get_path(old_raw, path.split(".")))
                except (KeyError, IndexError, ValueError, TypeError):
                    pass
            self.db.execute("INSERT OR REPLACE INTO config (id, data) VALUES (1, ?)", (json.dumps(data),))
            old, self.config = self.config, self._load()
        for fn in self.listeners:
            fn(old, self.config)
        return self.config

    # --- users / tokens ---------------------------------------------------------------------------
    def user_hash(self, username: str) -> str | None:
        row = self.db.execute("SELECT password_hash FROM users WHERE username = ?", (username,)).fetchone()
        return row["password_hash"] if row else None

    def set_user(self, username: str, password_hash: str) -> None:
        with self.lock:
            self.db.execute("DELETE FROM users")  # single admin account
            self.db.execute("INSERT INTO users VALUES (?, ?)", (username, password_hash))

    def has_user(self) -> bool:
        return self.db.execute("SELECT 1 FROM users").fetchone() is not None

    def create_token(self, name: str) -> str:
        token = "lxb_" + secrets.token_urlsafe(32)
        h = hashlib.sha256(token.encode()).hexdigest()
        self.db.execute("INSERT INTO api_tokens (name, token_hash, created) VALUES (?, ?, ?)", (name, h, time.time()))
        return token

    def check_token(self, token: str) -> bool:
        h = hashlib.sha256(token.encode()).hexdigest()
        cur = self.db.execute("UPDATE api_tokens SET last_used = ? WHERE token_hash = ?", (time.time(), h))
        return cur.rowcount == 1

    def tokens(self) -> list[dict]:
        return [dict(r) for r in self.db.execute("SELECT id, name, created, last_used FROM api_tokens")]

    def delete_token(self, token_id: int) -> None:
        self.db.execute("DELETE FROM api_tokens WHERE id = ?", (token_id,))

    # --- job history ------------------------------------------------------------------------------
    def record_job(self, job: dict) -> None:
        with self.lock:
            self.db.execute(
                "INSERT OR REPLACE INTO jobs VALUES (?, ?, ?, ?, ?, ?, ?)",
                (job["id"], job["kind"], job.get("target"), job["started"], job.get("finished"),
                 job["status"], json.dumps(job.get("summary"))),
            )
            self.db.execute("DELETE FROM jobs WHERE id NOT IN (SELECT id FROM jobs ORDER BY started DESC LIMIT 100)")

    def jobs(self, kind: str | None = None, target: str | None = None, limit: int = 100) -> list[dict]:
        q, args = "SELECT * FROM jobs WHERE 1", list[str]()
        if kind:
            q, args = q + " AND kind = ?", [*args, kind]
        if target:
            q, args = q + " AND target = ?", [*args, target]
        rows = self.db.execute(q + " ORDER BY started DESC LIMIT ?", (*args, limit))
        return [{**dict(r), "summary": json.loads(r["summary"] or "null")} for r in rows]

    # --- history import progress ------------------------------------------------------------------
    def history_progress(self, ms: str) -> dict[str, int]:
        rows = self.db.execute("SELECT file, last_ts FROM history_progress WHERE miniserver = ?", (ms,))
        return {r["file"]: r["last_ts"] for r in rows}

    def set_history_progress(self, ms: str, file: str, last_ts: int) -> None:
        with self.lock:
            self.db.execute("INSERT OR REPLACE INTO history_progress VALUES (?, ?, ?)", (ms, file, last_ts))
