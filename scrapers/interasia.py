"""
scrapers/interasia.py

Interasia uses server-rendered HTML (Imperva-protected, no JSON API).
Flow: Selenium form POST → parse results table directly.
No detail-page detour — POL/POD/Vessel all available in the main table.
"""

import re
import time
import logging
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.common.exceptions import TimeoutException

log = logging.getLogger(__name__)

TRACKING_URL = "https://www.interasia.cc/Service/Form?servicetype=0"
WAIT = 20

_EMPTY = {
    "POL": "", "POD": "", "POR": "", "FND": "",
    "Container No": "", "Vessel": "", "ATA": "", "ATD": "",
    "Latest Status": "",
}


def _first_visible(driver, selector):
    try:
        for el in driver.find_elements(By.CSS_SELECTOR, selector):
            if el.is_displayed():
                return el
    except Exception:
        pass
    return None


def _find_results_table(driver):
    try:
        tables = driver.find_elements(By.TAG_NAME, "table")
    except Exception:
        return None
    for tbl in tables:
        try:
            headers = tbl.find_elements(By.CSS_SELECTOR, "thead th")
            h_texts = [h.text.strip().lower() for h in headers]
            if any("container" in h for h in h_texts) and any(
                "event" in h or "date" in h for h in h_texts
            ):
                return tbl
        except Exception:
            continue
    return None


def scrape(driver, bl: str) -> dict:
    result = dict(_EMPTY)
    bl = bl.strip()
    log.info("[INTERASIA] Scraping BL: %s", bl)

    driver.get(TRACKING_URL)

    # Activate Cargo Tracking tab if present
    try:
        el = driver.find_element(By.XPATH, "//*[contains(text(), 'Cargo Tracking')]")
        driver.execute_script("arguments[0].click();", el)
        time.sleep(0.3)
    except Exception:
        pass

    # Find BL input
    bl_input = None
    for selector in ["input[name='query']", "input[type='text']"]:
        try:
            WebDriverWait(driver, 10).until(
                lambda d, sel=selector: _first_visible(d, sel) is not None
            )
            bl_input = _first_visible(driver, selector)
            if bl_input:
                break
        except TimeoutException:
            continue

    if bl_input is None:
        result["Latest Status"] = "Error: BL input not found"
        return result

    bl_input.clear()
    bl_input.send_keys(bl)
    time.sleep(0.3)

    # Submit form
    submitted = False
    for selector in ["input[type='submit']", "button[type='submit']", ".footer-group button"]:
        btn = _first_visible(driver, selector)
        if btn:
            try:
                btn.click()
                submitted = True
                break
            except Exception:
                pass
    if not submitted:
        bl_input.send_keys(Keys.RETURN)

    # Wait for results table
    try:
        WebDriverWait(driver, WAIT).until(lambda d: _find_results_table(d) is not None)
        log.info("[INTERASIA] Results table found")
    except TimeoutException:
        result["Latest Status"] = "No result found"
        return result

    # Parse results table
    try:
        tbl = _find_results_table(driver)
        if not tbl:
            result["Latest Status"] = "No result found"
            return result

        headers = tbl.find_elements(By.CSS_SELECTOR, "thead th")
        h_texts = [h.text.strip().lower() for h in headers]
        log.info("[INTERASIA] Headers: %s", h_texts)

        def col(name):
            return next((i for i, h in enumerate(h_texts) if name in h), None)

        cn_idx     = col("container")
        date_idx   = col("event date") or col("date")
        port_idx   = col("port")
        desc_idx   = col("event description") or col("description")
        voy_idx    = col("voyage")
        vessel_idx = col("vessel name") or col("vessel")

        containers = []
        events     = []
        vessels    = []

        for tr in tbl.find_elements(By.CSS_SELECTOR, "tbody tr"):
            cells = tr.find_elements(By.TAG_NAME, "td")
            if not cells:
                continue

            def cell(idx, _cells=cells):
                if idx is None or idx >= len(_cells):
                    return ""
                return _cells[idx].text.strip()

            cn     = cell(cn_idx)
            date   = cell(date_idx)
            port   = cell(port_idx)
            desc   = cell(desc_idx)
            voyage = cell(voy_idx)
            vessel = cell(vessel_idx)

            # 2026/02/15 14:00:00 → 2026-02-15 14:00
            date_n = re.sub(r'(\d{4})/(\d{2})/(\d{2})', r'\1-\2-\3', date)[:16]

            # Validate container number
            cn = re.sub(r'\(Show.*?\)', '', cn).strip()
            cn = re.sub(r'\s+', '', cn)
            if not re.match(r'^[A-Z]{4}\d{7}$', cn):
                continue

            if cn not in containers:
                containers.append(cn)
            if vessel and vessel not in vessels:
                vessels.append(vessel)

            # Clean description (strip CJK, prefixes)
            desc_n = re.sub(r'[^\x00-\x7F]+', '', desc).strip()
            desc_n = re.sub(r'^[A-Z]{0,3}:', '', desc_n).strip()
            desc_n = re.sub(r'\s+', ' ', desc_n).strip()

            # Clean port (strip 5-letter code prefix like "INTUT ")
            port_n = re.sub(r'^[A-Z]{5}\s+', '', port).strip() or port

            if date_n and desc_n:
                line = f"{desc_n}  {port_n}  {date_n}"
                if vessel:
                    line += f"  {vessel}"
                if voyage:
                    line += f"  {voyage}"
                events.append({"dt": date_n, "desc": desc_n, "port": port_n, "line": line})

        # Sort chronologically
        events.sort(key=lambda e: e["dt"])

        # Derive POL/POD from event keywords
        for ev in events:
            dl = ev["desc"].lower()
            if not result["POL"] and any(k in dl for k in ("depart", "load", "sail")):
                result["POL"] = ev["port"]
                if not result["ATD"]:
                    result["ATD"] = ev["dt"]
            if any(k in dl for k in ("discharg", "arriv")):
                result["POD"] = ev["port"]
                result["ATA"] = ev["dt"]  # keep updating → last discharge = final POD

        # Fallbacks
        if not result["POL"] and events:
            result["POL"] = events[0]["port"]
        if not result["POD"] and events:
            result["POD"] = events[-1]["port"]
        if not result["ATD"] and events:
            result["ATD"] = events[0]["dt"]
        if not result["ATA"] and events:
            result["ATA"] = events[-1]["dt"]

        if containers:
            result["Container No"] = " | ".join(containers)
        if vessels:
            result["Vessel"] = vessels[0]

        if events:
            hdr = "Status  Place of Activity  Date  Time  Transport  Voyage No."
            result["Latest Status"] = hdr + "\n" + "\n".join(e["line"] for e in events)

        log.info("[INTERASIA] POL=%s POD=%s Containers=%d Events=%d",
                 result["POL"], result["POD"], len(containers), len(events))

    except Exception as e:
        log.error("[INTERASIA] Parse error: %s", e)
        result["Latest Status"] = f"Parse error: {e}"

    return result
