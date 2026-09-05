#!/usr/bin/env python3
"""
Chrome Hearts new-product monitor (always-on / Railway edition).

Runs as a long-lived worker: every ~30s it sweeps all Chrome Hearts category
pages, builds the live product set, diffs against the last sweep, and posts any
new product (name + price + link) to Discord. State persists to disk so it
survives restarts -- on Railway, point CH_STATE_FILE at a mounted volume.

Chrome Hearts is a Salesforce Commerce Cloud (Demandware/SFRA) storefront behind
Cloudflare. Live category pages render product tiles server-side; each tile has a
`product-metadata` span (data-pid / data-name / data-price / data-category) plus
a canonical `/cat/subcat/PID.html` link. Empty/sold-out categories 200-redirect
to the homepage (no tiles) and simply contribute nothing.

Run modes:
    python chrome_hearts_monitor.py --loop     # always-on (Railway start cmd)
    python chrome_hearts_monitor.py --once      # one sweep, then exit (testing)
    python chrome_hearts_monitor.py --seed      # record catalog, notify nothing
    python chrome_hearts_monitor.py --once --dry-run   # detect + print, no send

Key env vars:
    NOTIFY_METHOD=discord            (see notifier.py)
    DISCORD_WEBHOOK_URL=...
    CH_STATE_FILE=/data/seen_products.json   # persistent volume path on Railway
    CH_POLL_SECONDS=30              # target seconds between sweep starts
    CH_MAX_INDIVIDUAL=8             # items per message in a large batch
    CH_STARTUP_PING=1               # send a "monitor online" Discord msg on boot
"""

from __future__ import annotations

import argparse
import json
import os
import random
import re
import sys
import time
from dataclasses import dataclass, asdict
from pathlib import Path
from urllib.parse import urlparse

import requests

# --------------------------------------------------------------------------- #
# Config
# --------------------------------------------------------------------------- #

BASE = "https://www.chromehearts.com"
STATE_FILE = Path(os.environ.get("CH_STATE_FILE", "seen_products.json"))
POLL_SECONDS = int(os.environ.get("CH_POLL_SECONDS", "30"))
MAX_INDIVIDUAL = int(os.environ.get("CH_MAX_INDIVIDUAL", "8"))
STARTUP_PING = os.environ.get("CH_STARTUP_PING", "1") == "1"
SUMMARY_MESSAGE_LIMIT = 1850  # leave headroom below Discord's 2,000-char limit

# Broad net: every known category slug. Live ones render grids; the rest are
# valid-but-usually-empty and populate when a drop lands -- which is the point.
# De-duplicated by PID, so alias slugs (bag/bags, hoodie/hoodies) are harmless.
CATEGORIES = [
    # usually live
    "socks", "scents", "baccarat", "intimates", "boxers-leggings",
    # valid, drops land here
    "hat", "eyewear", "sunglasses", "eyewear-accessories", "bag", "bags",
    "belt", "earring", "t-shirt", "t-shirts", "shirt", "shirts", "hoodie",
    "hoodies", "sweatshirt", "sweatshirts", "sweater", "jacket", "denim",
    "pants", "shorts", "shoes", "boots", "slippers", "home", "gloves",
    "scarf", "tie",
]

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}

# Small jittered gap between page fetches within a sweep so 34 pages read like a
# steady trickle (~1 req/s) rather than a synchronized burst that trips Cloudflare.
INTRA_DELAY = (0.2, 0.6)
REQUEST_TIMEOUT = 25
MAX_RETRIES = 3
MAX_DISCOVERED_CATEGORIES = 12

METADATA_RE = re.compile(
    r'<span[^>]*class="[^"]*product-metadata[^"]*"[^>]*></span>',
    re.IGNORECASE | re.DOTALL,
)
ATTR_RE = re.compile(r'data-([a-z]+)="([^"]*)"', re.IGNORECASE)
HREF_RE = re.compile(r'href="([^"]+)"', re.IGNORECASE)
LINK_RE_TMPL = r'href="(/[a-z0-9\-]+/[a-z0-9\-]+/{pid}\.html)"'

UTILITY_SLUGS = {
    "account", "cart", "checkout", "contact-us", "customer-service", "login",
    "order-status", "privacy-policy", "search", "stores", "wishlist",
}

SIZE_CODES = {
    "XSM": "Extra Small",
    "SML": "Small",
    "MED": "Medium",
    "LRG": "Large",
    "1XL": "XL",
    "2XL": "2XL",
    "3XL": "3XL",
    "4XL": "4XL",
    "5XL": "5XL",
    # Chrome Hearts sometimes uses XXX as a size-like SKU segment; keep it raw
    # instead of guessing wrong in a time-sensitive alert.
    "XXX": "XXX",
    "OS": "One Size",
}

