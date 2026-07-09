"""FastAPI
"""

import contextlib
import logging
import os
import queue as _queue
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from typing import Optional

from main import SCRAPER_MAP, get_scraper, make_driver
import sync_excel
import sheet_sync

log = logging.getLogger("api")


# ── Chrome driver pool ────────────────────────────────────────────────────────
# Pre-warmed Chrome instances reused across individual-tracking requests.
# Eliminates the ~4-5s Chrome startup cost on every search.
# Excel sync workers bypass the pool and get fresh drivers (parallel-safe).

class _DriverPool:
    """Thread-safe pool of reusable Chrome WebDriver instances."""

    def __init__(self, size: int, headless: bool, offscreen: bool = False):
        self._headless  = headless
        self._offscreen = offscreen
        self._q: _queue.Queue = _queue.Queue()
        for _ in range(size):
            self._q.put(None)          # None = slot exists but driver not yet spawned

    def _spawn(self):
        d = make_driver(headless=self._headless)
        if self._offscreen:
            try:
                d.set_window_position(-2000, 0)
                d.set_window_size(1400, 900)
            except Exception:
                pass
        d._pool_uses = 0
        return d

    @contextlib.contextmanager
    def borrow(self, timeout: float = 120.0):
        try:
            slot = self._q.get(timeout=timeout)
        except _queue.Empty:
            slot = None                # all slots busy — spin up an extra

        driver = None
        try:
            if slot is None:
                driver = self._spawn()
            else:
                # Health check; restart driver if crashed or too old
                try:
                    _ = slot.current_url
                    slot._pool_uses = getattr(slot, "_pool_uses", 0) + 1
                    if slot._pool_uses >= 25:  # retire after 25 uses (CDP script bloat)
                        try: slot.quit()
                        except Exception: pass
                        driver = self._spawn()
                    else:
                        driver = slot
                except Exception:
                    try: slot.quit()
                    except Exception: pass
                    driver = self._spawn()

            yield driver

        finally:
            # Clear page state; return to pool (or None if driver crashed)
            alive = True
            try:
                driver.get("about:blank")
            except Exception:
                alive = False
                try: driver.quit()
                except Exception: pass

            self._q.put(driver if alive else None)


# Size = 1 is enough for individual search (one user at a time).
# Increase to 2-3 if you expect simultaneous users.
_headless_pool  = _DriverPool(size=1, headless=True,  offscreen=False)
_offscreen_pool = _DriverPool(size=1, headless=False, offscreen=True)
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-7s %(message)s")

