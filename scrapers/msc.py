"""
scrapers/msc.py — Direct HTTP API

MSC's TrackingInfo endpoint returns clean JSON (no encryption).

Fast path: direct HTTP POST — no browser needed.
Fallback:  browser-fetch via execute_async_script — navigate to msc.com
           once (to get Cloudflare clearance cookies), then call the
           API endpoint directly from JS (same cookies + TLS fingerprint
           as a real browser, no form-filling or DOM parsing needed).
"""

import time
import logging
import requests
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.common.exceptions import TimeoutException

log = logging.getLogger(__name__)

_API_URL      = "https://www.msc.com/api/feature/tools/TrackingInfo"
_TRACKING_URL = "https://www.msc.com/en/track-a-shipment"
# MSC also accepts a BL in the URL — the React app auto-searches on load
_TRACKING_URL_BL = "https://www.msc.com/en/track-a-shipment?trackingNumber={bl}&searchType=BN"

# No User-Agent here — curl_cffi's Chrome impersonation supplies one that
# matches its TLS fingerprint. X-Requested-With is REQUIRED: without it the
# API answers 200 with an empty body.
_HEADERS = {
    "Accept":           "application/json, text/plain, */*",
    "Content-Type":     "application/json",
    "Origin":           "https://www.msc.com",
    "Referer":          "https://www.msc.com/en/track-a-shipment",
    "X-Requested-With": "XMLHttpRequest",
}

_INTERCEPT_JS = """
window.__msc_data = null;
(function() {
    var _origFetch = window.fetch;
    window.fetch = function(url, opts) {
        var p = _origFetch.apply(this, arguments);
        try {
            if (url && url.toString().indexOf('TrackingInfo') !== -1) {
                p.then(function(r) {
                    r.clone().json().then(function(d) {
                        window.__msc_data = d;
                    }).catch(function(){});
                }).catch(function(){});
            }
        } catch(e) {}
        return p;
    };
    var _open = XMLHttpRequest.prototype.open;
    var _send = XMLHttpRequest.prototype.send;
    XMLHttpRequest.prototype.open = function(method, url) {
        this.__msc_url = url ? url.toString() : '';
        return _open.apply(this, arguments);
    };
    XMLHttpRequest.prototype.send = function() {
        var self = this;
        var _orig = self.onreadystatechange;
        self.onreadystatechange = function() {
            if (self.readyState === 4 && self.__msc_url &&
                    self.__msc_url.indexOf('TrackingInfo') !== -1) {
                try { window.__msc_data = JSON.parse(self.responseText); } catch(e) {}
            }
            if (_orig) _orig.apply(self, arguments);
        };
        self.addEventListener('load', function() {
            if (self.__msc_url && self.__msc_url.indexOf('TrackingInfo') !== -1) {
                try { window.__msc_data = JSON.parse(self.responseText); } catch(e) {}
            }
        });
        return _send.apply(this, arguments);
    };
})();
"""

_EMPTY = {
    "POL": "", "POD": "", "POR": "", "FND": "",
    "Container No": "", "Vessel": "", "ATA": "", "ATD": "",
    "Latest Status": "",
}


def _fmt_date(d: str) -> str:
    """DD/MM/YYYY → YYYY-MM-DD"""
    try:
        day, month, year = d.split("/")
        return f"{year}-{month}-{day}"
    except Exception:
        return d


