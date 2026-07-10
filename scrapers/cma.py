"""
scrapers/cma.py — CDP XHR-intercept approach

CMA CGM uses server-side rendering — no clean JSON API. But when the
user clicks "Display Details", the page POSTs all tracking data
(vessels, dates, locations) to /ebusiness/tracking/mapdetail for map
rendering. We intercept that XHR request body instead of parsing the
complex DOM text.

Flow:
  1. Inject XHR interceptor via Page.addScriptToEvaluateOnNewDocument
  2. Navigate to the tracking page, fill BL, submit
  3. Wait for "Display Details" button → click it
  4. Our interceptor captures the form-encoded mapdetail request body
  5. Parse the body for POL/POD/events — dates already ISO, no date parsing needed
  6. Fallback: if intercept times out, parse DOM text (original approach)
"""
import os
import re
import time
import logging
from urllib.parse import parse_qs

from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.common.exceptions import TimeoutException
from scrapers._common import dismiss_cookies_js

log = logging.getLogger(__name__)

TRACKING_URL = "https://www.cma-cgm.com/ebusiness/tracking/search"

# ── Official CMA CGM API (api.cma-cgm.com) ───────────────────────────────────
# When an API key is configured we call CMA's official Track & Trace API
# instead of driving Chrome. Register at https://api.cma-cgm.com, subscribe to
# the tracking API, then:
#   • put the key in credentials/cma_api_key.txt   (simplest), or
#   • set the CMA_API_KEY environment variable.
# The endpoint and key-header name are overridable via env because CMA's
# products/paths differ per subscription. The JSON response is parsed by the
# existing _parse_cma_json(); if your product returns a different shape, that
# parser is the one place to adjust (verify with GET /api/cma/test).
_API_URL = os.environ.get(
    "CMA_API_URL",
    "https://apis.cma-cgm.com/tracking/v1/tracking?shippingCompany=0001&bookingReference={bl}",
)
_API_KEY_HEADER = os.environ.get("CMA_API_KEY_HEADER", "KeyId")
_API_KEY_FILE = os.environ.get("CMA_API_KEY_FILE", "credentials/cma_api_key.txt")


def _get_api_key() -> str:
    key = os.environ.get("CMA_API_KEY", "").strip()
    if key:
        return key
    try:
        with open(_API_KEY_FILE, "r", encoding="utf-8") as f:
            return f.read().strip()
    except Exception:
        return ""


def _api_scrape(bl: str) -> dict:
    """Direct CMA API call using an API key. Returns {} when no key is
    configured or the call fails, so callers fall back to the browser path."""
    key = _get_api_key()
    if not key:
        return {}
    try:
        import requests
        r = requests.get(
            _API_URL.format(bl=bl.strip()),
            headers={
                _API_KEY_HEADER: key,
                "Accept": "application/json",
            },
            timeout=20,
        )
        if r.status_code == 200 and r.text.strip()[:1] in "{[":
            data = r.json()
            parsed = _parse_cma_json(data if isinstance(data, dict) else {"data": data})
            if parsed.get("POL") or parsed.get("POD") or parsed.get("Container No"):
                log.info("[CMA] Official API success for %s", bl)
                return parsed
            log.warning("[CMA] API returned no usable data for %s", bl)
        else:
            log.warning("[CMA] API HTTP %d for %s: %s",
                        r.status_code, bl, r.text[:120].replace("\n", " "))
    except Exception as e:
        log.warning("[CMA] API call failed for %s: %s", bl, e)
    return {}

MONTHS = {"JAN":"01","FEB":"02","MAR":"03","APR":"04","MAY":"05","JUN":"06",
           "JUL":"07","AUG":"08","SEP":"09","OCT":"10","NOV":"11","DEC":"12"}

