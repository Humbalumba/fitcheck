"""Transactional email via Resend (https://resend.com) REST API.

Env: RESEND_API_KEY, EMAIL_FROM (e.g. 'FitCheck <no-reply@yourdomain.com>' on a domain verified in Resend),
APP_URL (public site URL used in links). Without a key nothing is sent; FITCHECK_DEV_EMAIL_LOG=1 (local only)
logs the message instead so sign-up / reset can be tested. `outbox` keeps the last messages in-process (tests).
"""
from __future__ import annotations

import html
import json
import logging
import os
import urllib.request

log = logging.getLogger("fitcheck.email")
outbox: list[dict] = []


def configured() -> bool:
    return bool(os.environ.get("RESEND_API_KEY") and os.environ.get("EMAIL_FROM"))


def app_url() -> str:
    return (os.environ.get("APP_URL") or os.environ.get("AUTH_URL") or "http://localhost:3000").rstrip("/")


def _layout(title: str, body: str, button: str, link: str) -> str:
    return f"""<div style="background:#ede5da;padding:32px 16px;font-family:-apple-system,Segoe UI,Helvetica,Arial,sans-serif">
<div style="max-width:440px;margin:0 auto;background:#fff;border-radius:16px;padding:28px">
<div style="font-weight:800;font-size:22px;color:#111">F&#10003; FitCheck</div>
<h1 style="font-size:20px;color:#111;margin:20px 0 8px">{html.escape(title)}</h1>
<p style="color:#444;line-height:1.5">{body}</p>
<p style="margin:24px 0"><a href="{html.escape(link)}" style="background:#111;color:#fff;padding:12px 20px;border-radius:999px;text-decoration:none;font-weight:600">{html.escape(button)}</a></p>
<p style="color:#888;font-size:12px">If the button doesn't work, paste this link into your browser:<br>{html.escape(link)}</p>
</div></div>"""


def send(to: str, subject: str, html_body: str, text: str) -> bool:
    msg = {"to": to, "subject": subject, "text": text}
    outbox.append(msg)
    del outbox[:-20]
    if not configured():
        if os.environ.get("FITCHECK_DEV_EMAIL_LOG") == "1" and not os.environ.get("VERCEL"):
            log.warning("DEV EMAIL to %s: %s\n%s", to, subject, text)
        else:
            log.error("email not sent (RESEND_API_KEY / EMAIL_FROM not set): %s", subject)
        return False
    body = json.dumps({"from": os.environ["EMAIL_FROM"], "to": [to], "subject": subject, "html": html_body,
                       "text": text}).encode()
    req = urllib.request.Request("https://api.resend.com/emails", data=body, method="POST", headers={
        "Authorization": f"Bearer {os.environ['RESEND_API_KEY']}", "Content-Type": "application/json",
        "User-Agent": "fitcheck"})
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            return 200 <= r.status < 300
    except Exception as e:  # noqa: BLE001
        log.error("email send failed: %s", e)
        return False


def send_verification(to: str, token: str) -> bool:
    link = f"{app_url()}/verify-email?token={token}"
    return send(to, "Confirm your FitCheck email",
                _layout("Confirm your email", "Tap below to confirm your email and start building your closet. "
                        "This link expires in 24 hours.", "Confirm email", link),
                f"Confirm your FitCheck email: {link}\nThis link expires in 24 hours.")


def send_reset(to: str, token: str) -> bool:
    link = f"{app_url()}/reset-password?token={token}"
    return send(to, "Reset your FitCheck password",
                _layout("Reset your password", "Someone (hopefully you) asked to reset your FitCheck password. "
                        "This link expires in 1 hour. If it wasn't you, ignore this email.", "Choose a new password", link),
                f"Reset your FitCheck password: {link}\nThis link expires in 1 hour. If it wasn't you, ignore this email.")


def send_already_registered(to: str) -> bool:
    link = f"{app_url()}/login"
    return send(to, "You already have a FitCheck account",
                _layout("You already have an account", "Someone tried to sign up with this email. If it was you, "
                        "just sign in (or reset your password from the sign-in page).", "Sign in", link),
                f"You already have a FitCheck account. Sign in: {link}")
