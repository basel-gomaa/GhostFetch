"""Reading an Actor's dataset.jsonl: the pure half of item ingestion.

No database and no threads here -- just "given this file and how far we have
already read, what are the next items?". The worker's tailer and both registry
backends call it, so the file is parsed by exactly one piece of code.

THREE RULES, each for a reason:

1. SEQ IS THE LINE NUMBER, counted over EVERY complete line, valid or not. So an
   item's identity is its position in the file and nothing else, and reading the
   same file twice yields the same (run_id, seq) pairs -- which is what makes
   ingestion idempotent without the platform needing to understand a key.

2. A LINE IS ONLY READ ONCE IT IS COMPLETE. The Actor is appending while we read;
   a line without its trailing newline may be half-written, and parsing it would
   store a truncated item or count a good one as malformed. It is left for the
   next read -- except on the FINAL read, after the container has exited, when
   nothing more can arrive and an unterminated last line is simply the last line.

3. A MALFORMED LINE IS COUNTED AND SKIPPED, never fatal. One bad line from an
   Actor must not cost the run every good line after it.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from shared import safe_fs

# Collection names are grouping labels, not free text: short, and shaped so they
# are safe in a log line. `#` IS allowed because batch collections are named
# "data#<n>" (the console shows them as sections) -- and it was not, so the
# worker skipped every batch item as malformed and the Data tab stayed empty
# (measured 2026-09-21 on two live group runs). A `?collection=` filter must
# therefore encode it as %23, or the browser treats the rest as a fragment.
COLLECTION_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:#-]{0,63}")
MAX_KEY_LENGTH = 512
# One item larger than this is a mistake (an Actor dumping its whole result as a
# line), not data, and would otherwise be read into memory whole.
MAX_LINE_BYTES = 4 * 1024 * 1024
# Scraped records nest a few levels (a post, its comments, their replies). A
# line nested far deeper is not a record, and past ~1000 levels json.loads
# raised RecursionError -- which nothing caught, so the ingest thread died.
MAX_ITEM_DEPTH = 100


def _unstorable(value: Any, depth: int = 0) -> str | None:
    """Why `value` cannot go into a jsonb column, or None. Never raises.

    Postgres rejects NUL anywhere in jsonb text and unpaired surrogates are not
    UTF-8; either one failed the WHOLE batch insert, which was then retried
    every two seconds until the run ended, with every later item lost.
    """
    if depth > MAX_ITEM_DEPTH:
        return f"nested more than {MAX_ITEM_DEPTH} levels deep"
    if isinstance(value, str):
        if "\x00" in value:
            return "contains a NUL character"
        try:
            value.encode("utf-8")
        except UnicodeEncodeError:
            return "contains an unpaired surrogate character"
        return None
    if isinstance(value, dict):
        for key, item in value.items():
            reason = _unstorable(key, depth + 1) or _unstorable(item, depth + 1)
            if reason:
                return reason
    elif isinstance(value, list):
        for item in value:
            reason = _unstorable(item, depth + 1)
            if reason:
                return reason
    return None


@dataclass
class DatasetItem:
    seq: int
    collection: str
    key: str | None
    data: Any

    def to_dict(self) -> dict[str, Any]:
        return {"seq": self.seq, "collection": self.collection, "key": self.key,
                "data": self.data}


@dataclass
class ReadResult:
    items: list[DatasetItem] = field(default_factory=list)
    # Byte offset just past the last line consumed, and the seq the next line
    # will get. Hand both back to read_from to continue.
    offset: int = 0
    next_seq: int = 1
    malformed: int = 0
    # Why each malformed line was rejected, for the log. Capped; never the line.
    reasons: list[str] = field(default_factory=list)


def parse_line(raw: bytes, seq: int) -> tuple[DatasetItem | None, str | None]:
    """One line -> (item, None) or (None, reason). Never raises."""
    if len(raw) > MAX_LINE_BYTES:
        return None, f"line {seq}: longer than {MAX_LINE_BYTES} bytes"
    text = raw.decode("utf-8", errors="replace").strip()
    if not text:
        return None, f"line {seq}: empty"
    try:
        doc = json.loads(text)
    except json.JSONDecodeError as exc:
        return None, f"line {seq}: not JSON ({exc.msg})"
    except (RecursionError, ValueError):
        return None, f"line {seq}: not JSON (nested too deeply or malformed)"
    if not isinstance(doc, dict):
        return None, f"line {seq}: not a JSON object"
    collection = doc.get("collection")
    if not isinstance(collection, str) or not COLLECTION_PATTERN.fullmatch(collection):
        return None, f"line {seq}: missing or invalid 'collection'"
    key = doc.get("key")
    if key is not None:
        if not isinstance(key, (str, int)):
            return None, f"line {seq}: 'key' must be a string"
        key = str(key)
        if len(key) > MAX_KEY_LENGTH:
            return None, f"line {seq}: 'key' longer than {MAX_KEY_LENGTH}"
    if "data" not in doc:
        return None, f"line {seq}: missing 'data'"
    reason = _unstorable(doc["data"])
    if reason:
        return None, f"line {seq}: 'data' {reason}"
    return DatasetItem(seq=seq, collection=collection, key=key, data=doc["data"]), None


def read_from(path: Path, offset: int = 0, next_seq: int = 1, *,
              final: bool = False, max_items: int | None = None) -> ReadResult:
    """Read complete lines from `offset`. A missing file is simply no items.

    `final=True` also consumes an unterminated last line (rule 2).
    `max_items` stops early, leaving offset/next_seq pointing at the rest.
    """
    result = ReadResult(offset=offset, next_seq=next_seq)
    try:
        # Not open(path): this file lives in the Actor's own mount, so it may be
        # a link to a host file (whose lines would become this run's items,
        # readable over /items) or a FIFO (on which open() would block the
        # tailer for ever). Either is treated as "no items", and said so.
        handle = os.fdopen(safe_fs.open_regular(path), "rb")
    except FileNotFoundError:
        return result
    except safe_fs.NotRegularFile as exc:
        result.reasons.append(f"ignored: {exc}")
        return result
    with handle:
        handle.seek(offset)
        while max_items is None or len(result.items) < max_items:
            start = handle.tell()
            raw = handle.readline(MAX_LINE_BYTES + 2)
            if not raw:
                break
            if not raw.endswith(b"\n"):
                if len(raw) > MAX_LINE_BYTES:
                    # An oversized line: skip to its end so it cannot wedge the
                    # reader for ever, and count it once.
                    while raw and not raw.endswith(b"\n"):
                        raw = handle.readline(MAX_LINE_BYTES + 2)
                    if not raw and not final:
                        handle.seek(start)  # still being written; retry later
                        break
                    oversized = b"x" * (MAX_LINE_BYTES + 1)
                    _consume(result, oversized, handle.tell())
                    continue
                if not final:
                    handle.seek(start)  # half-written; leave it for next time
                    break
            _consume(result, raw, handle.tell())
    return result


def _consume(result: ReadResult, raw: bytes, end_offset: int) -> None:
    seq = result.next_seq
    item, reason = parse_line(raw, seq)
    if item is not None:
        result.items.append(item)
    elif raw.strip():
        result.malformed += 1
        if len(result.reasons) < 10:
            result.reasons.append(reason or f"line {seq}: rejected")
    # A blank line still occupies a seq, so positions never shift (rule 1),
    # but it is not worth calling malformed.
    result.next_seq = seq + 1
    result.offset = end_offset
