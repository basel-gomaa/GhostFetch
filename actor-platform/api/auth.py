#!/usr/bin/env python3
"""API key authentication and per-key scopes for the HTTP surface.

The platform became network-reachable when the API landed: anything that can
reach the port can start containers, and `GET /actors` advertises exactly what
to send. This closes that, and limits what a given key may do once through.

SECURE BY DEFAULT. Three modes, and the third is the point:

    ENFORCED      keys are configured; a valid one is required
    DISABLED      ACTOR_PLATFORM_AUTH=off -- running open, deliberately
    UNCONFIGURED  no keys and no opt-out -- every request is refused

Forgetting to configure keys therefore fails loudly instead of quietly serving
an execution engine to the internet.

SCOPES. A key carries what it may do, so a dashboard that only watches runs
cannot start them:

    export ACTOR_PLATFORM_API_KEYS="apk_aaa:read,apk_bbb:read+run,apk_ccc"

    read   inspect actors, runs, inputs, outputs and logs
    run    submit new runs

A key with no scope suffix gets everything, which is what every key granted
before scopes existed -- so adding this changed no existing deployment.

    export ACTOR_PLATFORM_API_KEYS="$(python3 api/auth.py)"
    python3 api/auth.py read            # mint a read-only entry

WHAT THIS DOES NOT PROTECT: the Registry and Queue are files on disk, and
cli/runs.py reads them directly. Authentication guards the HTTP surface, not
the machine -- anyone with filesystem access has everything regardless.
"""

from __future__ import annotations

import os
import secrets
import sys
from dataclasses import dataclass
from enum import Enum

API_KEYS_ENV = "ACTOR_PLATFORM_API_KEYS"
AUTH_MODE_ENV = "ACTOR_PLATFORM_AUTH"

# Prefixed so a leaked key is recognisable as one -- in a log, a bug report, or
# a secret scanner -- rather than looking like any other opaque string.
KEY_PREFIX = "apk_"
KEY_BYTES = 32

BEARER = "bearer"

# The console's session cookie. Declared HERE, in the module with no
# dependencies, because api/main.py needs the name to read the cookie and must
# not pull in psycopg and argon2 to learn a string -- a deployment with no
# database has no accounts and should not need their libraries installed.
SESSION_COOKIE_NAME = "ap_session"

# "apk_abc:read+run" -- comma separates entries, so the scope separators must
# not be a comma. Both characters are outside token_urlsafe's alphabet, so they
# can never appear inside a generated key.
SCOPE_SEPARATOR = ":"
SCOPE_JOINER = "+"
WILDCARD = "*"


class Scope(str, Enum):
    """What a key may do. Deliberately two.

    The distinction that matters is observer versus operator: reading a run is
    harmless, starting one spends CPU and reaches the network. Finer grants
    (per-Actor, per-run) would need a real key store, not an environment
    variable.
    """

    READ = "read"
    RUN = "run"


ALL_SCOPES = frozenset(Scope)


class AuthMode(str, Enum):
    ENFORCED = "enforced"
    DISABLED = "disabled"
    UNCONFIGURED = "unconfigured"


class AuthError(Exception):
    """Base: the request may not proceed."""


class AuthNotConfigured(AuthError):
    """No keys and no explicit opt-out. The operator has not decided yet."""


class CredentialsMissing(AuthError):
    """No Authorization header, or one this API does not speak."""


class CredentialsInvalid(AuthError):
    """A well-formed credential that is not one of ours."""


class InsufficientScope(AuthError):
    """A valid key that is not allowed to do this.

    Distinct from the others on purpose: the caller is known, so the answer is
    403 rather than 401, and no better credential is going to help.
    """

    def __init__(self, required: Scope, granted: frozenset[Scope]) -> None:
        super().__init__(
            f"this key does not have the '{required.value}' scope "
            f"(it has: {', '.join(sorted(s.value for s in granted)) or 'none'})"
        )
        self.required = required
        self.granted = granted


