"""
scrapers/cosco.py — COSCO Shipping (HTTP-only, no Chrome)

Uses the public ebtracking JSON API directly:

    GET /ebtracking/public/bill/{number}?timestamp={ms}

The number must be the BARE BL number — WITHOUT the "COSU" prefix
(e.g. "6452489450", not "COSU6452489450"). With the prefix the API
answers 200 + "No data", which is why the old bill/export tier almost
never hit.

The payload contains everything the frontend needs:
  trackingPath            → POL / POD / from-to cities / main vessel
  cargoTrackingContainer  → container numbers
  actualShipment          → per-leg vessel, voyage, actual dep/arr/discharge

Not available publicly: per-container yard events (Empty Return, Gate Out) —
the container/status endpoint returns null fields without a customer login.
Delivered shipments therefore surface as "Discharged" at the POD, which
sync_excel._determine_status maps to "Arrived / Discharged".

scrape(driver, bl) keeps the standard signature; driver is ignored.
"""

import logging
import re
import threading
import time

import requests

log = logging.getLogger(__name__)

_BASE = "https://elines.coscoshipping.com"
_BILL_URL = _BASE + "/ebtracking/public/bill/{number}?timestamp={ts}"

_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": _BASE + "/ebusiness/cargoTracking",
    "language": "en_US",
    "sys": "eb",
}

_TIMEOUT = 12
_RETRIES = 2

# ── Rate limiting ─────────────────────────────────────────────────────────────
# COSCO 403-bans IPs that fire many parallel requests (learned the hard way:
# 8 sync workers × ~100 BLs = temp ban). All requests are serialised through
# one lock with a minimum gap, and a 403 opens a cooldown window during which
# every COSCO scrape fails fast instead of extending the ban.

_req_lock = threading.Lock()
_MIN_INTERVAL = 0.8          # seconds between consecutive COSCO requests
_last_req = 0.0
_block_until = 0.0
_COOLDOWN = 300              # back off 5 min after a 403

_session = requests.Session()


class _RateLimited(Exception):
    pass


def _throttled_get(url: str):
    """Single-flight GET with pacing and a 403 circuit-breaker."""
    global _last_req, _block_until
    with _req_lock:
        now = time.time()
        if now < _block_until:
            raise _RateLimited(
                f"COSCO rate-limited — cooling down {int(_block_until - now)}s more"
            )
        gap = _last_req + _MIN_INTERVAL - now
        if gap > 0:
            time.sleep(gap)
        try:
            r = _session.get(url, headers=_HEADERS, timeout=_TIMEOUT)
        finally:
            _last_req = time.time()
        if r.status_code == 403:
            _block_until = time.time() + _COOLDOWN
            log.warning("[COSCO] HTTP 403 — pausing all COSCO requests for %ds", _COOLDOWN)
            raise _RateLimited("COSCO rate-limited (HTTP 403)")
        return r

_EMPTY = {
    "POL": "", "POD": "", "Container No": "",
    "Vessel": "", "ATA": "", "ATD": "", "Latest Status": "", "FND": "",
}


def _clean_dt(raw: str) -> str:
    if not raw:
        return ""
    s = re.sub(r'\s+[A-Z]{2,5}$', '', str(raw).strip())
    s = s.replace("T", " ")
    return s[:16].strip()


def _port_name(raw: str) -> str:
    """'Tuticorin-Dakshin Bharat Gateway Terminal' → 'Tuticorin'."""
    return (raw or "").split("-")[0].strip()


def _useful(r: dict) -> bool:
    return bool(r.get("POL") or r.get("POD") or r.get("Container No") or r.get("ATA"))


