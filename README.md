# 🚢 Exim Shipment Tracker

An automated multi-carrier container-tracking system for Exim Routes. It looks
up live shipment status (port of discharge, ETA, current status) across major
shipping lines and keeps a shared **Google Sheet** up to date automatically —
replacing the manual "open each carrier's website and copy the ETA" workflow.

The system prefers each carrier's **direct HTTP/JSON API** for speed and only
falls back to a headless **Chrome browser** for carriers whose sites are behind
anti-bot protection.

---

## Key features

- **11 shipping lines** supported, 5 of them via fast direct APIs.
- **Google Sheets sync** — the team types BL numbers into a shared sheet; the
  server fills in POD / ETA / Status automatically every couple of minutes.
- **Web API + lookup UI** — a FastAPI service for on-demand single-BL tracking.
- **Excel export** — the same data can be written to an `.xlsx` report.
- **Resilient by design** — gradual (rate-limited) syncing to avoid IP bans,
  a consistent `No data found` message on failures, and automatic Chrome
  fallback when an API path is unavailable.

---

## Supported carriers

| Carrier | Method | Speed | Notes |
|---|---|---|---|
| COSCO | Direct API | ~2 s | Public tracking API |
| Hapag-Lloyd | Direct API | ~1.5 s | |
| ONE Line | Direct API | ~1 s | Internal track-and-trace API |
| MSC | Direct API | ~1–2 s | Browser-like request headers |
| HMM | Direct API | ~0.5–13 s | Occasional server-side stalls |
| Maersk | API *(with key)* / Chrome | varies | Uses official API when a Consumer-Key is configured; otherwise Chrome |
| CMA CGM | Chrome | ~10–30 s | DataDome anti-bot |
| OOCL | Chrome | ~10–30 s | |
| Interasia | Chrome | ~10–30 s | Imperva anti-bot |
| KMTC | Chrome | ~10–30 s | |
| PIL | Chrome | ~10–30 s | reCAPTCHA (browser-only) |

> **Why some carriers need Chrome:** their websites are protected by commercial
> anti-bot systems (DataDome, Akamai, Imperva, reCAPTCHA) that reject direct
> requests. Those carriers are scraped through a real browser instead, which
> works but is slower.

---

## Architecture

```
┌─────────────┐    ┌──────────────┐    ┌───────────────────────────┐
│ Google Sheet│◄──►│  api.py      │◄──►│ scrapers/  (one per line) │
│ (team edits)│    │  FastAPI +   │    │  HTTP API  ─or─  Chrome    │
└─────────────┘    │  scheduler   │    └───────────────────────────┘
                   │              │
┌─────────────┐    │              │
│ Excel report│◄──►│              │
└─────────────┘    └──────────────┘
```

| File | Responsibility |
|---|---|
| `api.py` | FastAPI web service, Chrome driver pool, background scheduler, endpoints |
| `sheet_sync.py` | Google Sheets polling — reads BLs, writes POD/ETA/Status back |
| `sync_excel.py` | Excel (`shipments.xlsx`) batch sync + status classification |
| `main.py` | Carrier registry (`SCRAPER_MAP`), Chrome driver factory |
| `scrapers/*.py` | One module per carrier; each exposes `scrape(driver, bl)` |
| `scrapers/_common.py` | Shared parsing helpers and the standard result schema |

Each scraper returns a common dictionary (`POL`, `POD`, `Container No`,
`Vessel`, `ATD`, `ATA`, `Latest Status`, …) so the API, Excel, and Sheets
layers all treat every carrier the same way.

---

## Setup

### Prerequisites
- Python 3.11+
- Google Chrome (for the browser-based carriers)

### Installation
```bash
git clone https://github.com/Vicky181818/Exim-Shipment-Tracker.git
cd Exim-Shipment-Tracker
python -m venv venv
venv\Scripts\activate          # Windows
# source venv/bin/activate     # macOS/Linux
pip install -r requirements.txt
```

