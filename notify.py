"""Notification fan-out: ntfy first, email second.

Both channels are attempted on every alert and are fully independent — one
failing never blocks the other, and a run that reaches at least one counts as
sent (so state records `last_notified_at` and we don't re-alert in a loop).

  ntfy  — fast (1-5s). Priority "urgent" maps to iOS time-sensitive, which
          escapes most Focus modes. It cannot override the physical mute
          switch; nothing free can.
  email — slower (15s to several minutes) and Gmail may throttle identical
          bursts. Kept as the durable audit trail.

Both are optional: missing env vars skip that channel.

SECURITY: a public ntfy topic is world-READABLE and world-WRITABLE. Anyone who
guesses it can read your alerts or push you a fake "EN STOCK" at 3am. Use a
long random topic, or a server with auth via NTFY_SERVER.
"""
from __future__ import annotations

import os
import smtplib
import ssl
import urllib.request
from email.message import EmailMessage
from typing import List, Optional

TIMEOUT = 15


def send_ntfy(subject: str, body: str, url: Optional[str], urgent: bool) -> None:
    topic = os.environ.get("NTFY_TOPIC")
    if not topic:
        raise RuntimeError("NTFY_TOPIC not set")
    headers = {
        # ntfy headers must be latin-1 safe; the emoji live in Tags instead.
        "Title": subject.encode("ascii", errors="replace").decode("ascii"),
        "Priority": "urgent" if urgent else "default",
        "Tags": "video_game,rotating_light" if urgent else "warning",
        "Content-Type": "text/plain; charset=utf-8",
    }
    if url:
        headers["Click"] = url
    server = os.environ.get("NTFY_SERVER", "https://ntfy.sh").rstrip("/")
    req = urllib.request.Request(f"{server}/{topic}", data=body.encode("utf-8"),
                                 headers=headers, method="POST")
    with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
        r.read()


def send_email(subject: str, body: str, url: Optional[str], urgent: bool) -> None:
    user, password = os.environ.get("SMTP_USER"), os.environ.get("SMTP_PASS")
    if not (user and password):
        raise RuntimeError("SMTP_USER/SMTP_PASS not set")
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = user
    msg["To"] = os.environ.get("MAIL_TO", user)
    msg.set_content(body if not url else f"{body}\n\n{url}")
    ctx = ssl.create_default_context()
    host = os.environ.get("SMTP_HOST", "smtp.gmail.com")
    port = int(os.environ.get("SMTP_PORT", "465"))
    with smtplib.SMTP_SSL(host, port, context=ctx, timeout=30) as s:
        s.login(user, password)
        s.send_message(msg)


CHANNELS = (
    ("ntfy", send_ntfy),
    ("email", send_email),
)


def notify(subject: str, body: str, url: Optional[str] = None,
           urgent: bool = True) -> List[str]:
    """Fan out to every configured channel. Returns the ones that succeeded."""
    sent: List[str] = []
    for name, fn in CHANNELS:
        try:
            fn(subject, body, url, urgent)
            sent.append(name)
            print(f"  notify[{name}]: ok")
        except Exception as e:                    # noqa: BLE001 - never fatal
            print(f"  notify[{name}]: {type(e).__name__}: {e}")
    return sent
