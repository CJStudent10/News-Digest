"""Optional email delivery over SMTP. Configured with environment variables (GitHub secrets)."""
from __future__ import annotations

import logging
import os
import smtplib
import ssl
from email.message import EmailMessage

log = logging.getLogger(__name__)


def email_configured() -> bool:
    return all(os.environ.get(k) for k in ("SMTP_HOST", "SMTP_USER", "SMTP_PASSWORD", "DIGEST_EMAIL_TO"))


def send_email(subject: str, html: str, text: str) -> bool:
    if not email_configured():
        log.info("email not configured (SMTP_* secrets missing); skipping")
        return False
    host, user = os.environ["SMTP_HOST"], os.environ["SMTP_USER"]
    port = int(os.environ.get("SMTP_PORT") or 465)
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = os.environ.get("DIGEST_EMAIL_FROM") or user
    msg["To"] = os.environ["DIGEST_EMAIL_TO"]
    msg.set_content(text)
    msg.add_alternative(html, subtype="html")
    ctx = ssl.create_default_context()
    try:
        if port == 465:
            with smtplib.SMTP_SSL(host, port, context=ctx, timeout=30) as s:
                s.login(user, os.environ["SMTP_PASSWORD"])
                s.send_message(msg)
        else:
            with smtplib.SMTP(host, port, timeout=30) as s:
                s.starttls(context=ctx)
                s.login(user, os.environ["SMTP_PASSWORD"])
                s.send_message(msg)
        log.info("emailed digest to %s", msg["To"])
        return True
    except Exception as exc:
        log.error("email failed: %s", exc)
        return False