### Run the server
```bash
uvicorn api:app --host 0.0.0.0 --port 8000
```
Then open <http://localhost:8000> for the lookup UI.

---

## Google Sheets sync (optional)

Lets the team manage shipments from a shared Google Sheet.

1. Create a Google Cloud project and enable the **Google Sheets API**.
2. Create a **Service Account**, add a **JSON key**, and save it as
   `credentials/service_account.json`.
3. Share your Google Sheet with the service account's `client_email`
   (as **Editor**), plus any teammates who should use it.
4. Set the sheet ID via the `GOOGLE_SHEET_ID` environment variable (or edit the
   default in `sheet_sync.py`).

Once configured, the server polls the sheet every 2 minutes: it fills in
`POD`, `ETA / Arrival`, and `Status` for every row that has a BL No. Typing
`sync` into a row's Status cell forces an immediate refresh.

> The `credentials/` folder is git-ignored — service-account keys are never
> committed.

---

## Maersk official API (optional)

Maersk's public site is bot-protected, so tracking is unreliable via Chrome.
For a stable path, register a free **Consumer-Key** at
[developer.maersk.com](https://developer.maersk.com) (subscribe to the
Track & Trace API), then either:

- save it to `credentials/maersk_consumer_key.txt`, or
- set the `MAERSK_CONSUMER_KEY` environment variable.

When a key is present, Maersk uses the API first and Chrome only as a fallback.
Verify the key with:
```
GET http://localhost:8000/api/maersk/test
```

---

## API endpoints

| Endpoint | Description |
|---|---|
| `GET /` | Web lookup UI |
| `GET /api/carriers` | List supported carriers |
| `GET /api/track?bl=<bl>&carrier=<name>` | Track a single BL |
| `POST /api/excel/run` | Run the Excel batch sync |
| `GET /api/excel/download` | Download the Excel report |
| `GET /api/sheet/status` | Google Sheet sync status |
| `POST /api/sheet/sync` | Trigger a Google Sheet poll now |
| `GET /api/maersk/test` | Check the Maersk Consumer-Key |

The app's own endpoints are also served interactively at `/docs` (Swagger) and
`/redoc`.

---

## Carrier modules & the shipping-line APIs

Each shipping line has its own module under [`scrapers/`](scrapers). Six use
direct carrier APIs (COSCO, Hapag-Lloyd, ONE Line, MSC, HMM, Transliner); the
rest are scraped through a browser session because the carrier has no public
API. See [`docs/SCRAPERS.md`](docs/SCRAPERS.md) for the full per-carrier
breakdown (endpoint, technique, and anti-bot notes).

[`postman_collection.json`](postman_collection.json) is a Postman collection of
the five carriers' **direct tracking APIs** (COSCO, Hapag-Lloyd, ONE Line, MSC,
HMM) — request URLs, headers, and bodies. Import it via Postman → Import.

---

## Configuration (environment variables)

| Variable | Default | Purpose |
|---|---|---|
| `GOOGLE_SHEET_ID` | *(hard-coded default)* | Target Google Sheet |
| `SHEET_POLL_SECONDS` | `120` | How often to poll the sheet |
| `SHEET_MAX_PER_CYCLE` | `25` | Rows scraped per poll (rate-limit) |
| `SHEET_REFRESH_HOURS` | `24` | Re-check a row after this many hours |
| `MAERSK_CONSUMER_KEY` | — | Maersk API key (or use the file) |

---

## Notes & limitations

- Browser-based carriers require a machine with a desktop session (Chrome runs
  off-screen, not headless, because some carrier sites break in headless mode).
- Anti-bot-protected carriers (CMA CGM, Maersk, Interasia, OOCL, KMTC, PIL) are
  inherently slower and occasionally return `No data found`; this is a
  limitation of the carriers' sites, not the tracker.
- Syncing is deliberately gradual to avoid triggering carrier IP bans.

## License
MIT