# Map CMA's internal containerStatus codes to human-readable labels
_STATUS_MAP = {
    "ActualVesselDeparture":    "Vessel Departure",
    "ActualVesselArrival":      "Vessel Arrival",
    "EmptyInDepotMEA":          "Empty In Depot",
    "ActualGateIn":             "Gate In",
    "ActualGateOut":            "Gate Out",
    "ActualDischarge":          "Discharged from Vessel",
    "ActualLoad":               "Loaded on Vessel",
    "ActualTransshipDeparture": "Transshipment Departure",
    "ActualTransshipArrival":   "Transshipment Arrival",
    "FullInDepot":              "Full In Depot",
    "EmptyToShipper":           "Empty to Shipper",
    "FullToConsignee":          "Full to Consignee",
    "ActualRailDeparture":      "Rail Departure",
    "ActualRailArrival":        "Rail Arrival",
}

_INTERCEPT_JS = """
window.__cma_payload = null;
window.__cma_response = null;
(function() {
    var _open = XMLHttpRequest.prototype.open;
    var _send = XMLHttpRequest.prototype.send;

    function _is_tracking(url) {
        return url && (
            url.indexOf('mapdetail')  !== -1 ||
            url.indexOf('tracking')   !== -1 ||
            url.indexOf('cargotrack') !== -1
        );
    }

    XMLHttpRequest.prototype.open = function(method, url) {
        this.__cma_url = url ? url.toString() : '';
        return _open.apply(this, arguments);
    };

    XMLHttpRequest.prototype.send = function(body) {
        var self = this;
        if (_is_tracking(self.__cma_url)) {
            // Capture request body (form payload for mapdetail)
            if (self.__cma_url.indexOf('mapdetail') !== -1) {
                try {
                    if (typeof body === 'string') {
                        window.__cma_payload = body;
                    } else if (body && body.toString && body.toString() !== '[object FormData]') {
                        window.__cma_payload = body.toString();
                    }
                } catch(e) {}
            }
            // Also capture JSON response for any tracking XHR
            self.addEventListener('load', function() {
                try {
                    var ct = self.getResponseHeader('content-type') || '';
                    if (ct.indexOf('json') !== -1 && self.responseText) {
                        window.__cma_response = JSON.parse(self.responseText);
                    }
                } catch(e) {}
            });
        }
        return _send.apply(this, arguments);
    };

    // Also intercept fetch() for newer CMA page versions
    var _origFetch = window.fetch;
    if (_origFetch) {
        window.fetch = function(url, opts) {
            var p = _origFetch.apply(this, arguments);
            try {
                if (_is_tracking(url ? url.toString() : '')) {
                    p.then(function(r) {
                        r.clone().json().then(function(d) {
                            window.__cma_response = d;
                        }).catch(function(){});
                    }).catch(function(){});
                }
            } catch(e) {}
            return p;
        };
    }
})();
"""

_EMPTY = {
    "POL": "", "POD": "", "POR": "", "FND": "",
    "Container No": "", "Vessel": "", "ATA": "", "ATD": "",
    "Latest Status": "",
}


def _js_click(driver, el):
    driver.execute_script("arguments[0].scrollIntoView(true); arguments[0].click();", el)


def _clean_port(s: str) -> str:
    """'FELIXSTOWE (GB)' → 'FELIXSTOWE'"""
    return re.sub(r'\s*\([A-Z]{2}\)\s*$', '', s).strip().upper()


def _iso_to_parts(iso: str):
    """'2026-01-21T18:40:00' → ('2026-01-21', '18:40')"""
    if not iso or iso.startswith("0001"):
        return "", ""
    date = iso[:10]
    time_part = iso[11:16] if len(iso) >= 16 else "00:00"
    return date, time_part


