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
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urljoin, urlparse

from common import (
    TODAY, MONTHS,
    fetch, clean, walk, make_event,
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


# ---------------------------------------------- festival umbrella rows
#
# Some THT listing rows are a whole festival (e.g. "Baboro 2026", 9-18
# Oct) rather than one show. Their hidden description cell on the same
# /all page already carries the complete programme as flat text - no tags,
# just newlines - as a repeating block per show:
#
#     <GENRE HEADER, only when the group changes>
#      <Title> <Weekday Nth - Weekday Nth, Weekday Nth> 
#     <age range>
#     <venue>
#     TICKETS | FREE
#     ...description...
#
# so these can be expanded into individual events with no extra page
# fetches. Detection is by that block pattern (two or more of them), NOT
# by the word "festival", so a future festival laid out the same way is
# picked up automatically. Each show is labelled with its festival in
# brackets, e.g. "The Sticky Dance (Baboro)".

THT_DAY_TOKEN = r"(?:Mon|Tues?|Wed|Thu(?:rs?)?|Fri|Sat|Sun)\.?\s+\d{1,2}(?:st|nd|rd|th)"
THT_SUB_DATES = rf"{THT_DAY_TOKEN}(?:\s*(?:[-\u2013\u2014]|,)\s*{THT_DAY_TOKEN})*"
THT_SUB_HEAD_RE = re.compile(rf"^(?P<title>.+?)\s+(?P<dates>{THT_SUB_DATES})\s*$")
THT_SUB_DAY_RE = re.compile(r"(Mon|Tue|Wed|Thu|Fri|Sat|Sun)[a-z]*\.?\s+(\d{1,2})", re.I)
THT_WEEKDAY = {"mon": 0, "tue": 1, "wed": 2, "thu": 3, "fri": 4, "sat": 5, "sun": 6}
THT_NOT_HEADERS = {"TICKETS", "FREE", "BUY TICKETS"}

# THT's programme text spells the same place several ways
THT_VENUE_ALIASES = {
    "town hall": "Town Hall Theatre",
    "black box": "Black Box Theatre",
    "o' donoghue centre": "O'Donoghue Centre",
}


def tht_festival_label(title):
    """'Baboro 2026' -> 'Baboro': the festival's name without its year."""
    label = re.sub(r"\s*\b(?:19|20)\d{2}\b\s*", " ", title).strip()
    return label or title


def split_tht_festival_summary(text):
    """Finds each show block in a festival row's flat-text description.
    Anchors on the lone 'TICKETS'/'FREE' line that ends every block's
    header, then reads back over the three non-blank lines before it
    (venue, age range, 'title + dates'). A genre-group header (e.g.
    'WORKSHOPS') is an ALL-CAPS line sitting just above a block's title
    line, and applies to that block and every later one until the next
    header. Returns [] when there are fewer than two blocks, i.e. when
    this isn't a festival row at all."""
    lines = [ln.strip() for ln in text.splitlines()]
    blocks, group = [], None
    for i, ln in enumerate(lines):
        if ln not in ("TICKETS", "FREE"):
            continue
        prev, j = [], i - 1
        while j >= 0 and len(prev) < 3:
            if lines[j]:
                prev.append((j, lines[j]))
            j -= 1
        if len(prev) < 3:
            continue
        (_, venue), (_, age), (head_idx, head) = prev
        m = THT_SUB_HEAD_RE.match(head)
        if not m:
            continue
        k = head_idx - 1
        while k >= 0 and not lines[k]:
            k -= 1
        if (k >= 0 and lines[k].isupper() and len(lines[k]) <= 40
                and lines[k] not in THT_NOT_HEADERS):
            group = lines[k]
        blocks.append({"title": m.group("title").strip(),
                       "dates": m.group("dates"), "age": age,
                       "venue": venue, "group": group})
    return blocks if len(blocks) >= 2 else []


def resolve_tht_sub_dates(dates_text, fest_start, fest_end):
    """Turns e.g. 'Fri 9th - Sat 10th, Sat 17th' into one (start, end)
    pair per contiguous run - [(Oct 9, Oct 10), (Oct 17, None)] - rather
    than one false continuous range. The text never states a month, so
    each weekday+day is matched against the festival's own date range
    (padded a few days either side); a weekday+day pair can't repeat
    within a window that short, so there's no ambiguity. A run that
    can't be placed is skipped rather than guessed."""
    span, d = [], fest_start - timedelta(days=3)
    while d <= fest_end + timedelta(days=3):
        span.append(d)
        d += timedelta(days=1)

    def find(weekday, day):
        for cand in span:
            if cand.day == day and cand.weekday() == THT_WEEKDAY[weekday]:
                return cand
        return None

    groups = []
    for piece in dates_text.split(","):
        found = [find(wd.lower()[:3], int(day))
                 for wd, day in THT_SUB_DAY_RE.findall(piece)]
        if not found or any(f is None for f in found):
            continue
        end = found[-1] if len(found) > 1 and found[-1] != found[0] else None
        groups.append((found[0], end))
    return groups


def expand_tht_festival(summary_text, fest_title, fest_start, fest_end,
                        fest_category, fest_url, source):
    """Individual events for a festival umbrella row, or [] if the row
    isn't one (or nothing in it could be dated), so the caller can fall
    back to the single umbrella entry. Each show keeps its OWN venue
    (festivals use many) and links to the festival's page on tht.ie -
    the flattened text carries no per-show links to deep-link to. A show
    for adults only (age line 'Adults') doesn't inherit the festival's
    own genre, so a family festival's industry talks aren't tagged as
    family events."""
    blocks = split_tht_festival_summary(summary_text)
    if not blocks:
        return []
    label = tht_festival_label(fest_title)
    events = []
    for b in blocks:
        venue_key = re.sub(r"\s+", " ", b["venue"]).lower()
        venue = THT_VENUE_ALIASES.get(venue_key, b["venue"])
        if b["age"].strip().lower() == "adults":
            category = b["group"]
        else:
            category = ", ".join(p for p in (fest_category, b["group"]) if p)
        for start, end in resolve_tht_sub_dates(b["dates"], fest_start, fest_end):
            events.append(make_event(
                source, f"{b['title']} ({label})", start,
                end_date=end.isoformat() if end else None,
                url=fest_url, category=category or None, venue=venue))
    return events


# Venue for rows THT's listing doesn't name one for: read from each event's
# own page, where it's the <p> straight after the title <h1>, e.g.
#   <div class="presents">...</div><h1>Bothar na Smaointe...</h1>
#   <p>St. Nicholas Church</p>
# Fetched once per event URL, ever - persisted in events.json like the
# North West's location cache. Only a venue actually FOUND is cached, so
# a page that failed to load (or whose layout wasn't recognised) is simply
# retried next run rather than being remembered as 'no venue'.
THT_VENUE_CACHE = {}
THT_VENUE_MAX_LEN = 80   # a venue is a short line; anything longer is prose


def parse_tht_event_page_venue(soup, title=None):
    """The venue line from a THT event page, or None. Matches the <h1>
    to the event's own title (so a site-header <h1>, if there is one,
    can't be mistaken for it); if no <h1> matches, only trusts the page
    when it has exactly one <h1> followed by a <p>, rather than guessing
    between several."""
    def norm(t):
        return re.sub(r"\W+", "", t or "").lower()

    want, cands = norm(title), []
    for h1 in soup.find_all("h1"):
        p = h1.find_next_sibling("p")
        text = clean(p.get_text(" ")) if p else ""
        if text and len(text) <= THT_VENUE_MAX_LEN:
            cands.append((norm(h1.get_text()), text))
    if want:
        for got, text in cands:
            if got and (got == want or got in want or want in got):
                return text
    return cands[0][1] if len(cands) == 1 else None


def tht_event_page_venue(url, title):
    cached = THT_VENUE_CACHE.get(url)
    if cached:
        return cached
    try:
        venue = parse_tht_event_page_venue(fetch(url), title)
    except Exception as exc:
        print(f"  could not read venue for {title!r}: {exc}", file=sys.stderr)
        return None
    if venue:
        THT_VENUE_CACHE[url] = venue
    time.sleep(0.4)   # be gentle - per-event page requests
    return venue


# The 5th cell of each THT listing row says WHERE the show is, as a code.
THT_VENUE_BY_CODE = {
    "tht": "Town Hall Theatre",
    "studio": "Town Hall Theatre",     # a smaller stage inside the Town Hall
    "black box": "Black Box Theatre",  # its own, separate venue
}
# 'Other' (and any code not listed above) means off-site, with no venue
# named on the listing page - so the venue is read from the event's own
# page (see above) instead of being guessed (these were previously all
# mislabelled Town Hall Theatre). If that page can't be read, True shows
# the event anyway under a placeholder venue that points people to the
# event page for the details (retried next run); False skips it until the
# venue can be found.
THT_INCLUDE_OFFSITE = True
THT_OFFSITE_VENUE = "Off-site (see event page)"


def parse_tht(soup, source):
    """tht.ie/all - the 'At A Glance' listing is one clean HTML table,
    one <tr data-all> per event: genre in td.type, title and its own
    specific event-page link in td.title, a date range with no year in
    td.date, and a Ticketsolve booking link (when the event is ticketed
    at all - free/unticketed events have an empty td.buy) captured as a
    bonus extra. A 5th cell names which of the venue's several
    performance spaces hosts it: 'THT' (the main house) and 'Studio' (a
    smaller stage inside the Town Hall) are both the Town Hall Theatre;
    'Black Box' is its own separate venue, Black Box Theatre; 'Other' is
    off-site with no venue named on this listing, so each of those rows'
    own event page is read once (ever - cached) for the venue instead.
    Genre and the
    event's own link are both already right here on the listing table -
    no per-event page visits needed at all."""
    events, unresolved = [], []
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
        title = clean(title_a.get_text())
        url = f"https://tht.ie{href}" if href.startswith("/") else href
        category = clean(type_td.get_text())
        # a multi-day row whose hidden description is really a festival
        # programme becomes one event per show instead of one umbrella
        summary_td = tr.select_one("td.summary")
        if summary_td is not None and end:
            try:
                shows = expand_tht_festival(
                    summary_td.get_text("\n"), title, start, end,
                    category, url, source)
            except Exception as exc:
                shows = []
                print(f"  could not expand {title!r}: {exc}", file=sys.stderr)
            if shows:
                events.extend(shows)
                continue
        tds = tr.find_all("td")
        code = tds[4].get_text(strip=True).lower() if len(tds) > 4 else ""
        venue = THT_VENUE_BY_CODE.get(code)
        if venue is None:
            venue = tht_event_page_venue(url, title)
            if venue is None:
                unresolved.append(title)
                if not THT_INCLUDE_OFFSITE:
                    continue
        events.append(make_event(
            source, title, start,
            end_date=end.isoformat() if end else None,
            url=url,
            booking_url=buy_a.get("href") if buy_a else None,
            category=category,
            venue=venue or THT_OFFSITE_VENUE))
    if unresolved:
        action = ("shown under a placeholder venue" if THT_INCLUDE_OFFSITE
                  else "skipped")
        print(f"  no venue found for {len(unresolved)} off-site row(s), "
              f"{action}: " + "; ".join(unresolved))
    return events


# ---------------------------------------------------------------- sources

# ------------------------------------------------------------ Monroe's Live

MONROES_DATE_RE = re.compile(
    r"^(?:Mon|Tue|Wed|Thu|Fri|Sat|Sun)[a-z]*\.?\s+([A-Za-z]{3})[a-z]*\s+"
    r"(\d{1,2})\s+(\d{4})$", re.I)
MONROES_TIME_RE = re.compile(r"\d.*(?:am|pm)\b", re.I)


def parse_monroes(soup, source):
    """monroes.ie/pages/gigs - a Shopify page. Each gig is a link to its
    ticket product (/products/<slug>), then a heading with the FULL date
    including the year ('Thu Oct 08 2026', so no year inference is
    needed), then a 'Doors 7pm'-style line (kept as the event's time). A
    sold-out gig has an extra 'Sold Out' link to the same product. Built
    on the page's link/text order rather than its CSS classes, so a theme
    tweak is less likely to break it. The page gives no genre: Monroe's
    is a live-music venue that also hosts an 'Outpost Comedy Club' night,
    so everything is Music unless the title says comedy."""
    events, cur, sold_href = [], None, None

    def norm(href):
        return urlparse(href).path.rstrip("/")

    def finalise():
        if cur and cur["date"]:
            comedy = re.search(r"\bcomedy\b", cur["title"], re.I)
            events.append(make_event(
                source, cur["title"], cur["date"], time=cur["time"],
                url=cur["url"], sold_out=cur["sold"],
                category="Comedy" if comedy else "Music"))

    for kind, a, b in walk(soup):
        if kind == "link":
            href, text = a, b
            if "/products/" not in href or not text:
                continue   # image-only links have no text of their own
            if text.lower() == "sold out":
                if cur and norm(cur["url"]) == norm(href):
                    cur["sold"] = True
                else:
                    sold_href = norm(href)   # the title link follows
                continue
            if (cur and not cur["date"] and cur["title"] == text
                    and norm(cur["url"]) == norm(href)):
                continue   # the same title link repeated
            finalise()
            cur = {"title": text, "url": urljoin(source["url"], href),
                   "sold": sold_href == norm(href), "date": None, "time": None}
            sold_href = None
        elif cur:
            if cur["date"] is None:
                m = MONROES_DATE_RE.match(a)
                mon = MONTHS.get(m.group(1).lower()) if m else None
                if mon:
                    try:
                        cur["date"] = date(int(m.group(3)), mon, int(m.group(2)))
                    except ValueError:
                        pass
            elif (cur["time"] is None and MONROES_TIME_RE.search(a)
                    and a.lower() != "sold out"):
                cur["time"] = a
    finalise()
    return events


SOURCES = [
    {"name": "tht", "venue": "Town Hall Theatre", "town": "Galway",
     "county": "Galway", "url": "https://tht.ie/all",
     "parser": parse_tht},
    {"name": "monroes", "venue": "Monroe's Live", "town": "Galway",
     "county": "Galway", "url": "https://monroes.ie/pages/gigs",
     "parser": parse_monroes},
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

CATEGORY_ALIASES = {
    # THT's own FAMILY genre and the title-based Kids/Family detector were
    # producing two separate tags for the same thing
    "family": "Kids/Family",
    "workshops": "Workshop",
}
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
    previous = load_previous(DATA_FILE, extra_defaults={"venue_cache": {}})
    THT_VENUE_CACHE.update(previous.get("venue_cache", {}))
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
            threshold=DEDUP_THRESHOLD, merge_same_source=False):
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
        "venue_cache": THT_VENUE_CACHE,
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
