-- The multi-tenant schema.
--
-- Everything the filesystem cannot answer. Run ARTIFACTS (input.json,
-- output.json, log.txt) deliberately stay on disk under storage/runs/<run_id>/:
-- they are large, append-only, and already served correctly. What moves here is
-- run METADATA, because the questions multi-tenancy asks -- "whose run is this",
-- "is this session already busy", "which accounts need reconnecting" -- are
-- queries, not directory scans.

CREATE EXTENSION IF NOT EXISTS citext;    -- case-insensitive email/username
CREATE EXTENSION IF NOT EXISTS pgcrypto;  -- gen_random_uuid() only; encryption is app-side


-- --- people ----------------------------------------------------------------

CREATE TABLE users (
    id                uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    -- citext, so Basel@gmail.com and basel@gmail.com cannot both sign up. A
    -- UNIQUE constraint over a case-SENSITIVE column would let them, and the
    -- second person would silently never receive a report.
    email             citext NOT NULL UNIQUE,
    username          citext NOT NULL UNIQUE,
    -- argon2id. Never reversible: we do not store, and cannot recover, anyone's
    -- password -- neither for this app nor, ever, for a social platform.
    password_hash     text   NOT NULL,
    email_verified_at timestamptz,
    is_admin          boolean NOT NULL DEFAULT false,
    -- Disabling is reversible and preserves the user's runs; deleting is not.
    disabled_at       timestamptz,
    created_at        timestamptz NOT NULL DEFAULT now()
);


-- Console sessions. Server-side rows rather than self-contained tokens, because
-- REVOCATION is a requirement here (logout, admin disable, password change) and
-- a stateless token cannot be revoked -- only outlived.
CREATE TABLE user_sessions (
    id           uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id      uuid NOT NULL REFERENCES users ON DELETE CASCADE,
    created_at   timestamptz NOT NULL DEFAULT now(),
    last_seen_at timestamptz NOT NULL DEFAULT now(),
    expires_at   timestamptz NOT NULL,
    revoked_at   timestamptz,
    user_agent   text
);
CREATE INDEX user_sessions_live ON user_sessions (user_id) WHERE revoked_at IS NULL;


-- API keys, for driving the platform from curl or a script.
--
-- SPLIT INTO A PUBLIC HANDLE AND A SECRET. `key_id` is indexed and not secret,
-- so authenticating is one indexed lookup followed by ONE constant-time compare.
-- Hashing the whole key instead would mean either a non-constant-time lookup or
-- a walk over every key in the platform on every request.
--
-- sha256, not argon2, and that is deliberate: the secret half is 256 bits of
-- os.urandom, so there is no low-entropy guess to slow down. argon2 is for the
-- password column above, where the input is human-chosen.
CREATE TABLE api_keys (
    id           uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    key_id       text  NOT NULL UNIQUE,
    user_id      uuid  NOT NULL REFERENCES users ON DELETE CASCADE,
    secret_hash  bytea NOT NULL,
    scopes       text[] NOT NULL,
    label        text,
    created_at   timestamptz NOT NULL DEFAULT now(),
    last_used_at timestamptz,
    revoked_at   timestamptz
);
CREATE INDEX api_keys_by_user ON api_keys (user_id) WHERE revoked_at IS NULL;


-- --- connected social accounts ---------------------------------------------