def generate_key() -> str:
    """Mint a key with enough entropy that nobody is tempted to invent one."""
    return f"{KEY_PREFIX}{secrets.token_urlsafe(KEY_BYTES)}"


def parse_scopes(text: str) -> frozenset[Scope]:
    """Read 'read+run', or '*'. Unknown names raise rather than being ignored.

    Silently dropping a scope nobody recognises is how a key ends up with less
    access than intended, or a typo grants nothing and the failure surfaces
    somewhere far away.
    """
    text = text.strip()
    if not text or text == WILDCARD:
        return ALL_SCOPES

    scopes = set()
    for part in text.split(SCOPE_JOINER):
        name = part.strip().lower()
        if not name:
            continue
        if name == WILDCARD:
            return ALL_SCOPES
        try:
            scopes.add(Scope(name))
        except ValueError:
            valid = ", ".join(s.value for s in Scope)
            raise ValueError(f"unknown scope {name!r}; valid scopes are: {valid}") from None

    if not scopes:
        raise ValueError(f"no scopes given in {text!r}")
    return frozenset(scopes)


@dataclass(frozen=True)
class Principal:
    """Who the request turned out to be, and what they may do.

    SCOPES AND IDENTITY ARE SEPARATE, and keeping them so is deliberate. A scope
    answers "which ACTIONS may this caller take" (read a run, start one); the
    identity fields answer "WHOSE DATA is this caller looking at". Conflating
    them -- adding an `admin` scope -- would have meant either that `read+run`
    stopped meaning "everything" or that the platform started accepting `admin`
    as a scope name, and this module's own tests pin both of those. Two scopes,
    as the Scope docstring insists.
    """

    scopes: frozenset[Scope]
    # The signed-in user, when the caller is one. None for an operator key
    # configured in the environment, which belongs to the deployment rather
    # than to any account.
    user_id: str | None = None
    # May see every run, including the ownerless ones that predate accounts.
    # An operator property, never granted to a signed-up user by default.
    is_admin: bool = False

    def has(self, scope: Scope) -> bool:
        return scope in self.scopes

    def require(self, scope: Scope) -> None:
        if not self.has(scope):
            raise InsufficientScope(scope, self.scopes)

    @property
    def scope_names(self) -> list[str]:
        return sorted(scope.value for scope in self.scopes)


ANONYMOUS = Principal(scopes=frozenset())
# Authentication switched off means nothing is hidden, ownership included.
UNRESTRICTED = Principal(scopes=ALL_SCOPES, is_admin=True)


