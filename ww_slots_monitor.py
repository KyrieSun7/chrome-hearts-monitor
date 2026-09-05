#!/usr/bin/env python3
"""
Chrome Hearts appointment-slot monitor (Waitwhile, always-on / Railway edition).

Watches the Waitwhile booking calendar for the Chrome Hearts New York West
Village store and posts to Discord the moment any time slot becomes bookable —
a cancellation freeing a spot, or a new day/block of slots being released.

How it works (verified against the live API):
  - Waitwhile exposes an unauthenticated public API. One call returns the whole
    booking window:
      GET https://api.waitwhile.com/v2/public/visits/{shortname}/availability
          ?fromDate=YYYY-MM-DDThh:mm&toDate=YYYY-MM-DDThh:mm
    Each slot: {"date": "2026-08-23T17:00" (location-local), "duration": 1800,
    "numSpots", "numBookedSpots", "numAvailableSpots",
    "numAvailableSpotsByServiceId": {...}, ...}
  - A slot is OPEN iff numAvailableSpots > 0. The store is usually fully booked
    wall-to-wall, so open slots are rare and worth an instant alert.
  - Service names come from GET /v2/public/locations/{shortname}
    (servicesById), fetched at startup so renames/additions self-heal.
  - "first-available-dates" only lists calendar-selectable dates, NOT real
    capacity — do not use it as the open/closed signal.

Diff semantics: state = the set of currently-open slots. A slot alerting, being
booked (drops out), then freeing up again alerts again — each reopening is
actionable. All newly-open slots in one sweep are sent as ONE Discord message
(a released day can be 16 slots; nobody wants 16 pings).

Run modes:
    python ww_slots_monitor.py --loop           # always-on (Railway start cmd)
    python ww_slots_monitor.py --once           # one sweep, then exit
    python ww_slots_monitor.py --seed           # record current state, no alerts
    python ww_slots_monitor.py --once --dry-run # detect + print, never send

Key env vars:
    NOTIFY_METHOD=discord / DISCORD_WEBHOOK_URL=...   (see notifier.py)
    WW_LOCATION=chromehearts        # Waitwhile shortname (NY West Village)
    WW_STATE_FILE=/data/ww_slots.json
    WW_POLL_SECONDS=20              # one API request per sweep -> can be tight
    WW_DAYS_AHEAD=21                # how far ahead to watch
    WW_SERVICE_FILTER=              # optional: comma-sep service ids or name
                                    #   substrings, e.g. "in-store" to only
                                    #   alert on In-store shopping capacity
    WW_STARTUP_PING=1
"""

from __future__ import annotations

import argparse
import json
import os
import random
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import requests

# --------------------------------------------------------------------------- #
# Config
# --------------------------------------------------------------------------- #

API = "https://api.waitwhile.com/v2/public"
LOCATION = os.environ.get("WW_LOCATION", "chromehearts")
BOOK_URL = os.environ.get(
    "WW_BOOK_URL", f"https://waitwhile.com/locations/{LOCATION}/time?registration=booking")
STATE_FILE = Path(os.environ.get("WW_STATE_FILE", "ww_slots.json"))
POLL_SECONDS = int(os.environ.get("WW_POLL_SECONDS", "20"))
DAYS_AHEAD = int(os.environ.get("WW_DAYS_AHEAD", "21"))
SERVICE_FILTER = [t.strip().lower() for t in
                  os.environ.get("WW_SERVICE_FILTER", "").split(",") if t.strip()]
STARTUP_PING = os.environ.get("WW_STARTUP_PING", "1") == "1"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36"
    ),
    "Accept": "application/json",
    "Origin": "https://waitwhile.com",
    "Referer": "https://waitwhile.com/",
}
REQUEST_TIMEOUT = 25
MAX_ALERT_LINES = 12   # list at most this many slots in one message


def log(msg: str) -> None:
    print(f"{time.strftime('%Y-%m-%d %H:%M:%S')}  {msg}", flush=True)


# --------------------------------------------------------------------------- #
# API
# --------------------------------------------------------------------------- #

