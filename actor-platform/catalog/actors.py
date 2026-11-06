"""The Actor catalog: what Actors exist, and is this name one of them.

The third abstraction the platform is built on, beside the Run Registry and the
Queue. It exists so that asking "what can I run?" does not require importing the
module that runs things: the API depends on this, the executor depends on this,
and neither depends on the other.

An Actor is a directory under actors/ containing a Dockerfile. That is the same
definition the executor has always used to resolve one, so discovery and
execution cannot disagree about what exists.

An Actor MAY also ship an input_schema.json -- a plain JSON Schema document --
which makes it self-describing and lets a bad input be rejected at submission
instead of failing inside a container seconds later.

THE SCHEMA DOES NOT REPLACE THE ACTOR'S OWN VALIDATION, and must never be read
that way. An Actor is an independent executable: it can be started by `docker
run` with no platform involved at all, so it remains responsible for checking
what it was given. The schema is an early filter for callers, not a security
boundary, and every Actor here still validates its own input.

Nothing here knows about Docker, HTTP or storage -- only about names, where
build contexts live, and what shape of input each Actor accepts.
"""

from __future__ import annotations

import json
import re
import sys
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from jsonschema import Draft202012Validator, FormatChecker, validators  # noqa: E402
from jsonschema.exceptions import SchemaError, ValidationError  # noqa: E402

from shared.models import ACTOR_NAME_RULE, is_valid_actor_name  # noqa: E402

ACTORS_DIR = REPO_ROOT / "actors"

# A build context is identified by its Dockerfile; anything else in actors/ is
# not an Actor as far as the platform is concerned.
ACTOR_MARKER = "Dockerfile"

# Optional. An Actor without one accepts any JSON object: the platform must not
# require a schema, or adding this feature would break every Actor that predates
# it and every Actor written in a language whose author does not want one.
INPUT_SCHEMA_FILE = "input_schema.json"


class CatalogError(RuntimeError):
    """The catalog cannot answer for this name."""


class InvalidActorName(CatalogError):
    """The name could never denote an Actor."""


class UnknownActor(CatalogError):
    """A well-formed name, but no such Actor is installed."""


class InvalidActorSchema(CatalogError):
    """The Actor ships an input_schema.json that is not a usable JSON Schema."""


class InvalidActorInput(CatalogError):
    """The submitted input does not satisfy the Actor's declared schema."""

    def __init__(self, message: str, violations: list[dict[str, str]]) -> None:
        super().__init__(message)
        self.violations = violations


@dataclass(frozen=True)
class ActorInfo:
    """One installed Actor.

    `path` is deliberately not something callers serialise: it is where the
    build context lives on this machine, which is the platform's business.

    `schema_error` is set instead of `input_schema` when the Actor ships a
    schema that cannot be used. Reporting that is the point: silently falling
    back to "no schema" would turn a deploy mistake into validation that is
    quietly switched off.
    """

    name: str
    path: Path
    title: str | None = None
    description: str | None = None
    input_schema: dict[str, Any] | None = field(default=None, compare=False)
    schema_error: str | None = None


def actors_dir() -> Path:
    return ACTORS_DIR


