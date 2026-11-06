"""E-mail a finished run's report to whoever submitted it.

    ACTOR_PLATFORM_SMTP_HOST     default smtp.gmail.com
    ACTOR_PLATFORM_SMTP_PORT     default 465 (implicit TLS)
    ACTOR_PLATFORM_SMTP_USER
    ACTOR_PLATFORM_SMTP_PASSWORD an app password, not an account password
    ACTOR_PLATFORM_SMTP_FROM     defaults to the user

THE WORKER SENDS, NOT THE ACTOR, and there are three reasons rather than one:
SMTP credentials would otherwise have to be handed to a container running
vendored third-party code; the recipient's address would have to travel in
input.json, which is served over HTTP; and delivery would then be
unobservable -- report_sent_at belongs beside the run, not inside it.

NOT CONFIGURED IS NOT AN ERROR. The artifacts are already on disk and reachable
through the API, so an unsent e-mail is a missing convenience, not a lost
result. Failure is logged and never propagates: a run that scraped for forty
minutes must not be reported as failed because a mail server was down.

Deliberately not an import of the pipeline repository's notifications/gmail.py.
That module belongs to a single-user CLI, reads its configuration from that
repo's .env, and sending is the one thing this platform must own -- it decides
who the recipient is.
"""

from __future__ import annotations

import logging
import mimetypes
import os
from collections.abc import Mapping
import smtplib
from email.message import EmailMessage
from pathlib import Path

from shared import safe_fs

logger = logging.getLogger(__name__)

HOST_ENV = "ACTOR_PLATFORM_SMTP_HOST"
PORT_ENV = "ACTOR_PLATFORM_SMTP_PORT"
USER_ENV = "ACTOR_PLATFORM_SMTP_USER"
PASSWORD_ENV = "ACTOR_PLATFORM_SMTP_PASSWORD"
FROM_ENV = "ACTOR_PLATFORM_SMTP_FROM"

DEFAULT_HOST = "smtp.gmail.com"
DEFAULT_PORT = 465  # implicit TLS, so the connection is never briefly plaintext

TIMEOUT_S = 30.0

# Big enough for a report with charts, small enough that a runaway artifact does
# not try to push tens of megabytes through SMTP and time out.
MAX_ATTACHMENT_BYTES = 15 * 1024 * 1024


def is_configured(environ: Mapping[str, str] | None = None) -> bool:
    env = os.environ if environ is None else environ
    return bool(env.get(USER_ENV) and env.get(PASSWORD_ENV))


def _attach(message: EmailMessage, path: Path) -> bool:
    # Opened as a REGULAR FILE only: artifacts come from the Actor's mount, and
    # a link there would otherwise mail a host file to the run's owner. The
    # size is taken from the open descriptor, so it is the size of what is read.
    try:
        fd = safe_fs.open_regular(path)
    except FileNotFoundError:
        logger.warning("not attaching %s: it does not exist", path.name)
        return False
    except OSError as exc:
        logger.warning("not attaching %s: %s", path.name, exc)
        return False
    with os.fdopen(fd, "rb") as handle:
        size = os.fstat(handle.fileno()).st_size
        if size > MAX_ATTACHMENT_BYTES:
            logger.warning("not attaching %s: %d bytes is over the limit", path.name, size)
            return False
        data = handle.read()
    guessed, _ = mimetypes.guess_type(path.name)
    main, _, sub = (guessed or "application/octet-stream").partition("/")
    message.add_attachment(data, maintype=main,
                           subtype=sub or "octet-stream", filename=path.name)
    return True


def send_report(recipient: str, subject: str, body: str,
                attachments: list[Path] | None = None,
                environ: Mapping[str, str] | None = None) -> bool:
    """Send one report. Returns whether it went. NEVER raises."""
    env = os.environ if environ is None else environ
    if not is_configured(env):
        logger.info("no SMTP configuration; not e-mailing the report to %s", recipient)
        return False
    if not recipient:
        return False

    message = EmailMessage()
    message["From"] = env.get(FROM_ENV) or env[USER_ENV]
    message["To"] = recipient
    message["Subject"] = subject
    message.set_content(body)

    attached = sum(1 for path in (attachments or []) if _attach(message, path))

    host = env.get(HOST_ENV) or DEFAULT_HOST
    port = int(env.get(PORT_ENV) or DEFAULT_PORT)
    try:
        # SMTP_SSL rather than STARTTLS: the session is encrypted from the first
        # byte, so the credentials are never sent over a connection that has to
        # be upgraded first.
        with smtplib.SMTP_SSL(host, port, timeout=TIMEOUT_S) as server:
            server.login(env[USER_ENV], env[PASSWORD_ENV])
            server.send_message(message)
    except Exception as exc:  # noqa: BLE001 - see the module docstring
        logger.warning("could not e-mail the report to %s: %s", recipient, exc)
        return False

    logger.info("e-mailed the report to %s with %d attachment(s)", recipient, attached)
    return True