def get_json(session: requests.Session, url: str, params: dict | None = None):
    for attempt in (1, 2, 3):
        try:
            r = session.get(url, params=params, timeout=REQUEST_TIMEOUT)
            if r.status_code == 200:
                return r.json()
            log(f"  ! {url} -> HTTP {r.status_code}: {r.text[:120]}")
        except (requests.RequestException, ValueError) as exc:
            log(f"  ! {url} attempt {attempt}: {exc}")
        time.sleep(attempt * 1.5)
    return None


def load_location(session: requests.Session) -> tuple[dict[str, str], ZoneInfo, str]:
    """Return (serviceId->name, tz, display name); safe fallbacks on failure."""
    services: dict[str, str] = {}
    tz = ZoneInfo("America/New_York")
    name = LOCATION
    data = get_json(session, f"{API}/locations/{LOCATION}")
    if isinstance(data, dict):
        name = data.get("name") or name
        try:
            tz = ZoneInfo(data.get("timeZone") or "America/New_York")
        except Exception:
            pass
        by_id = data.get("servicesById") or {}
        for sid, sv in by_id.items():
            if isinstance(sv, dict) and sv.get("name"):
                services[sid] = sv["name"]
    return services, tz, name


def fetch_slots(session: requests.Session, tz: ZoneInfo) -> list[dict]:
    now = datetime.now(tz)
    params = {
        "fromDate": now.strftime("%Y-%m-%dT00:00"),
        "toDate": (now + timedelta(days=DAYS_AHEAD)).strftime("%Y-%m-%dT23:59"),
    }
    data = get_json(session, f"{API}/visits/{LOCATION}/availability", params)
    return data if isinstance(data, list) else []


# --------------------------------------------------------------------------- #
# Open-slot logic
# --------------------------------------------------------------------------- #

def slot_services_open(slot: dict) -> dict[str, int]:
    return {k: v for k, v in (slot.get("numAvailableSpotsByServiceId") or {}).items()
            if isinstance(v, int) and v > 0}


def passes_filter(slot: dict, services: dict[str, str]) -> bool:
    if not SERVICE_FILTER:
        return True
    by_svc = slot_services_open(slot)
    if not by_svc:          # no per-service detail -> don't drop a real opening
        return True
    for sid in by_svc:
        label = (services.get(sid, sid)).lower()
        if any(t in label or t == sid.lower() for t in SERVICE_FILTER):
            return True
    return False


def open_slots(slots: list[dict], services: dict[str, str],
               tz: ZoneInfo) -> dict[str, dict]:
    """Map slot-local-datetime -> details, for future slots with spots free."""
    now_local = datetime.now(tz).replace(tzinfo=None)
    out: dict[str, dict] = {}
    for s in slots:
        if s.get("numAvailableSpots", 0) <= 0:
            continue
        try:
            when = datetime.strptime(s["date"], "%Y-%m-%dT%H:%M")
        except (KeyError, ValueError):
            continue
        if when < now_local:            # already started/past
            continue
        if not passes_filter(s, services):
            continue
        out[s["date"]] = {
            "avail": s["numAvailableSpots"],
            "services": {services.get(k, k): v
                         for k, v in slot_services_open(s).items()},
        }
    return out


def fmt_slot(iso_local: str, info: dict, tz: ZoneInfo) -> str:
    when = datetime.strptime(iso_local, "%Y-%m-%dT%H:%M").replace(tzinfo=tz)
    stamp = when.strftime("%a %b %d, %I:%M %p").replace(" 0", " ").lstrip("0")
    svc = ""
    if info.get("services"):
        svc = " [" + ", ".join(f"{n}: {v}" for n, v in info["services"].items()) + "]"
    return f"{stamp} {when.strftime('%Z')} — {info['avail']} spot(s){svc}"


