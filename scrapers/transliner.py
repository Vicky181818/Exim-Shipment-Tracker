"""
scrapers/transliner.py — Transliner (Transliner Pte Ltd)

Transliner's tracking is powered by Tigris Systems and exposes a clean public
JSON API — no login, no browser, no bot protection:

    GET https://translinergroup.track.tigris.systems/api/bookings/{ref}?include_emails=true

The reference is the full BL/booking number (e.g. TRLSINTUT6515489). Response:
    route[]      — ports in order; first = POL, last = POD  ({port, port_name})
    vessels[]    — {name, voyage, etd, atd, eta, ata}
    milestones[] — {type, location:{port}, event_date, actual_*_date, ...}

scrape(driver, bl) keeps the standard signature; the driver is ignored.
"""

import logging
import requests

log = logging.getLogger(__name__)

_API_URL = ("https://translinergroup.track.tigris.systems/api/bookings/"
            "{ref}?include_emails=true")

_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
    "Accept": "application/json, text/plain, */*",
    "Referer": "https://translinergroup.track.tigris.systems/",
}

_EMPTY = {
    "POL": "", "POD": "", "POR": "", "FND": "",
    "Container No": "", "Vessel": "", "ATD": "", "ATA": "",
    "Latest Status": "",
}

# Tigris milestone codes → human labels the status classifier understands.
# GATE_IN_DEPOT = the empty container was returned to the depot → Delivered
# (the label carries "Empty Return" so sync_excel._determine_status maps it).
_MILESTONE_LABELS = {
    "BOOKING_CONFIRMED":  "Booking Confirmed",
    "GATE_OUT_DEPOT":     "Empty Pickup",
    "GATE_IN_TERMINAL":   "Gate In",
    "LOADED":             "Loaded on Vessel",
    "VESSEL_DEPARTURE":   "Vessel Departed",
    "IN_TRANSIT":         "In Transit",
    "TRANSHIPMENT":       "Transshipment",
    "VESSEL_ARRIVAL":     "Vessel Arrived",
    "DISCHARGED":         "Discharged",
    "GATE_OUT_TERMINAL":  "Gate Out",
    "GATE_IN_DEPOT":      "Empty Return to Depot",
    "DELIVERED":          "Delivered",
}


def _dt(iso: str) -> str:
    """'2026-06-25T00:00:00Z' → '2026-06-25 00:00'."""
    return iso.replace("T", " ")[:16] if iso else ""


def _parse(data: dict) -> dict:
    result = dict(_EMPTY)

    route = data.get("route") or []
    if route:
        result["POL"] = (route[0].get("port_name") or route[0].get("port") or "").strip()
        result["POD"] = (route[-1].get("port_name") or route[-1].get("port") or "").strip()
    # port code → readable name, for the event rows
    names = {r.get("port"): (r.get("port_name") or r.get("port"))
             for r in route if r.get("port")}

    vessels = data.get("vessels") or []
    voyage = ""
    if vessels:
        v = vessels[0]
        result["Vessel"] = (v.get("name") or "").strip()
        voyage = (v.get("voyage") or "").strip()
        result["ATD"] = _dt(v.get("atd") or "")
        result["ATA"] = _dt(v.get("ata") or v.get("eta") or "")

    rows = ["Status  Place of Activity  Date  Time  Transport  Voyage No."]
    for m in data.get("milestones") or []:
        code = m.get("type", "")
        label = _MILESTONE_LABELS.get(code, code.replace("_", " ").title())
        port = (m.get("location") or {}).get("port", "")
        place = (names.get(port) or port or "").upper()
        dt = _dt(m.get("event_date") or m.get("actual_arrival_date")
                 or m.get("actual_departure_date") or "")
        if not dt:
            continue
        row = f"{label}  {place}  {dt}"
        if result["Vessel"] and code in ("LOADED", "VESSEL_DEPARTURE",
                                         "VESSEL_ARRIVAL", "DISCHARGED"):
            row += f"  {result['Vessel']}  {voyage}"
        rows.append(row)

    if len(rows) > 1:
        result["Latest Status"] = "\n".join(rows)

    # ATA fallback: actual arrival of the last milestone that has one
    if not result["ATA"]:
        for m in reversed(data.get("milestones") or []):
            if m.get("actual_arrival_date"):
                result["ATA"] = _dt(m["actual_arrival_date"])
                break

    return result


def scrape(driver, bl: str) -> dict:
    """HTTP-only; the driver argument is accepted but never used."""
    bl = bl.strip()
    try:
        r = requests.get(_API_URL.format(ref=bl), headers=_HEADERS, timeout=15)
        if r.status_code == 200 and r.text.strip()[:1] in "{[":
            data = r.json()
            if data.get("route") or data.get("milestones"):
                log.info("[TRANSLINER] API success for %s", bl)
                return _parse(data)
            return dict(_EMPTY, **{"Latest Status": "Not found on Transliner"})
        log.warning("[TRANSLINER] API HTTP %d for %s", r.status_code, bl)
    except Exception as e:
        log.warning("[TRANSLINER] API error for %s: %s", bl, e)
    return dict(_EMPTY, **{"Latest Status": "Error: Transliner tracking not available"})
