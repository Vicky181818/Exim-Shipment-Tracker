"""
scrapers/kmtc.py — HTTP API first, Selenium fallback

Fast path:  POST to eKMTC's internal tracking API. (~2-3s)
Fallback:   Selenium form fill on ekmtc.com. (~20-30s)
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

_TRACKING_URL = "https://www.ekmtc.com/index.html#/cargo-tracking"

# eKMTC internal API endpoints (Vue SPA calls these)
_API_URLS = [
    "https://www.ekmtc.com/api/cargo-tracking/bl",
    "https://apis.ekmtc.com/api/v1/cargo/tracking",
    "https://www.ekmtc.com/ekmtc-web/cargo/bl-tracking",
]

_API_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Accept": "application/json, text/plain, */*",
    "Content-Type": "application/json",
    "Origin": "https://www.ekmtc.com",
    "Referer": "https://www.ekmtc.com/index.html",
}

WAIT = 25


def _api_scrape(bl: str) -> dict:
    """Try each known eKMTC API endpoint."""
    for url in _API_URLS:
        try:
            r = requests.post(
                url,
                json={"blNo": bl, "blNumber": bl, "searchType": "BL"},
                headers=_API_HEADERS,
                timeout=10,
            )
            if r.status_code == 200:
                data = r.json()
                result = _parse_api(data)
                if result.get("POL") or result.get("POD") or result.get("Latest Status"):
                    log.info("[KMTC] HTTP API succeeded via %s", url)
                    return result
        except Exception as e:
            log.debug("[KMTC] %s failed: %s", url, e)
    return {}


def _parse_api(data) -> dict:
    result = dict(_EMPTY)
    item = data[0] if isinstance(data, list) and data else data
    if not isinstance(item, dict):
        return result
    result["POL"] = (item.get("pol") or item.get("polName") or item.get("portOfLoading") or "").upper()
    result["POD"] = (item.get("pod") or item.get("podName") or item.get("portOfDischarge") or "").upper()
    result["Vessel"]       = item.get("vessel") or item.get("vesselName") or ""
    result["Container No"] = item.get("cntrNo") or item.get("containerNo") or ""
    result["ATA"] = item.get("ata") or item.get("arrivalDate") or ""
    result["ATD"] = item.get("atd") or item.get("departureDate") or ""

    events = item.get("eventList") or item.get("events") or item.get("trackingEvents") or []
    if events:
        rows = []
        for ev in events:
            desc = ev.get("evtNm") or ev.get("description") or ev.get("eventName") or ""
            loc  = ev.get("location") or ev.get("port") or ev.get("portName") or ""
            dt   = ev.get("evtDt") or ev.get("date") or ev.get("eventDate") or ""
            if desc:
                rows.append(f"{desc}  {loc}  {dt}".strip())
        result["Latest Status"] = "\n".join(rows)
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


def _selenium_scrape(driver, bl: str) -> dict:
    result = dict(_EMPTY)
    driver.get(_TRACKING_URL)
    dismiss_cookies_js(driver)

    inp = None
    for css in ["input[placeholder*='B/L']", "input[placeholder*='Booking']",
                "input[placeholder*='Container']", "input[type='text']"]:
        inp = _wait_for_visible(driver, css, timeout=8)
        if inp:
            break

    if inp is None:
        result["Latest Status"] = "Error: KMTC input not found"
        return result

    _js_click(driver, inp)
    time.sleep(0.3)
    inp.clear()
    inp.send_keys(bl)
    time.sleep(0.5)

    submitted = False
    for css in ["button[id*='search']", "button[class*='search']",
                "button[class*='btn'][type='button']"]:
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

        tables = driver.find_elements(By.CSS_SELECTOR, "table")
        for table in tables:
            headers = [h.text.strip().lower() for h in table.find_elements(By.TAG_NAME, "th")]
            if not any(("location" in h or "event" in h or "status" in h or "date" in h or "port" in h)
                       for h in headers):
                continue
            loc_idx   = next((i for i, h in enumerate(headers) if "location" in h or "port" in h), None)
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
            if loc_idx is not None and loc_idx < len(cells) and not result["POD"]:
                result["POD"] = cells[loc_idx].text.strip()
            break
    except Exception:
        pass

    return result


def scrape(driver, bl: str) -> dict:
    bl = bl.strip()
    log.info("[KMTC] Scraping %s", bl)

    result = _api_scrape(bl)
    if result.get("POL") or result.get("POD") or result.get("Latest Status"):
        return result

    log.info("[KMTC] HTTP failed, falling back to Selenium")
    return _selenium_scrape(driver, bl)