def _parse_mapdetail(payload: str) -> dict:
    """Parse the form-encoded mapdetail POST body into our standard result dict."""
    result = dict(_EMPTY)

    try:
        qs = parse_qs(payload, keep_blank_values=True)
    except Exception as e:
        log.warning("[CMA] parse_qs failed: %s", e)
        return result

    def get(key):
        return (qs.get(key) or [''])[0].strip()

    pol_raw = get("ContainerMoveDetail[routingInformation][portOfLoading][name]")
    pod_raw = get("ContainerMoveDetail[routingInformation][portOfDischarge][name]")
    por_raw = get("ContainerMoveDetail[routingInformation][placeOfReceipt][name]")
    fnd_raw = get("ContainerMoveDetail[routingInformation][placeOfDelivery][name]")

    result["POL"] = _clean_port(pol_raw) if pol_raw else ""
    result["POD"] = _clean_port(pod_raw) if pod_raw else ""
    result["POR"] = _clean_port(por_raw) if por_raw else ""
    result["FND"] = _clean_port(fnd_raw) if fnd_raw else ""

    cref = (get("ContainerMoveDetail[references][containerReference]")
            or get("ContainerReference"))
    result["Container No"] = cref

    # Collect pastMoves + currentMoves into a single event list
    events = []
    for move_key in ("pastMoves", "currentMoves"):
        i = 0
        while True:
            prefix = f"ContainerMoveDetail[{move_key}][{i}]"
            status_code = get(f"{prefix}[containerStatus]")
            if not status_code:
                break
            location  = get(f"{prefix}[location][name]")
            date_iso  = get(f"{prefix}[containerStatusDate]")
            vessel    = get(f"{prefix}[vesselName]")
            voyage    = get(f"{prefix}[voyageReference]")
            date_s, time_s = _iso_to_parts(date_iso)
            if date_s:
                events.append({
                    "status":   _STATUS_MAP.get(status_code, status_code),
                    "location": location,
                    "date":     date_s,
                    "time":     time_s,
                    "vessel":   vessel,
                    "voyage":   voyage,
                })
            i += 1

    events.sort(key=lambda e: f"{e['date']} {e['time']}")

    rows   = ["Status  Place of Activity  Date  Time  Transport  Voyage No."]
    vessel = ""
    for ev in events:
        line = f"{ev['status']}  {ev['location']}  {ev['date']} {ev['time']}"
        if ev["vessel"]:
            line += f"  {ev['vessel']}"
            if ev["voyage"]:
                line += f"  {ev['voyage']}"
            if not vessel:
                vessel = ev["vessel"]
        rows.append(line)

    result["Latest Status"] = "\n".join(rows)
    result["Vessel"] = vessel

    for ev in events:
        if "departure" in ev["status"].lower() and not result["ATD"]:
            result["ATD"] = f"{ev['date']} {ev['time']}"
            break
    for ev in reversed(events):
        if "arrival" in ev["status"].lower():
            result["ATA"] = f"{ev['date']} {ev['time']}"
            break

    # No actual arrival yet (still in transit) → use CMA's estimated arrival.
    # estimatedTimeOfArrival is CMA's overall ETA at the POD; fall back to the
    # portOfDischarge date if that field is blank.
    if not result["ATA"]:
        eta_iso = (get("ContainerMoveDetail[estimatedTimeOfArrival]")
                   or get("ContainerMoveDetail[routingInformation][portOfDischarge][date]"))
        d_s, t_s = _iso_to_parts(eta_iso)
        if d_s and d_s > "2000":               # ignore the 0001-01-01 placeholder
            result["ATA"] = f"{d_s} {t_s}"

    log.info("[CMA] mapdetail parse: POL=%s POD=%s events=%d", result["POL"], result["POD"], len(events))
    return result


