"""
notifications/gmail.py

Responsibility: email the finished report via Gmail SMTP. Uses a Gmail
App Password (create one at https://myaccount.google.com/apppasswords -
requires 2-step verification) stored in .env:

    GMAIL_ADDRESS=you@gmail.com
    GMAIL_APP_PASSWORD=xxxxxxxxxxxxxxxx
    REPORT_RECIPIENT=boss@company.com   # optional, defaults to GMAIL_ADDRESS

If the credentials are not configured, send_report_email() logs a warning
and returns False instead of failing the pipeline - the report files are
already on disk at that point.
"""

from __future__ import annotations

import logging
import smtplib
from email.message import EmailMessage
from pathlib import Path

from config import GMAIL_ADDRESS, GMAIL_APP_PASSWORD, REPORT_RECIPIENT

logger = logging.getLogger(__name__)

SMTP_HOST = "smtp.gmail.com"
SMTP_PORT = 465  # SSL


def gmail_is_configured() -> bool:
    return bool(GMAIL_ADDRESS and GMAIL_APP_PASSWORD)


def send_report_email(
    subject: str,
    body: str,
    attachments: list[str | Path] | None = None,
    recipient: str | None = None,
) -> bool:
    """Send the report email. Returns True on success, False if skipped/failed."""
    # The same test as gmail_is_configured(), on locals the type checker can narrow.
    address, password = GMAIL_ADDRESS, GMAIL_APP_PASSWORD
    if not (address and password):
        logger.warning(
            "Gmail not configured (set GMAIL_ADDRESS and GMAIL_APP_PASSWORD "
            "in .env) - skipping email notification."
        )
        return False

    to_address = recipient or REPORT_RECIPIENT or address

    message = EmailMessage()
    message["From"] = address
    message["To"] = to_address
    message["Subject"] = subject
    message.set_content(body)

    for attachment in attachments or []:
        path = Path(attachment)
        if not path.exists():
            logger.warning("Attachment not found, skipping: %s", path)
            continue
        maintype, subtype = ("application", "pdf") if path.suffix.lower() == ".pdf" \
            else ("text", "html") if path.suffix.lower() in (".html", ".htm") \
            else ("application", "octet-stream")
        message.add_attachment(
            path.read_bytes(), maintype=maintype, subtype=subtype, filename=path.name
        )

    try:
        with smtplib.SMTP_SSL(SMTP_HOST, SMTP_PORT, timeout=30) as smtp:
            smtp.login(address, password)
            smtp.send_message(message)
    except Exception as exc:
        logger.error("Failed to send Gmail notification: %s", exc)
        return False

    logger.info("Report emailed to %s", to_address)
    return True