SIZED_CATEGORY_SLUGS = {
    "boxers-leggings", "denim", "hoodie", "hoodies", "intimates", "jacket",
    "pants", "shirt", "shirts", "shorts", "sweater", "sweatshirt",
    "sweatshirts", "t-shirt", "t-shirts",
}


@dataclass
class Product:
    pid: str
    name: str
    price: str
    category: str
    url: str
    size: str = ""

    def pretty(self) -> str:
        price = f"${self.price}" if self.price else "price n/a"
        size = f"\nSize: {self.size}" if self.size else ""
        return f"{self.name} ({price}){size}\n{self.url}"


# --------------------------------------------------------------------------- #
# Crawl + parse
# --------------------------------------------------------------------------- #

def log(msg: str) -> None:
    print(f"{time.strftime('%Y-%m-%d %H:%M:%S')}  {msg}", flush=True)


def fetch(session: requests.Session, url: str) -> requests.Response | None:
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            r = session.get(url, timeout=REQUEST_TIMEOUT, allow_redirects=True)
            if r.status_code in (200, 404):
                return r
        except requests.RequestException as exc:
            log(f"  ! {url} attempt {attempt}: {exc}")
        time.sleep(attempt * 1.5)
    return None


def parse_products(html: str) -> dict[str, Product]:
    found: dict[str, Product] = {}
    for span in METADATA_RE.findall(html):
        attrs = {k.lower(): v for k, v in ATTR_RE.findall(span)}
        pid = attrs.get("pid", "").strip()
        if not pid:
            continue
        m = re.search(LINK_RE_TMPL.format(pid=re.escape(pid)), html)
        path = m.group(1) if m else f"/p/{pid}.html"
        category = attrs.get("category", "").strip()
        category_hint = category or path.strip("/").split("/", 1)[0]
        found[pid] = Product(
            pid=pid,
            name=attrs.get("name", "").strip() or "(unnamed)",
            price=attrs.get("price", "").strip(),
            category=category,
            url=BASE + path,
            size=infer_size(pid, category_hint),
        )
    return found


