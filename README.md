# Chrome Hearts new-product monitor (Railway, always-on)

Watches [chromehearts.com](https://www.chromehearts.com) continuously and posts
to Discord the moment a new product appears — name, price, and direct link.

```
🚨 New Chrome Hearts drop
CH LOGO SOCKS ($255.00)
https://www.chromehearts.com/socks/ch-logo-socks/176354XXXXXX349.html
```

For apparel links that encode size in the product id, alerts include the
inferred size without making any extra requests:

```
🚨 New Chrome Hearts drop
SHORT SLEEVE POCKET CREW ($320.00)
Size: Medium (MED)
https://www.chromehearts.com/shirt/short-sleeve-pocket-crew/129111BLKMED756.html
```

## How it works

Chrome Hearts runs a Salesforce Commerce Cloud (Demandware/SFRA) storefront
behind Cloudflare with a small, fast-rotating web catalog. Live category pages
render product tiles server-side; each tile exposes a `product-metadata` span
(product id, name, price) plus a canonical `/cat/subcat/PID.html` link. Sold-out
or empty categories transparently redirect to the homepage (no tiles), so they
just contribute nothing.

The worker runs forever. Each ~30s sweep:
1. Fetches the homepage + every category in `CATEGORIES` as a gentle, jittered
   trickle of requests (reads like browsing, not a burst — keeps Cloudflare calm).
2. Builds the live product set, keyed by product id (PID).
3. Diffs against the saved snapshot; PIDs never seen before are "new."
4. Infers size from the PID/link when Chrome Hearts encodes one in the SKU.
5. Posts new items to Discord, then saves the snapshot.

Keying on PID means it catches genuinely new items even inside categories that
already had products. The first sweep with no prior snapshot **seeds silently**
(records the catalog, sends nothing), so you never get flooded on boot.

## Deploy on Railway

1. Push this repo to GitHub.
2. Railway -> **New Project -> Deploy from GitHub repo** -> pick this repo.
   Railway auto-detects Python and uses the `Procfile`
   (`worker: python chrome_hearts_monitor.py --loop`). No port/domain needed —
   it's a background worker.
3. **Variables** (Settings -> Variables): add
   - `NOTIFY_METHOD = discord`
   - `DISCORD_WEBHOOK_URL = <your webhook>`
   - `CH_STATE_FILE = /data/seen_products.json`
4. **Volume** (so state survives restarts — important): add a Volume to the
   service mounted at `/data`. Without it a redeploy wipes the snapshot and the
   monitor re-seeds (harmless, but you lose history; with it, nothing is lost).
5. Deploy. You should get a "monitor online" Discord ping within a minute, then
   alerts as drops land.

> The container is always-on by design (no scale-to-zero) — that's what makes
> sub-minute detection possible. It's a tiny process and costs very little.

## Discord webhook

Discord -> Server Settings -> Integrations -> Webhooks -> New Webhook -> pick a
channel -> Copy Webhook URL -> use it as `DISCORD_WEBHOOK_URL`. The URL itself is
the secret; keep it in Railway Variables, never in the repo. Install the Discord
app and enable that channel's notifications to get pings on your phone.

## Appointment-slot monitor

`ww_slots_monitor.py` is a second always-on worker that watches the Waitwhile
calendar for the Chrome Hearts New York West Village store and sends one Discord
message whenever appointment slots become bookable. It uses Waitwhile's public,
unauthenticated availability endpoint, which returns the whole configured window
in one request; a slot is open only when `numAvailableSpots > 0`. Service names
come from the public location endpoint at startup. The monitor deliberately does
not use `first-available-dates`, because those are calendar-selectable dates, not
proof of real capacity. It snapshots currently open datetimes, so a slot that is
booked and later reopens alerts again, while multiple openings in one sweep are
grouped into one message.

Alert links start at Waitwhile's service-selection step rather than linking
directly to the calendar. A calendar URL without a selected service can display
times but rejects the user's choice later in the flow. Waitwhile supports
service and party-size query parameters, but the monitor intentionally leaves
those choices to the customer because openings can apply to multiple services
and party size is user-specific.

### Production deployment

The appointment monitor is deployed in the existing Railway project as a
separate service named `chrome-hearts-appointments`. It uses this repository's
`main` branch with the following **Custom Start Command**:

```text
python ww_slots_monitor.py --loop
```

This service has its own Railway Volume mounted at `/data` and sends appointment
notifications to the Discord `#nyc-instore` channel. The webhook URL is stored
only in Railway Variables and must never be committed to the repository.

The production variables are:

| Variable | Value / purpose |
|----------|-----------------|
| `DISCORD_WEBHOOK_URL` | Secret webhook for `#nyc-instore` |
| `NOTIFY_METHOD` | `discord` |
| `WW_LOCATION` | `chromehearts` |
| `WW_STATE_FILE` | `/data/ww_slots.json` |
| `WW_POLL_SECONDS` | `20` |
| `WW_DAYS_AHEAD` | `21` |
| `WW_STARTUP_PING` | `1` |

The deployment was verified end to end: Railway mounted the dedicated volume,
resolved “Chrome Hearts - New York West Village” and its five live service
types, seeded 324 slots, completed subsequent polls, and successfully delivered
both the startup notification and a labeled test message to Discord.

To recreate the service, add the same GitHub repository as a second Railway
service, apply the custom start command and variables above, attach a dedicated
volume at `/data`, and deploy. The first sweep seeds silently; subsequent newly
open slots trigger alerts. The repo-level `Procfile` remains reserved for the
product-monitor service.

## Local testing

```bash
pip install -r requirements.txt
cp .env.example .env            # fill in DISCORD_WEBHOOK_URL
set -a && source .env && set +a

python chrome_hearts_monitor.py --seed             # record catalog, no alerts
python chrome_hearts_monitor.py --once --dry-run   # detect + print, send nothing
python chrome_hearts_monitor.py --once             # one real sweep
python chrome_hearts_monitor.py --loop             # what Railway runs

python ww_slots_monitor.py --seed                  # record open slots, no alerts
python ww_slots_monitor.py --once --dry-run        # detect + print, send nothing
python ww_slots_monitor.py --once                  # one real appointment sweep
python ww_slots_monitor.py --loop                  # appointment Railway service
```

## Tuning (env vars)

| Var | Default | Meaning |
|-----|---------|---------|
| `CH_POLL_SECONDS` | `30` | target seconds between sweep starts |
| `CH_MAX_INDIVIDUAL` | `8` | more new than this in one sweep -> one summary message |
| `CH_STARTUP_PING` | `1` | send a "monitor online" Discord ping on boot |
| `CH_STATE_FILE` | `seen_products.json` | snapshot path (set to `/data/...` on Railway) |

Appointment monitor:

| Var | Default | Meaning |
|-----|---------|---------|
| `WW_LOCATION` | `chromehearts` | Waitwhile location shortname |
| `WW_POLL_SECONDS` | `20` | target seconds between availability sweeps |
| `WW_DAYS_AHEAD` | `21` | number of calendar days to request |
| `WW_SERVICE_FILTER` | empty | optional service-ID or name-substring filter |
| `WW_STARTUP_PING` | `1` | send a "slot monitor online" Discord ping on boot |
| `WW_STATE_FILE` | `ww_slots.json` | open-slot snapshot path (use `/data/ww_slots.json` on Railway) |

## Categories

`CATEGORIES` in `chrome_hearts_monitor.py` is the full known slug list — the few
that are usually live plus ~28 valid-but-usually-empty ones that populate when a
drop lands. Each sweep also discovers same-site top-level category links from
the homepage/category HTML and crawls a small capped number of those dynamically,
so a newly linked slug can be checked before it is manually added to the list.

**Known gap:** fine jewelry (rings/necklaces/bracelets) has no working top-level
slug observed live; `/ring`, `/jewelry`, etc. 404. The homepage sweep plus
dynamic link discovery is the safety net for featured/newly linked drops
meanwhile — once a real jewelry drop is caught, read its category from the
product URL and add that slug here.

## Other notification backends

`send_notification()` in `notifier.py` also supports Twilio SMS and carrier
email-to-SMS; set `NOTIFY_METHOD` accordingly with the matching env vars.

This scrapes a public site for personal use — keep the cadence reasonable.