def _parse(data: dict) -> dict:
    result = dict(_EMPTY)

    if not data.get("IsSuccess"):
        result["Latest Status"] = "No tracking data found"
        return result

    bols = (data.get("Data") or {}).get("BillOfLadings") or []
    if not bols:
        result["Latest Status"] = "No tracking data found"
        return result

    bol  = bols[0]
    info = bol.get("GeneralTrackingInfo") or {}

    result["POL"] = info.get("PortOfLoad", "").upper()
    result["POD"] = info.get("PortOfDischarge", "").upper()
    result["POR"] = info.get("ShippedFrom", "").upper()
    result["FND"] = info.get("ShippedTo", "").upper()

    containers = bol.get("ContainersInfo") or []
    cnums = [c.get("ContainerNumber", "") for c in containers if c.get("ContainerNumber")]
    if cnums:
        result["Container No"] = " | ".join(cnums)

    if not containers:
        return result

    # Events sorted chronologically (Order 0 = oldest, highest = newest)
    events_raw = sorted(containers[0].get("Events") or [], key=lambda e: e.get("Order", 0))

    rows   = ["Status  Place of Activity  Date  Time  Transport  Voyage No."]
    vessel = ""

    for ev in events_raw:
        desc     = ev.get("Description", "")
        loc      = ev.get("Location", "")
        date_iso = _fmt_date(ev.get("Date", ""))
        detail   = ev.get("Detail") or []

        ev_vessel = ev_voyage = ""
        if len(detail) >= 2 and detail[0].upper() not in ("EMPTY", "LADEN"):
            ev_vessel, ev_voyage = detail[0], detail[1]
        elif len(detail) == 1 and detail[0].upper() not in ("EMPTY", "LADEN"):
            ev_vessel = detail[0]

        if ev_vessel and not vessel:
            vessel = ev_vessel

        # "Estimated Time of Arrival" is a future projection, not a movement
        # event — route it to ATA (sync shows it as "ETA: …") and keep it out
        # of the event rows so it can't be mistaken for an actual arrival.
        # There can be one estimate per leg; the one at the POD wins over
        # intermediate transshipment ports.
        if "estimated" in desc.lower():
            if date_iso:
                pod_city = result["POD"].split(",")[0].strip()
                if pod_city and pod_city in loc.upper():
                    result["ATA"] = f"{date_iso} 00:00"
                elif not result["ATA"]:
                    result["ATA"] = f"{date_iso} 00:00"
            continue

        # Frontend dateRe requires "YYYY-MM-DD HH:MM" — MSC gives date only, use 00:00
        line = f"{desc}  {loc}  {date_iso} 00:00"
        if ev_vessel:
            line += f"  {ev_vessel}"
            if ev_voyage:
                line += f"  {ev_voyage}"
        rows.append(line)

        desc_lower = desc.lower()
        if not result["ATD"] and "loaded on vessel" in desc_lower:
            result["ATD"] = f"{date_iso} 00:00"
        if "discharged from vessel" in desc_lower or "import discharged" in desc_lower:
            result["ATA"] = f"{date_iso} 00:00"

    result["Latest Status"] = "\n".join(rows)
    result["Vessel"] = vessel
    return result


# MSC's Akamai edge rejects the python-requests TLS fingerprint (403) but
# accepts curl_cffi's Chrome impersonation. Two gotchas learned in testing:
#   - trackingMode MUST be an integer; the string "0" gets an empty response
#   - a GET is always 403; only POST is allowed through
_cffi_session = None


def _get_session():
    global _cffi_session
    if _cffi_session is None:
        from curl_cffi import requests as cffi_requests
        s = cffi_requests.Session(impersonate="chrome")
        try:
            s.get(_TRACKING_URL, timeout=20)   # collect site cookies once
        except Exception as e:
            log.debug("[MSC] session warm-up failed (continuing): %s", e)
        _cffi_session = s
    return _cffi_session


def _api_scrape(bl: str) -> dict:
    """Direct HTTP POST via curl_cffi — fastest path, no browser."""
    global _cffi_session
    try:
        r = _get_session().post(
            _API_URL,
            json={"trackingNumber": bl, "trackingMode": 0},
            headers=_HEADERS,
            timeout=15,
        )
        if r.status_code == 200 and r.text.strip().startswith("{"):
            data = r.json()
            if data.get("IsSuccess"):
                log.info("[MSC] Direct HTTP API success for %s", bl)
                return _parse(data)
            log.info("[MSC] API: %s", str(data.get("Data"))[:80])
        else:
            log.warning("[MSC] Direct HTTP status %d — resetting session", r.status_code)
            _cffi_session = None
    except Exception as e:
        log.warning("[MSC] Direct HTTP failed: %s", e)
        _cffi_session = None
    return {}


