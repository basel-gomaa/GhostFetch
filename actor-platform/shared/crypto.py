"""Authenticated encryption for credentials the platform stores at rest.

One thing is encrypted here today: a user's social-media session (a Playwright
storage_state document holding live login cookies). It is the only data in this
system whose disclosure would compromise an account belonging to someone else,
so it never touches the filesystem in plaintext -- not the run directory, not
the queue, not a log. It lives as ciphertext in a database column and exists in
the clear only inside the process that is about to use it.

    seal(plaintext, aad)          -> (ciphertext, nonce)
    open_(ciphertext, nonce, aad) -> plaintext

AES-256-GCM, because the requirement is AUTHENTICATED encryption: a session that
has been tampered with must fail to decrypt rather than decrypt into something
else. GCM gives that for free; AES-CBC or Fernet-without-care would not.

THE AAD IS NOT OPTIONAL, and it is the subtle half. Additional Authenticated
Data is bound into the tag but not stored, so a ciphertext only opens when the
caller supplies the same value it was sealed with. We pass
`f"{social_account_id}|{platform}"`, which makes a stolen row unusable in any
other row: copying user A's ciphertext into user B's record produces a
decryption FAILURE rather than user B silently scraping as user A. Without it,
the column is a set of interchangeable blobs and row-level integrity rests
entirely on the database being untampered with.

THE KEY LIVES IN THE ENVIRONMENT, NEVER IN THE DATABASE. Keeping it beside the
ciphertext it protects would mean a single database dump is a full compromise --
the one property this module exists to deny. ACTOR_PLATFORM_SECRET_KEY holds 32
base64 bytes; generate one with:

    python3 shared/crypto.py

KEY ROTATION is why every sealed row carries a `key_version`. The environment
may name several keys (`v1:<b64>,v2:<b64>`); the HIGHEST version seals, and any
named version can open. So rotation is: add a key, re-seal rows in the
background, drop the old key -- never a flag day, and never a window where some
rows cannot be read.
"""

from __future__ import annotations

import base64
import binascii
import os
import secrets

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.hashes import SHA256
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

KEY_ENV = "ACTOR_PLATFORM_SECRET_KEY"

KEY_BYTES = 32  # AES-256
NONCE_BYTES = 12  # GCM's standard nonce size; 96 bits is the fast, safe path.


class CryptoError(RuntimeError):
    """The platform's encryption is not usable as configured."""


class KeyNotConfigured(CryptoError):
    """No key in the environment. Fatal by design -- see KeyRing.from_env."""


class DecryptionFailed(CryptoError):
    """Wrong key, wrong AAD, or the ciphertext was altered.

    Deliberately does not say which. A caller cannot act differently on the
    three, and an error that distinguishes them is an oracle.
    """


def generate_key() -> str:
    """A fresh base64 key, for an operator to paste into the environment."""
    return base64.b64encode(secrets.token_bytes(KEY_BYTES)).decode("ascii")


def _decode_key(raw: str, *, label: str) -> bytes:
    try:
        key = base64.b64decode(raw.strip(), validate=True)
    except (binascii.Error, ValueError) as exc:
        raise CryptoError(f"{label} is not valid base64: {exc}") from None
    if len(key) != KEY_BYTES:
        raise CryptoError(
            f"{label} must decode to {KEY_BYTES} bytes, got {len(key)}. "
            f"Generate one with: python3 shared/crypto.py"
        )
    return key


