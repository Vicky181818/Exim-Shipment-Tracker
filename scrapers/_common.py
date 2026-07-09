"""
scrapers/_common.py

Shared helpers used across carrier scrapers, so every carrier follows the
same standardized result schema and produces the same canonical
"Latest Status" table format the frontend's parseStatusBlock() already
knows how to render — the format validated end-to-end with cosco.py.

Carrier scrapers should import from here instead of re-implementing the
same regexes / table-building logic in every file.
"""
import re
import time

# ---- Standard result schema --------------------------------------------
# Every scraper should return a dict with (at least) these keys. A carrier
# that doesn't expose a particular field (e.g. no separate POR) just
# leaves it as "". Keeping this identical across carriers is what lets
# api.py and the frontend treat every carrier the same way.
EMPTY_RESULT = {
    "POR": "", "POL": "", "POD": "", "Container No": "",
    "Vessel": "", "ATD": "", "ATA": "", "Latest Status": "", "FND": "",
}


def empty_result() -> dict:
    """Return a fresh copy of the standard result schema."""
    return dict(EMPTY_RESULT)


# ---- Container number extraction ---------------------------------------
# ISO 6346 format: 4 letters + 7 digits (e.g. CBHU8989425). Works the same
# way regardless of carrier, since it's an industry-wide standard, not a
# carrier-specific format.
CONTAINER_RE = re.compile(r'\b([A-Z]{4}\d{7})\b')


def extract_containers(text: str) -> str:
    """Find every container number in raw page text. De-duplicated,
    keeps first-seen order, returns the standard ' | '-joined string
    used by both the API and the frontend's Containers tab."""
    found = CONTAINER_RE.findall(text)
    return " | ".join(dict.fromkeys(found))


# ---- Canonical multi-leg "Latest Status" table builder ------------------
DATE_TIME_RE = re.compile(r'(\d{4}-\d{2}-\d{2})\s+(\d{2}:\d{2})')


def build_status_block(legs: list, final_event: dict = None) -> str:
    """
    Build the canonical multi-line status table the frontend already
    parses into a full stop-by-stop timeline (validated with COSCO's
    multi-leg Felixstowe -> Colombo -> Tuticorin journey).

    legs: list of dicts, each with:
        vessel, voyage, pol, pod,
        atd  ("YYYY-MM-DD HH:MM" or "")
        ata  ("YYYY-MM-DD HH:MM" or "")

    final_event: optional dict for a closing event with no further legs,
        e.g. {"status": "Empty Return", "place": "ARIES CONTAINER TERMINAL",
              "date": "2026-03-11", "time": "18:11"}

    Returns a string ready to assign directly to result["Latest Status"].
    Returns "" if there's nothing to build (caller should keep whatever
    simple status string it already had in that case).
    """
    rows = []
    for leg in legs:
        vessel = leg.get("vessel", "")
        voyage = leg.get("voyage", "")
        pol = (leg.get("pol") or "").upper()
        pod = (leg.get("pod") or "").upper()

        if leg.get("atd") and DATE_TIME_RE.match(leg["atd"]):
            d, t = leg["atd"].split(" ")
            rows.append(f"Vessel departed  {pol}  {d} {t}  {vessel}  {voyage}")
        if leg.get("ata") and DATE_TIME_RE.match(leg["ata"]):
            d, t = leg["ata"].split(" ")
            rows.append(f"Vessel arrived  {pod}  {d} {t}  {vessel}  {voyage}")

    if final_event and final_event.get("status") and final_event.get("date"):
        place = (final_event.get("place") or "").upper()
        rows.append(
            f"{final_event['status']}  {place}  "
            f"{final_event['date']} {final_event.get('time', '')}".rstrip()
        )

    return "\n".join(rows)


# ---- Generic "label on its own line, value a few lines later" reader ----
def find_value_after_label(lines: list, label: str, max_lookahead: int = 4) -> str:
    """
    The same pattern used to pull POL/POD/ATA out of both the Hapag and
    COSCO page dumps: a line that's exactly the label text, followed a
    few lines later by the actual value.

    lines: page text already split into stripped, non-empty lines.
    """
    for i, line in enumerate(lines):
        if line.strip() == label:
            for j in range(i + 1, min(i + 1 + max_lookahead, len(lines))):
                val = lines[j].strip()
                if val:
                    return val
    return ""


