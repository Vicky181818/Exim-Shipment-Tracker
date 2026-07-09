"""
scrapers/pil.py  —  PIL Shipping (Pacific International Lines) via SeaRates

SeaRates uses reCAPTCHA Enterprise as a Bearer token, which only runs in a
real browser.  We use the same CDP fetch-intercept pattern as Maersk:

  1. Inject fetch/XHR wrapper before page JS runs
  2. Navigate to searates.com tracking page
  3. reCAPTCHA fires automatically in Chrome → widget calls the API
  4. Interceptor stores the JSON in window.__sr_data
  5. We poll for it, parse, and return

JSON structure (from bundle analysis):
  data.locations[]         — {id, name, country_code}
  data.route.pol/pod       — {location: id, date, actual: bool}
  data.containers[].events — [{status, location: id, date, actual, vessel, voyage}]
  data.vessels[]           — [{id, name}]
"""

import time
import logging

log = logging.getLogger(__name__)

_TRACKING_URL = "https://www.searates.com/container/tracking/?number={bl}&type=BL"

# Event status codes → human-readable labels
_EVENT_LABELS = {
    "CEP": "Empty container picked up",
    "CPS": "Container picked up",
    "CGI": "Container gate in",
    "CLL": "Loaded on vessel",
    "VDL": "Vessel departed (load port)",
    "VAT": "Vessel arrived (transshipment)",
    "CDT": "Container discharged (transshipment)",
    "TSD": "Transshipment discharge",
    "CLT": "Loaded on vessel (transshipment)",
    "VDT": "Vessel departed (transshipment)",
    "VAD": "Vessel arrived",
    "CDD": "Container discharged",
    "CGO": "Container gate out",
    "CDC": "Delivered to consignee",
    "CER": "Empty container returned",
    "LTS": "Less-than-ship transshipment",
    "BTS": "Break transshipment",
}

# Codes that indicate arrival at final POD
_ARRIVAL_CODES = {"VAD", "CDD", "CGO", "CDC"}
# Codes that indicate departure from POL
_DEPART_CODES  = {"VDL", "CLL"}

_INTERCEPT_JS = """
window.__sr_data = null;
(function() {
    function _is_sr(url) {
        return url && (
            url.indexOf('tracking.searates.com') !== -1 ||
            url.indexOf('tracking-system/reverse/tracking') !== -1
        );
    }
    var _orig = window.fetch;
    window.fetch = function(url, opts) {
        var p = _orig.apply(this, arguments);
        try {
            if (_is_sr(url ? url.toString() : '')) {
                p.then(function(r) {
                    r.clone().json().then(function(d) {
                        if (d && d.data) window.__sr_data = d;
                    }).catch(function(){});
                }).catch(function(){});
            }
        } catch(e) {}
        return p;
    };
    var _xo = XMLHttpRequest.prototype.open;
    var _xs = XMLHttpRequest.prototype.send;
    XMLHttpRequest.prototype.open = function(method, url) {
        this.__sr_url = url ? url.toString() : '';
        return _xo.apply(this, arguments);
    };
    XMLHttpRequest.prototype.send = function() {
        var self = this;
        self.addEventListener('load', function() {
            if (_is_sr(self.__sr_url)) {
                try { window.__sr_data = JSON.parse(self.responseText); } catch(e) {}
            }
        });
        return _xs.apply(this, arguments);
    };
})();
"""

_EMPTY = {
    "POR": "", "POL": "", "POD": "", "FND": "",
    "Container No": "", "Vessel": "", "ATD": "", "ATA": "",
    "Latest Status": "",
}


def _fmt(dt: str) -> str:
    return (dt or "")[:16].replace("T", " ")


