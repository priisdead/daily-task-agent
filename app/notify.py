"""Outbound notifications: email via SMTP (e.g. a Gmail app password).
Fully optional — when SMTP is not configured, messages are logged instead,
so the agent keeps working and you can still read digests at /digest."""
import logging
import smtplib
from email.mime.text import MIMEText

from . import config

log = logging.getLogger("notify")


def configured() -> bool:
    return bool(config.SMTP_USER and config.SMTP_PASS and config.NOTIFY_TO)


def send_email(subject: str, body: str) -> bool:
    """Send a plain-text email to NOTIFY_TO. Returns True on success."""
    if not configured():
        log.info("SMTP not configured — printing instead\nSUBJECT: %s\n%s",
                 subject, body)
        return False
    try:
        recipients = [a.strip() for a in config.NOTIFY_TO.split(",") if a.strip()]
        msg = MIMEText(body, "plain", "utf-8")
        msg["Subject"] = subject
        msg["From"] = config.SMTP_USER
        msg["To"] = ", ".join(recipients)
        with smtplib.SMTP_SSL(config.SMTP_HOST, config.SMTP_PORT, timeout=30) as s:
            s.login(config.SMTP_USER, config.SMTP_PASS)
            s.sendmail(config.SMTP_USER, recipients, msg.as_string())
        log.info("sent email %r to %s", subject, recipients)
        return True
    except Exception:
        log.exception("failed to send email %r", subject)
        return False