def find_date_after_label(lines: list, label: str) -> str:
    """
    Reads the common 3-line date pattern that follows a label, e.g.:
        ATA
        2026-02-25
        00:48:00
        IST
    Returns "YYYY-MM-DD HH:MM:SS TZ" (or as many parts as are present),
    or "" if not found / not a valid date.
    """
    for i, line in enumerate(lines):
        if line.strip() == label:
            date = lines[i + 1] if i + 1 < len(lines) else ""
            tme = lines[i + 2] if i + 2 < len(lines) else ""
            tz = lines[i + 3] if i + 3 < len(lines) else ""
            if re.match(r'\d{4}-\d{2}-\d{2}', date):
                return f"{date} {tme} {tz}".strip()
    return ""


def build_status_block_from_events(events: list) -> str:
    """
    Like build_status_block(), but for carriers that expose a flat,
    already-dated list of events directly (e.g. MSC's per-container
    movement history) rather than a leg-by-leg vessel schedule.

    Each event dict needs:
        status   - e.g. "Full Transshipment Loaded"
        place    - e.g. "Djibouti, DJ"
        date     - "YYYY-MM-DD"
        time     - "HH:MM" (use "00:00" if the carrier has no time of day)
        transport (optional) - vessel/rail name
        voyage    (optional) - voyage/service code

    Events should be passed in CHRONOLOGICAL order (oldest first), the
    same convention used by build_status_block(), since the frontend
    reverses this list itself to show newest-first.
    """
    rows = []
    for ev in events:
        date = ev.get("date", "")
        time_ = ev.get("time") or "00:00"
        status = ev.get("status", "")
        if not (date and status):
            continue
        place = (ev.get("place") or "").upper()
        row = f"{status}  {place}  {date} {time_}"
        transport = ev.get("transport", "")
        voyage = ev.get("voyage", "")
        if transport:
            row += f"  {transport}"
        if voyage:
            row += f"  {voyage}"
        rows.append(row)
    return "\n".join(rows)


def dismiss_cookies_js(driver, extra_selectors=None):
    """
    Click a cookie/consent dialog via a single JS call — effectively free
    (~5ms) when no dialog is visible, vs 1s × N selectors with WebDriverWait.

    Tries once immediately, then once more after 0.4s to catch dialogs that
    are injected by JS just after page load (OneTrust, Didomi, etc.).

    extra_selectors: site-specific selectors to prepend to the default list.
    """
    defaults = [
        "#onetrust-accept-btn-handler",
        "#accept-recommended-btn-handler",
        "#didomi-notice-agree-button",
        "button[id*='accept']",
        "[class*='accept-all']",
        "[data-test='coi-allow-all-button']",
        ".cookie-accept",
        "[aria-label*='Accept']",
        ".didomi-continue-without-agreeing",
    ]
    selectors = (extra_selectors or []) + defaults
    js = """
var sels = arguments[0];
for (var i = 0; i < sels.length; i++) {
    try {
        var el = document.querySelector(sels[i]);
        if (el && el.offsetParent !== null && !el.disabled) {
            el.click();
            return sels[i];
        }
    } catch(e) {}
}
return null;
"""
    for attempt in range(2):
        try:
            hit = driver.execute_script(js, selectors)
            if hit:
                time.sleep(0.3)
                return True
        except Exception:
            pass
        if attempt == 0:
            time.sleep(0.4)
    return False


def safe_scrape(scrape_fn, driver, bl: str, retries: int = 1) -> dict:
    """
    Optional wrapper for a carrier's scrape logic: retries once on any
    exception, then falls back to a standard empty_result() with the
    error recorded in 'Latest Status' instead of letting the exception
    propagate (api.py will still surface a clean error either way, but
    this lets a transient page hiccup self-heal without a full failure).
    """
    last_err = None
    for _ in range(retries + 1):
        try:
            return scrape_fn(driver, bl)
        except Exception as e:
            last_err = e
            continue
    result = empty_result()
    result["Latest Status"] = f"Error: {str(last_err)[:120]}"
    return result
