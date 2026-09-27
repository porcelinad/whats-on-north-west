"""
Out and About - Galway event scraper
Scrapes cultural venues in Galway into docs/galway/events.json.

Independent from the North West's scrape.py - its own venue list, its
own town/county whitelist, its own output file - but shares the same
region-agnostic helpers (date resolution, generic dedup, the page-
walking machinery) from common.py, so a fix made there benefits both
scrapers at once.
"""

import json
import os
import re
import sys
from datetime import date, datetime, timezone
from pathlib import Path

from common import (
    TODAY, MONTHS,
    fetch, clean, make_event,
    infer_range_years,
    make_town_resolver,
    apply_generic_sold_out, apply_kids_family_tag,
    merge_cross_source_duplicates, event_key, load_previous, notify,
)

# ---------------------------------------------------------------- config

ROOT = Path(__file__).resolve().parent.parent
DATA_FILE = ROOT / "docs" / "galway" / "events.json"

NTFY_TOPIC = os.environ.get("NTFY_TOPIC_GALWAY", "").strip()
PAGE_URL = os.environ.get("PAGE_URL_GALWAY", "").strip()


# ---------------------------------------------------------------- parsers

THT_DATE_TOKEN_RE = re.compile(r"(\d{1,2})\s*(?:st|nd|rd|th)?\s*([A-Za-z]+)?")


def parse_tht_date(text):
    """Parses THT's date cell text: '14th - 26th Sep' (same-month range,
    first day missing its month, borrowed from the second), '30th Sep -
    03rd Oct' (cross-month range, both given), or a single '27th Sep'.
    No year is ever given, and a range can cross into the next year
    (e.g. '29th Dec - 10th Jan')."""
    matches = [[int(m.group(1)), m.group(2)]
               for m in THT_DATE_TOKEN_RE.finditer(text)]
    if not matches:
        return None, None
    for i in range(len(matches) - 1):
        if not matches[i][1]:
            later = next((p for p in matches[i + 1:] if p[1]), None)
            if later:
                matches[i][1] = later[1]
    tokens = []
    for day, mon_name in matches:
        mon = MONTHS.get(mon_name.lower()[:3]) if mon_name else None
        if mon:
            tokens.append((mon, day))
    dates = [d for d in infer_range_years(tokens) if d]
    if not dates:
        return None, None
    start = dates[0]
    end = dates[-1] if len(dates) > 1 and dates[-1] != start else None
    return start, end


def parse_tht(soup, source):
    """tht.ie/all - the 'At A Glance' listing is one clean HTML table,
    one <tr data-all> per event: genre in td.type, title and its own
    specific event-page link in td.title, a date range with no year in
    td.date, and a Ticketsolve booking link (when the event is ticketed
    at all - free/unticketed events have an empty td.buy) captured as a
    bonus extra. A 5th cell names which of the venue's several
    performance spaces (main house, Black Box, Studio, or an occasional
    off-site 'Other') hosts it, but all are the same physical building,
    so that isn't surfaced as a separate venue for now. Genre and the
    event's own link are both already right here on the listing table -
    no per-event page visits needed at all."""
    events = []
    for tr in soup.select("tr[data-all]"):
        type_td = tr.select_one("td.type")
        title_a = tr.select_one("td.title a")
        date_td = tr.select_one("td.date")
        if not (type_td and title_a and date_td):
            continue
        href = title_a.get("href")
        if not href:
            continue
        start, end = parse_tht_date(date_td.get_text(" "))
        if not start:
            continue
        buy_a = tr.select_one("td.buy a")
        events.append(make_event(
            source, clean(title_a.get_text()), start,
            end_date=end.isoformat() if end else None,
            url=f"https://tht.ie{href}" if href.startswith("/") else href,
            booking_url=buy_a.get("href") if buy_a else None,
            category=clean(type_td.get_text())))
    return events


# ---------------------------------------------------------------- sources

SOURCES = [
    {"name": "tht", "venue": "Town Hall Theatre", "town": "Galway",
     "county": "Galway", "url": "https://tht.ie/all",
     "parser": parse_tht},
]


# --------------------------------------------------- county/town whitelist

# Galway is being built one venue at a time, starting with the city
# itself. Add towns here as sources needing them get added, same
# incremental pattern as the North West's own whitelist.
ALLOWED_COUNTIES = {"Galway"}
COUNTY_ALIASES = {}
COUNTY_CANONICAL = {c.lower(): c for c in ALLOWED_COUNTIES}
COUNTY_CANONICAL.update({k.lower(): v for k, v in COUNTY_ALIASES.items()})

TOWN_TO_COUNTY = {
    "galway": "Galway", "galway city": "Galway",
}
_COUNTY_NAME_TOWNS = {"galway"}
find_specific_town, nearest_known_town = make_town_resolver(
    TOWN_TO_COUNTY, _COUNTY_NAME_TOWNS)


