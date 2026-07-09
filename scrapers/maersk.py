"""
scrapers/maersk.py — CDP fetch-intercept approach

Instead of parsing Maersk's SPA HTML, we intercept the JSON response from
the page's own API call. The page's React code handles all Akamai headers
automatically; we just capture what comes back.

Flow:
  1. Inject a fetch() wrapper via Page.addScriptToEvaluateOnNewDocument
     (runs before any page JS, so it wraps the native fetch before React loads)
  2. Navigate to maersk.com/tracking/<BL>
  3. Page JS makes the synergy/tracking API call (with all proper Akamai headers)
  4. Our wrapper saves the JSON to window.__maersk_data
  5. Poll until data appears, parse and return

This avoids waiting for the full SPA to render — we get the data as soon as
the API response lands (~2-3s after navigation), not after React finishes painting.
"""

import os
import time
import logging
from scrapers._common import dismiss_cookies_js

log = logging.getLogger(__name__)

_TRACKING_URL = "https://www.maersk.com/tracking/{bl}"

# ── Official Maersk API (developer.maersk.com) ───────────────────────────────
# When a Consumer-Key is configured, we call Maersk's tracking API directly
# instead of driving Chrome. Get a free key by registering at
# https://developer.maersk.com, subscribing to the Track & Trace API, then:
#   • put the key in credentials/maersk_consumer_key.txt   (simplest), or
#   • set the MAERSK_CONSUMER_KEY environment variable.
# Endpoint and header name are overridable via env in case your subscription
# uses a different product — the response is parsed by the same _parse() the
# browser path uses (Maersk's tracking API returns the same shape).
_API_URL = os.environ.get(
    "MAERSK_API_URL",
    "https://api.maersk.com/synergy/tracking/{bl}?operator=MAEU",
)
_API_KEY_HEADER = os.environ.get("MAERSK_API_KEY_HEADER", "Consumer-Key")
_API_KEY_FILE = os.environ.get(
    "MAERSK_API_KEY_FILE", "credentials/maersk_consumer_key.txt"
)


def _get_api_key() -> str:
    key = os.environ.get("MAERSK_CONSUMER_KEY", "").strip()
    if key:
        return key
    try:
        with open(_API_KEY_FILE, "r", encoding="utf-8") as f:
            return f.read().strip()
    except Exception:
        return ""


def _api_scrape(bl: str) -> dict:
    """Direct Maersk API call using a Consumer-Key. Returns {} if no key is
    configured or the call fails, so callers fall back to the browser path."""
    key = _get_api_key()
    if not key:
        return {}
    try:
        from curl_cffi import requests as cffi_requests
        r = cffi_requests.get(
            _API_URL.format(bl=bl.strip()),
            headers={
                _API_KEY_HEADER: key,
                "Accept": "application/json",
                "Origin": "https://www.maersk.com",
                "Referer": "https://www.maersk.com/",
            },
            impersonate="chrome",
            timeout=20,
        )
        if r.status_code == 200 and r.text.strip()[:1] in "{[":
            data = _parse(r.json())
            if data.get("POL") or data.get("POD") or data.get("Container No"):
                log.info("[MAERSK] Official API success for %s", bl)
                return data
            log.warning("[MAERSK] API returned no usable data for %s", bl)
        else:
            log.warning("[MAERSK] API HTTP %d for %s: %s",
                        r.status_code, bl, r.text[:120].replace("\n", " "))
    except Exception as e:
        log.warning("[MAERSK] API call failed for %s: %s", bl, e)
    return {}

_INTERCEPT_JS = """
window.__maersk_data = null;
(function() {
    function _is_tracking(s){
        return s && (
            s.indexOf('synergy/tracking') !== -1 ||
            s.indexOf('/tracking/') !== -1 ||
            s.indexOf('maersk.com/api') !== -1 ||
            s.indexOf('trackingNumber') !== -1
        );
    }
    var _orig = window.fetch;
    window.fetch = function(url, opts) {
        var p = _orig.apply(this, arguments);
        try {
            if (_is_tracking(url ? url.toString() : '')) {
                p.then(function(r) {
                    r.clone().json().then(function(d) {
                        if (d && (d.origin || d.destination || d.containers)) {
                            window.__maersk_data = d;
                        } else if (d && !window.__maersk_data) {
                            window.__maersk_data = d;
                        }
                    }).catch(function(){});
                }).catch(function(){});
            }
        } catch(e) {}
        return p;
    };
    var _xo = XMLHttpRequest.prototype.open, _xs = XMLHttpRequest.prototype.send;
    XMLHttpRequest.prototype.open = function(m, url){
        this.__msk_url = url ? url.toString() : '';
        return _xo.apply(this, arguments);
    };
    XMLHttpRequest.prototype.send = function(){
        var self = this;
        self.addEventListener('load', function(){
            if (_is_tracking(self.__msk_url)){
                try { window.__maersk_data = JSON.parse(self.responseText); } catch(e) {}
            }
        });
        return _xs.apply(this, arguments);
    };
})();
"""

_ACTIVITY_LABELS = {
    "GATE-OUT":           "Gate out",
    "GATE-IN":            "Gate in",
    "LOAD":               "Loaded on vessel",
    "DISC":               "Discharged from vessel",
    "DISCH":              "Discharged from vessel",
    "DISCHARD":           "Discharged from vessel",
    "ARRI":               "Vessel arrived",
    "DEPA":               "Vessel departed",
    "CONTAINER ARRIVAL":  "Vessel arrived",
    "CONTAINER DEPARTURE":"Vessel departed",
    "CONTAINER RETURN":   "Empty return",
    "TRANSHIP":           "Transshipment",
    "DELIVERED":          "Delivered",
    "EMPTY-RET":          "Empty return",
    "EMPTY-REL":          "Empty released",
    "RAIL-ARR":           "Rail arrived",
    "RAIL-DEP":           "Rail departed",
}


