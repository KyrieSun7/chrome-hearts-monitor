#!/usr/bin/env python3
"""
Chrome Hearts appointment-slot monitor — Los Angeles / Malibu (Appointedd).

The LA store books through Appointedd (https://chrome-hearts-los-angeles.appointedd.com/),
NOT Waitwhile. Its booking widget talks to a GraphQL API:
    POST https://graphql.services.appointedd.com/
with a STATIC public "access-token" header baked into the widget bundle
(booking-tools.appointedd.com/main.<hash>.js). This monitor extracts that token
at startup (and re-extracts on 401), so it self-heals if Appointedd rotates it.

Availability query (verified live):
    GetOrganisationAvailabilityIntervals(id, serviceId, ranges, duration, spaces,
                                         timezone, meta) -> availabilityIntervals
Each open slot is an `AvailableInterval` {start, end (UTC ISO), remainingSpaces}.
`GroupBookingInterval` entries are group bookings — ignored.

The organisation (5f63c04c102373240c4e3d54, tz America/Los_Angeles) has TWO
locations, each with its own service ids:
    CHROME HEARTS LOS ANGELES   IN-STORE VISIT   5f649cb02f6894080a6a49e4
                                REPAIR DROP OFF  5f649b5e9c476f6c0a4557a2
                                REPAIR PICK UP   5f64a86c44a4f86af76f6212
    CHROME HEARTS MALIBU        IN-STORE VISIT   611d5b11f59673555d177f22
                                REPAIR DROP OFF  611d5b40f59673555d177f23
                                REPAIR PICK UP   611d5b6142e5f81feb08c842

Diff semantics mirror ww_slots_monitor.py: state = set of currently-open slots
per service; newly-open -> ONE Discord message per sweep; a slot that closes and
reopens alerts again.

Run modes:
    python appointedd_slots_monitor.py --loop | --once | --seed | --once --dry-run

Key env vars:
    NOTIFY_METHOD=discord / DISCORD_WEBHOOK_URL=...
    APPT_SERVICES="LA In-store=5f649cb02f6894080a6a49e4"   # comma-separated
        label=serviceId pairs. Add Malibu / repair services as desired.
    APPT_STATE_FILE=/data/appt_slots.json
    APPT_POLL_SECONDS=30
    APPT_DAYS_AHEAD=45
    APPT_ACCESS_TOKEN=            # optional manual override of the public token
    APPT_STARTUP_PING=1
"""

from __future__ import annotations

import argparse
import json
import os
import random
import re
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import requests

# --------------------------------------------------------------------------- #
# Config
# --------------------------------------------------------------------------- #

GQL = "https://graphql.services.appointedd.com/"
WIDGET = "https://booking-tools.appointedd.com/"
ORG_ID = os.environ.get("APPT_ORG_ID", "5f63c04c102373240c4e3d54")
BOOK_URL = os.environ.get("APPT_BOOK_URL",
                          "https://chrome-hearts-los-angeles.appointedd.com/")
STATE_FILE = Path(os.environ.get("APPT_STATE_FILE", "appt_slots.json"))
POLL_SECONDS = int(os.environ.get("APPT_POLL_SECONDS", "30"))
DAYS_AHEAD = int(os.environ.get("APPT_DAYS_AHEAD", "45"))
STARTUP_PING = os.environ.get("APPT_STARTUP_PING", "1") == "1"
DURATION_MIN = int(os.environ.get("APPT_DURATION", "30"))
MAX_ALERT_LINES = 15

DEFAULT_SERVICES = "LA In-store=5f649cb02f6894080a6a49e4"


def parse_services() -> dict[str, str]:
    """'Label=id,Label2=id2' -> {label: id}"""
    out: dict[str, str] = {}
    for pair in os.environ.get("APPT_SERVICES", DEFAULT_SERVICES).split(","):
        if "=" in pair:
            label, sid = pair.split("=", 1)
            out[label.strip()] = sid.strip()
    return out


SERVICES = parse_services()

HEADERS = {
    "Content-Type": "application/json",
    "Accept": "application/json",
    "Origin": "https://booking-tools.appointedd.com",
    "Referer": "https://booking-tools.appointedd.com/",
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36"
    ),
}
REQUEST_TIMEOUT = 30

Q_ORG = """query GetOrganisation($id: ObjectID!) {
  organisation(id: $id) { _id name defaultTimeZone slug } }"""

Q_INTERVALS = """query GetOrganisationAvailabilityIntervals($id: ObjectID!, $serviceId: ObjectID!, $ranges: [AvailabilityRange!]!, $duration: Int!, $spaces: Int!, $timezone: String!, $meta: AvailabilityRequestMetaData!) {
  organisation(id: $id) {
    _id
    availabilityIntervals(ranges: $ranges duration: $duration serviceId: $serviceId spaces: $spaces timezone: $timezone meta: $meta) {
      __typename
      ... on AvailableInterval { start end resourceIds remainingSpaces }
    }
  }
}"""


