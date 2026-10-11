"""E-mail (round 5): only invitations, and only when the operator gave an SMTP server."""
from __future__ import annotations

import smtplib
import ssl
from email.message import EmailMessage
from email.utils import formataddr, make_msgid

from .config import Settings


class MailError(Exception):
    pass


def configured(settings: Settings) -> bool:
    return bool(settings.smtp_host and settings.smtp_from)


def send(settings: Settings, to: str, subject: str, body: str) -> None:
    msg = EmailMessage()
    msg["From"] = formataddr(("dwyt", settings.smtp_from))
    msg["To"] = to
    msg["Subject"] = subject
    msg["Message-ID"] = make_msgid(domain=settings.smtp_from.split("@")[-1])
    msg.set_content(body)
    context = ssl.create_default_context()
    try:
        if settings.smtp_security == "ssl":
            server = smtplib.SMTP_SSL(settings.smtp_host, settings.smtp_port, timeout=20, context=context)
        else:
            server = smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=20)
        with server:
            if settings.smtp_security == "starttls":
                server.starttls(context=context)
            if settings.smtp_user:
                server.login(settings.smtp_user, settings.smtp_password)
            server.send_message(msg)
    except (OSError, smtplib.SMTPException) as exc:
        raise MailError(f"Az e-mail nem ment el ({exc.__class__.__name__}: {exc}).") from exc


def invite(settings: Settings, to: str, link: str, inviter: str) -> None:
    days = f"{settings.invite_days:g}"
    body = (f"Szia!\n\n{inviter} meghívott a dwyt-be ({settings.base_url}), ahol videók és hangfelvételek "
            f"átiratát, fordítását és összefoglalóját készítheted el, és kereshetsz bennük.\n\n"
            f"A belépéshez nyisd meg ezt a linket, és lépj be a(z) {to} címhez tartozó Google-fiókkal:\n\n"
            f"{link}\n\nA link {days} napig érvényes, és egyszer használható.\n")
    send(settings, to, "Meghívó a dwyt-be", body)
