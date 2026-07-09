"""
scrapers/oocl.py — HTTP API first, Selenium fallback

Fast path:  POST to OOCL's MOC (Merchant Online Center) tracking endpoint.
            Returns JSON directly — no Chrome needed. (~2-3s)
Fallback:   Selenium form fill on the public tracking page. (~20-30s)
"""
import re
import time
import logging
import requests
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.common.exceptions import TimeoutException
from scrapers._common import dismiss_cookies_js

log = logging.getLogger(__name__)

_EMPTY = {
    "POL": "", "POD": "", "Container No": "", "Vessel": "",
    "ATA": "", "ATD": "", "FND": "", "Latest Status": "",
}

# OOCL's Merchant Online Center public tracking API
_API_URL = "https://moc.oocl.com/party/cargo/oocl/cargoTrackingList.do"
_API_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Accept": "application/json, text/plain, */*",
    "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
    "Referer": "https://www.oocl.com/",
    "Origin": "https://www.oocl.com",
}

TRACKING_URL = "https://www.oocl.com/eng/ourservices/eservices/cargotracking/Pages/cargotracking.aspx"
WAIT = 25


def _api_scrape(bl: str) -> dict:
    """Direct HTTP to OOCL MOC API."""
    try:
        r = requests.post(
            _API_URL,
            data=f"blno={bl}&cntrno=&podcd=&oocl_moc_service_action=doTrack&type=bl",
            headers=_API_HEADERS,
            timeout=12,
        )
        if r.status_code == 200:
            data = r.json()
            items = data if isinstance(data, list) else (data.get("list") or data.get("data") or [])
            if items:
                return _parse_api(items[0])
            # Try alternate response shape
            if isinstance(data, dict) and (data.get("blNo") or data.get("blno")):
                return _parse_api(data)
    except Exception as e:
        log.warning("[OOCL] HTTP API failed: %s", e)
    return {}


def _parse_api(item: dict) -> dict:
    result = dict(_EMPTY)
    result["POL"] = (item.get("pol") or item.get("portOfLoading") or "").upper()
    result["POD"] = (item.get("pod") or item.get("portOfDischarge") or item.get("finalDestination") or "").upper()
    result["Vessel"]       = item.get("vessel") or item.get("vesselName") or ""
    result["Container No"] = item.get("cntrNo") or item.get("containerNo") or ""
    result["ATA"] = item.get("ata") or item.get("actualArrival") or ""
    result["ATD"] = item.get("atd") or item.get("actualDeparture") or ""

    events = item.get("eventList") or item.get("events") or item.get("movements") or []
    if events:
        rows = []
        for ev in events:
            desc = ev.get("evtNm") or ev.get("description") or ev.get("status") or ""
            loc  = ev.get("location") or ev.get("port") or ""
            dt   = ev.get("evtDt") or ev.get("date") or ev.get("eventDate") or ""
            if desc:
                rows.append(f"{desc}  {loc}  {dt}".strip())
        result["Latest Status"] = "\n".join(rows)
    log.info("[OOCL] HTTP API: POL=%s POD=%s events=%d", result["POL"], result["POD"], len(events))
    return result


# ── Selenium fallback ─────────────────────────────────────────────────────────

def _wait_for_visible(driver, css, timeout=WAIT):
    try:
        return WebDriverWait(driver, timeout).until(
            EC.visibility_of_element_located((By.CSS_SELECTOR, css)))
    except TimeoutException:
        return None


def _js_click(driver, element):
    driver.execute_script("arguments[0].scrollIntoView(true); arguments[0].click();", element)


def _label_value(body_lines, *labels):
    for i, line in enumerate(body_lines):
        if line.strip() in labels:
            for j in range(i + 1, min(i + 4, len(body_lines))):
                val = body_lines[j].strip()
                if val and val not in labels:
                    return val
    return ""


def _selenium_scrape(driver, bl: str) -> dict:
    result = dict(_EMPTY)
    driver.get(TRACKING_URL)
    dismiss_cookies_js(driver)

    inp = None
    for css in ["input[id*='SEARCH_NUMBER']", "input[placeholder*='B/L']",
                "input[placeholder*='Booking']", "input[type='text']"]:
        inp = _wait_for_visible(driver, css, timeout=8)
        if inp:
            break

    if inp is None:
        result["Latest Status"] = "Error: OOCL input not found"
        return result

    _js_click(driver, inp)
    time.sleep(0.3)
    inp.clear()
    inp.send_keys(bl)
    time.sleep(0.5)

    submitted = False
    for css in ["button[id*='search']", "button[class*='search']",
                "input[type='submit']", "button[type='submit']"]:
        try:
            btn = driver.find_element(By.CSS_SELECTOR, css)
            if btn.is_displayed():
                _js_click(driver, btn)
                submitted = True
                break
        except Exception:
            continue
    if not submitted:
        inp.send_keys(Keys.RETURN)

    try:
        WebDriverWait(driver, WAIT).until(
            EC.presence_of_element_located((By.CSS_SELECTOR, "table tbody tr")))
        time.sleep(0.5)
    except TimeoutException:
        body = driver.find_element(By.TAG_NAME, "body").text
        result["Latest Status"] = ("No tracking data found"
                                    if "no result" in body.lower() or "not found" in body.lower()
                                    else "Timeout - tracking data did not load")
        return result

    try:
        body_text = driver.find_element(By.TAG_NAME, "body").text
        m = re.search(r"\b([A-Z]{4}\d{7})\b", body_text)
        if m:
            result["Container No"] = m.group(1)
        body_lines = body_text.split("\n")
        result["POL"] = _label_value(body_lines, "Origin", "Port of Load", "POL")
        result["POD"] = _label_value(body_lines, "Destination", "Port of Discharge", "POD")
    except Exception:
        pass

    try:
        tables = driver.find_elements(By.CSS_SELECTOR, "table")
        for table in tables:
            headers = [h.text.strip().lower() for h in table.find_elements(By.TAG_NAME, "th")]
            if not any(("location" in h or "event" in h or "status" in h or "date" in h) for h in headers):
                continue
            loc_idx   = next((i for i, h in enumerate(headers) if "location" in h), None)
            date_idx  = next((i for i, h in enumerate(headers) if "date" in h), None)
            event_idx = next((i for i, h in enumerate(headers) if "event" in h or "status" in h), None)
            rows = table.find_elements(By.CSS_SELECTOR, "tbody tr")
            if not rows:
                continue
            cells = rows[0].find_elements(By.TAG_NAME, "td")
            if event_idx is not None and event_idx < len(cells):
                result["Latest Status"] = cells[event_idx].text.strip()
            if date_idx is not None and date_idx < len(cells):
                result["ATA"] = cells[date_idx].text.strip()
            break
    except Exception:
        pass

    return result


def scrape(driver, bl: str) -> dict:
    bl = bl.strip()
    log.info("[OOCL] Scraping %s", bl)

    result = _api_scrape(bl)
    if result.get("POL") or result.get("POD") or result.get("Latest Status"):
        return result

    log.info("[OOCL] HTTP failed, falling back to Selenium")
    return _selenium_scrape(driver, bl)