# ---------------------------------------------------------------- genre

CATEGORY_ALIASES = {}
CATEGORY_DROP = set()


def normalize_category(cat):
    """Same consolidation approach as the North West: a substring match
    for children's content (robust to new phrasings), an alias table
    for near-duplicate genre names, and a drop-list for tokens an
    existing tag already covers. Both start empty for Galway and grow
    as real venues surface real near-duplicates worth merging. THT's own
    genre values are ALL CAPS ('FILM', 'THEATRE') on the source page, so
    they're title-cased here for visual consistency with the rest of
    the site's tags."""
    if not cat:
        return cat
    parts = [p.strip() for p in cat.split(",")]
    out = []
    for p in parts:
        low = p.lower()
        if low in CATEGORY_DROP:
            continue
        if "child" in low or "kids" in low:
            out.append("Kids/Family")
        else:
            out.append(CATEGORY_ALIASES.get(low, p.title()))
    seen, deduped = set(), []
    for p in out:
        if p not in seen:
            seen.add(p)
            deduped.append(p)
    return ", ".join(deduped) if deduped else None


# ------------------------------------------------------------ dedup config

# Empty for now - grows as real cross-source duplicates turn up, same as
# the North West's own DEDUP_NOISE_PREFIXES/DEDUP_ALIASES did over time.
SOURCE_PRIORITY = {}
DEDUP_NOISE_PREFIXES = []
DEDUP_ALIASES = []
DEDUP_THRESHOLD = 0.7


# ---------------------------------------------------------------- pipeline

def main():
    previous = load_previous(DATA_FILE)
    prev_by_key = {event_key(e): e for e in previous.get("events", [])}

    all_events, failed = [], []
    source_last_run = dict(previous.get("source_last_run", {}))
    consecutive_failures = dict(previous.get("consecutive_failures", {}))
    FAILURE_THRESHOLD = 5

    for source in SOURCES:
        try:
            soup = fetch(source["url"])
            found = source["parser"](soup, source)
            print(f"{source['venue']}: {len(found)} events")
            if not found:
                extra = f" Page preview: {soup.get_text(' ', strip=True)[:200]!r}"
                raise ValueError(
                    "parsed zero events - selectors may be stale, filters "
                    "may be too strict, or the site blocked this request."
                    + extra)
            all_events.extend(found)
            consecutive_failures[source["name"]] = 0
        except Exception as exc:
            count = consecutive_failures.get(source["name"], 0) + 1
            consecutive_failures[source["name"]] = count
            print(f"WARNING {source['venue']} failed ({count} in a row): {exc}",
                  file=sys.stderr)
            if count >= FAILURE_THRESHOLD:
                failed.append(source["venue"])
            all_events.extend(
                e for e in prev_by_key.values() if e["source"] == source["name"])

    seen, final = set(), []
    for ev in merge_cross_source_duplicates(
            all_events, source_priority=SOURCE_PRIORITY,
            noise_prefixes=DEDUP_NOISE_PREFIXES, aliases=DEDUP_ALIASES,
            threshold=DEDUP_THRESHOLD):
        canon = COUNTY_CANONICAL.get((ev.get("county") or "").strip().lower())
        if not canon:
            continue
        ev["county"] = canon
        ev["town"] = nearest_known_town(ev.get("town"))
        ev["category"] = normalize_category(ev.get("category"))
        apply_kids_family_tag(ev)
        apply_generic_sold_out(ev)
        last_day = date.fromisoformat(ev.get("end_date", ev["date"]))
        if last_day < TODAY:
            continue
        key = event_key(ev)
        if key in seen:
            continue
        seen.add(key)
        ev["id"] = key
        ev["first_seen"] = prev_by_key.get(key, {}).get(
            "first_seen", TODAY.isoformat())
        final.append(ev)

    final.sort(key=lambda e: (e["date"], e["venue"], e["title"]))

    now = datetime.now(timezone.utc)
    DATA_FILE.parent.mkdir(parents=True, exist_ok=True)
    DATA_FILE.write_text(json.dumps({
        "generated_at": now.strftime("%Y-%m-%dT%H:%MZ"),
        "failed_sources": failed,
        "source_last_run": source_last_run,
        "consecutive_failures": consecutive_failures,
        "events": final,
    }, indent=1, ensure_ascii=False), encoding="utf-8")
    print(f"Wrote {len(final)} upcoming events to {DATA_FILE}")

    if previous.get("events"):
        new = [e for e in final
               if e["id"] not in prev_by_key
               and e["source"] not in [s["name"] for s in SOURCES
                                       if s["venue"] in failed]]
        notify(new, NTFY_TOPIC, PAGE_URL)

    if failed:
        print(f"Completed with failures: {', '.join(failed)}", file=sys.stderr)


if __name__ == "__main__":
    main()