def log(msg: str) -> None:
    print(f"{time.strftime('%Y-%m-%d %H:%M:%S')}  {msg}", flush=True)


# --------------------------------------------------------------------------- #
# Token discovery (static public token inside the widget bundle)
# --------------------------------------------------------------------------- #

# Last-known token, used only if live extraction fails. Static as of build.
FALLBACK_TOKEN = ("9HzWf7vzQS3pnUMF++dT2G9E8yxY+siKWy54TRyUTqaLOXiC34+78WGXbRA1DoJmcxdn5"
                  "BaNUXZN6jZYHrPU2a4Zmy5sM8/zWiMZEvT5caH1kbnvZJAozG8zK494s2ttjxwh9p6XNBA9"
                  "NXpwtjqG3YzrKlINg9G+LCsqvp6vXJA=")


def discover_token(session: requests.Session) -> str:
    env = os.environ.get("APPT_ACCESS_TOKEN", "").strip()
    if env:
        return env
    try:
        page = session.get(f"{WIDGET}?organisationId={ORG_ID}",
                           timeout=REQUEST_TIMEOUT).content.decode("utf-8", "ignore")
        m = re.search(r'(main\.[0-9a-f]{8,}\.js)', page)
        if m:
            # decode bytes explicitly: requests' guessed text encoding can mangle
            # this 4 MB bundle and silently break the regex below
            js = session.get(WIDGET + m.group(1), timeout=60).content.decode("utf-8", "ignore")
            # token is the string returned for the production environment
            t = re.search(r'"production"===\w\?"([A-Za-z0-9+/=]{100,})"', js)
            if t:
                return t.group(1)
            # generic fallback: a long base64 blob with '+' and '/' ending in '='
            for cand in re.findall(r'"([A-Za-z0-9+/]{140,220}=)"', js):
                if "+" in cand and "/" in cand:
                    return cand
    except requests.RequestException as exc:
        log(f"token discovery failed: {exc}")
    log("using fallback token")
    return FALLBACK_TOKEN


# --------------------------------------------------------------------------- #
# GraphQL
# --------------------------------------------------------------------------- #

class Client:
    def __init__(self) -> None:
        self.s = requests.Session()
        self.s.headers.update(HEADERS)
        self.token = discover_token(self.s)
        self.session_id = f"mon{random.randint(100000, 999999)}"

    def query(self, op: str, q: str, variables: dict):
        for attempt in (1, 2):
            try:
                r = self.s.post(GQL, json={"operationName": op, "variables": variables,
                                           "query": q},
                                headers={"access-token": self.token},
                                timeout=REQUEST_TIMEOUT)
            except requests.RequestException as exc:
                log(f"  ! {op}: {exc}")
                time.sleep(2)
                continue
            if r.status_code == 401 and attempt == 1:
                log("  401 — re-discovering access token")
                self.token = discover_token(self.s)
                continue
            if r.status_code != 200:
                log(f"  ! {op} -> HTTP {r.status_code}: {r.text[:150]}")
                return None
            try:
                return r.json()
            except ValueError:
                return None
        return None

    def org(self) -> tuple[str, ZoneInfo]:
        d = self.query("GetOrganisation", Q_ORG, {"id": ORG_ID}) or {}
        o = (d.get("data") or {}).get("organisation") or {}
        tz = ZoneInfo(o.get("defaultTimeZone") or "America/Los_Angeles")
        return o.get("name") or "Chrome Hearts Los Angeles", tz

    def intervals(self, service_id: str, tz: ZoneInfo) -> list[dict] | None:
        now = datetime.now(timezone.utc)
        v = {
            "id": ORG_ID, "serviceId": service_id, "duration": DURATION_MIN,
            "spaces": 1, "timezone": tz.key, "meta": {"sessionId": self.session_id},
            "ranges": [{"start": now.strftime("%Y-%m-%dT00:00:00.000Z"),
                        "end": (now + timedelta(days=DAYS_AHEAD)).strftime(
                            "%Y-%m-%dT00:00:00.000Z")}],
        }
        d = self.query("GetOrganisationAvailabilityIntervals", Q_INTERVALS, v)
        if not d:
            return None
        return ((d.get("data") or {}).get("organisation") or {}).get(
            "availabilityIntervals") or []


# --------------------------------------------------------------------------- #
# Open-slot logic
# --------------------------------------------------------------------------- #

def open_slots(client: Client, tz: ZoneInfo) -> dict[str, dict] | None:
    """key 'label|startUTC' -> {label, start(utc iso), spaces}; None on total failure."""
    out: dict[str, dict] = {}
    any_ok = False
    now = datetime.now(timezone.utc)
    for label, sid in SERVICES.items():
        ivs = client.intervals(sid, tz)
        if ivs is None:
            log(f"  {label}: no data (API hiccup)")
            continue
        any_ok = True
        for iv in ivs:
            if iv.get("__typename") != "AvailableInterval" or not iv.get("start"):
                continue
            try:
                start = datetime.fromisoformat(iv["start"].replace("Z", "+00:00"))
            except ValueError:
                continue
            if start < now:
                continue
            out[f"{label}|{iv['start']}"] = {
                "label": label, "start": iv["start"],
                "spaces": iv.get("remainingSpaces"),
            }
    return out if any_ok else None


