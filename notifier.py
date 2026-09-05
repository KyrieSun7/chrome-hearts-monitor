#!/usr/bin/env python3
"""
Notification backends, chosen via the NOTIFY_METHOD env var:

  NOTIFY_METHOD=discord     Easiest. Free. No phone. Just a webhook URL.
  NOTIFY_METHOD=twilio      SMS. Reliable, paid (~$1/mo number + ~$0.008/text).
  NOTIFY_METHOD=email_sms   SMS via your carrier's email gateway. Free, flaky.

Discord/Twilio use plain HTTP (no extra dependency). Email uses stdlib smtplib.
"""

from __future__ import annotations

import os
import smtplib
import sys
from email.mime.text import MIMEText

import requests


def _env(name: str) -> str:
    val = os.environ.get(name)
    if not val:
        print(f"[notifier] missing env var: {name}", file=sys.stderr)
        raise SystemExit(1)
    return val


def _send_discord(body: str, *, suppress_embeds: bool = False) -> None:
    """
    POST to a Discord channel webhook. To get the URL:
      Discord -> Server Settings -> Integrations -> Webhooks -> New Webhook
      -> pick a channel -> Copy Webhook URL.
    The URL itself is the secret; anyone with it can post to that channel.
    """
    url = _env("DISCORD_WEBHOOK_URL")
    payload = {"content": body[:2000]}
    if suppress_embeds:
        payload["flags"] = 1 << 2  # Discord SUPPRESS_EMBEDS
    resp = requests.post(url, json=payload, timeout=30)
    if resp.status_code >= 300:
        print(f"[discord] error {resp.status_code}: {resp.text}", file=sys.stderr)
    else:
        print("[discord] sent")


def _send_twilio(body: str) -> None:
    sid = _env("TWILIO_ACCOUNT_SID")
    token = _env("TWILIO_AUTH_TOKEN")
    resp = requests.post(
        f"https://api.twilio.com/2010-04-01/Accounts/{sid}/Messages.json",
        auth=(sid, token),
        data={"From": _env("TWILIO_FROM"), "To": _env("TWILIO_TO"), "Body": body},
        timeout=30,
    )
    if resp.status_code >= 300:
        print(f"[twilio] error {resp.status_code}: {resp.text}", file=sys.stderr)
    else:
        print("[twilio] sent")


def _send_email_sms(body: str) -> None:
    """
    Free path: email -> carrier SMS gateway. Set SMS_TO to your gateway address.
        AT&T  5551234567@txt.att.net   Verizon  5551234567@vtext.com
        T-Mobile 5551234567@tmomail.net   Google Fi 5551234567@msg.fi.google.com
    """
    host = os.environ.get("SMTP_HOST", "smtp.gmail.com")
    port = int(os.environ.get("SMTP_PORT", "587"))
    user = _env("SMTP_USER")
    password = _env("SMTP_PASS")
    to_addr = _env("SMS_TO")
    msg = MIMEText(body)
    msg["From"], msg["To"], msg["Subject"] = user, to_addr, ""
    with smtplib.SMTP(host, port, timeout=30) as server:
        server.starttls()
        server.login(user, password)
        server.sendmail(user, [to_addr], msg.as_string())
    print("[email_sms] sent")


def send_notification(body: str, *, suppress_embeds: bool = False) -> None:
    method = os.environ.get("NOTIFY_METHOD", "discord").lower()
    if method == "discord":
        _send_discord(body, suppress_embeds=suppress_embeds)
    elif method == "twilio":
        _send_twilio(body)
    elif method == "email_sms":
        _send_email_sms(body)
    else:
        print(f"[notifier] unknown NOTIFY_METHOD={method!r}", file=sys.stderr)
        raise SystemExit(1)


if __name__ == "__main__":
    # manual test: NOTIFY_METHOD=discord DISCORD_WEBHOOK_URL=... python notifier.py "hi"
    send_notification(sys.argv[1] if len(sys.argv) > 1 else "test from chrome hearts monitor")
