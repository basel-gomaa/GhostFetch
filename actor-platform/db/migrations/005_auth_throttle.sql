-- Sign-in and sign-up throttling (security review H-02, 2026-10-23).
--
-- One row per thing being counted: `ip:<address>` for request-rate limits on
-- the public auth routes, `user:<identifier>` for consecutive failed logins.
-- The identifier is what the person TYPED (lower-cased), deliberately not a
-- foreign key to users: an unknown name is counted and locked exactly like a
-- real one, so the throttle cannot be used to discover who has an account.
--
-- In Postgres rather than in API memory so a lockout survives a restart and
-- holds across several API processes.
CREATE TABLE auth_throttle (
    key           text PRIMARY KEY,
    hits          int NOT NULL DEFAULT 0,
    window_start  timestamptz NOT NULL DEFAULT now(),
    failures      int NOT NULL DEFAULT 0,
    lockouts      int NOT NULL DEFAULT 0,
    locked_until  timestamptz
);