def _parse(raw: dict) -> dict:
    result = dict(_EMPTY)
    data = raw.get("data") or {}
    if not data:
        result["Latest Status"] = "No tracking data found"
        return result

    # Build location id → name lookup
    loc_map = {}
    for loc in (data.get("locations") or []):
        lid = loc.get("id")
        if lid:
            name = loc.get("name") or loc.get("country") or loc.get("country_code") or ""
            country = loc.get("country_code") or ""
            loc_map[lid] = f"{name}, {country}".strip(", ") if name else country

    def loc_name(lid):
        return loc_map.get(lid, "").upper()

    # Build vessel id → name lookup
    vessel_map = {}
    for v in (data.get("vessels") or []):
        vid = v.get("id")
        if vid:
            vessel_map[vid] = v.get("name", "")

    # POL / POD from route
    route = data.get("route") or {}
    if route.get("pol", {}).get("location"):
        result["POL"] = loc_name(route["pol"]["location"])
    if route.get("pod", {}).get("location"):
        result["POD"] = loc_name(route["pod"]["location"])

    # ATD / ATA from route dates (actual only)
    if route.get("pol", {}).get("actual") and route.get("pol", {}).get("date"):
        result["ATD"] = _fmt(route["pol"]["date"])
    if route.get("pod", {}).get("actual") and route.get("pod", {}).get("date"):
        result["ATA"] = _fmt(route["pod"]["date"])
    elif route.get("pod", {}).get("date"):
        # Not yet actual — still useful as ETA
        result["ATA"] = _fmt(route["pod"]["date"])

    # Container numbers
    containers = data.get("containers") or []
    cnums = [c.get("number") for c in containers if c.get("number")]
    if cnums:
        result["Container No"] = " | ".join(cnums)

    # Events from first container
    raw_events = (containers[0].get("events") if containers else None) or []

    # Sort by date (actual events first within same date)
    raw_events = sorted(raw_events, key=lambda e: (_fmt(e.get("date", "")), not e.get("actual", False)))

    rows = ["Status  Place of Activity  Date  Time  Transport  Voyage No."]
    vessel = ""

    for ev in raw_events:
        code   = ev.get("status", "")
        loc    = loc_name(ev.get("location", ""))
        dt     = _fmt(ev.get("date", ""))
        actual = ev.get("actual", False)
        vid    = ev.get("vessel", "")
        voyage = ev.get("voyage", "") or ""
        vname  = vessel_map.get(vid, "") if vid else ""

        if not dt:
            continue
        if not actual:
            continue  # skip estimated/planned events from status timeline

        label = _EVENT_LABELS.get(code, code)
        if vname and not vessel:
            vessel = vname

        line = f"{label}  {loc}  {dt}"
        if vname:
            line += f"  {vname}"
        if voyage:
            line += f"  {voyage}"
        rows.append(line)

        if code in _DEPART_CODES and not result["ATD"]:
            result["ATD"] = dt
        if code in _ARRIVAL_CODES:
            result["ATA"] = dt   # keep updating — last arrival wins

    if len(rows) > 1:
        result["Latest Status"] = "\n".join(rows)
        result["Vessel"] = vessel
    else:
        result["Latest Status"] = "No tracking events found"

    # Fallback: if POL/POD empty, derive from first/last actual event locations
    if not result["POL"]:
        for ev in raw_events:
            if ev.get("actual") and ev.get("status") in _DEPART_CODES:
                result["POL"] = loc_name(ev.get("location", ""))
                break
    if not result["POD"]:
        for ev in reversed(raw_events):
            if ev.get("actual") and ev.get("status") in _ARRIVAL_CODES:
                result["POD"] = loc_name(ev.get("location", ""))
                break

    log.info("[PIL/SR] POL=%s POD=%s ATD=%s ATA=%s events=%d",
             result["POL"], result["POD"], result["ATD"], result["ATA"], len(raw_events))
    return result


def scrape(driver, bl: str) -> dict:
    bl = bl.strip().upper()
    log.info("[PIL/SR] Scraping %s via SeaRates CDP intercept", bl)

    try:
        driver.execute_cdp_cmd("Page.addScriptToEvaluateOnNewDocument",
                               {"source": _INTERCEPT_JS})
    except Exception as e:
        log.warning("[PIL/SR] CDP inject failed: %s", e)

    driver.get(_TRACKING_URL.format(bl=bl))

    # Poll for window.__sr_data (SeaRates widget fires reCAPTCHA → API → our intercept)
    deadline = time.time() + 35
    while time.time() < deadline:
        try:
            data = driver.execute_script("return window.__sr_data;")
            if data:
                log.info("[PIL/SR] Data captured")
                return _parse(data)
        except Exception:
            pass
        time.sleep(0.5)

    log.warning("[PIL/SR] Timed out waiting for SeaRates data for %s", bl)
    return dict(_EMPTY, **{"Latest Status": "Error: SeaRates tracking data not received in time"})
