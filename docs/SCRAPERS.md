# Carrier Tracking Modules

Each shipping line has its own module under [`scrapers/`](../scrapers). Every
module exposes the same entry point:

```python
def scrape(driver, bl: str) -> dict:
    # driver: a Selenium WebDriver (or None for API-only carriers)
    # returns: {POL, POD, POR, FND, Container No, Vessel, ATD, ATA, Latest Status}
```

The shared result schema and helpers live in
[`scrapers/_common.py`](../scrapers/_common.py).

---

## Carriers **with** a public/JSON API (no browser needed)

These call the carrier's own JSON endpoint directly over HTTP and return in ~1–2s.

| Carrier | Module | How it works |
|---|---|---|
| COSCO | [`scrapers/cosco.py`](../scrapers/cosco.py) | `GET ebtracking/public/bill/{number}` — the number must be the **bare** BL (no `COSU` prefix). |
| Hapag-Lloyd | [`scrapers/hapag.py`](../scrapers/hapag.py) | Public HTTP tracking endpoint. |
| ONE Line | [`scrapers/one_line.py`](../scrapers/one_line.py) | `POST /api/v2/edh/containers/track-and-trace/search`. |
| MSC | [`scrapers/msc.py`](../scrapers/msc.py) | `POST /api/feature/tools/TrackingInfo` via `curl_cffi` (Chrome TLS impersonation) + `X-Requested-With` header. |
| HMM | [`scrapers/hmm.py`](../scrapers/hmm.py) | `POST selectTrackNTrace.do` via `curl_cffi` over **HTTP/1.1** (CSRF token pulled from the page). |
| Transliner | [`scrapers/transliner.py`](../scrapers/transliner.py) | `GET .../api/bookings/{ref}` on the Tigris tracking platform — plain JSON, no auth. |

---

## Carriers **without** an API — browser-based scraping

These sites have no usable public API (most are behind commercial anti-bot
systems), so the module drives a headless/off-screen Chrome via Selenium,
intercepts the page's own network call or parses the rendered DOM, and returns
in ~10–30s. **These are the "scraping scripts for lines that don't have APIs."**

| Carrier | Module | Site / protection | Technique |
|---|---|---|---|
| CMA CGM | [`scrapers/cma.py`](../scrapers/cma.py) | cma-cgm.com — DataDome | Optional official API path if an API key is configured (see below); otherwise Selenium + CDP XHR intercept of the `mapdetail` request, DOM-parse fallback. |
| Maersk | [`scrapers/maersk.py`](../scrapers/maersk.py) | maersk.com — Akamai | CDP `fetch` intercept of the page's tracking API. Optional official API path if a Consumer-Key is configured (see below). |
| InterAsia | [`scrapers/interasia.py`](../scrapers/interasia.py) | interasia.cc — Imperva/Incapsula | Selenium form submit → parse the results table. |
| OOCL | [`scrapers/oocl.py`](../scrapers/oocl.py) | oocl.com | Selenium form fill on the public cargo-tracking page. |
| KMTC | [`scrapers/kmtc.py`](../scrapers/kmtc.py) | ekmtc.com | Selenium form fill on the Vue tracking SPA. |
| PIL | [`scrapers/pil.py`](../scrapers/pil.py) | searates.com — reCAPTCHA Enterprise | CDP intercept of the tracking widget's JSON response. |

### Notes
- **Maersk** can use the official API instead of the browser when a free
  Consumer-Key from [developer.maersk.com](https://developer.maersk.com) is
  placed in `credentials/maersk_consumer_key.txt` (or the `MAERSK_CONSUMER_KEY`
  env var). Verify with `GET /api/maersk/test`.
- **CMA CGM** can use its official API from [api.cma-cgm.com](https://api.cma-cgm.com)
  when a key is placed in `credentials/cma_api_key.txt` (or the `CMA_API_KEY`
  env var). The endpoint/auth-header default to the common Track & Trace product
  but are overridable via `CMA_API_URL` / `CMA_API_KEY_HEADER` since they vary by
  subscription. Verify with `GET /api/cma/test`. Without a key it uses the
  browser path.
- **OOCL, KMTC, PIL** modules exist but have **not been verified** against live
  BLs (no sample data was available), so they are hidden from the UI picker.
- The browser carriers need a host with a desktop session — Chrome runs
  off-screen rather than fully headless because some carrier sites break in
  headless mode. See `make_driver()` in [`main.py`](../main.py).

---

## How a carrier is selected

`SCRAPER_MAP` in [`main.py`](../main.py) maps every carrier name (and its
spelling variants) to the right module. `api.py` decides the execution path:

- `HTTP_ONLY_CARRIERS` — never launch a browser (COSCO, Hapag-Lloyd).
- `HTTP_FIRST_CARRIERS` — try the API first, fall back to Chrome (ONE, MSC, HMM, Maersk).
- everything else — driven through the pooled Chrome driver.
