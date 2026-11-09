"""Healthcheck pings, webhook, ntfy, Gotify and SMTP notifications (blocking; run in a thread)."""

import logging
import smtplib
from email.message import EmailMessage

import httpx

from . import activity
from .settings.models import Notifications

log = logging.getLogger(__name__)


def notify(n: Notifications, ok: bool, title: str, message: str) -> list[str]:
    """Send everything configured. Returns errors (also logged); never raises."""
    errors: list[str] = []
    sent: list[str] = []

    def attempt(name: str, fn) -> None:
        try:
            fn()
            sent.append(name)
        except Exception as e:  # noqa: BLE001
            errors.append(f"{name}: {e}")
            log.warning("notification via %s failed: %s", name, e)

    with httpx.Client(timeout=10) as c:
        url = n.healthcheck_success_url if ok else n.healthcheck_fail_url
        if url:
            attempt("healthcheck", lambda: c.post(url, content=message.encode()).raise_for_status())
        if ok and n.on == "failure":
            return _done(title, sent, errors)
        if n.webhook_url:
            attempt("webhook", lambda: c.post(n.webhook_url, json={"ok": ok, "title": title,
                                                                    "message": message}).raise_for_status())
        if n.ntfy_url:
            headers = {"Title": title, "Priority": "default" if ok else "high", "Tags": "white_check_mark" if ok
                       else "warning"}
            if n.ntfy_token.get_secret_value():
                headers["Authorization"] = f"Bearer {n.ntfy_token.get_secret_value()}"
            attempt("ntfy", lambda: c.post(n.ntfy_url, content=message.encode(), headers=headers).raise_for_status())
        if n.gotify_url:
            attempt("gotify", lambda: c.post(
                n.gotify_url.rstrip("/") + "/message", params={"token": n.gotify_token.get_secret_value()},
                json={"title": title, "message": message, "priority": 3 if ok else 8}).raise_for_status())
    if n.smtp_host and n.smtp_to:
        def mail() -> None:
            msg = EmailMessage()
            msg["Subject"], msg["From"], msg["To"] = title, n.smtp_from or n.smtp_user, n.smtp_to
            msg.set_content(message)
            with smtplib.SMTP(n.smtp_host, n.smtp_port, timeout=15) as s:
                if n.smtp_starttls:
                    s.starttls()
                if n.smtp_user:
                    s.login(n.smtp_user, n.smtp_password.get_secret_value())
                s.send_message(msg)

        attempt("smtp", mail)
    return _done(title, sent, errors)


def _done(title: str, sent: list[str], errors: list[str]) -> list[str]:
    if sent or errors:
        activity.record("notify", f"{title}: sent via {', '.join(sent) or 'nothing'}"
                        + (f"; failed: {'; '.join(errors)}" if errors else ""), "warn" if errors else "info")
    return errors
