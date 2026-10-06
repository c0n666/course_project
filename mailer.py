"""Outgoing email (password reset, email confirmation).

Configured with environment variables, like the AI keys:

    MAIL_SERVER     smtp.gmail.com (Gmail with an app password) or smtp-relay.brevo.com, …
    MAIL_PORT       587 (STARTTLS, default) or 465 (SSL)
    MAIL_USERNAME   login for the SMTP server
    MAIL_PASSWORD   password / app password / SMTP key
    MAIL_FROM       sender, e.g. "Kolos <kolos.app@gmail.com>" (defaults to MAIL_USERNAME)

Without MAIL_SERVER the message is written to the server log instead of being sent, so the
flows can be tried and demonstrated without a mailbox. Under TESTING every message is also
kept in app.config["MAIL_OUTBOX"].
"""
from __future__ import annotations

import logging
import os
import smtplib
import ssl
from dataclasses import dataclass
from email.message import EmailMessage
from email.utils import make_msgid

from flask import current_app, render_template

logger = logging.getLogger(__name__)


@dataclass
class Mail:
    to: str
    subject: str
    text: str
    html: str


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def mail_configured() -> bool:
    return bool(_env("MAIL_SERVER"))


def send_action_mail(to: str, subject: str, intro: str, button: str, url: str, note: str) -> bool:
    """A short email with one call-to-action link. Returns False if sending failed."""
    html = render_template(
        "emails/action.html", subject=subject, intro=intro, button=button, url=url, note=note
    )
    text = f"{intro}\n\n{button}: {url}\n\n{note}\n\n— Kolos"
    return send(Mail(to=to, subject=subject, text=text, html=html))


def send(mail: Mail) -> bool:
    if current_app.config.get("TESTING"):
        current_app.config.setdefault("MAIL_OUTBOX", []).append(mail)
        return True
    if not mail_configured():
        logger.warning("MAIL_SERVER is not set; email to %s not sent:\n%s\n%s", mail.to, mail.subject, mail.text)
        print(f"\n--- email to {mail.to}: {mail.subject}\n{mail.text}\n---\n", flush=True)
        return True

    username = _env("MAIL_USERNAME")
    sender = _env("MAIL_FROM") or username
    msg = EmailMessage()
    msg["Subject"] = mail.subject
    msg["From"] = sender
    msg["To"] = mail.to
    msg["Message-ID"] = make_msgid(domain=sender.rsplit("@", 1)[-1].strip(">") or None)
    msg.set_content(mail.text)
    msg.add_alternative(mail.html, subtype="html")

    server, port = _env("MAIL_SERVER"), int(_env("MAIL_PORT", "587") or 587)
    try:
        context = ssl.create_default_context()
        if port == 465:
            with smtplib.SMTP_SSL(server, port, context=context, timeout=15) as smtp:
                if username:
                    smtp.login(username, _env("MAIL_PASSWORD"))
                smtp.send_message(msg)
        else:
            with smtplib.SMTP(server, port, timeout=15) as smtp:
                smtp.starttls(context=context)
                if username:
                    smtp.login(username, _env("MAIL_PASSWORD"))
                smtp.send_message(msg)
        return True
    except (OSError, smtplib.SMTPException):
        logger.exception("Could not send email to %s", mail.to)
        return False
