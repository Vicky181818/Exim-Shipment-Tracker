"""
sheet_sync.py — Auto-fill the team's shipment-tracking Google Sheet.

Replaces the manual "open each carrier link, copy the ETA into the sheet"
workflow. While the FastAPI server runs, a background job polls the shared
Google Sheet: for every row that has a BL No, it scrapes the live carrier
status and writes POD / ETA / Status back into the sheet.

Team workflow (they only need the Google Sheet, never the server):
  1. Owner shares the sheet with teammates (Editor) — that's the access list.
  2. A teammate types a BL No in a new row (Shipping Line optional — it's
     auto-detected from the BL prefix when left blank).
  3. Within a couple of minutes the server fills Shipping Line / POD /
     ETA / Status / Last Updated for that row.
  4. Typing "sync" into a row's Status cell forces it to re-scrape.

Columns (matched by header text, so they can be reordered freely):
  BL No | Shipping Line | POD | ETA / Arrival | Status | Last Updated

Load management: a single poll cycle scrapes at most MAX_PER_CYCLE rows,
oldest-first, so a large backlog fills in gradually instead of hammering
carriers all at once (which gets the IP rate-limited/banned).
"""

import logging
import os
import threading
import time
from datetime import datetime

import gspread

from main import get_scraper
from sync_excel import _clean_date, _determine_status, _detect_carrier, _fmt_date

log = logging.getLogger("sheet_sync")

GOOGLE_SHEET_ID = os.environ.get(
    "GOOGLE_SHEET_ID", "1eWPK27r1lOzpdBOCjOkOIADsG92Eb7qRggJ8qHiyit0"
)
CREDENTIALS_PATH = os.environ.get(
    "GOOGLE_SHEETS_CREDENTIALS_FILE", "credentials/service_account.json"
)

POLL_SECONDS  = int(os.environ.get("SHEET_POLL_SECONDS", "120"))
REFRESH_HOURS = int(os.environ.get("SHEET_REFRESH_HOURS", "24"))
# Rows scraped per poll cycle — keeps carrier request volume low so the IP
# isn't rate-limited. ~25/cycle × 2-min cycles ≈ a few hundred rows in a few
# hours, then it just maintains freshness.
MAX_PER_CYCLE = int(os.environ.get("SHEET_MAX_PER_CYCLE", "25"))

# ── Columns (matched by header text, case-insensitive) ───────────────────────
COL_BL      = "BL No"
COL_CARRIER = "Shipping Line"
COL_POD     = "POD"
COL_ETA     = "ETA / Arrival"
COL_STATUS  = "Status"
COL_UPDATED = "Last Updated"

_ALL_COLS = [COL_BL, COL_CARRIER, COL_POD, COL_ETA, COL_STATUS, COL_UPDATED]
_CARRIER_ALIASES = (COL_CARRIER, "Carrier", "Line")
_BL_ALIASES = (COL_BL, "bl", "b/l no", "bl number")

# Shown in the Status cell whenever a scrape can't return usable tracking
# data — a failed/timed-out scrape, an empty response, or a carrier-side
# "not found". Keeps the sheet's failure wording consistent and clear.
NO_DATA = "No data found"


def _has_data(pod: str, ata: str) -> bool:
    """True only when the scrape returned something usable (a port or a date)."""
    return bool((pod or "").strip() or (ata or "").strip())

_HEADER_FORMAT = {
    "backgroundColor": {"red": 0.12, "green": 0.22, "blue": 0.39},
    "textFormat": {"bold": True,
                   "foregroundColor": {"red": 1, "green": 1, "blue": 1}},
    "horizontalAlignment": "CENTER",
}

_poll_lock = threading.Lock()
_status_lock = threading.Lock()
_last: dict = {"enabled": None, "time": None, "checked": 0, "synced": 0,
               "errors": 0, "pending": 0, "message": "Not run yet"}

_warned_no_creds = False


def enabled() -> bool:
    global _warned_no_creds
    if os.path.exists(CREDENTIALS_PATH):
        return True
    if not _warned_no_creds:
        log.warning("[SHEET] Disabled — no credentials at %s", CREDENTIALS_PATH)
        _warned_no_creds = True
    return False