def category_slug(category: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", category.lower()).strip("-")


def infer_size(pid: str, category: str = "") -> str:
    """Infer apparel size from the SKU/PID without adding any extra requests."""
    if category and category_slug(category) not in SIZED_CATEGORY_SLUGS:
        return ""
    upper_pid = pid.upper()
    for code in sorted(SIZE_CODES, key=len, reverse=True):
        prefix_guard = r"(?<!X)" if code == "XXX" else ""
        if re.search(rf"{prefix_guard}{re.escape(code)}\d{{3}}$", upper_pid):
            label = SIZE_CODES[code]
            return label if label == code else f"{label} ({code})"
    return ""


def discover_category_paths(html: str) -> list[str]:
    """Find same-site top-level category URLs newly linked from fetched pages."""
    paths: list[str] = []
    for href in HREF_RE.findall(html):
        parsed = urlparse(href)
        if parsed.netloc and parsed.netloc != "www.chromehearts.com":
            continue
        path = parsed.path.rstrip("/")
        parts = [p for p in path.split("/") if p]
        if len(parts) != 1:
            continue
        slug = parts[0].lower()
        if slug in UTILITY_SLUGS or not re.fullmatch(r"[a-z0-9\-]+", slug):
            continue
        candidate = f"/{slug}"
        if candidate not in paths:
            paths.append(candidate)
    return paths


def crawl(session: requests.Session) -> dict[str, Product]:
    catalog: dict[str, Product] = {}
    queued = ["/"] + [f"/{c}" for c in CATEGORIES]
    seen: set[str] = set()
    discovered = 0
    for path in queued:
        if path in seen:
            continue
        seen.add(path)
        r = fetch(session, BASE + path)
        time.sleep(random.uniform(*INTRA_DELAY))
        if r is None or r.status_code != 200:
            continue
        html = r.text
        catalog.update(parse_products(html))  # empty pages add nothing
        for discovered_path in discover_category_paths(html):
            if discovered >= MAX_DISCOVERED_CATEGORIES:
                break
            if discovered_path in seen or discovered_path in queued:
                continue
            queued.append(discovered_path)
            discovered += 1
    return catalog


# --------------------------------------------------------------------------- #
# State
# --------------------------------------------------------------------------- #

def load_state() -> dict[str, dict]:
    try:
        return json.loads(STATE_FILE.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def save_state(catalog: dict[str, Product | dict]) -> None:
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = STATE_FILE.with_suffix(STATE_FILE.suffix + ".tmp")
    serialized = {
        pid: asdict(product) if isinstance(product, Product) else product
        for pid, product in catalog.items()
    }
    tmp.write_text(json.dumps(serialized, indent=2, ensure_ascii=False))
    tmp.replace(STATE_FILE)  # atomic, so a crash mid-write can't corrupt state


# --------------------------------------------------------------------------- #
# Notify
# --------------------------------------------------------------------------- #

def _send(body: str, *, suppress_embeds: bool = False) -> None:
    from notifier import send_notification
    send_notification(body, suppress_embeds=suppress_embeds)


def build_batch_messages(products: list[Product]) -> list[str]:
    """Format every item into readable, Discord-safe summary messages."""
    item_lines = []
    for p in products:
        price = f"${p.price}" if p.price else "price n/a"
        size = f" · {p.size}" if p.size else ""
        item_lines.append(
            f"• **{p.name}**{size} — {price} · [View item]({p.url})"
        )

    chunks: list[list[str]] = []
    current: list[str] = []
    current_chars = 0
    for line in item_lines:
        if current and (len(current) >= MAX_INDIVIDUAL or
                        current_chars + len(line) + 1 > SUMMARY_MESSAGE_LIMIT):
            chunks.append(current)
            current = []
            current_chars = 0
        current.append(line)
        current_chars += len(line) + 1
    if current:
        chunks.append(current)

    total = len(chunks)
    return [
        "\n".join([
            f"\U0001f6a8 {len(products)} new Chrome Hearts items — "
            f"batch {index}/{total}",
            *chunk,
        ])
        for index, chunk in enumerate(chunks, 1)
    ]


def notify_new(products: list[Product]) -> None:
    """Send small drops individually and paginate every item in large drops."""
    if len(products) <= MAX_INDIVIDUAL:
        for p in products:
            _send(f"\U0001f6a8 New Chrome Hearts drop\n{p.pretty()}")
            time.sleep(1)
        return
    for message in build_batch_messages(products):
        _send(message, suppress_embeds=True)
        time.sleep(1)


# --------------------------------------------------------------------------- #
# Sweep
# --------------------------------------------------------------------------- #

def sweep(session: requests.Session, *, seed: bool, dry_run: bool) -> None:
    catalog = crawl(session)
    previous = load_state()

    if seed or not previous:
        save_state(catalog)
        log(f"seeded {len(catalog)} products (no notifications).")
        return

    new = [catalog[pid] for pid in catalog if pid not in previous]
    seen = {**previous, **{pid: asdict(p) for pid, p in catalog.items()}}
    if not new:
        log(f"{len(catalog)} live, 0 new.")
        save_state(seen)
        return

    log(f"{len(catalog)} live, {len(new)} NEW:")
    for p in new:
        log("   + " + p.pretty().replace("\n", " | "))
    if dry_run:
        log("[dry-run] not sending.")
    else:
        notify_new(new)
        log("notified.")
    save_state(seen)


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #

def main() -> int:
    ap = argparse.ArgumentParser(description="Chrome Hearts new-product monitor")
    ap.add_argument("--loop", action="store_true", help="run forever (Railway)")
    ap.add_argument("--once", action="store_true", help="one sweep then exit")
    ap.add_argument("--seed", action="store_true", help="record catalog, no alerts")
    ap.add_argument("--dry-run", action="store_true", help="detect but never send")
    args = ap.parse_args()

    session = requests.Session()
    session.headers.update(HEADERS)

    if args.seed:
        sweep(session, seed=True, dry_run=True)
        return 0
    if args.once:
        sweep(session, seed=False, dry_run=args.dry_run)
        return 0
    if not args.loop:
        ap.error("choose a mode: --loop, --once, or --seed")

    log(f"Chrome Hearts monitor online. {len(CATEGORIES)} base categories + homepage, "
        f"~{POLL_SECONDS}s sweeps. state={STATE_FILE}")
    if STARTUP_PING and not args.dry_run:
        try:
            _send(f"\U0001f7e2 Chrome Hearts monitor online — watching "
                  f"{len(CATEGORIES)} base categories + discovered links, "
                  f"~{POLL_SECONDS}s sweeps.")
        except Exception as exc:  # don't die if the first ping fails
            log(f"startup ping failed: {exc}")

    while True:
        t0 = time.monotonic()
        try:
            sweep(session, seed=False, dry_run=args.dry_run)
        except Exception as exc:  # never let one bad sweep kill the worker
            log(f"sweep error (continuing): {exc!r}")
        elapsed = time.monotonic() - t0
        time.sleep(max(2.0, POLL_SECONDS - elapsed) + random.uniform(0, 4))


if __name__ == "__main__":
    raise SystemExit(main())
