-- One random id per deployment (one database), used to LABEL the Docker
-- resources this deployment creates.
--
-- Why it exists: several deployments can share one Docker daemon -- the
-- development install and every throwaway database the test suites start, at
-- the very least. The login manager's profile sweep deletes a kept browser
-- profile whose owner is no longer in the users table. Judged by the database
-- alone, a TEST database would see every production profile as "owner gone"
-- and delete them all. A volume is only ever claimed as an orphan when it
-- carries THIS deployment's id, so one database can never sweep another's.
--
-- Exactly one row, enforced by the boolean primary key.
CREATE TABLE deployment (
    singleton  boolean     PRIMARY KEY DEFAULT true CHECK (singleton),
    id         uuid        NOT NULL DEFAULT gen_random_uuid(),
    created_at timestamptz NOT NULL DEFAULT now()
);

INSERT INTO deployment DEFAULT VALUES;
