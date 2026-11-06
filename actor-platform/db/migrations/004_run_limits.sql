-- Per-run resource limits and the memory a run actually used
-- (security review H-03, 2026-10-23).
--
-- timeout_s / memory_mb / cpus are written by the API as what the run ASKED
-- for, then overwritten by the Worker with what it ENFORCED once it clamped the
-- request to the Actor's maximum (config/actors.json "limits"). NULL means
-- unlimited, which is what every run before this migration had.
--
-- peak_memory_mb is the highest usage the Worker sampled while the container
-- ran. It exists so the per-Actor memory defaults can be tightened from
-- measurement rather than guessed (actor-platform/scripts/review_run_limits.py).
ALTER TABLE runs ADD COLUMN timeout_s int;
ALTER TABLE runs ADD COLUMN memory_mb int;
ALTER TABLE runs ADD COLUMN cpus real;
ALTER TABLE runs ADD COLUMN peak_memory_mb int;