def get_status() -> dict:
    with _status_lock:
        return dict(_last)


def _get_worksheet():
    client = gspread.service_account(filename=CREDENTIALS_PATH)
    return client.open_by_key(GOOGLE_SHEET_ID).get_worksheet(0)


def _header_map(header_row: list) -> dict:
    index = {}
    for i, name in enumerate(header_row, start=1):
        clean = (name or "").strip().lower()
        if clean and clean not in index:
            index[clean] = i
    return index


def _resolve_columns(ws) -> dict:
    """Ensure row 1 is the styled header and every column we need exists.
    Empty sheet → full header written. Missing columns → appended. Never
    reorders or overwrites the team's own extra columns."""
    header_row = ws.row_values(1)
    changed = False

    if not header_row:
        ws.update("A1", [_ALL_COLS])
        header_row = list(_ALL_COLS)
        changed = True

    hmap = _header_map(header_row)

    def find(*names):
        for n in names:
            if n.lower() in hmap:
                return hmap[n.lower()]
        return None

    idx = {COL_BL: find(*_BL_ALIASES), COL_CARRIER: find(*_CARRIER_ALIASES)}
    next_col = len(header_row) + 1
    for col in (COL_BL, COL_CARRIER, COL_POD, COL_ETA, COL_STATUS, COL_UPDATED):
        if idx.get(col):
            continue
        found = find(col)
        if found:
            idx[col] = found
        else:
            ws.update_cell(1, next_col, col)
            idx[col] = next_col
            next_col += 1
            changed = True

    if changed:
        try:
            last = gspread.utils.rowcol_to_a1(1, max(idx.values()))
            ws.format(f"A1:{last}", _HEADER_FORMAT)
            ws.freeze(rows=1)
        except Exception as e:
            log.debug("[SHEET] Header styling skipped: %s", e)
    return idx


def _is_stale(updated: str) -> bool:
    if not updated:
        return True
    try:
        dt = datetime.strptime(updated.strip()[:16], "%Y-%m-%d %H:%M")
        return (datetime.now() - dt).total_seconds() > REFRESH_HOURS * 3600
    except Exception:
        return True


def _select_tasks(values: list, idx: dict):
    """Group rows by BL. The FIRST row of each BL is the primary (scraped and
    filled); any later rows with the same BL are marked "Duplicate". A BL is
    queued if its primary is new/forced/stale, or if it has any unmarked
    duplicate rows. Oldest-first, capped to MAX_PER_CYCLE. Returns
    (tasks, total_pending) where each task is
    (priority, sortkey, bl, carrier, primary_row, need_scrape, dup_rows)."""
    bl_c, car_c = idx[COL_BL], idx.get(COL_CARRIER)
    stat_c, upd_c = idx[COL_STATUS], idx[COL_UPDATED]

    groups = {}  # bl -> {"rows": [(row, status, updated)], "carrier": str}
    for row_num, row in enumerate(values[1:], start=2):
        def cell(c):
            return (row[c - 1] or "").strip() if c and c <= len(row) else ""

        bl = cell(bl_c)
        if not bl:
            continue
        g = groups.setdefault(bl, {"rows": [], "carrier": ""})
        g["rows"].append((row_num, cell(stat_c), cell(upd_c)))
        if not g["carrier"] and car_c:
            g["carrier"] = cell(car_c)

    def _final(status: str) -> bool:
        s = status.lower()
        return ("delivered" in s or "carrier not supported" in s)

    candidates = []
    for bl, g in groups.items():
        rows = g["rows"]
        p_row, p_status, p_updated = rows[0]        # primary = first occurrence
        forced = any(s.lower() == "sync" for _, s, _ in rows)
        need_scrape = forced or (not _final(p_status) and
                                 (not p_status or _is_stale(p_updated)))
        # later rows not yet marked "Duplicate"
        dup_rows = [r for r, s, _ in rows[1:] if s.strip().lower() != "duplicate"]

        if not (need_scrape or dup_rows):
            continue
        carrier = g["carrier"] or _detect_carrier(bl)
        candidates.append((0 if forced else 1, p_updated or "",
                           bl, carrier, p_row, need_scrape, dup_rows))

    candidates.sort(key=lambda t: (t[0], t[1]))
    return candidates[:MAX_PER_CYCLE], len(candidates)


