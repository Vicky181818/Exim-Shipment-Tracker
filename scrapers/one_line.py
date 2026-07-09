"""
scrapers/one_line.py  —  ONE LINE  (HTTP API → CDP intercept → Selenium DOM)

Fast path:   Direct HTTP POST to ONE LINE's internal JSON API.
             No Chrome required if it works. (~2-3s)
Middle path: CDP fetch-intercept injected before navigation — captures the
             React app's own API call without any DOM interaction. (~5-8s)
Slow path:   Full Selenium DOM (click rows, click tabs) — original approach.
             Only used as last resort.
"""

import re
import time
import logging
import requests

from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.common.exceptions import TimeoutException
from scrapers._common import dismiss_cookies_js

log = logging.getLogger(__name__)

_TRACKING_URL = (
    "https://ecomm.one-line.com/one-ecom/manage-shipment/cargo-tracking"
    "?trakNoParam={bl}&trakNoTpCdParam=B"
)

# ONE LINE's internal JSON API (called by their React SPA, works without login).
# Discovered 2026-07: POST {page, page_length, filters:{search_text, search_type}}
# → per-container items with por/pod, vesselVoyage and cargoEvents.
_SEARCH_URL = "https://ecomm.one-line.com/api/v2/edh/containers/track-and-trace/search"

# cargoEvents carry only a matrixId; the UI translates them client-side.
# Names below match ONE's own UI wording (E061/E089 confirmed against
# latestEvent.eventName). E170/E171 phrased so "EMPTY RETURN" triggers the
# Delivered rule in sync_excel._determine_status.
_MATRIX_EVENTS = {
    "E011": "Empty Container Release to Shipper",
    "E024": "Gate In to Outbound Terminal",
    "E040": "Loaded on Vessel at Port of Loading",
    "E061": "Vessel Departure from Port of Loading",
    "E089": "Vessel Arrival at Port of Discharging",
    "E105": "Unloaded from Vessel at Port of Discharging",
    "E138": "Gate Out from Inbound Terminal",
    "E170": "Empty Return from Consignee",
    "E171": "Empty Return from Consignee",
}

