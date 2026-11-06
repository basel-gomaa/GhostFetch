-- Invite-only signup (security re-review: "no rate limiting, lockout or email
-- verification"). With ACTOR_PLATFORM_SIGNUP=invite, the default, a new account
-- needs a single-use invite an administrator created. See api/invites.py.
--
-- Only the sha256 of the token is stored, like an API key: the link is shown
-- once, to the admin who made it, and this table cannot give it back. Neither
-- can anyone who reads a backup of it.
CREATE TABLE signup_invites (
    id          uuid        PRIMARY KEY DEFAULT gen_random_uuid(),
    token_hash  bytea       NOT NULL UNIQUE,
    -- Free text for the admin ("for Sara, marketing"). Never shown to the
    -- person signing up.
    note        text,
    -- SET NULL, not CASCADE: deleting the admin who made an invite must not
    -- erase the record of who joined through it.
    created_by  uuid        REFERENCES users(id) ON DELETE SET NULL,
    created_at  timestamptz NOT NULL DEFAULT now(),
    expires_at  timestamptz NOT NULL,
    -- used_at is what makes an invite single-use. used_by may later become
    -- NULL (that user deleted) while used_at stays: the invite stays spent.
    used_at     timestamptz,
    used_by     uuid        REFERENCES users(id) ON DELETE SET NULL,
    revoked_at  timestamptz,
    CHECK (expires_at > created_at)
);

CREATE INDEX signup_invites_newest ON signup_invites (created_at DESC);