def load_input_schema(path: Path) -> dict[str, Any] | None:
    """Read an Actor's declared input schema, or None if it ships none.

    Raises InvalidActorSchema rather than shrugging: a schema that cannot be
    parsed is a broken deployment, and treating it as absent would disable
    validation for that Actor without anyone noticing.
    """
    schema_path = path / INPUT_SCHEMA_FILE
    if not schema_path.is_file():
        return None

    try:
        schema = json.loads(schema_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise InvalidActorSchema(f"{INPUT_SCHEMA_FILE} could not be read: {exc}") from exc

    if not isinstance(schema, dict):
        raise InvalidActorSchema(
            f"{INPUT_SCHEMA_FILE} must be a JSON object, got {type(schema).__name__}"
        )

    try:
        Draft202012Validator.check_schema(schema)
    except SchemaError as exc:
        raise InvalidActorSchema(f"{INPUT_SCHEMA_FILE} is not a valid JSON Schema: {exc.message}") from exc

    return schema


def _describe(path: Path, name: str) -> ActorInfo:
    """Build an ActorInfo, keeping a broken schema visible rather than fatal."""
    try:
        schema = load_input_schema(path)
        error = None
    except InvalidActorSchema as exc:
        schema, error = None, str(exc)

    return ActorInfo(
        name=name,
        path=path,
        title=(schema or {}).get("title"),
        description=(schema or {}).get("description"),
        input_schema=schema,
        schema_error=error,
    )


# --- what JSON Schema alone does not say ---------------------------------------
#
# Every input is stored three times (input.json, the run record, the database)
# and handed to a container, so some values must be refused whatever an Actor's
# schema says: they could not be stored, or they make the platform do work out
# of proportion to the request. Checked for EVERY Actor, schema or not.
MAX_INPUT_DEPTH = 32
# A violation message quotes the offending value; one quoted in full would echo
# a megabyte back to the caller. The path still says which field it was.
MAX_MESSAGE_LENGTH = 200

# Dates: what every platform Actor parses (collectors/daterange.py), as a FORMAT
# so the check is a real calendar check -- a pattern accepts 2026-99-99, matches
# non-ASCII digits under \d, and lets a trailing newline through `$`. The years
# are the range any target site has posts in, with room to spare; outside it a
# value is a typo, and year 9999 overflowed X's search window outright.
DATE_FORMAT = "date-or-datetime"
_DATE_SHAPE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}(?:[ T][0-9]{2}:[0-9]{2}(?::[0-9]{2})?)?")
MIN_YEAR, MAX_YEAR = 2000, 2100


def parse_date_or_datetime(value: str) -> datetime | None:
    """The moment a date field names, or None if it is not a real one."""
    if not isinstance(value, str) or not _DATE_SHAPE.fullmatch(value):
        return None
    try:
        moment = datetime.fromisoformat(value.replace(" ", "T"))
    except ValueError:
        return None
    return moment if MIN_YEAR <= moment.year <= MAX_YEAR else None


_FORMATS = FormatChecker(formats=())


@_FORMATS.checks(DATE_FORMAT)
def _is_date_or_datetime(value: Any) -> bool:
    # Non-strings are the `type` keyword's business, as JSON Schema intends.
    return not isinstance(value, str) or parse_date_or_datetime(value) is not None


def _date_window(validator, spec, instance, schema):
    """`x-date-window: {start, end}` -- the start may not come after the end.

    Expressed by the schema rather than by recognising field names, so the
    platform still knows nothing about what an Actor's fields mean.
    """
    if not isinstance(instance, dict) or not isinstance(spec, dict):
        return
    start = parse_date_or_datetime(instance.get(spec.get("start")))
    raw_end = instance.get(spec.get("end"))
    end = parse_date_or_datetime(raw_end)
    # A bare END date means the end of that day, as collectors/daterange.py
    # reads it, so 10:00 on the 1st to "the 1st" is a valid window.
    if end is not None and len(raw_end) == 10:
        end = end.replace(hour=23, minute=59, second=59, microsecond=999999)
    if start is not None and end is not None and start > end:
        yield ValidationError(f"{spec.get('start')} is after {spec.get('end')}")


def _is_strict_integer(_checker, instance) -> bool:
    # JSON Schema counts 5.0 and 1e3 as integers. No Actor does -- each one
    # refuses a float -- so such an input was accepted, queued, started, and then
    # failed in its container. bool is excluded as the default checker does.
    return isinstance(instance, int) and not isinstance(instance, bool)


_InputValidator = validators.extend(
    Draft202012Validator,
    validators={"x-date-window": _date_window},
    type_checker=Draft202012Validator.TYPE_CHECKER.redefine("integer", _is_strict_integer),
)


