"""Synchronous Miniserver HTTP client: /dev/fslist, /dev/fsget, jdev commands."""

import hashlib
import json
import logging
import time
import urllib.parse
from collections.abc import Callable
from pathlib import Path

import httpx

from . import auth
from .listing import Entry, parse_listing

log = logging.getLogger(__name__)

_REDIRECT = "redirected (Remote Connect / dns.loxonecloud.com URL?): use the Miniserver's LAN address"
_HINTS = {
    301: _REDIRECT, 302: _REDIRECT, 307: _REDIRECT, 308: _REDIRECT,
    401: "wrong user or password",
    403: "user lacks rights (needs 'FTP' / file access for backups)",
    404: "not found",
    500: "Miniserver internal error",
}


class MiniserverError(Exception):
    pass


def explain(status: int) -> str:
    return f"{status} from Miniserver: {_HINTS.get(status, 'unexpected response')}"


class MiniserverHTTP:
    def __init__(
        self,
        url: str,
        user: str,
        password: str,
        verify_tls: bool = False,
        retries: int = 3,
        delay: float = 0.0,
        timeout: float = 30.0,
        auth_mode: str = "token",
    ):
        self.base = url.rstrip("/")
        self.user, self.password = user, password
        self.retries = retries
        self.delay = delay
        self.token_mode = auth_mode == "token"
        self.client = httpx.Client(
            auth=None if self.token_mode else httpx.BasicAuth(user, password),
            verify=verify_tls,
            timeout=timeout,
        )

    def close(self) -> None:
        self.client.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    def _plain(self, path: str) -> str:
        return f"{self.base}/{urllib.parse.quote(path.lstrip('/'), safe='/')}"

    def _ll(self, path: str) -> dict:
        """Unauthenticated jdev call. The LL code wins over the HTTP status (200 can carry LL code 401)."""
        try:
            r = self.client.get(self._plain(path))
        except httpx.HTTPError as e:
            raise MiniserverError(f"connection to Miniserver failed: {e or type(e).__name__}") from e
        try:
            ll = r.json().get("LL", {})
        except ValueError:
            ll = {}
        code = int(str(ll.get("Code", ll.get("code", r.status_code))))
        if r.status_code != 200 or code != 200:
            raise MiniserverError(explain(code if r.status_code == 200 else r.status_code))
        return ll

    def _token(self, force: bool = False) -> auth.Token:
        # the password is part of the key: a changed password (or a Test with a wrong one) must not reuse a token
        key = (self.base, self.user, hashlib.sha256(self.password.encode()).hexdigest())
        tok = auth.TOKENS.get(key)
        if tok and not force and tok.valid_until - time.time() > auth.REFRESH_BEFORE:
            return tok
        user = urllib.parse.quote(self.user, safe="")
        k = self._ll(f"jdev/sys/getkey2/{user}")["value"]
        alg = k.get("hashAlg") or "SHA1"
        h = auth.password_hash(self.user, self.password, k["key"], k["salt"], alg)
        v = self._ll(f"jdev/sys/getjwt/{h}/{user}/{auth.PERMISSION_APP}/"
                     f"{auth.client_uuid(self.base, self.user)}/loxbridge")["value"]
        tok = auth.Token(v["token"], auth.EPOCH_2009 + int(v.get("validUntil", 0)), alg)
        auth.TOKENS[key] = tok
        return tok

    def _url(self, path: str, force_token: bool = False) -> str:
        url = self._plain(path)
        if self.token_mode:
            tok = self._token(force_token)
            # the getkey2 key is single use: every signed request needs a fresh one
            k = self._ll(f"jdev/sys/getkey2/{urllib.parse.quote(self.user, safe='')}")["value"]
            q = urllib.parse.urlencode({"autht": auth.token_hash(tok.token, k["key"], tok.alg), "user": self.user})
            url += ("&" if "?" in url else "?") + q
        return url

    def _request(self, path: str, stream_to: Path | None = None) -> bytes:
        try:
            return self._request_once(path, stream_to, force_token=False)
        except MiniserverError as e:
            if not (self.token_mode and "401" in str(e)):
                raise
            # the token may be gone on the Miniserver although validUntil looks fine: get a new one, retry once
            return self._request_once(path, stream_to, force_token=True)

    def _request_once(self, path: str, stream_to: Path | None, force_token: bool) -> bytes:
        last: Exception | None = None
        for attempt in range(self.retries):
            if self.delay:
                time.sleep(self.delay)
            try:
                url = self._url(path, force_token and attempt == 0)
                if stream_to is None:
                    r = self.client.get(url)
                    self._check(r)
                    return r.content
                with self.client.stream("GET", url) as r:
                    self._check(r)
                    with open(stream_to, "wb") as f:
                        for chunk in r.iter_bytes(65536):
                            f.write(chunk)
                return b""
            except MiniserverError as e:
                if "401" in str(e) or "403" in str(e) or "404" in str(e):
                    raise  # retrying won't help
                last = e
            except httpx.HTTPError as e:
                last = MiniserverError(f"connection to Miniserver failed: {e or type(e).__name__}")
            if attempt + 1 < self.retries:
                time.sleep(min(2**attempt, 10))
        assert last
        raise last

    @staticmethod
    def _check(r: httpx.Response) -> None:
        if r.status_code != 200:
            raise MiniserverError(explain(r.status_code))

    def fslist(self, path: str = "/") -> list[Entry]:
        p = path.strip("/")
        return parse_listing(self._request(f"dev/fslist/{p}/" if p else "dev/fslist/").decode(errors="replace"))

    def fsget(self, path: str, dest: Path) -> None:
        self._request(f"dev/fsget/{path.strip('/')}", stream_to=dest)

    def get(self, path: str) -> bytes:
        return self._request(path)

    def jdev(self, cmd: str):
        data = json.loads(self._request(f"jdev/{cmd.removeprefix('jdev/')}"))
        ll = data.get("LL", {})
        code = str(ll.get("Code", ll.get("code", "200")))
        if code != "200":
            raise MiniserverError(explain(int(code)))
        return ll.get("value")

    def walk(self, root: str = "/", exclude: Callable[[str], bool] = lambda p: False):
        """Yield (path, Entry) for every file, depth first, directories read one at a time."""
        stack = [root.strip("/")]
        while stack:
            d = stack.pop()
            try:
                entries = self.fslist(d)
            except MiniserverError as err:
                # Loxone refuses some system folders even to admins (sys/internal, measured on 17.3).
                # A refused root still fails: that's a user without file rights.
                if not d or "403" not in str(err):
                    raise
                log.info("skipping %s: the Miniserver doesn't allow listing it", d)
                continue
            for e in entries:
                p = f"{d}/{e.name}" if d else e.name
                if exclude(p):
                    continue
                if e.is_dir:
                    stack.append(p)
                else:
                    yield p, e