def _parse_cma_json(data: dict) -> dict:
    """Parse a JSON tracking response from CMA's newer API (if available)."""
    result = dict(_EMPTY)
    inner = data.get("data") or data.get("trackingData") or data
    containers = (inner.get("containerList") or inner.get("containers") or
                  inner.get("equipments") or [])
    if not containers:
        return result

    c = containers[0] if isinstance(containers, list) else containers
    result["POL"] = _clean_port(c.get("portOfLoading") or c.get("pol") or "")
    result["POD"] = _clean_port(c.get("portOfDischarge") or c.get("pod") or "")
    result["Container No"] = c.get("containerReference") or c.get("containerNo") or ""

    events = (c.get("moves") or c.get("events") or c.get("pastMoves") or [])
    rows = []
    vessel = ""
    for ev in events:
        if not isinstance(ev, dict):
            continue
        status   = _STATUS_MAP.get(ev.get("containerStatus",""), ev.get("containerStatus",""))
        loc      = ev.get("location",{}).get("name","") if isinstance(ev.get("location"), dict) else ev.get("location","")
        date_iso = ev.get("containerStatusDate","")
        vsl      = ev.get("vesselName","")
        voy      = ev.get("voyageReference","")
        date_s, time_s = _iso_to_parts(date_iso)
        if date_s and status:
            line = f"{status}  {loc}  {date_s} {time_s}"
            if vsl:
                line += f"  {vsl}"
                if not vessel:
                    vessel = vsl
            if voy:
                line += f"  {voy}"
            rows.append((f"{date_s} {time_s}", line))

    rows.sort(key=lambda x: x[0])
    if rows:
        result["Latest Status"] = "\n".join(r[1] for r in rows)
    result["Vessel"] = vessel
    return result


# ── Original DOM-parse fallback (kept intact) ─────────────────────────────────

def _parse_date(date_line, time_line=""):
    try:
        clean = re.sub(r'^[A-Za-z]+[,\.]?\s*', '', date_line.strip())
        m = re.match(r'(\d{1,2})-([A-Z]{3})-(\d{4})', clean, re.I)
        if not m:
            return ""
        d, mon, y = m.groups()
        mm = MONTHS.get(mon.upper(), "00")
        h, mi = "00", "00"
        if time_line:
            tm = re.match(r'(\d{1,2}):(\d{2})\s*(AM|PM)?', time_line.strip(), re.I)
            if tm:
                h, mi, ampm = tm.groups()
                h = int(h)
                if ampm and ampm.upper() == "PM" and h != 12:
                    h += 12
                elif ampm and ampm.upper() == "AM" and h == 12:
                    h = 0
                h = str(h).zfill(2)
        return f"{y}-{mm}-{d.zfill(2)} {h}:{mi}"
    except Exception:
        return ""