def _write(ws, idx: dict, row: int, updates: dict):
    batch = [{"range": gspread.utils.rowcol_to_a1(row, idx[c]), "values": [[v]]}
             for c, v in updates.items() if idx.get(c)]
    if batch:
        ws.batch_update(batch)


def poll(scrape_fn) -> dict:
    if not enabled():
        with _status_lock:
            _last.update({"enabled": False, "message": "No credentials — disabled"})
        return get_status()

    if not _poll_lock.acquire(blocking=False):
        return get_status()

    synced = errors = 0
    try:
        ws  = _get_worksheet()
        idx = _resolve_columns(ws)
        values = ws.get_all_values()
        tasks, pending = _select_tasks(values, idx)
        now = datetime.now().strftime("%Y-%m-%d %H:%M")

        if tasks:
            log.info("[SHEET] Scraping %d row(s) this cycle (%d pending)",
                     len(tasks), pending)

        for _prio, _upd, bl, carrier, row, need_scrape, dup_rows in tasks:
            # Mark repeat rows of the same BL — free, no scraping.
            for d in dup_rows:
                _write(ws, idx, d, {COL_STATUS: "Duplicate", COL_UPDATED: now})

            if not need_scrape:
                continue

            if not carrier or get_scraper(carrier) is None:
                _write(ws, idx, row, {
                    COL_CARRIER: carrier,
                    COL_STATUS: f"{NO_DATA} (carrier not supported)",
                    COL_UPDATED: now})
                errors += 1
                continue
            try:
                data   = scrape_fn(bl, carrier)
                pod    = data.get("POD") or ""
                ata    = _clean_date(data.get("ATA") or "")
                atd    = _clean_date(data.get("ATD") or "")

                if not _has_data(pod, ata):
                    # Scrape ran but the carrier returned nothing usable —
                    # timeout, blocked, or BL not in their system yet.
                    _write(ws, idx, row, {
                        COL_CARRIER: carrier, COL_POD: "", COL_ETA: "",
                        COL_STATUS: NO_DATA, COL_UPDATED: now})
                    errors += 1
                    continue

                status = _determine_status(data.get("Latest Status") or "", ata, atd, pod)
                if status == "In Transit" and ata:
                    try:
                        if datetime.strptime(ata[:10], "%Y-%m-%d").date() >= datetime.now().date():
                            ata = f"ETA: {ata}"
                    except Exception:
                        pass
                _write(ws, idx, row, {
                    COL_CARRIER: carrier, COL_POD: pod,
                    COL_ETA: _fmt_date(ata), COL_STATUS: status, COL_UPDATED: now})
                synced += 1
            except Exception as e:
                # Any crash mid-scrape is a failure to find data — say so plainly.
                log.error("[SHEET] %s failed: %s", bl, e)
                _write(ws, idx, row, {
                    COL_CARRIER: carrier, COL_STATUS: NO_DATA, COL_UPDATED: now})
                errors += 1
            time.sleep(1)

        remaining = pending - len(tasks)
        msg = (f"Synced {synced}, errors {errors}, {remaining} still pending"
               if tasks else "Up to date — nothing to sync")
        checked = len(values) - 1
    except Exception as e:
        log.error("[SHEET] Poll failed: %s", e)
        msg, checked, pending = f"Poll failed: {str(e)[:80]}", 0, 0
        errors += 1
    finally:
        _poll_lock.release()

    with _status_lock:
        _last.update({"enabled": True, "time": datetime.now().strftime("%Y-%m-%d %H:%M"),
                      "checked": checked, "synced": synced, "errors": errors,
                      "pending": pending, "message": msg})
    return get_status()
