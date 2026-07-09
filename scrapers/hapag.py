"""
scrapers/hapag.py — Direct HTTP API

Hapag-Lloyd's internal API at tracking.api.hlag.cloud accepts x-token: public
— no Cloudflare dance, no Selenium needed.

Response: {groups: [{containerNumber, events: [{eventDescription, eventLocation,
           eventDate, eventTime, eventTransport, eventVoyageNo,
           eventClassifierCode}]}]}
"""

import logging
import requests

log = logging.getLogger(__name__)

_API_URL = "https://tracking.api.hlag.cloud/api/tracking/events?reference={ref}"

_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Accept": "application/json, text/plain, */*",
    "x-token": "public",
    "Origin": "https://www.hapag-lloyd.com",
    "Referer": "https://www.hapag-lloyd.com/",
}

_EMPTY = {
    "POL": "", "POD": "", "POR": "", "FND": "",
    "Container No": "", "Vessel": "", "ATA": "", "ATD": "",
    "Latest Status": "",
}


def _fmt_time(t: str) -> str:
    """HH:MM:SS or HH:MM → HH:MM"""
    parts = (t or "").split(":")
    if len(parts) >= 2:
        try:
            return f"{int(parts[0]):02d}:{parts[1]:0>2}"
        except ValueError:
            pass
    return "00:00"


def _parse(data: dict) -> dict:
    result = dict(_EMPTY)
    groups = data.get("groups") or []
    if not groups:
        result["Latest Status"] = "No tracking data found"
        return result

    cnums = [g["containerNumber"] for g in groups if g.get("containerNumber")]
    if cnums:
        result["Container No"] = " | ".join(cnums)

    actual_events  = []
    planned_events = []

    for ev in (groups[0].get("events") or []):
        date = ev.get("eventDate", "")
        tme  = _fmt_time(ev.get("eventTime", ""))
        entry = {
            "dt":        f"{date} {tme}",
            "desc":      ev.get("eventDescription", ""),
            "loc":       (ev.get("eventLocation") or "").upper(),
            "transport": ev.get("eventTransport", ""),
            "voyage":    ev.get("eventVoyageNo", ""),
        }
        if ev.get("eventClassifierCode") == "Actual":
            actual_events.append(entry)
        else:
            planned_events.append(entry)

    actual_events.sort(key=lambda e: e["dt"])
    planned_events.sort(key=lambda e: e["dt"])

    # ── Status timeline: Actual events only ──────────────────────────────────
    rows   = ["Status  Place of Activity  Date  Time  Transport  Voyage No."]
    vessel = ""

    for ev in actual_events:
        desc      = ev["desc"]
        loc       = ev["loc"]
        dt        = ev["dt"]
        transport = ev["transport"]
        voyage    = ev["voyage"]

        is_vessel = transport and transport.upper() not in ("TRUCK", "RAIL", "BARGE", "")
        if is_vessel and not vessel:
            vessel = transport

        line = f"{desc}  {loc}  {dt}"
        if transport:
            line += f"  {transport}"
        if voyage:
            line += f"  {voyage}"
        rows.append(line)

        desc_l = desc.lower()
        if not result["ATD"] and "loaded" in desc_l:
            result["ATD"] = dt
        if "discharged" in desc_l:
            result["ATA"] = dt  # keep updating — last actual discharge = ATA

    # ── POL: first Actual "Loaded" location ──────────────────────────────────
    for ev in actual_events:
        if "loaded" in ev["desc"].lower() and not result["POL"]:
            result["POL"] = ev["loc"]

    # ── POD: need to distinguish direct delivery vs transshipment ────────────
    # If Planned events show a "Loaded" leg AFTER the last Actual discharge,
    # the container is sitting at a transshipment port — the real final POD
    # is the last Planned "Discharged" location, and ATA should be that ETA.
    #
    # If no Planned "Loaded" exists after the Actual discharge, the Actual
    # discharge IS at the final POD and ATA is real.

    last_actual_disch_dt = ""
    for ev in actual_events:
        if "discharged" in ev["desc"].lower():
            result["POD"] = ev["loc"]
            result["ATA"] = ev["dt"]
            last_actual_disch_dt = ev["dt"]

    # Is there a Planned "Loaded" AFTER the last Actual discharge?
    # (signals transshipment — container is not at final POD yet)
    is_transshipping = False
    if last_actual_disch_dt:
        for ev in planned_events:
            if "loaded" in ev["desc"].lower() and ev["dt"] >= last_actual_disch_dt:
                is_transshipping = True
                break

    # In-transit (no actual discharge yet) OR at transshipment port:
    # use the last Planned "Discharged" as final POD + ETA
    if not result["POD"] or is_transshipping:
        for ev in reversed(planned_events):
            if "discharged" in ev["desc"].lower():
                result["POD"] = ev["loc"]
                if ev["dt"].strip() > "2000":
                    result["ATA"] = ev["dt"]  # future date → sync_excel prefixes "ETA:"
                break

    result["Latest Status"] = "\n".join(rows)
    result["Vessel"] = vessel
    return result


def scrape(driver, bl: str) -> dict:
    """Pure HTTP — driver argument is unused."""
    bl = bl.strip()
    log.info("[HAPAG] Scraping %s via direct API", bl)
    try:
        r = requests.get(
            _API_URL.format(ref=bl),
            headers=_HEADERS,
            timeout=15,
        )
        if r.status_code == 200:
            data = r.json()
            if data.get("groups"):
                log.info("[HAPAG] API success")
                return _parse(data)
        log.warning("[HAPAG] API %d: %s", r.status_code, r.text[:200])
    except Exception as e:
        log.warning("[HAPAG] API error: %s", e)

    return dict(_EMPTY, **{"Latest Status": "Error: Hapag-Lloyd tracking data not available"})