def _dom_fallback(driver, bl: str) -> dict:
    """Original DOM-text parser — used when mapdetail intercept fails."""
    result = dict(_EMPTY)
    body_text = driver.find_element(By.TAG_NAME, "body").text
    containers = list(dict.fromkeys(re.findall(r'\b([A-Z]{4}\d{7})\b', body_text)))
    if containers:
        result["Container No"] = " | ".join(containers)

    lines_pre = [l.strip() for l in body_text.split("\n") if l.strip()]
    for i, line in enumerate(lines_pre):
        if re.match(r'^[A-Z]{4}\d{7}$', line) and i + 2 < len(lines_pre):
            status_candidate = lines_pre[i + 2]
            if status_candidate and status_candidate.isupper():
                result["Latest Status"] = status_candidate
                break

    for i, line in enumerate(lines_pre):
        if "ETA Berth at POD" in line and i + 2 < len(lines_pre):
            eta = _parse_date(lines_pre[i + 1], lines_pre[i + 2])
            if eta:
                result["ATA"] = eta
            break

    try:
        detail_spans = driver.find_elements(By.XPATH, "//*[contains(text(),'Display Details')]")
        if detail_spans:
            _js_click(driver, detail_spans[0])
            try:
                WebDriverWait(driver, 15).until(
                    EC.presence_of_element_located((By.XPATH, "//*[contains(text(),'Vessel (Voyage)')]")))
                time.sleep(0.5)
            except TimeoutException:
                time.sleep(2)

            body2 = driver.find_element(By.TAG_NAME, "body").text
            lines = [l.strip() for l in body2.split("\n") if l.strip()]

            try:
                prev_moves = driver.find_elements(By.XPATH,
                    "//*[contains(text(),'Display Previous Moves')]")
                if prev_moves:
                    _js_click(driver, prev_moves[0])
                    time.sleep(1.5)
                    body2 = driver.find_element(By.TAG_NAME, "body").text
                    lines = [l.strip() for l in body2.split("\n") if l.strip()]
            except Exception as e:
                log.warning("[CMA] Display Previous Moves click failed: %s", e)

            for i, line in enumerate(lines):
                if line == "POO" and i + 1 < len(lines):
                    result["POR"] = lines[i + 1]; break
            for i, line in enumerate(lines):
                if line == "POL" and i + 1 < len(lines):
                    result["POL"] = lines[i + 1]
                elif line == "POD" and i + 1 < len(lines):
                    result["POD"] = lines[i + 1]
                if result["POL"] and result["POD"]:
                    break

            table_start = None
            for i, line in enumerate(lines):
                if line == "Vessel (Voyage)":
                    table_start = i + 1; break

            events = []
            DAY_RE = re.compile(
                r'^(?:Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday),\s+\d{1,2}-[A-Z]{3}-\d{4}$', re.I)
            TIME_RE = re.compile(r'^\d{1,2}:\d{2}\s*(?:AM|PM)$', re.I)

            if table_start is not None:
                i = table_start
                while i < len(lines):
                    line = lines[i]
                    if "Times reflected are local" in line or "Provisional moves" in line:
                        break
                    if DAY_RE.match(line) and i + 2 < len(lines):
                        date_l = line
                        time_l = lines[i + 1] if TIME_RE.match(lines[i + 1]) else ""
                        offset = 2 if time_l else 1
                        status_raw = lines[i + offset] if i + offset < len(lines) else ""
                        if status_raw.upper().startswith("TRAIN "):
                            m = re.match(r'^(TRAIN\s+\w+)\s+(.+)$', status_raw, re.I)
                            status   = m.group(1) if m else status_raw
                            location = m.group(2) if m else ""
                            iso = _parse_date(date_l, time_l)
                            if iso:
                                events.append({"status": status, "location": location,
                                               "date": iso[:10], "time": iso[11:16] if len(iso) >= 16 else "00:00",
                                               "transport": "", "voyage": "",
                                               "line": f"{status}  {location}  {iso[:10]} {iso[11:16] if len(iso) >= 16 else '00:00'}"})
                            i += offset + 1
                            continue
                        status   = status_raw
                        location = lines[i + offset + 1] if i + offset + 1 < len(lines) else ""
                        vessel_line = lines[i + offset + 3] if i + offset + 3 < len(lines) else ""
                        transport, voyage = "", ""
                        if (vessel_line and re.search(r'\(', vessel_line)
                                and not DAY_RE.match(vessel_line)
                                and not vessel_line.upper().startswith("TRAIN ")):
                            vparts = vessel_line.rsplit('(', 1)
                            transport = vparts[0].strip()
                            voyage = vparts[1].rstrip(')').strip() if len(vparts) > 1 else ""
                            i += offset + 4
                        else:
                            i += offset + 3
                        iso = _parse_date(date_l, time_l)
                        if not iso:
                            continue
                        event_line = f"{status}  {location}  {iso[:10]} {iso[11:16] if len(iso) >= 16 else '00:00'}"
                        if transport:
                            event_line += f"  {transport}"
                            if voyage:
                                event_line += f"  {voyage}"
                        events.append({"status": status, "location": location,
                                       "date": iso[:10], "time": iso[11:16] if len(iso) >= 16 else "00:00",
                                       "transport": transport, "voyage": voyage, "line": event_line})
                    else:
                        i += 1

            if events:
                events.sort(key=lambda e: f"{e['date']} {e['time']}")
                result["Latest Status"] = "\n".join(e["line"] for e in events)
                for ev in events:
                    if "VESSEL DEPARTURE" in ev["status"].upper():
                        result["ATD"] = f"{ev['date']} {ev['time']}"; break
                for ev in reversed(events):
                    if "VESSEL ARRIVAL" in ev["status"].upper():
                        result["ATA"] = f"{ev['date']} {ev['time']}"; break
                for ev in reversed(events):
                    if ev["transport"]:
                        result["Vessel"] = ev["transport"]
                        if ev["voyage"]:
                            result["Vessel"] += f" / {ev['voyage']}"
                        break
    except Exception as e:
        log.error("[CMA] DOM fallback error: %s", e)

    return result


