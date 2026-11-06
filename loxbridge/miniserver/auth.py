"""Token auth helpers, following Loxone's "Communicating with the Miniserver".

Flow (done by ws.py over the WebSocket):
  getPublicKey -> keyexchange(RSA(aeskey:iv)) -> getkey2/<user> -> enc(getjwt/<hash>/<user>/...)
"""

import base64
import hashlib
import hmac
import os
import secrets
import urllib.parse
from dataclasses import dataclass

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import padding as apad
from cryptography.hazmat.primitives.asymmetric.rsa import RSAPublicKey
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

PERMISSION_WEB = 2  # short lived; used for the bridge WebSocket, refreshed while connected
PERMISSION_APP = 4  # weeks; used for HTTPS backup requests and cached per process
EPOCH_2009 = 1230768000  # 2009-01-01T00:00:00Z, Loxone time base
REFRESH_BEFORE = 86400  # re-acquire an HTTP token when less than a day is left


@dataclass(frozen=True)
class Token:
    token: str
    valid_until: float  # unix seconds
    alg: str


# ponytail: in-memory per process (one token per Miniserver user while the server runs). A CLI run acquires a
# fresh one; persist tokens encrypted in the settings DB if the Miniserver's token list ever fills up.
TOKENS: dict[tuple[str, str], Token] = {}


def client_uuid(base: str, user: str) -> str:
    """Stable per Miniserver and user so the Miniserver doesn't collect a new client entry per token."""
    h = hashlib.sha256(f"loxbridge|{base}|{user}".encode()).hexdigest()
    return f"{h[:8]}-{h[8:12]}-{h[12:16]}-{h[16:32]}"


def load_public_key(text: str) -> RSAPublicKey:
    # the Miniserver labels its public key as a certificate
    pem = text.replace("CERTIFICATE", "PUBLIC KEY").strip()
    if "\n" not in pem:  # single line: re-wrap the base64 body
        body = pem.split("-----")[2]
        pem = "-----BEGIN PUBLIC KEY-----\n" + body + "\n-----END PUBLIC KEY-----"
    key = serialization.load_pem_public_key(pem.encode())
    assert isinstance(key, RSAPublicKey)
    return key


def _hash(alg: str):
    return hashlib.sha256 if alg.upper() == "SHA256" else hashlib.sha1


def password_hash(user: str, password: str, key_hex: str, salt: str, alg: str = "SHA1") -> str:
    h = _hash(alg)
    pw = h(f"{password}:{salt}".encode()).hexdigest().upper()
    return hmac.new(bytes.fromhex(key_hex), f"{user}:{pw}".encode(), h).hexdigest()


def token_hash(token: str, key_hex: str, alg: str = "SHA1") -> str:
    return hmac.new(bytes.fromhex(key_hex), token.encode(), _hash(alg)).hexdigest()


class Session:
    """AES session used to encrypt commands (jdev/sys/enc/...)."""

    def __init__(self, public_key: RSAPublicKey):
        self.key = os.urandom(32)
        self.iv = os.urandom(16)
        raw = f"{self.key.hex()}:{self.iv.hex()}".encode()
        self.session_key = base64.b64encode(public_key.encrypt(raw, apad.PKCS1v15())).decode()

    def encrypt_command(self, cmd: str) -> str:
        plain = f"salt/{secrets.token_hex(2)}/{cmd}".encode() + b"\0"
        plain += b"\0" * (-len(plain) % 16)  # zero byte padding
        enc = Cipher(algorithms.AES(self.key), modes.CBC(self.iv)).encryptor()
        cipher = base64.b64encode(enc.update(plain) + enc.finalize()).decode()
        return "jdev/sys/enc/" + urllib.parse.quote(cipher, safe="")


def decrypt_command(key: bytes, iv: bytes, cipher_b64: str) -> str:
    """Inverse of Session.encrypt_command (used by the mock Miniserver and tests)."""
    dec = Cipher(algorithms.AES(key), modes.CBC(iv)).decryptor()
    plain = dec.update(base64.b64decode(urllib.parse.unquote(cipher_b64))) + dec.finalize()
    text = plain.rstrip(b"\0").decode()
    return text.split("/", 2)[2] if text.startswith("salt/") else text
