"""Copy an Actor's dataset.jsonl into the registry WHILE the container runs.

One tailer per run, started just before the container and drained just after
it exits -- before the run is marked terminal, so a finished run's items are
complete the moment anyone can see it finished.

NEVER FATAL. A database that is briefly unreachable, a malformed line, a file
the Actor never wrote: each is logged and the run carries on. Items are a
convenience on top of output.json, and failing a forty-minute scrape because
the copy of its results stalled would be absurd -- the same judgement the
executor already makes about report delivery.

A failed insert does NOT advance the read position, so the next tick retries
the same lines, and the registry's (run_id, seq) idempotency makes that retry
harmless even if the first insert partly landed.
"""

from __future__ import annotations

import threading
from typing import Callable

from registry.dataset import read_from
from registry.run_registry import RunRegistry

# How often to look for new lines. Items appear in the API within about this
# long of the Actor writing them; the read itself is a seek and a few lines.
POLL_INTERVAL_S = 2.0
# Lines per INSERT batch. Bounded so one burst from the Actor is several short
# transactions rather than one long one.
BATCH_SIZE = 500


class DatasetIngestor:
    def __init__(
        self,
        registry: RunRegistry,
        run_id: str,
        *,
        interval: float = POLL_INTERVAL_S,
        batch_size: int = BATCH_SIZE,
        log: Callable[[str], None] = print,
    ) -> None:
        self.registry = registry
        self.run_id = run_id
        self.path = registry.dataset_path(run_id)
        self.interval = interval
        self.batch_size = batch_size
        self._log = log
        self._offset = 0
        self._next_seq = 1
        self._stop = threading.Event()
        self._lock = threading.Lock()   # the tick and the final drain never overlap
        self._thread: threading.Thread | None = None
        self.read = 0
        self.inserted = 0
        self.malformed = 0
        self.failures = 0
        self._warned: set[str] = set()  # refusal reasons already logged

    def start(self) -> "DatasetIngestor":
        self._thread = threading.Thread(
            target=self._loop, name=f"items-{self.run_id[-6:]}", daemon=True)
        self._thread.start()
        return self

    def stop_and_drain(self) -> dict[str, int]:
        """Stop tailing and read whatever is left, unterminated last line included."""
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=self.interval + 5)
        self.pump(final=True)
        if self.read or self.malformed or self.failures:
            self._log(f"run {self.run_id}: items read={self.read} stored={self.inserted} "
                      f"malformed={self.malformed} insert_failures={self.failures}")
        return {"read": self.read, "inserted": self.inserted,
                "malformed": self.malformed, "failures": self.failures}

    def _loop(self) -> None:
        while not self._stop.wait(self.interval):
            self.pump(final=False)

    def pump(self, *, final: bool) -> None:
        with self._lock:
            while True:
                try:
                    chunk = read_from(self.path, self._offset, self._next_seq,
                                      final=final, max_items=self.batch_size)
                except Exception as exc:  # noqa: BLE001 - this thread must not die
                    # OSError is the expected case; anything else is a bug or a
                    # hostile file, and either way the tail must keep running.
                    self.failures += 1
                    self._log(f"run {self.run_id}: could not read items: "
                              f"{type(exc).__name__}: {_first_line(exc)}")
                    return
                if chunk.offset == self._offset:
                    # Nothing new -- but say ONCE if that is because the file
                    # was refused (a link or FIFO the Actor left in its mount),
                    # or the run would just look like one that wrote no items.
                    for reason in chunk.reasons:
                        if reason not in self._warned:
                            self._warned.add(reason)
                            self._log(f"run {self.run_id}: dataset {reason}")
                    return  # nothing new
                try:
                    self.inserted += self.registry.ingest_items(self.run_id, chunk.items)
                except Exception as exc:  # noqa: BLE001 - never fatal, see module doc
                    self.failures += 1
                    # First line only: a database error's DETAIL/CONTEXT lines
                    # quote the row, which is scraped content.
                    self._log(f"run {self.run_id}: could not store {len(chunk.items)} "
                              f"item(s), will retry: {type(exc).__name__}: {_first_line(exc)}")
                    return  # position NOT advanced: the next tick retries these lines
                for reason in chunk.reasons:
                    self._log(f"run {self.run_id}: skipped a malformed item ({reason})")
                self.read += len(chunk.items)
                self.malformed += chunk.malformed
                self._offset, self._next_seq = chunk.offset, chunk.next_seq


def _first_line(exc: BaseException, limit: int = 200) -> str:
    text = str(exc).splitlines()[0] if str(exc) else ""
    return text[:limit]