def _unstorable(value: Any, path: str, depth: int = 0) -> tuple[str, str] | None:
    """(path, reason) for the first value that cannot be stored, or None.

    NUL and unpaired surrogates are valid JSON but not valid UTF-8 text or
    Postgres jsonb: writing them failed with a 500 AFTER the run directory had
    been created. Depth is bounded for the same reason recursion is: an input
    nested past MAX_INPUT_DEPTH is not a real Actor input.
    """
    if depth > MAX_INPUT_DEPTH:
        return path or "(root)", f"is nested more than {MAX_INPUT_DEPTH} levels deep"
    if isinstance(value, str):
        if "\x00" in value:
            return path or "(root)", "contains a NUL character"
        try:
            value.encode("utf-8")
        except UnicodeEncodeError:
            return path or "(root)", "contains an unpaired surrogate character"
        return None
    if isinstance(value, dict):
        for key, item in value.items():
            child = f"{path}.{key}" if path else str(key)
            found = _unstorable(key, child, depth + 1) or _unstorable(item, child, depth + 1)
            if found:
                return found
        return None
    if isinstance(value, list):
        for index, item in enumerate(value):
            found = _unstorable(item, f"{path}.{index}" if path else str(index), depth + 1)
            if found:
                return found
    return None


def _short(message: str) -> str:
    if len(message) <= MAX_MESSAGE_LENGTH:
        return message
    return message[: MAX_MESSAGE_LENGTH - 1] + "\u2026"


def check_storable(payload: Any) -> None:
    """Refuse an input no storage layer can hold. Raises InvalidActorInput."""
    found = _unstorable(payload, "")
    if found:
        # The path is shortened too: keys are caller-chosen text.
        path, reason = _short(found[0]), found[1]
        raise InvalidActorInput(f"input {path}: {reason}", [{"path": path, "message": reason}])


def validate_input(actor: ActorInfo, payload: Any) -> None:
    """Check a submitted input against the Actor's declared schema.

    Silent when the Actor declares nothing -- apart from check_storable, which
    holds for every Actor. Reports EVERY schema violation rather than the
    first, because being told about one mistake at a time is the worst way to
    fix a document.
    """
    if actor.schema_error is not None:
        raise InvalidActorSchema(f"actor '{actor.name}': {actor.schema_error}")

    check_storable(payload)

    if actor.input_schema is None:
        return

    validator = _InputValidator(actor.input_schema, format_checker=_FORMATS)
    errors = sorted(validator.iter_errors(payload), key=lambda e: list(e.absolute_path))
    if not errors:
        return

    violations = [
        {
            "path": _short(".".join(str(part) for part in error.absolute_path) or "(root)"),
            "message": _short(error.message),
        }
        for error in errors
    ]
    summary = "; ".join(f"{v['path']}: {v['message']}" for v in violations)
    raise InvalidActorInput(f"input does not match the schema for '{actor.name}': {summary}", violations)


def list_actors(root: Path | None = None) -> list[ActorInfo]:
    """Every installed Actor, sorted by name.

    A directory whose name the platform could never build an image for is left
    out rather than advertised: offering an Actor that any attempt to run would
    reject with "invalid actor name" would be a lie told twice.
    """
    base = root or ACTORS_DIR
    if not base.is_dir():
        return []

    found = [
        _describe(entry, entry.name)
        for entry in base.iterdir()
        if entry.is_dir()
        and not entry.name.startswith((".", "__"))
        and is_valid_actor_name(entry.name)
        and (entry / ACTOR_MARKER).is_file()
    ]
    return sorted(found, key=lambda actor: actor.name)


def find_actor(name: str, root: Path | None = None) -> ActorInfo:
    """Resolve one Actor by name, distinguishing the two ways it can fail.

    The name is checked BEFORE it is joined onto a path: '..' must never reach
    the filesystem, and a name Docker's grammar rejects should be refused here
    rather than surfacing later as `invalid reference format` from inside a
    build, which blames the build rather than the name.
    """
    if not is_valid_actor_name(name):
        raise InvalidActorName(f"invalid actor name {name!r}: {ACTOR_NAME_RULE}")

    path = (root or ACTORS_DIR) / name
    if not (path / ACTOR_MARKER).is_file():
        # The build-context path is this machine's business, and the message
        # reaches HTTP clients verbatim.
        raise UnknownActor(f"unknown actor '{name}'")

    return _describe(path, name)