def scrape(driver, bl: str) -> dict:
    bl = bl.strip()

    # Fastest path: official API (only when an API key is configured).
    data = _api_scrape(bl)
    if data.get("POL") or data.get("POD") or data.get("Container No"):
        return data

    # No driver (HTTP-first mode) and API didn't yield data → let api.py
    # retry with a real Chrome driver.
    if driver is None:
        log.info("[CMA] API miss for %s — need Chrome fallback", bl)
        return {}

    log.info("[CMA] Scraping BL: %s", bl)

    # Inject interceptor BEFORE navigation
    try:
        driver.execute_cdp_cmd("Page.addScriptToEvaluateOnNewDocument",
                               {"source": _INTERCEPT_JS})
    except Exception as e:
        log.warning("[CMA] CDP inject failed: %s", e)

    driver.get(TRACKING_URL)
    dismiss_cookies_js(driver, ["#didomi-notice-agree-button", ".didomi-continue-without-agreeing"])

    # Fill in BL number
    inp = None
    for sel in ["input#Reference", "input[name='SearchViewModelReference']", "input[name='Reference']"]:
        try:
            inp = WebDriverWait(driver, 10).until(EC.visibility_of_element_located((By.CSS_SELECTOR, sel)))
            break
        except TimeoutException:
            continue
    if inp is None:
        return dict(_EMPTY, **{"Latest Status": "Error: tracking input not found"})

    _js_click(driver, inp)
    inp.clear()
    time.sleep(0.2)
    inp.send_keys(bl)
    time.sleep(0.5)

    for sel in ["button#btnTracking", "button[name='search']", ".o-button.primary", "input[type='submit']"]:
        try:
            btn = driver.find_element(By.CSS_SELECTOR, sel)
            if btn.is_displayed():
                _js_click(driver, btn)
                break
        except Exception:
            pass

    # Wait for container cards to appear
    try:
        WebDriverWait(driver, 20).until(
            EC.presence_of_element_located((By.XPATH, "//*[contains(text(),'Display Details')]")))
    except TimeoutException:
        time.sleep(3)

    # Read ALL container numbers from DOM now (mapdetail only gives one)
    all_containers = []
    try:
        body_pre = driver.find_element(By.TAG_NAME, "body").text
        all_containers = list(dict.fromkeys(re.findall(r'\b([A-Z]{4}\d{7})\b', body_pre)))
        log.info("[CMA] All containers from DOM: %s", all_containers)
    except Exception:
        pass

    # Click "Display Details" on the first container — triggers the mapdetail XHR
    try:
        detail_spans = driver.find_elements(By.XPATH, "//*[contains(text(),'Display Details')]")
        if detail_spans:
            _js_click(driver, detail_spans[0])
            log.info("[CMA] Clicked Display Details")
    except Exception as e:
        log.warning("[CMA] Could not click Display Details: %s", e)

    # Poll for the intercepted mapdetail payload (or JSON response)
    deadline = time.time() + 20
    while time.time() < deadline:
        try:
            payload = driver.execute_script("return window.__cma_payload;")
            if payload and "ContainerMoveDetail" in payload:
                log.info("[CMA] mapdetail payload intercepted (%d bytes)", len(payload))
                result = _parse_mapdetail(payload)
                if result.get("POL") or result.get("POD"):
                    if all_containers:
                        result["Container No"] = " | ".join(all_containers)
                    return result
        except Exception:
            pass
        try:
            json_resp = driver.execute_script("return window.__cma_response;")
            if isinstance(json_resp, dict) and (
                json_resp.get("data") or json_resp.get("trackingData")
                or json_resp.get("containerList")
            ):
                log.info("[CMA] JSON tracking response intercepted")
                result = _parse_cma_json(json_resp)
                if result.get("POL") or result.get("POD"):
                    if all_containers:
                        result["Container No"] = " | ".join(all_containers)
                    return result
        except Exception:
            pass
        time.sleep(0.3)

    log.warning("[CMA] intercept timed out — falling back to DOM parse")
    return _dom_fallback(driver, bl)
