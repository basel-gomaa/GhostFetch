-- Items an Actor produced while it ran: the optional dataset part of the
-- contract (shared/models.py: DATASET_FILE).
--
-- THE FIRST TABLE THAT HOLDS SCRAPED DATA, which 001's header deliberately
-- kept out of the database. That reasoning still stands for output.json -- one
-- document per run, served whole -- and it is still on disk. What changed is a
-- need 001 did not have: results reachable WHILE a run goes, and a partial
-- result kept when a run dies. A file read at exit gives neither.
--
-- OPAQUE BY CONSTRUCTION. `collection` and `item_key` are whatever the Actor
-- wrote; the platform groups and pages by them and interprets neither, so no
-- column here names a site or a kind of record.
--
-- `seq` is the item's LINE NUMBER in dataset.jsonl, and the primary key rests
-- on it: re-reading the same file yields the same (run_id, seq) pairs, which is
-- what makes ingestion idempotent without trusting an Actor's own key to be
-- unique. `item_key` is therefore NOT unique -- two lines may share one.
--
-- ON DELETE CASCADE: items are part of the run, and go with it.
CREATE TABLE run_items (
    run_id      text        NOT NULL REFERENCES runs(run_id) ON DELETE CASCADE,
    seq         bigint      NOT NULL CHECK (seq > 0),
    collection  text        NOT NULL,
    item_key    text,
    data        jsonb       NOT NULL,
    created_at  timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (run_id, seq)
);

-- The API's filtered page: one collection of one run, in production order.
CREATE INDEX run_items_by_collection ON run_items (run_id, collection, seq);
