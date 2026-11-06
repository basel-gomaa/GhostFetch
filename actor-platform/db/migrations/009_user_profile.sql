-- A profile a person can edit, separate from how they sign in.
--
-- display_name is what the console shows ("Basel Gomaa"); username stays the
-- sign-in identifier and is never changed by editing the profile. NULL means
-- "not set", and the console falls back to the username. The CHECK repeats the
-- API's length rule so a write that bypasses the API cannot store more.
ALTER TABLE users
    ADD COLUMN display_name text
        CHECK (display_name IS NULL OR char_length(display_name) BETWEEN 1 AND 50);

-- The profile photo, in its OWN table rather than a users column: users is read
-- with SELECT * on every sign-in and every /auth/me, and an image there would
-- ride along on all of them. One row per user, removed with the user.
--
-- Only raster types are accepted (never SVG, which can carry script), and the
-- size is capped here as well as in the API.
CREATE TABLE user_avatars (
    user_id      uuid        PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
    image        bytea       NOT NULL CHECK (octet_length(image) BETWEEN 1 AND 262144),
    content_type text        NOT NULL CHECK (content_type IN ('image/png', 'image/jpeg', 'image/webp')),
    updated_at   timestamptz NOT NULL DEFAULT now()
);