_API_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Accept": "application/json, text/plain, */*",
    "Content-Type": "application/json",
    "Origin": "https://ecomm.one-line.com",
    "Referer": "https://ecomm.one-line.com/one-ecom/manage-shipment/cargo-tracking",
}

# CDP fetch interceptor — captures the React app's own API call (no form interaction needed).
# Matches broadly so it still works if ONE LINE renames their internal tracking endpoint.
_INTERCEPT_JS = """
window.__one_data = null;
(function(){
    function _is_tracking(s){
        return s && (
            s.indexOf('cargo-tracking') !== -1 ||
            s.indexOf('tracking-list')  !== -1 ||
            s.indexOf('trakNo')         !== -1 ||
            s.indexOf('track-trace')    !== -1 ||
            s.indexOf('/tracking/')     !== -1
        );
    }
    var _f = window.fetch;
    window.fetch = function(url, opts){
        var p = _f.apply(this, arguments);
        try {
            if(_is_tracking(url ? url.toString() : '')){
                p.then(function(r){
                    r.clone().json().then(function(d){ window.__one_data = d; }).catch(function(){});
                }).catch(function(){});
            }
        } catch(e){}
        return p;
    };
    var _o = XMLHttpRequest.prototype.open, _s = XMLHttpRequest.prototype.send;
    XMLHttpRequest.prototype.open = function(m, url){
        this.__one_url = url ? url.toString() : '';
        return _o.apply(this, arguments);
    };
    XMLHttpRequest.prototype.send = function(){
        var self = this;
        self.addEventListener('load', function(){
            if(_is_tracking(self.__one_url)){
                try{ window.__one_data = JSON.parse(self.responseText); } catch(e){}
            }
        });
        return _s.apply(this, arguments);
    };
})();
"""

WAIT = 35

# Date and time appear on SEPARATE lines in ONE LINE's React SPA
_DATE_LINE_RE = re.compile(r'^\d{4}-\d{2}-\d{2}$')
_TIME_LINE_RE = re.compile(r'^\d{2}:\d{2}$')

# Fallback: date+time on the same line (e.g. from Sailing Info table)
_DATE_INLINE_RE = re.compile(r'(\d{4}-\d{2}-\d{2})\s+(\d{2}:\d{2})')

_CN_RE  = re.compile(r'\b([A-Z]{4}\d{7})\b')
# Location header: "CITY, COUNTRY"  (all-caps, contains comma)
_LOC_RE = re.compile(r'^[A-Z][A-Z ]+,\s+[A-Z][A-Z ]+$')

_EMPTY = {
    "POR": "", "POL": "", "POD": "", "FND": "",
    "Container No": "", "Vessel": "", "ATD": "", "ATA": "",
    "Latest Status": "",
}

_RESULT_CSS = (
    "table tbody tr, "
    "[class*='cargo-tracking'], [class*='CargoTracking'], "
    "[class*='tracking-result'], [class*='TrackingResult'], "
    "[class*='tableBody'], [class*='TableBody']"
)

_LOGIN_HINTS = [
    "sign in", "log in", "login", "username", "password",
    "sign-in", "auth/login", "/login",
]

_SAIL_SKIP = {
    "Vessel", "Port of Loading", "Departure Date", "Port of Discharging",
    "Arrival Time", "Actual Schedule", "Coastal Schedule", "Estimate Schedule",
    "Show Latest Event",
}




def _is_login_page(driver):
    url  = (driver.current_url or "").lower()
    body = ""
    try:
        body = (driver.find_element(By.TAG_NAME, "body").text or "").lower()
    except Exception:
        pass
    return any(hint in url or hint in body for hint in _LOGIN_HINTS)


def _strip_oney(bl: str) -> str:
    return re.sub(r'^ONEY', '', bl.strip().upper())


def _click_first_row(driver):
    """Click the first data row in the summary table to expand the detail panel."""
    try:
        rows = driver.find_elements(By.CSS_SELECTOR, "table tbody tr")
        if rows:
            rows[0].click()
            log.info("[ONE LINE] Clicked first container row")
            return True
    except Exception as e:
        log.warning("[ONE LINE] Row click failed: %s", e)
    return False


def _click_actual_schedule_tab(driver):
    """Click the 'Actual Schedule' tab to reveal the full event timeline."""
    for tab_css in [
        "[role='tab']",
        "button[class*='tab']",
        "li[class*='tab']",
        "a[class*='tab']",
        "div[class*='tab']",
    ]:
        try:
            tabs = driver.find_elements(By.CSS_SELECTOR, tab_css)
            for tab in tabs:
                txt = (tab.text or "").lower()
                if "actual" in txt:
                    tab.click()
                    time.sleep(1)
                    log.info("[ONE LINE] Clicked 'Actual Schedule' tab")
                    return True
        except Exception:
            continue
    return False


def _parse_body(body_text: str, bl_stripped: str) -> dict:
    """
    Parse page body text into tracking fields.

    ACTUAL body text structure (observed 2026-06):
    - Date and time are on SEPARATE consecutive lines:
        Empty Container Returned from Customer
        2026-03-18        ← _DATE_LINE_RE
        23:08             ← _TIME_LINE_RE
    - Location headers: "CITY, COUNTRY" (all-caps, _LOC_RE)
    - Summary table only loads first; detail section appears after clicking a row.
    - Detail section adds: Place of Receipt, Sailing Information, full timeline.
    """
    result = dict(_EMPTY)
    lines  = [l.strip() for l in body_text.split("\n") if l.strip()]

    # ── Container numbers ────────────────────────────────────────────────────
    containers = _CN_RE.findall(body_text)
    if containers:
        result["Container No"] = " | ".join(dict.fromkeys(containers))

    # ── POL / POD via label scan ─────────────────────────────────────────────
    for i, line in enumerate(lines):
        if line == "Place of Receipt" and i + 1 < len(lines):
            result["POR"] = lines[i + 1]
        if line in ("Place of Delivery", "Port of Discharge") and i + 1 < len(lines):
            result["POD"] = lines[i + 1]

    # ONE LINE uses "Place of Receipt" as the origin port
    if not result["POL"] and result["POR"]:
        result["POL"] = result["POR"]

    # POD fallback: last CITY, COUNTRY in body
    if not result["POD"]:
        loc_lines = [l for l in lines if _LOC_RE.match(l)]
        if loc_lines:
            result["POD"] = loc_lines[-1]

    # ── Sailing information → ATD / ATA / Vessel ─────────────────────────────
    sail_idx = next((i for i, l in enumerate(lines) if l == "Sailing Information"), None)
    vessels    = []
    sail_dates = []
    if sail_idx is not None:
        j = sail_idx + 1
        while j < min(sail_idx + 80, len(lines)):
            line = lines[j]
            if line in _SAIL_SKIP:
                j += 1
                continue
            # Stop at first timeline event (has lowercase)
            if (not _LOC_RE.match(line)
                    and not re.search(r'\d{3}[A-Z]|\([A-Z]{4}\)', line)
                    and not _DATE_LINE_RE.match(line)
                    and not _TIME_LINE_RE.match(line)
                    and any(c.islower() for c in line)):
                break
            # Vessel names: all-caps with digit code like "LANGENESS 033W (LNNT)"
            if (re.search(r'\d{3}[A-Z]', line) or re.search(r'\([A-Z]{4}\)', line)):
                vessels.append(line)
            # Collect date+time pairs (separate lines OR same line)
            if _DATE_LINE_RE.match(line) and j + 1 < len(lines) and _TIME_LINE_RE.match(lines[j + 1]):
                sail_dates.append((line, lines[j + 1]))
                j += 2
                continue
            # Same-line fallback
            m = _DATE_INLINE_RE.search(line)
            if m:
                sail_dates.append((m.group(1), m.group(2)))
            j += 1

        if vessels:
            result["Vessel"] = vessels[-1]
        if sail_dates:
            if not result["ATD"]:
                result["ATD"] = f"{sail_dates[0][0]} {sail_dates[0][1]}"
            if len(sail_dates) >= 2:
                result["ATA"] = f"{sail_dates[-1][0]} {sail_dates[-1][1]}"

    # ── Timeline events → Latest Status block ────────────────────────────────
    # Walk all lines; detect date+time pairs (on separate lines).
    # For each date, look back for the event name.
    # Skip sailing-info dates: those follow city/country lines directly.
    rows        = []
    current_loc = ""
    i           = 0

    while i < len(lines):
        line = lines[i]

        if _LOC_RE.match(line):
            current_loc = line
            i += 1
            continue

        # Detect date-only line followed by time-only line
        if (_DATE_LINE_RE.match(line)
                and i + 1 < len(lines)
                and _TIME_LINE_RE.match(lines[i + 1])):

            date_part = line
            time_part = lines[i + 1]

            # Look back for event name
            event_text = ""
            for back in range(1, 5):
                if i - back < 0:
                    break
                prev = lines[i - back]
                if _DATE_LINE_RE.match(prev) or _TIME_LINE_RE.match(prev):
                    break  # another date/time — stop
                if _LOC_RE.match(prev):
                    break  # city line → sailing-info date, skip
                if prev and len(prev) > 3:
                    # Skip vessel-name-like strings (all-caps + digits)
                    is_vessel = bool(
                        re.match(r'^[A-Z][A-Z0-9 ()/-]+$', prev)
                        and re.search(r'\d', prev)
                    )
                    if not is_vessel:
                        event_text = prev
                    break

            if event_text and current_loc:
                row = f"{event_text}  {current_loc}  {date_part} {time_part}"
                rows.append(row)

                s_lo = event_text.lower()
                if not result["ATA"] and "vessel arrival at port of discharge" in s_lo:
                    result["ATA"] = f"{date_part} {time_part}"
                if not result["ATD"] and "vessel departure from port of loading" in s_lo:
                    result["ATD"] = f"{date_part} {time_part}"

            i += 2  # skip the time line we just consumed
            continue

        # Fallback: date+time on same line
        m = _DATE_INLINE_RE.search(line)
        if m:
            event_text = line[:m.start()].strip()
            if event_text and len(event_text) > 3:
                row = f"{event_text}  {current_loc}  {m.group(1)} {m.group(2)}"
                rows.append(row)

        i += 1

    if rows:
        seen = set()
        deduped = []
        for row in rows:
            if row not in seen:
                seen.add(row)
                deduped.append(row)
        result["Latest Status"] = "\n".join(deduped)

    # ── Summary-table fallback (when detail section wasn't loaded) ────────────
    # Even without the full timeline, extract the "latest event" from each
    # container row in the summary table.
    if not result["Latest Status"]:
        summary_events = []
        i = 0
        while i < len(lines):
            line = lines[i]
            # Each container row has: booking_ref, container, ..., event, date, time, city, date2, time2
            if (line == bl_stripped and i + 1 < len(lines)
                    and _CN_RE.match(lines[i + 1])):
                # Find the event and date within the next ~15 lines
                for k in range(i + 2, min(i + 15, len(lines))):
                    if (_DATE_LINE_RE.match(lines[k])
                            and k + 1 < len(lines)
                            and _TIME_LINE_RE.match(lines[k + 1])):
                        event = lines[k - 1]
                        if (event and len(event) > 3
                                and not _CN_RE.match(event)
                                and not _LOC_RE.match(event)):
                            loc = result.get("POD", "")
                            summary_events.append(
                                f"{event}  {loc}  {lines[k]} {lines[k+1]}"
                            )
                        break
            i += 1

        if summary_events:
            result["Latest Status"] = "\n".join(dict.fromkeys(summary_events))

    return result


def _iso_to_dt(iso: str) -> str:
    """'2026-06-13T02:46:00.000Z' → '2026-06-13 02:46' (local port time)."""
    return iso.replace("T", " ")[:16] if iso else ""


def _parse_search(items: list) -> dict:
    """Parse the track-and-trace/search response (list of per-container items)."""
    result = dict(_EMPTY)
    if not items:
        return result

    first = items[0]

    por = first.get("por") or {}
    pod = first.get("pod") or {}
    result["POL"] = ", ".join(x for x in (por.get("locationName"), por.get("countryName")) if x)
    result["POD"] = ", ".join(x for x in (pod.get("locationName"), pod.get("countryName")) if x)

    nums = [it.get("containerNo", "") for it in items if it.get("containerNo")]
    result["Container No"] = " | ".join(dict.fromkeys(nums))

    vv = first.get("vesselVoyage") or {}
    result["Vessel"] = (vv.get("vesselName") or "").strip()
    voyage = (vv.get("voyageNo") or "").strip()

    latest = first.get("latestEvent") or {}
    latest_name = (latest.get("eventName") or "").strip()
    latest_dt   = _iso_to_dt(latest.get("date") or "")

    pod_loc = (pod.get("locationName") or "").upper()

    rows = []
    ata_actual, ata_estimated = "", ""
    for ev in first.get("cargoEvents") or []:
        mid  = ev.get("matrixId") or ""
        loc  = (ev.get("locationName") or "").upper()
        dt   = _iso_to_dt(ev.get("localPortDate") or ev.get("date") or "")
        act  = (ev.get("trigger") or "").upper() == "ACTUAL"
        name = _MATRIX_EVENTS.get(mid) or (
            latest_name if latest_name and dt == latest_dt else f"Event {mid}"
        )
        if not dt:
            continue

        if act:
            row = f"{name}  {loc}  {dt}"
            if result["Vessel"] and ("Vessel" in name or "Loaded" in name or "Unloaded" in name):
                row += f"  {result['Vessel']}  {voyage}"
            rows.append(row)
            if not result["ATD"] and mid in ("E040", "E061"):
                result["ATD"] = dt
            if loc == pod_loc and mid in ("E089", "E105"):
                ata_actual = ata_actual or dt
        elif loc == pod_loc and mid == "E089":
            ata_estimated = ata_estimated or dt

    result["ATA"] = ata_actual or ata_estimated

    if rows:
        hdr = "Status  Place of Activity  Date  Time  Transport  Voyage No."
        result["Latest Status"] = hdr + "\n" + "\n".join(rows)

    log.info("[ONE LINE] API parsed: POL=%s POD=%s containers=%d events=%d",
             result["POL"], result["POD"], len(nums), len(rows))
    return result


def _api_scrape(bl_stripped: str) -> dict:
    """Fast path: direct POST to ONE LINE's track-and-trace API (~1s, no Chrome)."""
    for search_type in ("BKG_NO", "BL_NO"):
        try:
            r = requests.post(
                _SEARCH_URL,
                json={"page": 1, "page_length": 50,
                      "filters": {"search_text": bl_stripped, "search_type": search_type},
                      "timestamp": int(time.time() * 1000)},
                headers=_API_HEADERS,
                timeout=12,
            )
            if r.status_code not in (200, 201):
                log.warning("[ONE LINE] search API HTTP %d", r.status_code)
                continue
            data = r.json()
            items = data.get("data") or []
            if items:
                return _parse_search(items)
        except Exception as e:
            log.warning("[ONE LINE] HTTP API failed (%s): %s", search_type, e)
    return {}


def _parse_api(items) -> dict:
    """Parse ONE LINE JSON API/intercepted response into standard tracking dict.

    ONE LINE uses multiple field-name conventions across their API versions:
      polNm / pol_nm / pol / porNm — port of loading
      podNm / pod_nm / pod        — port of discharge
      cntrNo / cntr_no / cntNo    — container number
      vslNm / vsl_nm              — vessel name
      evtNm / evt_nm / description— event description
      plcNm / plc_nm / location   — event location
      evtDt / evt_dt / eventDate  — event date
    """
    result = dict(_EMPTY)
    if not items:
        return result

    # Unwrap common envelope shapes
    if isinstance(items, dict):
        items = (items.get("list") or items.get("data") or
                 items.get("trackingList") or items.get("trkg") or [items])
    if not items:
        return result

    item = items[0] if isinstance(items, list) and items else items
    if not isinstance(item, dict):
        return result

    def _f(*keys):
        for k in keys:
            v = item.get(k) or item.get(k.replace("Nm","_nm")) or item.get(k.lower())
            if v:
                return str(v).strip()
        return ""

    result["POL"] = (_f("polNm","pol_nm","por","porNm","por_nm","pol") or "").upper()
    result["POD"] = (_f("podNm","pod_nm","pod","delPlcNm","del_plc_nm") or "").upper()
    result["POR"] = (_f("porNm","por_nm","por") or "").upper()
    result["FND"] = (_f("fndNm","fnd_nm","fnd","finalDestination") or "").upper()
    result["Container No"] = _f("cntrNo","cntr_no","cntNo","containerNo")
    result["Vessel"] = _f("vslNm","vsl_nm","vesselName","vessel")
    result["ATA"]   = _f("ata","arrivalDate","atd_pol")   # ATA at POD
    result["ATD"]   = _f("atd","departureDate","atd_pol") # ATD from POL

    events = (item.get("evtList") or item.get("eventList") or
              item.get("events") or item.get("trkgEvtList") or [])
    if events:
        rows = []
        for ev in (events if isinstance(events, list) else []):
            desc = (ev.get("evtNm") or ev.get("evt_nm") or ev.get("eventDesc") or
                    ev.get("description") or "")
            loc  = (ev.get("plcNm") or ev.get("plc_nm") or ev.get("location") or
                    ev.get("portNm") or "")
            dt   = (ev.get("evtDt") or ev.get("evt_dt") or ev.get("eventDate") or
                    ev.get("date") or "")
            vsl  = ev.get("vslNm") or ev.get("vsl_nm") or ""
            voy  = ev.get("voyNo") or ev.get("voy_no") or ""
            if desc and dt:
                row = f"{desc}  {loc}  {dt}"
                if vsl:
                    row += f"  {vsl}"
                    if voy:
                        row += f"  {voy}"
                rows.append(row)
        if rows:
            result["Latest Status"] = "\n".join(rows)

    log.info("[ONE LINE] API parsed: POL=%s POD=%s events=%d",
             result["POL"], result["POD"],
             len(events) if isinstance(events, list) else 0)
    return result


def _intercept_scrape(driver, bl_stripped: str) -> dict:
    """
    Middle-fast path: inject fetch/XHR interceptor, navigate directly to the
    tracking URL (BL pre-filled in query string) and capture the JSON response.
    No DOM clicking required — ~5-8s vs 25-35s for the DOM path.
    """
    try:
        driver.execute_cdp_cmd("Page.addScriptToEvaluateOnNewDocument",
                               {"source": _INTERCEPT_JS})
    except Exception as e:
        log.warning("[ONE LINE] CDP inject failed: %s", e)

    url = _TRACKING_URL.format(bl=bl_stripped)
    driver.get(url)
    dismiss_cookies_js(driver)

    deadline = time.time() + 20
    while time.time() < deadline:
        try:
            data = driver.execute_script("return window.__one_data;")
            if data:
                log.info("[ONE LINE] Intercepted API response")
                if isinstance(data, list):
                    return _parse_api(data)
                items = data.get("list") or data.get("data") or data.get("trackingList") or []
                if items:
                    return _parse_api(items)
                # Data present but no items → BL not found
                return dict(_EMPTY, **{"Latest Status": "BL not found on ONE LINE"})
        except Exception:
            pass
        time.sleep(0.3)

    log.warning("[ONE LINE] Intercept timed out, falling through to DOM path")
    return {}


# ─── main scrape function ───────────────────────────────────────────────────

def scrape(driver, bl: str) -> dict:
    result      = dict(_EMPTY)
    bl          = bl.strip().upper()
    bl_stripped = _strip_oney(bl)

    log.info("[ONE LINE] Scraping BL: %s (stripped: %s)", bl, bl_stripped)

    # ── Fastest path: direct HTTP to the track-and-trace API (no Chrome) ─────
    result = _api_scrape(bl_stripped)
    if result.get("POL") or result.get("POD") or result.get("Container No"):
        return result

    # No driver (HTTP-first mode): report the miss; api.py retries with Chrome
    if driver is None:
        log.info("[ONE LINE] HTTP miss for %s — need Chrome fallback", bl)
        return {}

    # ── Fast path: CDP fetch interceptor (no DOM clicking) ───────────────────
    # The ONE LINE React SPA fires an internal API call when the tracking URL
    # loads — we capture that response directly via the injected interceptor.
    result = _intercept_scrape(driver, bl_stripped)
    if result.get("POL") or result.get("POD") or result.get("Latest Status"):
        return result

    log.info("[ONE LINE] Falling back to full DOM path")
    result = dict(_EMPTY)

    # ── DOM path: navigate, wait, click ─────────────────────────────────────
    url = _TRACKING_URL.format(bl=bl_stripped)
    driver.get(url)
    dismiss_cookies_js(driver)

    # ── 2. Check for login redirect ───────────────────────────────────────────
    if _is_login_page(driver):
        log.warning("[ONE LINE] Redirected to login page")
        result["Latest Status"] = "ONE LINE requires login — please log in at ecomm.one-line.com first"
        return result

    # ── 3. Wait for summary table ─────────────────────────────────────────────
    loaded = False
    try:
        WebDriverWait(driver, WAIT).until(
            EC.presence_of_element_located((By.CSS_SELECTOR, _RESULT_CSS))
        )
        time.sleep(0.5)
        loaded = True
    except TimeoutException:
        pass

    body_text = driver.find_element(By.TAG_NAME, "body").text
    body_lo   = body_text.lower()

    if not loaded:
        if _CN_RE.search(body_text) or bl_stripped in body_text:
            loaded = True
        elif any(k in body_lo for k in ["not found", "no result", "no data"]):
            result["Latest Status"] = "BL not found on ONE LINE"
            return result
        else:
            result["Latest Status"] = "Timeout — ONE LINE tracking data did not load"
            return result

    if _is_login_page(driver):
        result["Latest Status"] = "ONE LINE requires login — please log in at ecomm.one-line.com first"
        return result

    # ── 4. Click first row → expand detail panel ─────────────────────────────
    row_clicked = _click_first_row(driver)
    if row_clicked:
        # Wait for the detail section (Place of Receipt) to appear
        try:
            WebDriverWait(driver, 20).until(
                EC.presence_of_element_located(
                    (By.XPATH, "//*[contains(text(),'Place of Receipt')]")
                )
            )
            time.sleep(1)
            log.info("[ONE LINE] Detail panel loaded")
        except TimeoutException:
            log.warning("[ONE LINE] Detail panel did not load — will parse summary only")
            time.sleep(3)

    # ── 5. Click "Actual Schedule" tab for full timeline ─────────────────────
    _click_actual_schedule_tab(driver)

    # ── 6. Read final body text ───────────────────────────────────────────────
    body_text = driver.find_element(By.TAG_NAME, "body").text
    # Encode to ASCII before logging to avoid cp1252 UnicodeEncodeError on Windows
    safe_snippet = body_text[:2000].encode("ascii", errors="replace").decode("ascii")
    log.info("[ONE LINE] Body snippet (first 2000 chars):\n%s", safe_snippet)

    # ── 7. Parse the page ─────────────────────────────────────────────────────
    parsed = _parse_body(body_text, bl_stripped)
    result.update(parsed)

    if not result["Latest Status"]:
        if result["POL"] or result["POD"] or result["Container No"]:
            result["Latest Status"] = "Data loaded — no event details parsed"
        else:
            result["Latest Status"] = "No tracking data found for this BL"

    log.info("[ONE LINE] Done. POL=%s POD=%s CN=%s Status=%.60s",
             result["POL"], result["POD"], result["Container No"],
             result["Latest Status"] or "")
    return result
