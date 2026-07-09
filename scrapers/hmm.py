"""
scrapers/hmm.py — HTTP-first, Selenium fallback

Fast path:  GET HMM homepage to collect Akamai cookies + CSRF token,
            then POST directly to selectTrackNTrace.do — no Chrome needed.
Fallback:   headless Chrome + in-page XHR injection (original approach).
"""

import re
import time
import logging
import requests
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.common.exceptions import TimeoutException

log = logging.getLogger(__name__)

_PAGE_URL = "https://www.hmm21.com/e-service/general/trackNTrace/TrackNTrace.do"
_XHR_URL  = "https://www.hmm21.com/e-service/general/trackNTrace/selectTrackNTrace.do"

_REQ_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Accept": "text/html, */*; q=0.01",
    "Accept-Language": "en-US,en;q=0.9",
    "Content-Type": "application/json; charset=UTF-8",
    "X-Requested-With": "XMLHttpRequest",
    "Origin": "https://www.hmm21.com",
    "Referer": _PAGE_URL,
}


# HMM's Akamai edge stalls plain python-requests (read timeout) but accepts
# curl_cffi Chrome impersonation. The tracking POST must go over HTTP/1.1 —
# on HTTP/2 the edge resets the stream mid-flight.
_cffi_session = None
_cffi_csrf = ""


def _get_session():
    global _cffi_session, _cffi_csrf
    if _cffi_session is None:
        from curl_cffi import requests as cffi_requests
        s = cffi_requests.Session(impersonate="chrome")
        r = s.get(_PAGE_URL, timeout=20)
        m = (re.search(r'<meta[^>]+name=["\']_csrf["\'][^>]+content=["\']([^"\']+)["\']', r.text)
             or re.search(r'<input[^>]+name=["\']_csrf["\'][^>]+value=["\']([^"\']+)["\']', r.text))
        _cffi_csrf = m.group(1) if m else ""
        _cffi_session = s
    return _cffi_session


def _http_scrape(bl: str) -> dict:
    """Direct HTTP via curl_cffi: page GET for cookies+CSRF, then tracking POST.

    HMM's backend occasionally stalls a request indefinitely (same BL succeeds
    seconds later), so keep the timeout short and retry once on a fresh
    session before letting the caller fall back to Chrome.
    """
    global _cffi_session
    from curl_cffi.const import CurlHttpVersion

    for attempt in range(2):
        try:
            session = _get_session()

            hdrs = dict(_REQ_HEADERS)
            hdrs.pop("User-Agent", None)   # let impersonation supply a matching UA
            if _cffi_csrf:
                hdrs["x-csrf-token"] = _cffi_csrf

            r2 = session.post(
                _XHR_URL,
                json={"type": "bl", "listBl": [bl], "listCntr": [], "listBkg": [], "listPo": []},
                headers=hdrs,
                timeout=12,
                http_version=CurlHttpVersion.V1_1,
            )

            if r2.status_code == 200 and ("shipmentProgress" in r2.text or len(r2.text) > 500):
                log.info("[HMM] HTTP fast path succeeded (%d bytes)", len(r2.text))
                return _parse(r2.text)
            log.warning("[HMM] HTTP status %d (attempt %d)", r2.status_code, attempt + 1)
        except Exception as e:
            log.warning("[HMM] HTTP attempt %d failed: %s", attempt + 1, str(e)[:80])
        _cffi_session = None   # retry on a fresh connection
    return {}

TRACKING_URL = _PAGE_URL

# Hide headless Chrome fingerprints before any page JS runs
_STEALTH_JS = """
Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
Object.defineProperty(navigator, 'plugins',   {get: () => [1,2,3,4,5]});
Object.defineProperty(navigator, 'languages', {get: () => ['en-US','en']});
window.chrome = {runtime: {}};
"""

# Fire the tracking XHR directly from the browser — keeps all Akamai cookies
# and the session CSRF token without any form interaction.
_FIRE_XHR_JS = """
window.__hmm_data = null;
(function(bl) {
    var csrf = '';
    ['_csrf','csrf-token','csrf_token','_csrf_token'].forEach(function(n) {
        if (csrf) return;
        var el = document.querySelector('meta[name="' + n + '"]');
        if (el) csrf = el.getAttribute('content') || '';
    });
    if (!csrf) {
        var inp = document.querySelector('input[name="_csrf"]');
        if (inp) csrf = inp.value || '';
    }
    var xhr = new XMLHttpRequest();
    xhr.open('POST',
        '/e-service/general/trackNTrace/selectTrackNTrace.do', true);
    xhr.setRequestHeader('Content-Type', 'application/json; charset=UTF-8');
    xhr.setRequestHeader('Accept', 'text/html, */*; q=0.01');
    xhr.setRequestHeader('X-Requested-With', 'XMLHttpRequest');
    if (csrf) xhr.setRequestHeader('x-csrf-token', csrf);
    xhr.onload    = function() { window.__hmm_data = xhr.responseText || '__EMPTY__'; };
    xhr.onerror   = function() { window.__hmm_data = '__XHR_ERROR__'; };
    xhr.ontimeout = function() { window.__hmm_data = '__XHR_TIMEOUT__'; };
    xhr.send(JSON.stringify({
        type:'bl', listBl:[bl], listCntr:[], listBkg:[], listPo:[]
    }));
})(arguments[0]);
"""

_EMPTY = {
    "POR": "", "POL": "", "POD": "", "FND": "",
    "Container No": "", "Vessel": "", "ATD": "", "ATA": "",
    "Latest Status": "",
}


def _cell_text(td_html: str) -> str:
    m = re.search(r'<div[^>]*>(.*?)</div>', td_html, re.DOTALL)
    raw = m.group(1) if m else td_html
    return re.sub(r'<[^>]+>', '', raw).strip()


def _parse(html: str) -> dict:
    result = dict(_EMPTY)

    # ── Container numbers ─────────────────────────────────────────────────────
    cnums = list(dict.fromkeys(re.findall(r'\b([A-Z]{4}\d{7})\b', html)))
    if cnums:
        result["Container No"] = " | ".join(cnums)

    # ── Shipment Progress (Date | Time | Location | Status | Mode) ───────────
    events = []
    prog_m = re.search(r'id="shipmentProgress"(.*?)</table>', html, re.DOTALL)
    if prog_m:
        tbody_m = re.search(r'<tbody>(.*?)</tbody>', prog_m.group(1), re.DOTALL)
        if tbody_m:
            for row_m in re.finditer(r'<tr>(.*?)</tr>', tbody_m.group(1), re.DOTALL):
                tds = re.findall(r'<td[^>]*>(.*?)</td>', row_m.group(1), re.DOTALL)
                if len(tds) < 4:
                    continue
                date = _cell_text(tds[0])
                tme  = _cell_text(tds[1])
                loc  = _cell_text(tds[2]).upper()
                desc = _cell_text(tds[3])
                mode = _cell_text(tds[4]) if len(tds) > 4 else ""

                if not re.match(r'\d{4}-\d{2}-\d{2}', date):
                    continue

                dt   = f"{date} {tme}"
                line = f"{desc}  {loc}  {dt}"
                if mode and mode.upper() not in ("TRUCK", ""):
                    line += f"  {mode}"

                events.append({"dt": dt, "desc": desc, "loc": loc,
                               "mode": mode, "line": line})

    # HMM table is newest-first → reverse for chronological order
    events.reverse()

    if events:
        hdr = "Status  Place of Activity  Date  Time  Transport  Voyage No."
        result["Latest Status"] = hdr + "\n" + "\n".join(e["line"] for e in events)

        # Vessel = first non-Truck transport
        for ev in events:
            m = (ev["mode"] or "").strip()
            if m and m.upper() not in ("TRUCK", ""):
                result["Vessel"] = m
                break

        # ATD + POL — "Departure from POL"
        for ev in events:
            if "departure from pol" in ev["desc"].lower():
                result["ATD"] = ev["dt"]
                result["POL"] = ev["loc"]
                break

        # ATA + POD — keep updating on every "Discharging Port" event so that
        # transshipment cases end up at the final destination, not a mid-voyage port.
        for ev in events:
            if "discharging port" in ev["desc"].lower():
                result["POD"] = ev["loc"]
                result["ATA"] = ev["dt"]

    # ── Route table: POL, POD, and ETA at Discharging Port ──────────────────
    route_m = re.search(
        r'<thead>.*?Loading Port.*?</thead>(.*?)</table>', html, re.DOTALL)
    if route_m:
        route_body = route_m.group(1)

        # Location row → POL (index 1) and POD (index -2)
        loc_row = re.search(
            r'<th[^>]*>.*?Location.*?</th>(.*?)</tr>', route_body, re.DOTALL)
        if loc_row:
            locs = [_cell_text(t) for t in
                    re.findall(r'<td[^>]*>(.*?)</td>',
                               loc_row.group(1), re.DOTALL)]
            if len(locs) >= 2 and not result["POL"]:
                result["POL"] = locs[1]
            if len(locs) >= 2 and not result["POD"]:
                result["POD"] = locs[-2]

        # Arrival(ETB) row → ETA at Discharging Port.
        # Actual arrivals (past ports) show real dates; future ports show ETA.
        # Only fills ATA if the events loop didn't already find an actual arrival.
        if not result["ATA"]:
            arr_row = re.search(
                r'<th[^>]*>.*?Arrival.*?</th>(.*?)</tr>', route_body, re.DOTALL)
            if arr_row:
                dates = [_cell_text(t) for t in
                         re.findall(r'<td[^>]*>(.*?)</td>',
                                    arr_row.group(1), re.DOTALL)]
                if len(dates) >= 2:
                    pod_eta = dates[-2].strip()[:16]  # trim seconds
                    if re.match(r'\d{4}-\d{2}-\d{2}', pod_eta):
                        result["ATA"] = pod_eta

    log.info("[HMM] POL=%s POD=%s ATA=%s Containers=%s Events=%d",
             result["POL"], result["POD"], result["ATA"],
             result["Container No"], len(events))
    return result


def scrape(driver, bl: str) -> dict:
    bl = bl.strip().upper()
    log.info("[HMM] Scraping %s", bl)

    result = _http_scrape(bl)
    if result.get("POL") or result.get("POD") or result.get("Latest Status"):
        return result

    # No driver (HTTP-first mode): report the miss; api.py retries with Chrome
    if driver is None:
        log.info("[HMM] HTTP miss for %s — need Chrome fallback", bl)
        return {}

    log.info("[HMM] Falling back to Selenium")

    # Inject stealth JS before the HMM page loads — Akamai bot detection checks
    # navigator.webdriver, plugins, and languages before firing the challenge.
    try:
        driver.execute_cdp_cmd("Page.addScriptToEvaluateOnNewDocument",
                               {"source": _STEALTH_JS})
    except Exception as e:
        log.warning("[HMM] Stealth inject failed: %s", e)

    driver.get(TRACKING_URL)

    # Wait for the actual HMM page (BL input field) — not an Akamai challenge page.
    # This confirms Akamai passed before we fire the XHR.
    inp_css = "input[name='blNo'], input[id*='blNo'], input[placeholder*='B/L']"
    try:
        WebDriverWait(driver, 25).until(
            EC.presence_of_element_located((By.CSS_SELECTOR, inp_css)))
        log.info("[HMM] Page loaded, firing XHR")
    except TimeoutException:
        log.warning("[HMM] Page did not load (Akamai challenge?)")
        return dict(_EMPTY, **{"Latest Status": "Error: HMM page blocked"})

    # Fire the tracking XHR from the page's own JS context
    try:
        driver.execute_script(_FIRE_XHR_JS, bl)
    except Exception as e:
        log.warning("[HMM] Fire XHR failed: %s", e)
        return dict(_EMPTY, **{"Latest Status": "Error: could not fire HMM request"})

    # Poll for the response
    deadline = time.time() + 25
    while time.time() < deadline:
        try:
            html = driver.execute_script("return window.__hmm_data;")
            if html:
                if html.startswith("__"):
                    log.warning("[HMM] XHR sentinel: %s", html)
                    return dict(_EMPTY, **{"Latest Status": f"Error: HMM XHR failed ({html})"})
                if "shipmentProgress" in html or len(html) > 500:
                    log.info("[HMM] Got response (%d bytes)", len(html))
                    return _parse(html)
        except Exception:
            pass
        time.sleep(0.3)

    return dict(_EMPTY, **{"Latest Status": "Error: HMM tracking data not received"})