def _parse_content(content: dict) -> dict:
    result = dict(_EMPTY)

    path = content.get("trackingPath") or {}
    result["POL"]    = _port_name(path.get("pol"))
    result["POD"]    = _port_name(path.get("pod"))
    result["FND"]    = (path.get("toCity") or "").strip()
    result["Vessel"] = (path.get("vslNme") or "").strip()

    containers = content.get("cargoTrackingContainer") or []
    nums = [c.get("cntrNum", "").strip() for c in containers
            if isinstance(c, dict) and c.get("cntrNum")]
    result["Container No"] = " | ".join(dict.fromkeys(nums))

    # actualShipment legs are chronological; within a leg the order of
    # events is departed → arrived → discharged.
    event_rows = []
    final_actual = ""   # actual arrival/discharge of the last leg
    final_eta = ""      # ETA of the last leg (used when not yet arrived)

    for leg in content.get("actualShipment") or []:
        if not isinstance(leg, dict):
            continue
        vessel  = (leg.get("vesselName") or "").strip()
        service = (leg.get("service") or "").strip()
        voyage  = (leg.get("voyageNo") or "").strip()
        voy_str = f"{service}/{voyage}" if service else voyage
        pol = _port_name(leg.get("portOfLoading")).upper()
        pod = _port_name(leg.get("portOfDischarge")).upper()

        atd  = _clean_dt(leg.get("actualDepartureDate") or "")
        ata  = _clean_dt(leg.get("actualArrivalDate") or "")
        disc = _clean_dt(leg.get("actualDischargeDate") or "")
        eta  = _clean_dt(leg.get("estimatedDateOfArrival") or "")

        if atd:
            event_rows.append(f"Vessel departed  {pol}  {atd}  {vessel}  {voy_str}")
            if not result["ATD"]:
                result["ATD"] = atd
        if ata:
            event_rows.append(f"Vessel arrived  {pod}  {ata}  {vessel}  {voy_str}")
        if disc:
            event_rows.append(f"Discharged  {pod}  {disc}  {vessel}  {voy_str}")

        if not result["Vessel"] and vessel:
            result["Vessel"] = vessel
        final_actual = ata or disc
        final_eta = eta

    if event_rows:
        hdr = "Status  Place of Activity  Date  Time  Transport  Voyage No."
        result["Latest Status"] = hdr + "\n" + "\n".join(event_rows)

    # ATA priority: actual arrival/discharge of the last leg → last leg's ETA →
    # trackingPath.cgoAvailTm (cargo-available time at destination). The last
    # is COSCO's overall estimate and is the only date present for shipments
    # whose remaining legs have no per-leg ETA yet.
    result["ATA"] = final_actual or final_eta or _clean_dt(path.get("cgoAvailTm") or "")
    return result


def scrape(driver, bl: str) -> dict:
    """HTTP-only; the driver argument is accepted but never used."""
    bl = bl.strip().upper()
    bare = re.sub(r"^COSU", "", bl)
    variants = [bare, bl] if bare != bl else [bl]

    last_err = None
    for number in variants:
        for attempt in range(_RETRIES):
            try:
                url = _BILL_URL.format(number=number, ts=int(time.time() * 1000))
                r = _throttled_get(url)
                if r.status_code != 200 or not r.text.strip():
                    last_err = f"HTTP {r.status_code}"
                    continue

                content = ((r.json().get("data") or {}).get("content") or {})
                if content.get("isbillOfLadingExist") is False:
                    break  # definitive "not found" for this variant — try next

                parsed = _parse_content(content)
                if _useful(parsed):
                    log.info("[COSCO] API hit for %s (as %s)", bl, number)
                    return parsed
                break  # 200 but no data — retrying won't change it

            except _RateLimited as e:
                # Don't hammer a banned IP — fail fast; old Excel data is kept.
                result = dict(_EMPTY)
                result["Latest Status"] = f"Error: {e}"
                return result
            except Exception as e:
                last_err = str(e)
                log.debug("[COSCO] attempt %d for %s failed: %s", attempt + 1, number, e)

    result = dict(_EMPTY)
    result["Latest Status"] = (
        f"Error: {last_err[:80]}" if last_err else "Not found on COSCO"
    )
    log.info("[COSCO] no data for %s (%s)", bl, last_err or "not found")
    return result