@dataclass(frozen=True)
class AuthPolicy:
    """Who may call the API, and for what. Injected, so tests never depend on
    the environment."""

    # A tuple of (key, scopes) rather than a dict: lookup walks every entry with
    # a constant-time comparison, and a dict would compare keys with ordinary
    # equality on the way to its answer.
    entries: tuple[tuple[str, frozenset[Scope]], ...] = ()
    mode: AuthMode = AuthMode.UNCONFIGURED

    @classmethod
    def from_env(cls, environ: dict[str, str] | None = None) -> AuthPolicy:
        env = environ if environ is not None else dict(os.environ)

        entries: list[tuple[str, frozenset[Scope]]] = []
        for part in env.get(API_KEYS_ENV, "").split(","):
            part = part.strip()
            if not part:
                continue
            key, separator, scope_text = part.partition(SCOPE_SEPARATOR)
            key = key.strip()
            if not key:
                continue
            # No suffix means everything, which is what every key granted before
            # scopes existed. Adding them broke no running deployment.
            entries.append((key, parse_scopes(scope_text) if separator else ALL_SCOPES))

        opted_out = env.get(AUTH_MODE_ENV, "").strip().lower() in {"off", "none", "disabled"}

        if entries:
            # Keys win over the opt-out: configuring both is a mistake, and the
            # safe reading of a mistake is the stricter one.
            return cls(entries=tuple(entries), mode=AuthMode.ENFORCED)
        if opted_out:
            return cls(mode=AuthMode.DISABLED)
        return cls(mode=AuthMode.UNCONFIGURED)

    @classmethod
    def for_keys(cls, keys, scopes: frozenset[Scope] | None = None) -> AuthPolicy:
        """An enforcing policy over explicit keys. For tests and embedding.

        Grants everything unless told otherwise, matching a bare env-var key.
        """
        granted = ALL_SCOPES if scopes is None else frozenset(scopes)
        entries = tuple((key, granted) for key in keys)
        if not entries:
            raise ValueError("an enforcing policy needs at least one key")
        return cls(entries=entries, mode=AuthMode.ENFORCED)

    @classmethod
    def disabled(cls) -> AuthPolicy:
        return cls(mode=AuthMode.DISABLED)

    @property
    def enforced(self) -> bool:
        return self.mode is AuthMode.ENFORCED

    @property
    def keys(self) -> frozenset[str]:
        return frozenset(key for key, _ in self.entries)

    def scopes_for(self, presented: str) -> frozenset[Scope] | None:
        """The scopes this credential grants, or None if it is not one of ours.

        secrets.compare_digest, not a plain comparison: an ordinary one returns
        as soon as it finds a differing byte, and that timing difference is
        enough to recover a key one character at a time. Every entry is walked
        even after a match, so the time taken does not depend on which key was
        presented.
        """
        found: frozenset[Scope] | None = None
        for key, scopes in self.entries:
            if secrets.compare_digest(presented, key):
                found = scopes
        return found

    def matches(self, presented: str) -> bool:
        return self.scopes_for(presented) is not None

    def authenticate(self, authorization: str | None) -> Principal:
        """Identify the caller, or raise. Never echoes the credential.

        A rejection message that quotes what was sent ends up in logs and error
        trackers, which is how a key that was merely wrong becomes a key that
        is also written down.
        """
        if self.mode is AuthMode.UNCONFIGURED:
            raise AuthNotConfigured(
                f"no API keys are configured. Set {API_KEYS_ENV} to require them, "
                f"or {AUTH_MODE_ENV}=off to run without authentication on purpose"
            )

        if self.mode is AuthMode.DISABLED:
            return UNRESTRICTED

        if not authorization:
            raise CredentialsMissing("an Authorization header is required")

        scheme, _, credential = authorization.partition(" ")
        if scheme.strip().lower() != BEARER or not credential.strip():
            raise CredentialsMissing("expected an 'Authorization: Bearer <key>' header")

        scopes = self.scopes_for(credential.strip())
        if scopes is None:
            raise CredentialsInvalid("the API key is not valid")

        # A key configured in the environment is the DEPLOYMENT's credential,
        # not a user's: whoever set it has filesystem access to the machine and
        # can read every run directly anyway. So it sees every run, while still
        # being held to whatever scopes it was granted -- a `:read` operator key
        # can look at everything and start nothing.
        return Principal(scopes=scopes, is_admin=True)


def main(argv: list[str] | None = None) -> int:
    """Mint a key. Any arguments are scopes: `python3 api/auth.py read`."""
    argv = list(sys.argv[1:] if argv is None else argv)
    key = generate_key()

    if argv:
        try:
            scopes = parse_scopes(SCOPE_JOINER.join(argv))
        except ValueError as exc:
            print(f"api/auth.py: {exc}", file=sys.stderr)
            return 2
        entry = f"{key}{SCOPE_SEPARATOR}{SCOPE_JOINER.join(sorted(s.value for s in scopes))}"
    else:
        entry = key

    print(entry)
    print(f'\n  export {API_KEYS_ENV}="{entry}"', file=sys.stderr)
    print(f"  curl -H 'Authorization: Bearer {key}' localhost:8000/actors", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