class KeyRing:
    """The keys this deployment can seal and open with.

    Sealing always uses the highest version; opening uses whichever version the
    row records. That asymmetry is the whole of key rotation.
    """

    def __init__(self, keys: dict[int, bytes]) -> None:
        if not keys:
            raise KeyNotConfigured("a KeyRing needs at least one key")
        self._keys = dict(keys)
        self._current = max(keys)

    @property
    def current_version(self) -> int:
        return self._current

    @property
    def versions(self) -> list[int]:
        return sorted(self._keys)

    @classmethod
    def from_env(cls, environ: dict[str, str] | None = None) -> KeyRing:
        """Build the ring from ACTOR_PLATFORM_SECRET_KEY.

        Accepts a bare base64 key (implicitly version 1) or a versioned list,
        `v1:<b64>,v2:<b64>`. The bare form is what a first-time operator will
        paste, and making them learn the versioned syntax before they can start
        the platform would be a poor trade for a feature they do not need yet.

        RAISES when unset. This is the deliberate mirror of the API refusing to
        serve with authentication unconfigured: a platform that quietly ran
        without encryption would write plaintext sessions and nobody would find
        out until a disclosure.
        """
        env = os.environ if environ is None else environ
        raw = (env.get(KEY_ENV) or "").strip()
        if not raw:
            raise KeyNotConfigured(
                f"{KEY_ENV} is not set, so stored sessions could not be encrypted. "
                f"Generate a key with: python3 shared/crypto.py"
            )

        keys: dict[int, bytes] = {}
        for part in (p.strip() for p in raw.split(",") if p.strip()):
            version, sep, material = part.partition(":")
            if not sep:
                keys[1] = _decode_key(part, label=KEY_ENV)
                continue
            if not (version.startswith("v") and version[1:].isdigit()):
                raise CryptoError(
                    f"{KEY_ENV} entry {version!r} is not a version like 'v2'"
                )
            keys[int(version[1:])] = _decode_key(material, label=f"{KEY_ENV} {version}")
        return cls(keys)

    # --- the operations -----------------------------------------------------

    def seal(self, plaintext: str, aad: str) -> tuple[bytes, bytes, int]:
        """Encrypt. Returns (ciphertext, nonce, key_version) -- store all three.

        A fresh random nonce every call. Reusing a nonce under one key is the
        catastrophic failure mode for GCM (it leaks the keystream and the
        authentication key), which is why one is generated here rather than
        accepted from a caller who might derive it from something stable.
        """
        nonce = secrets.token_bytes(NONCE_BYTES)
        ciphertext = AESGCM(self._keys[self._current]).encrypt(
            nonce, plaintext.encode("utf-8"), aad.encode("utf-8")
        )
        return ciphertext, nonce, self._current

    def open_(self, ciphertext: bytes, nonce: bytes, aad: str, key_version: int) -> str:
        """Decrypt, or raise DecryptionFailed. Never returns garbage."""
        key = self._keys.get(key_version)
        if key is None:
            raise DecryptionFailed(
                f"this deployment has no key version {key_version} "
                f"(it has: {', '.join(f'v{v}' for v in self.versions)})"
            )
        try:
            plaintext = AESGCM(key).decrypt(nonce, ciphertext, aad.encode("utf-8"))
        except InvalidTag:
            raise DecryptionFailed(
                "stored value could not be decrypted: wrong key, or it was altered"
            ) from None
        return plaintext.decode("utf-8")

    # --- derived keys -------------------------------------------------------

    def derive(self, info: str, *, length: int = 32) -> bytes:
        """A separate key for a separate purpose, from the same secret.

        Signing console session cookies needs a key too, and using the KEK
        directly for two unrelated jobs is how one weakness becomes two. HKDF
        with a distinct `info` string gives an independent key per purpose, so
        neither use can be attacked through the other.
        """
        return HKDF(
            algorithm=SHA256(),
            length=length,
            salt=None,
            info=info.encode("utf-8"),
        ).derive(self._keys[self._current])


def session_aad(social_account_id: str, platform: str) -> str:
    """The AAD binding a sealed session to the row that owns it.

    One function so the seal and open sides cannot drift; a mismatch here does
    not corrupt anything, it just makes every session undecryptable at once.
    """
    return f"{social_account_id}|{platform}"


if __name__ == "__main__":  # pragma: no cover - operator tool
    print(generate_key())