app = FastAPI(
    title="Shipment Tracker API",
    description="Look up live container/BL status across supported carriers.",
    version="1.0.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

app.mount("/static", StaticFiles(directory="static"), name="static")

executor = ThreadPoolExecutor(max_workers=2)

# Carriers that run fully headless (no Chrome window at all).
HEADLESS_CARRIERS = set()

# Carriers that need a real Chrome window (headless breaks them) but should
# stay invisible — moved off-screen instead of minimized because minimize
# pauses JS execution on some carrier sites.
OFFSCREEN_CARRIERS = {"MSC", "CMA CGM", "MAERSK", "INTERASIA", "HMM LINE", "HMM", "ONE LINE", "ONE", "KMTC", "OOCL", "PIL"}

# Carriers that use plain HTTP (no Selenium / Chrome window needed at all).
# These scrapers ignore the driver argument entirely.
HTTP_ONLY_CARRIERS = {"HAPAG-LLOYD", "HAPAG LLOYD", "HAPAG", "COSCO",
                      "TRANSLINER", "TRANS LINE", "TRANS LINES", "TRANSLINE"}

# Carriers that try HTTP first; Chrome is only used when the HTTP path returns
# no useful data (rare — covers locked BLs, rate-limited responses, etc.).
# scrape(None, bl) must return {} gracefully when HTTP fails for these.
# Maersk and CMA are HTTP-first only when their API key is configured; without
# a key the API path returns {} and run_scraper_sync falls back to Chrome.
HTTP_FIRST_CARRIERS = {"ONE LINE", "ONE", "MSC", "HMM", "HMM LINE", "MAERSK",
                       "CMA CGM", "CMA"}

# Fields that indicate the HTTP fast path returned real data
_HTTP_USEFUL = frozenset({"POL", "POD", "Container No", "ATA"})


class TrackingResult(BaseModel):
    bl: str
    carrier: str
    por: Optional[str] = None
    pol: Optional[str] = None
    pod: Optional[str] = None
    fnd: Optional[str] = None
    container_no: Optional[str] = None
    vessel: Optional[str] = None
    atd: Optional[str] = None
    ata: Optional[str] = None
    final_destination: Optional[str] = None
    status: Optional[str] = None
    last_updated: str
    error: Optional[str] = None


def run_scraper_sync(bl: str, carrier: str, _fresh_driver: bool = False) -> dict:
    """
    Scrape one BL.
    _fresh_driver=False  → use the warm pool (individual search, fast)
    _fresh_driver=True   → spawn a new Chrome (Excel sync parallel workers)
    """
    scraper = get_scraper(carrier)
    if scraper is None:
        supported = sorted(set(SCRAPER_MAP.keys()))
        raise ValueError(f"Carrier '{carrier}' not supported. Supported: {supported}")

    carrier_upper = carrier.strip().upper()

    # HTTP-only: no Chrome at all
    if carrier_upper in HTTP_ONLY_CARRIERS:
        data = scraper(None, bl)
        data["BL No"] = bl
        data["Shipping Line"] = carrier
        data["Last Updated"] = datetime.now().strftime("%Y-%m-%d %H:%M")
        return data

    # HTTP-first: try without Chrome; fall through to Chrome only if HTTP fails
    if carrier_upper in HTTP_FIRST_CARRIERS:
        data = scraper(None, bl)
        if data and any(data.get(f) for f in _HTTP_USEFUL):
            data["BL No"] = bl
            data["Shipping Line"] = carrier
            data["Last Updated"] = datetime.now().strftime("%Y-%m-%d %H:%M")
            return data
        log.info("[API] %s HTTP fast path missed for %s — falling back to Chrome", carrier_upper, bl)

    headless = carrier_upper in HEADLESS_CARRIERS

    if _fresh_driver:
        # Excel sync path: each parallel worker gets its own Chrome
        driver = make_driver(headless=headless)
        try:
            if carrier_upper in OFFSCREEN_CARRIERS:
                try:
                    driver.set_window_position(-2000, 0)
                    driver.set_window_size(1400, 900)
                except Exception:
                    pass
            data = scraper(driver, bl)
        finally:
            driver.quit()
    else:
        # Individual search path: borrow pre-warmed Chrome from pool
        pool = _headless_pool if headless else _offscreen_pool
        with pool.borrow() as driver:
            data = scraper(driver, bl)

    data["BL No"] = bl
    data["Shipping Line"] = carrier
    data["Last Updated"] = datetime.now().strftime("%Y-%m-%d %H:%M")
    return data


def _sync_scraper(bl: str, carrier: str) -> dict:
    """Wrapper for Excel sync. Sync is sequential now, so it can safely share
    the pre-warmed Chrome pool instead of spawning a fresh Chrome per BL."""
    return run_scraper_sync(bl, carrier, _fresh_driver=False)


@app.get("/api/carriers")
def list_carriers():
    seen = {}
    for name, fn in SCRAPER_MAP.items():
        seen.setdefault(fn, name)
    return {"carriers": sorted(seen.values())}


@app.get("/api/maersk/test")
def maersk_api_test(bl: str = Query("269381946", min_length=3)):
    """Check the Maersk Consumer-Key. Returns whether a key is configured and,
    if so, whether a live API call for `bl` succeeds. Falls back to a known
    sample BL when none is given so it works with a single click."""
    from scrapers import maersk

    key = maersk._get_api_key()
    if not key:
        return {
            "key_configured": False,
            "ok": False,
            "message": "No Consumer-Key found. Add it to "
                       "credentials/maersk_consumer_key.txt or set "
                       "MAERSK_CONSUMER_KEY, then try again.",
            "endpoint": maersk._API_URL,
        }

    masked = f"{key[:4]}…{key[-4:]}" if len(key) > 8 else "set"
    try:
        data = maersk._api_scrape(bl.strip())
    except Exception as e:  # defensive — _api_scrape already catches, but be safe
        return {"key_configured": True, "key": masked, "ok": False,
                "message": f"API call raised: {e}", "endpoint": maersk._API_URL}

    ok = bool(data.get("POL") or data.get("POD") or data.get("Container No"))
    return {
        "key_configured": True,
        "key": masked,
        "ok": ok,
        "bl": bl.strip(),
        "endpoint": maersk._API_URL,
        "message": ("API key works — live data returned." if ok else
                    "Key is set but the API returned no usable data. Check the "
                    "server log for the HTTP status; the endpoint or key-header "
                    "may need adjusting for your subscription (MAERSK_API_URL / "
                    "MAERSK_API_KEY_HEADER)."),
        "sample": {k: data.get(k) for k in ("POL", "POD", "Vessel", "ATA", "Container No")} if ok else None,
    }


@app.get("/api/cma/test")
def cma_api_test(bl: str = Query("LPL1527414", min_length=3)):
    """Check the CMA CGM API key. Returns whether a key is configured and, if
    so, whether a live API call for `bl` succeeds. Falls back to a sample BL."""
    from scrapers import cma

    key = cma._get_api_key()
    if not key:
        return {
            "key_configured": False,
            "ok": False,
            "message": "No API key found. Add it to credentials/cma_api_key.txt "
                       "or set CMA_API_KEY, then try again.",
            "endpoint": cma._API_URL,
        }

    masked = f"{key[:4]}…{key[-4:]}" if len(key) > 8 else "set"
    try:
        data = cma._api_scrape(bl.strip())
    except Exception as e:
        return {"key_configured": True, "key": masked, "ok": False,
                "message": f"API call raised: {e}", "endpoint": cma._API_URL}

    ok = bool(data.get("POL") or data.get("POD") or data.get("Container No"))
    return {
        "key_configured": True,
        "key": masked,
        "ok": ok,
        "bl": bl.strip(),
        "endpoint": cma._API_URL,
        "message": ("API key works — live data returned." if ok else
                    "Key is set but the API returned no usable data. Check the "
                    "server log for the HTTP status; the endpoint or key-header "
                    "may need adjusting for your subscription (CMA_API_URL / "
                    "CMA_API_KEY_HEADER)."),
        "sample": {k: data.get(k) for k in ("POL", "POD", "Vessel", "ATA", "Container No")} if ok else None,
    }


@app.get("/api/track", response_model=TrackingResult)
async def track_shipment(
    bl: str = Query(..., min_length=3),
    carrier: str = Query(...),
):
    bl = bl.strip()
    carrier = carrier.strip()

    if get_scraper(carrier) is None:
        supported = sorted(set(SCRAPER_MAP.keys()))
        raise HTTPException(
            status_code=400,
            detail=f"Unsupported carrier '{carrier}'. Supported: {supported}",
        )

    loop = __import__("asyncio").get_event_loop()
    try:
        data = await loop.run_in_executor(executor, run_scraper_sync, bl, carrier)
    except Exception as e:
        log.error("Scrape failed for %s / %s: %s", carrier, bl, e)
        return JSONResponse(
            status_code=502,
            content={
                "bl": bl,
                "carrier": carrier,
                "status": "ERROR",
                "error": str(e),
                "last_updated": datetime.now().strftime("%Y-%m-%d %H:%M"),
            },
        )

    return TrackingResult(
        bl=bl,
        carrier=carrier,
        por=data.get("POR") or None,
        pol=data.get("POL") or None,
        pod=data.get("POD") or None,
        fnd=data.get("FND") or None,
        container_no=data.get("Container No") or None,
        vessel=data.get("Vessel") or None,
        atd=data.get("ATD") or None,
        ata=data.get("ATA") or None,
        final_destination=data.get("FND") or None,
        status=data.get("Latest Status") or None,
        last_updated=data.get("Last Updated", ""),
    )


# ── Excel sync endpoints ──────────────────────────────────────────────────────

@app.post("/api/excel/run")
def run_excel_sync():
    """Start a sync in the background and return immediately. Poll /api/excel/status."""
    if sync_excel.get_status()["running"]:
        raise HTTPException(status_code=409, detail="Sync already running")
    import threading
    t = threading.Thread(target=sync_excel.run_sync, args=(_sync_scraper,), daemon=True)
    t.start()
    return {"started": True, "running": True, "message": "Sync started in background"}


@app.get("/api/excel/status")
def excel_status():
    """Return the result of the last sync (or running state)."""
    return sync_excel.get_status()


@app.get("/api/excel/download")
def download_excel():
    """Download shipments.xlsx."""
    sync_excel.ensure_excel()
    return FileResponse(
        path=sync_excel.EXCEL_FILE,
        filename="shipments.xlsx",
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )


# ── Daily auto-sync at 09:00 IST ─────────────────────────────────────────────

def _scheduled_sync():
    log.info("[SCHEDULER] Daily sync triggered")
    try:
        sync_excel.run_sync(_sync_scraper)
    except Exception as e:
        log.error("[SCHEDULER] Sync failed: %s", e)

def _scheduled_sheet_poll():
    try:
        sheet_sync.poll(_sync_scraper)
    except Exception as e:
        log.error("[SCHEDULER] Sheet poll failed: %s", e)


_scheduler = BackgroundScheduler(timezone="Asia/Kolkata")


@app.on_event("startup")
def _start_scheduler():
    # Start the scheduler from the ASGI startup hook (runs only in the serving
    # worker, not uvicorn's --reload watcher) so we don't end up with two
    # schedulers double-firing every job.
    if _scheduler.running:
        return
    _scheduler.add_job(_scheduled_sync, CronTrigger(hour=0, minute=0),
                       id="daily_excel", replace_existing=True)
    # Google Sheet polling: picks up BLs the team types into the shared sheet.
    # next_run_time=now → first poll fires immediately on startup (no 2-min
    # dead window). No-op if credentials/service_account.json is absent.
    _scheduler.add_job(_scheduled_sheet_poll, "interval",
                       seconds=sheet_sync.POLL_SECONDS,
                       max_instances=1, coalesce=True,
                       next_run_time=datetime.now(),
                       id="sheet_poll", replace_existing=True)
    _scheduler.start()
    log.info("[SCHEDULER] started (sheet poll every %ss)", sheet_sync.POLL_SECONDS)


# ── Google Sheet sync endpoints ──────────────────────────────────────────────

@app.get("/api/sheet/status")
def sheet_status():
    """Last sheet-poll result (enabled=False means credentials not set up)."""
    return sheet_sync.get_status()


@app.post("/api/sheet/sync")
def sheet_sync_now():
    """Trigger a sheet poll immediately instead of waiting for the timer."""
    if not sheet_sync.enabled():
        raise HTTPException(status_code=400,
                            detail="Sheet sync not configured — see sheet_sync.py setup")
    t = threading.Thread(target=_scheduled_sheet_poll, daemon=True)
    t.start()
    return {"started": True, "message": "Sheet poll started in background"}


@app.get("/", response_class=HTMLResponse)
def home():
    with open("static/index.html", "r", encoding="utf-8") as f:
        content = f.read()
    return HTMLResponse(content=content, headers={"Cache-Control": "no-store"})