-- One row per (user, platform). Holds a LIVE LOGIN as ciphertext.
--
-- This is the most sensitive table in the system. `session_ciphertext` is
-- AES-256-GCM sealed by shared/crypto.py with AAD "<id>|<platform>", so a row
-- copied into another row's place fails to decrypt instead of silently letting
-- one user scrape as another. The key is in the environment, never here: a
-- database dump alone must not be a compromise.
CREATE TABLE social_accounts (
    id                 uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id            uuid NOT NULL REFERENCES users ON DELETE CASCADE,
    platform           text NOT NULL,
    -- The numeric account id (Facebook's c_user). NOT a credential -- it is the
    -- number in every profile URL -- and it is here so a user can see which of
    -- their accounts is connected.
    external_id        text,
    display_name       text,
    session_ciphertext bytea NOT NULL,
    session_nonce      bytea NOT NULL,
    key_version        int  NOT NULL DEFAULT 1,

    status             text NOT NULL DEFAULT 'connected'
                       CHECK (status IN ('connected', 'expired', 'revoked')),
    connected_at       timestamptz NOT NULL DEFAULT now(),
    -- Set only by a POSITIVE check. An inconclusive preflight ('unknown') must
    -- not touch it: not knowing is not evidence of health.
    last_verified_at   timestamptz,
    -- Advisory only, from the cookie jar's own expiries. Facebook decides when
    -- a session really dies; this just lets the console warn early.
    expires_at         timestamptz,
    expired_at         timestamptz,
    -- The preflight reason verbatim ('expired', 'missing', ...), so a support
    -- question is answerable without re-running anything.
    expired_reason     text,
    -- Debounce. Only the confident signal flips an account on one occurrence;
    -- everything else has to happen twice. See the auth-expiry design.
    consecutive_auth_failures int NOT NULL DEFAULT 0,

    -- One connected account per platform per user. Reconnecting UPSERTs this
    -- row rather than accumulating dead sessions beside the live one.
    UNIQUE (user_id, platform)
);
CREATE INDEX social_accounts_by_user ON social_accounts (user_id);


-- --- runs -------------------------------------------------------------------

CREATE TABLE runs (
    -- KEEPS the platform's existing id format: <date>-<time-to-microsecond>-<rand>.
    -- String order is chronological order, which the whole system relies on for
    -- "newest first" -- so it stays the primary key rather than becoming a uuid.
    run_id            text PRIMARY KEY,
    -- NULL means a run that predates multi-tenancy (the ~1798 imported ones).
    -- Visible to admins only, never to a user, because it belongs to nobody.
    owner_id          uuid REFERENCES users(id),
    actor             text NOT NULL,
    status            text NOT NULL
                      CHECK (status IN ('PENDING', 'RUNNING', 'SUCCEEDED',
                                        'FAILED', 'PLATFORM_ERROR', 'CANCELLED')),

    -- Which stored session this run used. ON DELETE SET NULL: disconnecting an
    -- account must not delete the history of what was scraped with it.
    social_account_id uuid REFERENCES social_accounts(id) ON DELETE SET NULL,
    -- What this run must not run concurrently with. Opaque to the queue.
    concurrency_key   text,

    created_at        timestamptz NOT NULL,
    started_at        timestamptz,
    finished_at       timestamptz,
    duration_seconds  double precision,
    exit_code         int,
    error             text,
    -- A POINTER to the artifacts, which stay on disk. The output is not here,
    -- for the same reason it is not in run.json: a 150-post result would make
    -- every listing parse megabytes to render summaries containing none of it.
    run_dir           text NOT NULL,
    log_lines         int  NOT NULL DEFAULT 0,
    attempts          int  NOT NULL DEFAULT 0,
    input             jsonb NOT NULL DEFAULT '{}'::jsonb,

    -- The user's own analysis instructions, appended to the built-in prompt.
    -- Not a secret, and part of what makes a run reproducible.
    ai_prompt         text,
    -- Where the report goes. Kept HERE rather than in `input` so the user's
    -- address never reaches input.json, which is served over HTTP.
    report_email      text,
    report_sent_at    timestamptz
);

-- The console's main query: this user's runs, newest first.
CREATE INDEX runs_by_owner ON runs (owner_id, run_id DESC);
CREATE INDEX runs_by_actor ON runs (actor, run_id DESC);
-- Partial: the reconciler only ever asks about unfinished runs, and they are a
-- tiny fraction of the table.
CREATE INDEX runs_unfinished ON runs (status) WHERE status IN ('PENDING', 'RUNNING');


-- --- concurrency ------------------------------------------------------------

-- At most one running scrape per social session, ACROSS PROCESSES.
--
-- The worker's in-process counter is the fast path; this is the correctness
-- backstop. It has to exist because the counter is per-process, so two worker
-- processes would otherwise both run a scrape on the same Facebook session --
-- which races the session write-back and makes the account look like a bot.
--
-- PRIMARY KEY on the account id IS the constraint: a second INSERT conflicts.
CREATE TABLE session_leases (
    social_account_id uuid PRIMARY KEY REFERENCES social_accounts ON DELETE CASCADE,
    run_id            text NOT NULL REFERENCES runs(run_id) ON DELETE CASCADE,
    -- "<host>:<pid>", the same shape jobqueue/base.py::owner_id() produces, so
    -- owner_is_dead() reaps a crashed worker's lease here with no new code.
    owner             text NOT NULL,
    acquired_at       timestamptz NOT NULL DEFAULT now()
);


-- --- interactive login sessions ---------------------------------------------

-- One row per live "connect your account" browser container.
CREATE TABLE login_sessions (
    id                 uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id            uuid NOT NULL REFERENCES users ON DELETE CASCADE,
    platform           text NOT NULL,
    container_name     text NOT NULL UNIQUE,
    -- Ephemeral host ports, bound to 127.0.0.1 only. Recorded so the API can
    -- proxy to the right container; never exposed to the browser.
    vnc_port           int  NOT NULL,
    control_port       int  NOT NULL,
    control_token_hash bytea NOT NULL,
    status             text NOT NULL
                       CHECK (status IN ('starting', 'running', 'captured',
                                         'abandoned', 'expired', 'failed')),
    created_at         timestamptz NOT NULL DEFAULT now(),
    -- Hard TTL. A login that has not finished by now is not going to.
    expires_at         timestamptz NOT NULL,
    attached_at        timestamptz,
    detached_at        timestamptz,
    finished_at        timestamptz
);

-- One live login per user, enforced by the DATABASE rather than by a check the
-- application could race. A partial index, so finished sessions accumulate
-- freely as history.
CREATE UNIQUE INDEX one_live_login_per_user ON login_sessions (user_id)
    WHERE status IN ('starting', 'running');
CREATE INDEX login_sessions_live ON login_sessions (expires_at)
    WHERE status IN ('starting', 'running');