def _fmt(dt: str) -> str:
    return dt[:16].replace("T", " ") if dt else ""


def _parse(data: dict) -> dict:
    result = {
        "POL": "", "POD": "", "POR": "", "FND": "",
        "Container No": "", "Vessel": "", "ATD": "", "ATA": "",
        "Latest Status": "",
    }

    # Unwrap envelope shapes — Maersk has changed wrapper key names over time
    if isinstance(data, list):
        data = data[0] if data else {}
    inner = (data.get("shipments") or [data])[0] if "shipments" in data else data

    origin = inner.get("origin") or data.get("origin") or {}
    dest   = inner.get("destination") or data.get("destination") or {}
    result["POL"] = (origin.get("city") or origin.get("portName") or "").upper()
    result["POD"] = (dest.get("city") or dest.get("portName") or "").upper()

    containers = inner.get("containers") or data.get("containers") or []
    cnums = [c.get("container_num") or c.get("containerNum") or c.get("number") or ""
             for c in containers if c]
    cnums = [c for c in cnums if c]
    if cnums:
        result["Container No"] = " | ".join(cnums)

    events = []
    vessel = ""
    eta_at_pod = ""   # estimated arrival at the destination (ETA fallback)
    pod_city = result["POD"]

    if containers:
        first = containers[0]
        locations = first.get("locations") or first.get("moves") or []
        for loc in locations:
            city = (loc.get("city") or loc.get("terminal") or
                    loc.get("location") or loc.get("port") or "").upper()
            for ev in (loc.get("events") or []):
                activity = ev.get("activity") or ev.get("eventCode") or ""
                # Accept both "ACTUAL" type and events with a real date
                ev_type = ev.get("event_time_type") or ev.get("type") or ""
                if ev_type and ev_type.upper() not in ("ACTUAL", "REAL", ""):
                    # Estimated/expected event — capture the projected arrival
                    # at the destination as the ETA (used when the container
                    # hasn't actually arrived at the POD yet).
                    if pod_city and city == pod_city and "ARRIV" in activity.upper():
                        dt_e = _fmt(ev.get("event_time") or ev.get("eventTime") or "")
                        if dt_e:
                            eta_at_pod = dt_e
                    continue
                dt       = _fmt(ev.get("event_time") or ev.get("eventTime") or "")
                if not dt:
                    continue

                label     = _ACTIVITY_LABELS.get(activity, activity)
                ev_vessel = (ev.get("vessel_name") or ev.get("vessel") or
                             ev.get("vesselName") or "")
                ev_voyage = (ev.get("voyage") or ev.get("voyage_number") or
                             ev.get("voyageNumber") or "")

                if ev_vessel and not vessel:
                    vessel = ev_vessel

                line = f"{label}  {city}  {dt}"
                if ev_vessel:
                    line += f"  {ev_vessel}"
                if ev_voyage:
                    line += f"  {ev_voyage}"

                events.append((dt, line, activity))

    events.sort(key=lambda x: x[0])

    if events:
        result["Latest Status"] = "\n".join(e[1] for e in events)
        for dt, _, act in events:
            if act in ("LOAD", "DEPA") and not result["ATD"]:
                result["ATD"] = dt
                break
        for dt, _, act in reversed(events):
            if act in ("DISC", "DISCH", "ARRI") and not result["ATA"]:
                result["ATA"] = dt
                break
    else:
        result["Latest Status"] = "No tracking events found"

    # No actual arrival at the POD yet → use the estimated arrival there.
    if not result["ATA"] and eta_at_pod:
        result["ATA"] = eta_at_pod

    result["Vessel"] = vessel
    return result


def scrape(driver, bl: str) -> dict:
    bl = bl.strip()

    # Fastest path: official API (only if a Consumer-Key is configured).
    data = _api_scrape(bl)
    if data.get("POL") or data.get("POD") or data.get("Container No"):
        return data

    # No driver (HTTP-first mode) and API didn't yield data → let api.py
    # retry with a real Chrome driver.
    if driver is None:
        log.info("[MAERSK] API miss for %s — need Chrome fallback", bl)
        return {}

    log.info("[MAERSK] Scraping %s via CDP fetch-intercept", bl)

    # Inject the fetch wrapper BEFORE the page loads so it wraps fetch
    # before React/the app code runs.
    try:
        driver.execute_cdp_cmd("Page.addScriptToEvaluateOnNewDocument",
                               {"source": _INTERCEPT_JS})
    except Exception as e:
        log.warning("[MAERSK] CDP inject failed: %s", e)

    driver.get(_TRACKING_URL.format(bl=bl))
    dismiss_cookies_js(driver)

    # Poll for window.__maersk_data (set by our interceptor when the API responds)
    deadline = time.time() + 20
    while time.time() < deadline:
        try:
            data = driver.execute_script("return window.__maersk_data;")
            if data:
                log.info("[MAERSK] Data captured via interceptor")
                return _parse(data)
        except Exception:
            pass
        time.sleep(0.3)

    log.warning("[MAERSK] Timed out waiting for tracking data")
    return {
        "POL": "", "POD": "", "POR": "", "FND": "",
        "Container No": "", "Vessel": "", "ATD": "", "ATA": "",
        "Latest Status": "Error: Maersk tracking data not received in time",
    }