def _browser_scrape(driver, bl: str) -> dict:
    """
    Inject fetch/XHR interceptor BEFORE navigation; navigate directly to the
    BL-prefilled tracking URL so MSC's React app auto-searches without any
    form interaction. Fall back to manual form fill if needed.
    """
    try:
        driver.execute_cdp_cmd("Page.addScriptToEvaluateOnNewDocument",
                               {"source": _INTERCEPT_JS})
    except Exception as e:
        log.warning("[MSC] CDP inject failed: %s", e)

    # Try the BL-in-URL path first — React auto-fires TrackingInfo on load
    driver.get(_TRACKING_URL_BL.format(bl=bl))

    # Poll for the intercepted response (~5-8s if URL auto-search works)
    deadline = time.time() + 15
    while time.time() < deadline:
        try:
            data = driver.execute_script("return window.__msc_data;")
            if data:
                log.info("[MSC] Intercepted TrackingInfo (URL auto-search)")
                return _parse(data)
        except Exception:
            pass
        time.sleep(0.3)

    # Fallback: manual form fill on the plain tracking page
    log.info("[MSC] URL auto-search timed out — trying manual form fill")
    driver.get(_TRACKING_URL)

    inp = None
    for inp_sel in ["input#trackingNumber", "input[name='trackingNumber']",
                    "input[placeholder*='tracking']", "input[placeholder*='B/L']",
                    "input[type='search']", "input[type='text']"]:
        try:
            inp = WebDriverWait(driver, 12).until(
                EC.visibility_of_element_located((By.CSS_SELECTOR, inp_sel)))
            break
        except TimeoutException:
            continue

    if inp is None:
        log.error("[MSC] Tracking input not found")
        return dict(_EMPTY, **{"Latest Status": "Error: MSC page failed to load"})

    driver.execute_script("arguments[0].scrollIntoView(true); arguments[0].click();", inp)
    time.sleep(0.2)
    inp.clear()
    time.sleep(0.1)
    inp.send_keys(bl)
    time.sleep(0.3)

    submitted = False
    for sel in [
        "button.msc-cta-icon-simple.msc-search-autocomplete__search",
        ".msc-search-autocomplete__search",
        "button[class*='autocomplete'][class*='search']",
        "button[type='submit']",
        "button[class*='search']",
        "button[aria-label*='search' i]",
        "button[aria-label*='track' i]",
    ]:
        try:
            btn = WebDriverWait(driver, 4).until(EC.element_to_be_clickable((By.CSS_SELECTOR, sel)))
            driver.execute_script("arguments[0].scrollIntoView(true); arguments[0].click();", btn)
            submitted = True
            log.info("[MSC] Submitted via %s", sel)
            break
        except Exception:
            pass
    if not submitted:
        from selenium.webdriver.common.keys import Keys
        inp.send_keys(Keys.RETURN)
        log.info("[MSC] Submitted via Enter key")

    deadline = time.time() + 25
    while time.time() < deadline:
        try:
            data = driver.execute_script("return window.__msc_data;")
            if data:
                log.info("[MSC] Intercepted TrackingInfo response")
                return _parse(data)
        except Exception:
            pass
        time.sleep(0.3)

    return dict(_EMPTY, **{"Latest Status": "Error: MSC tracking data not received"})


def scrape(driver, bl: str) -> dict:
    bl = bl.strip()
    log.info("[MSC] Scraping BL: %s", bl)

    result = _api_scrape(bl)
    if result.get("POL") or result.get("POD") or result.get("Container No"):
        return result

    # No driver (HTTP-first mode): report the miss; api.py retries with Chrome
    if driver is None:
        log.info("[MSC] HTTP miss for %s — need Chrome fallback", bl)
        return {}

    log.info("[MSC] Falling back to browser-fetch")
    return _browser_scrape(driver, bl)