def build_alert(new: dict[str, dict], location_name: str, tz: ZoneInfo) -> str:
    lines = [f"\U0001f7e2 OPEN SLOT{'S' if len(new) > 1 else ''} — {location_name}"]
    for iso in sorted(new)[:MAX_ALERT_LINES]:
        lines.append(f"\u2022 {fmt_slot(iso, new[iso], tz)}")
    if len(new) > MAX_ALERT_LINES:
        lines.append(f"...and {len(new) - MAX_ALERT_LINES} more.")
    lines.append(f"Book now: {BOOK_URL}")
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# State
# --------------------------------------------------------------------------- #

def load_state() -> dict:
    try:
        return json.loads(STATE_FILE.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def save_state(open_now: dict[str, dict]) -> None:
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = STATE_FILE.with_suffix(STATE_FILE.suffix + ".tmp")
    tmp.write_text(json.dumps(open_now, indent=2))
    tmp.replace(STATE_FILE)


# --------------------------------------------------------------------------- #
# Sweep
# --------------------------------------------------------------------------- #

def _send(body: str) -> None:
    from notifier import send_notification
    send_notification(body)


def sweep(session: requests.Session, services: dict[str, str], tz: ZoneInfo,
          location_name: str, *, seed: bool, dry_run: bool,
          first_run: bool) -> None:
    slots = fetch_slots(session, tz)
    if not slots:
        log("no slot data returned (API hiccup?) — keeping previous state.")
        return

    current = open_slots(slots, services, tz)
    previous = load_state()

    if seed or first_run:
        save_state(current)
        log(f"seeded: {len(slots)} slots in window, {len(current)} open. "
            f"No notifications.")
        return

    new = {k: v for k, v in current.items() if k not in previous}
    if new:
        log(f"{len(current)} open now, {len(new)} NEWLY open:")
        for k in sorted(new):
            log("   + " + fmt_slot(k, new[k], tz))
        if dry_run:
            log("[dry-run] not sending.")
        else:
            _send(build_alert(new, location_name, tz))
            log("notified.")
    else:
        log(f"{len(slots)} slots in window, {len(current)} open, 0 newly open.")
    save_state(current)


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #

def main() -> int:
    ap = argparse.ArgumentParser(description="Waitwhile open-slot monitor")
    ap.add_argument("--loop", action="store_true", help="run forever (Railway)")
    ap.add_argument("--once", action="store_true", help="one sweep then exit")
    ap.add_argument("--seed", action="store_true", help="record state, no alerts")
    ap.add_argument("--dry-run", action="store_true", help="detect but never send")
    args = ap.parse_args()

    session = requests.Session()
    session.headers.update(HEADERS)

    services, tz, location_name = load_location(session)
    log(f"location: {location_name} | tz={tz.key} | "
        f"services: {', '.join(services.values()) or '(none found)'}")

    first_run = not STATE_FILE.exists()

    if args.seed:
        sweep(session, services, tz, location_name,
              seed=True, dry_run=True, first_run=True)
        return 0
    if args.once:
        sweep(session, services, tz, location_name,
              seed=False, dry_run=args.dry_run, first_run=first_run)
        return 0
    if not args.loop:
        ap.error("choose a mode: --loop, --once, or --seed")

    log(f"Waitwhile slot monitor online. window={DAYS_AHEAD}d, "
        f"~{POLL_SECONDS}s sweeps, state={STATE_FILE}")
    if STARTUP_PING and not args.dry_run:
        try:
            _send(f"\U0001f7e2 Slot monitor online — {location_name}, "
                  f"watching {DAYS_AHEAD} days ahead, ~{POLL_SECONDS}s sweeps.")
        except Exception as exc:
            log(f"startup ping failed: {exc}")

    while True:
        t0 = time.monotonic()
        try:
            sweep(session, services, tz, location_name,
                  seed=False, dry_run=args.dry_run,
                  first_run=not STATE_FILE.exists())
        except Exception as exc:
            log(f"sweep error (continuing): {exc!r}")
        elapsed = time.monotonic() - t0
        time.sleep(max(2.0, POLL_SECONDS - elapsed) + random.uniform(0, 3))


if __name__ == "__main__":
    raise SystemExit(main())