def fmt_slot(info: dict, tz: ZoneInfo) -> str:
    start = datetime.fromisoformat(info["start"].replace("Z", "+00:00")).astimezone(tz)
    stamp = start.strftime("%a %b %d, %I:%M %p").replace(" 0", " ")
    stamp = re.sub(r", 0(\d)", r", \1", stamp)
    sp = f" ({info['spaces']} left)" if info.get("spaces") else ""
    return f"{stamp} {start.strftime('%Z')}{sp}"


def build_alert(new: dict[str, dict], tz: ZoneInfo, org_name: str) -> str:
    lines = [f"\U0001f7e2 OPEN SLOT{'S' if len(new) > 1 else ''} — {org_name}"]
    by_label: dict[str, list[dict]] = {}
    for info in sorted(new.values(), key=lambda i: (i["label"], i["start"])):
        by_label.setdefault(info["label"], []).append(info)
    shown = 0
    for label, items in by_label.items():
        lines.append(f"**{label}**")
        for info in items:
            if shown >= MAX_ALERT_LINES:
                break
            lines.append(f"\u2022 {fmt_slot(info, tz)}")
            shown += 1
    if len(new) > MAX_ALERT_LINES:
        lines.append(f"...and {len(new) - MAX_ALERT_LINES} more.")
    lines.append(f"Book now: {BOOK_URL}")
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# State / notify / sweep
# --------------------------------------------------------------------------- #

def load_state() -> dict:
    try:
        return json.loads(STATE_FILE.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def save_state(cur: dict) -> None:
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = STATE_FILE.with_suffix(STATE_FILE.suffix + ".tmp")
    tmp.write_text(json.dumps(cur, indent=2))
    tmp.replace(STATE_FILE)


def _send(body: str) -> None:
    from notifier import send_notification
    send_notification(body)


def sweep(client: Client, tz: ZoneInfo, org_name: str, *, seed: bool,
          dry_run: bool, first_run: bool) -> None:
    current = open_slots(client, tz)
    if current is None:
        log("all services failed this sweep — keeping previous state.")
        return
    previous = load_state()
    if seed or first_run:
        save_state(current)
        log(f"seeded: {len(current)} open slot(s) across {len(SERVICES)} service(s). "
            f"No notifications.")
        return
    new = {k: v for k, v in current.items() if k not in previous}
    if new:
        log(f"{len(current)} open now, {len(new)} NEWLY open:")
        for k in sorted(new):
            log("   + " + fmt_slot(new[k], tz) + f"  [{new[k]['label']}]")
        if dry_run:
            log("[dry-run] not sending.")
        else:
            _send(build_alert(new, tz, org_name))
            log("notified.")
    else:
        log(f"{len(current)} open, 0 newly open.")
    save_state(current)


def main() -> int:
    ap = argparse.ArgumentParser(description="Appointedd open-slot monitor")
    ap.add_argument("--loop", action="store_true")
    ap.add_argument("--once", action="store_true")
    ap.add_argument("--seed", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    client = Client()
    org_name, tz = client.org()
    log(f"org: {org_name} | tz={tz.key} | token={'ok' if client.token else 'MISSING'} | "
        f"watching: {', '.join(SERVICES) or '(none)'}")
    if not SERVICES:
        ap.error("APPT_SERVICES is empty")

    if args.seed:
        sweep(client, tz, org_name, seed=True, dry_run=True, first_run=True)
        return 0
    if args.once:
        sweep(client, tz, org_name, seed=False, dry_run=args.dry_run,
              first_run=not STATE_FILE.exists())
        return 0
    if not args.loop:
        ap.error("choose a mode: --loop, --once, or --seed")

    log(f"Appointedd slot monitor online. window={DAYS_AHEAD}d, ~{POLL_SECONDS}s sweeps, "
        f"state={STATE_FILE}")
    if STARTUP_PING and not args.dry_run:
        try:
            _send(f"\U0001f7e2 Slot monitor online — {org_name} "
                  f"({', '.join(SERVICES)}), {DAYS_AHEAD}d window, ~{POLL_SECONDS}s sweeps.")
        except Exception as exc:
            log(f"startup ping failed: {exc}")

    while True:
        t0 = time.monotonic()
        try:
            sweep(client, tz, org_name, seed=False, dry_run=args.dry_run,
                  first_run=not STATE_FILE.exists())
        except Exception as exc:
            log(f"sweep error (continuing): {exc!r}")
        time.sleep(max(2.0, POLL_SECONDS - (time.monotonic() - t0))
                   + random.uniform(0, 4))


if __name__ == "__main__":
    raise SystemExit(main())

