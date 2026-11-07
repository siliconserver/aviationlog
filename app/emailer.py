import logging
import os
import smtplib
from email.mime.text import MIMEText

logger = logging.getLogger("mail")


def is_configured() -> bool:
    return bool(os.environ.get("SMTP_HOST"))


def send_mail(to: str, subject: str, body: str) -> bool:
    host = os.environ.get("SMTP_HOST")
    if not host or not to:
        logger.warning("Mail skipped (no SMTP_HOST or recipient): %s", subject)
        return False
    port = int(os.environ.get("SMTP_PORT", "587"))
    user = os.environ.get("SMTP_USER")
    password = os.environ.get("SMTP_PASSWORD")
    sender = os.environ.get("SMTP_FROM") or user or "aviationlog@localhost"
    use_tls = os.environ.get("SMTP_USE_TLS", "true").lower() == "true"

    msg = MIMEText(body, "plain", "utf-8")
    msg["From"] = sender
    msg["To"] = to
    msg["Subject"] = subject
    try:
        if port == 465:
            server = smtplib.SMTP_SSL(host, port, timeout=20)
        else:
            server = smtplib.SMTP(host, port, timeout=20)
        with server:
            if use_tls and port != 465:
                server.starttls()
            if user and password:
                server.login(user, password)
            server.sendmail(sender, [to], msg.as_string())
        return True
    except Exception:
        logger.exception("Sending mail failed")
        return False
