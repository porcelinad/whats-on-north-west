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
import requests
import sys
import time
from bs4 import Comment
from datetime import date, datetime, timedelta, timezone
from html import unescape
from pathlib import Path
from urllib.parse import quote, urljoin, urlparse

from common import (
    TODAY, MONTHS, HEADERS, TIMEOUT,
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


# ------------------------------------------------------------- Róisín Dubh
#
# roisindubh.net/listings/ is an empty shell: its listings are filled in by
# JavaScript, which calls a JSON endpoint once per month -
#     /remote/searchlistings.json   with   {"query": "", "month": "11/2026"}
# and /remote/listing-month-selector.json says which months exist. This calls
# the same endpoints directly (no browser needed). Each result is a full
# listing - title, an `alias` (the listing's URL slug, so the 'non-guessable'
# URLs are simply handed to us), the exact start date-time, venue, sold-out
# state and ticket link - so no per-event page visits are needed either.
#
# The request is a POST (confirmed in the browser's dev tools); whether its
# body is JSON or form-encoded wasn't shown, though the payload looked like
# JSON - so JSON is tried first, then form, on the second month. A style only
# counts as working if what comes back is really for the month asked for -
# a server that can't parse a request tends to quietly return the CURRENT
# month instead, which would otherwise duplicate it across every month.

ROISIN_BASE = "https://roisindubh.net"
ROISIN_API = ROISIN_BASE + "/remote/searchlistings.json"
ROISIN_MONTHS_API = ROISIN_BASE + "/remote/listing-month-selector.json"
ROISIN_STYLES = ("post_json", "post_form")
ROISIN_MAX_MONTHS = 14
# Recurring nights to leave out, lower-case - e.g. {"last orders"}
ROISIN_SKIP_TITLES = set()
_ROISIN_STYLE = None   # whichever request style turned out to work

ROISIN_COMEDY_RE = re.compile(r"\bcomed(?:y|ian|ians)\b|\bstand-?up\b", re.I)
ROISIN_FAMILY_RE = re.compile(
    r"whole family|family show|family[- ]friendly"
    r"|suitable for ages\s*(?:[1-9]|1[0-7])\b", re.I)


def _roisin_headers():
    return dict(HEADERS, **{
        "Accept": "application/json, text/plain, */*",
        "X-Requested-With": "XMLHttpRequest",
        "Referer": ROISIN_BASE + "/listings/",
        "Origin": ROISIN_BASE,
    })


def _roisin_request(style, label):
    payload = {"query": "", "month": label}
    headers = _roisin_headers()
    if style == "post_json":
        r = requests.post(ROISIN_API, json=payload, headers=headers, timeout=TIMEOUT)
    else:
        r = requests.post(ROISIN_API, data=payload, headers=headers, timeout=TIMEOUT)
    r.raise_for_status()
    return r.json()


def roisin_months():
    """[(label, year, month), ...] from this month on. The labels are read
    from the site's own month selector, so whatever format it sends
    ('11/2026', '1/2027'...) is exactly what gets asked for; if that can't
    be read, the next 12 months are generated zero-padded instead."""
    found, seen = [], set()
    try:
        r = requests.get(ROISIN_MONTHS_API, headers=_roisin_headers(), timeout=TIMEOUT)
        r.raise_for_status()
        text = r.text.replace("\\/", "/")   # PHP escapes slashes in JSON
        for mm, yy in re.findall(r"\b(\d{1,2})/(20\d{2})\b", text):
            key = (int(yy), int(mm))
            if 1 <= key[1] <= 12 and key >= (TODAY.year, TODAY.month) and key not in seen:
                seen.add(key)
                found.append((f"{mm}/{yy}", key[0], key[1]))
    except Exception as exc:
        print(f"  could not read Róisín Dubh's month list ({exc}); "
              f"generating it instead", file=sys.stderr)
    if found:
        return sorted(found, key=lambda t: (t[1], t[2]))[:ROISIN_MAX_MONTHS]
    out, y, m = [], TODAY.year, TODAY.month
    for _ in range(12):
        out.append((f"{m:02d}/{y}", y, m))
        m, y = (1, y + 1) if m == 12 else (m + 1, y)
    return out


def roisin_fetch_month(label, year, mon):
    """The raw results for one month. Until a request style has proven
    itself (by returning real listings for the right month) every style is
    tried; after that only the one that worked."""
    global _ROISIN_STYLE
    styles = [_ROISIN_STYLE] if _ROISIN_STYLE else list(ROISIN_STYLES)
    prefix = f"{year:04d}-{mon:02d}"
    last_exc, valid_empty = None, False
    for style in styles:
        try:
            data = _roisin_request(style, label)
        except Exception as exc:
            last_exc = exc
            continue
        results = data.get("results") if isinstance(data, dict) else None
        if not (data.get("success") and isinstance(results, list)):
            last_exc = ValueError(f"unexpected response to a {style} request")
            continue
        if not results:
            if _ROISIN_STYLE is None:
                valid_empty = True   # not proof - try the other styles too
                continue
            return results
        if not any((r.get("event_date_time") or "").startswith(prefix) for r in results):
            last_exc = ValueError(f"a {style} request returned the wrong month")
            continue
        if _ROISIN_STYLE is None:
            _ROISIN_STYLE = style
            print(f"  Róisín Dubh: requests work as {style}")
        total = data.get("total")
        if isinstance(total, int) and total != len(results):
            print(f"  WARNING {label}: the site reports {total} listings but "
                  f"returned {len(results)} - possibly truncated", file=sys.stderr)
        return results
    if valid_empty:
        return []
    raise last_exc or RuntimeError("no request style worked")


def roisin_time(dt):
    if dt.hour == 0 and dt.minute == 0:
        return None   # midnight is a placeholder, not a start time
    hour, suffix = dt.hour % 12 or 12, "am" if dt.hour < 12 else "pm"
    return f"{hour}{suffix}" if dt.minute == 0 else f"{hour}:{dt.minute:02d}{suffix}"


def roisin_item(r, source):
    """A tidy dict from one raw result, or None if it shouldn't be listed
    (postponed, untitled, or no usable date). The date and time come from
    event_date_time, NOT the URL slug - the slug keeps the time the listing
    was first created with, which can be out of date (a Silent Disco slug
    says 20:00 where the event is actually at 23:00)."""
    if r.get("postponed"):
        return None
    title = clean(unescape(r.get("pagetitle") or ""))
    alias = (r.get("alias") or "").strip()
    if not title or not alias or title.lower() in ROISIN_SKIP_TITLES:
        return None
    try:
        dt = datetime.fromisoformat((r.get("event_date_time") or "").strip())
    except ValueError:
        return None
    booking = (r.get("external_ticket_url") or "").strip()
    if not re.fullmatch(r"https?://\S+", booking):
        booking = None   # also drops the odd malformed one
    # ticket_remaining behaves like a flag, so only call it sold out when
    # tickets are actually on sale for an allocation that's now used up
    sold = (bool(r.get("on_sale")) and (r.get("ticket_allocation") or 0) > 0
            and not (r.get("ticket_remaining") or 0))
    text = " ".join((title, r.get("introtext") or "",
                     re.sub(r"<[^>]+>", " ", r.get("content") or "")))
    cats = ["Comedy" if ROISIN_COMEDY_RE.search(text) else "Music"]
    if ROISIN_FAMILY_RE.search(text):
        cats.append("Kids/Family")
    return {"title": title, "dt": dt, "booking": booking, "sold": sold,
            "venue": clean(unescape(r.get("name") or "")) or source["venue"],
            "url": ROISIN_BASE + "/listings/" + quote(alias, safe="-._~,"),
            "category": ", ".join(cats)}


def roisin_events(items, source):
    # a ticket tier listed as its own gig ('<Show> - Early Bird',
    # '<Show> - Club Gass Bundle') duplicates the main listing - drop it
    keep = [b for b in items if not any(
        a is not b and a["venue"] == b["venue"]
        and a["dt"].date() == b["dt"].date()
        and b["title"].lower().startswith(a["title"].lower() + " - ")
        for a in items)]
    # one show at two times on the same day is one card, with both times
    groups = {}
    for it in sorted(keep, key=lambda i: i["dt"]):
        groups.setdefault(
            (it["venue"], it["dt"].date(), it["title"].lower()), []).append(it)
    events = []
    for g in groups.values():
        first, times = g[0], []
        for it in g:
            t = roisin_time(it["dt"])
            if t and t not in times:
                times.append(t)
        events.append(make_event(
            source, first["title"], first["dt"].date(),
            time=" & ".join(times) or None, url=first["url"],
            booking_url=next((i["booking"] for i in g if i["booking"]), None),
            sold_out=all(i["sold"] for i in g),
            category=first["category"], venue=first["venue"]))
    return events


def parse_roisindubh(source):
    items, summary = [], []
    months = roisin_months()
    # Work out the request style on the SECOND month, not the current one:
    # a server that can't read a badly-formed request falls back to the
    # current month, which would look perfectly valid if that's also the
    # month being asked for - but is obviously wrong for any other.
    order = months[1:2] + months[:1] + months[2:] if len(months) > 1 else months
    for label, year, mon in order:
        try:
            results = roisin_fetch_month(label, year, mon)
        except Exception as exc:
            if _ROISIN_STYLE is None:
                raise   # never got a working request - nothing to build on
            print(f"  could not fetch Róisín Dubh {label}: {exc}", file=sys.stderr)
            continue
        summary.append(f"{label}={len(results)}")
        items.extend(i for i in (roisin_item(r, source) for r in results) if i)
        time.sleep(0.3)
    print("  Róisín Dubh listings by month: " + ", ".join(summary))
    return roisin_events(items, source)


# ------------------------------------ Ticket Tailor / Galway Film Society
#
# tickettailor.com/events/<organiser> is plain server-rendered HTML: one card
# per event with its title, a date line, the venue and links to the event's
# own page. tt_cards() reads those cards for ANY Ticket Tailor organiser (the
# organiser's name is taken from the source URL), so another venue that sells
# through Ticket Tailor is a few lines, not a new parser. A card is found by
# walking UP from each event link to the biggest ancestor holding only that
# one event, then reading the card's text as a whole - so it doesn't matter
# how the date's individual parts happen to be wrapped in tags.
#
# The GFS-specific part (parse_gfs): the society shows each film more than
# once - e.g. Sunday 5pm and 8pm, Monday 6:30pm - but the listing only gives
# the FIRST and LAST screening ("Sun 4 Oct 17:00 - Mon 5 Oct 18:30"). The full
# list of times is a sentence on each event's own page ("Screening times are
# ..."), so that's read from there, falling back to the listing's first and
# last if the page can't be read. Titles are SHOUTED in capitals on the site
# ('GFS: THE CYCLE OF LOVE') and are tidied, with the society named in
# brackets in the same way a festival is.

TT_STAMP_RE = re.compile(r"""
    (?P<wd1>Mon|Tue|Wed|Thu|Fri|Sat|Sun)[a-z]*\.?\s+
    (?P<d1>\d{1,2})\s+(?P<m1>[A-Za-z]{3,9})\.?\s+(?P<y1>\d{4})\s+
    (?P<h1>\d{1,2}):(?P<n1>\d{2})
    (?:\s*[-\u2013\u2014]\s*
        (?:(?P<wd2>Mon|Tue|Wed|Thu|Fri|Sat|Sun)[a-z]*\.?\s+
           (?P<d2>\d{1,2})\s+(?P<m2>[A-Za-z]{3,9})\.?\s+(?P<y2>\d{4})\s+)?
        (?P<h2>\d{1,2}):(?P<n2>\d{2})
    )?
""", re.I | re.X)
TT_CARD_ENDS_RE = re.compile(
    r"\b(?:Event details|Select tickets|Sold out|Book now|Get tickets)\b", re.I)
TT_EIRCODE_RE = re.compile(r"[,\s]*\b[A-Z]\d{2}\s?[A-Z0-9]{4}\b\s*$")
GFS_SOCIETY = "Galway Film Society"
GFS_SMALL_WORDS = {"a", "an", "and", "as", "at", "but", "by", "for", "in",
                   "of", "on", "or", "the", "to", "vs"}
# capitals that really are initials, so stay capitals; every OTHER shouted
# word - including short ones like MY, ME or IT - is capitalised normally
GFS_ACRONYMS = {"DJ", "TV", "UK", "US", "USA", "USSR", "UN", "EU", "MC",
                "OK", "AI", "CD", "DVD", "BBC", "RTE", "FBI", "CIA", "JFK"}
GFS_TIMES_RE = re.compile(
    r"Screening times?\s+(?:is|are)\s*[:\-]?\s*(.+?)(?:\.(?=\s|$|[A-Z])|$)", re.I)


def clock12(hour, minute):
    h, suffix = hour % 12 or 12, "am" if hour < 12 else "pm"
    return f"{h}{suffix}" if minute == 0 else f"{h}:{minute:02d}{suffix}"


def tt_cards(soup, source):
    """The events on a Ticket Tailor organiser page, as plain dicts."""
    org = urlparse(source["url"]).path.rstrip("/").rsplit("/", 1)[-1]
    id_re = re.compile(r"/events/" + re.escape(org) + r"/(\d+)")
    seen, cards = set(), []
    for a in soup.find_all("a", href=True):
        m = id_re.search(a["href"])
        title = clean(a.get_text(" "))
        if (not m or not title or m.group(1) in seen
                or title.lower() in ("event details", "select tickets")):
            continue
        seen.add(m.group(1))
        node = a
        while node.parent is not None and node.parent.name not in (
                "body", "html", "[document]"):
            ids = {id_re.search(x["href"]).group(1)
                   for x in node.parent.find_all("a", href=True)
                   if id_re.search(x["href"])}
            if len(ids) > 1:
                break
            node = node.parent
        text = clean(node.get_text(" "))
        dm = TT_STAMP_RE.search(text)
        if not dm:
            continue
        mon1 = MONTHS.get(dm["m1"].lower()[:3])
        try:
            start = date(int(dm["y1"]), mon1, int(dm["d1"]))
        except (TypeError, ValueError):
            continue
        end, t2 = None, None
        if dm["h2"]:
            t2 = (int(dm["h2"]), int(dm["n2"]))
            if dm["d2"]:
                try:
                    e = date(int(dm["y2"]), MONTHS.get(dm["m2"].lower()[:3]),
                             int(dm["d2"]))
                    end = e if e != start else None
                except (TypeError, ValueError):
                    pass
        venue = clean(TT_CARD_ENDS_RE.split(text[dm.end():])[0]).strip(" ,-")
        venue = TT_EIRCODE_RE.sub("", venue).strip(" ,-")
        cards.append({
            "title": title, "start": start, "end": end,
            "t1": (int(dm["h1"]), int(dm["n1"])), "t2": t2,
            "wd1": dm["wd1"].capitalize(),
            "wd2": dm["wd2"].capitalize() if dm["wd2"] else None,
            "venue": venue,
            "sold": bool(re.search(r"\bsold out\b", text, re.I)),
            "url": urljoin(source["url"], a["href"]).split("?")[0]})
    return cards


def gfs_title(raw):
    """'GFS: MY FATHER'S SHADOW' -> "My Father's Shadow". Only words that
    are entirely in capitals are touched, so mixed-case text such as
    '(additional screening)' is left as written; known acronyms like
    DJ or TV stay capitals; small words (of, the, and...) go lower-case."""
    t = re.sub(r"^\s*GFS\s*:\s*", "", raw, flags=re.I)
    out, prev = [], ""
    for i, w in enumerate(t.split(" ")):
        core = re.sub(r"[^A-Za-z\u00c0-\u00ff]", "", w)
        if core and core.isupper():
            low = core.lower()
            after_colon = prev.endswith(":")
            if i > 0 and low in GFS_SMALL_WORDS and not after_colon:
                w = w.lower()
            elif core not in GFS_ACRONYMS:
                w = re.sub(r"[A-Za-z\u00c0-\u00ff]", lambda m: m.group().upper(),
                           w.lower(), count=1)
        out.append(w)
        prev = w
    return " ".join(out)


def gfs_screening_times(url):
    """The 'Screening times are ...' sentence from an event's own page, or
    None. Smallest page blocks are tried first so the match comes from the
    paragraph itself, not a wrapper that has swallowed the paragraphs after
    it."""
    try:
        page = fetch(url)
    except Exception as exc:
        print(f"  could not read screening times at {url}: {exc}", file=sys.stderr)
        time.sleep(0.4)
        return None
    time.sleep(0.4)
    blocks = sorted(page.find_all(["p", "li", "div"]),
                    key=lambda b: len(b.get_text()))
    for block in blocks:
        m = GFS_TIMES_RE.search(clean(block.get_text()))
        if m:
            t = m.group(1).strip(" .")
            if 0 < len(t) <= 80:
                return t
    return None


def parse_gfs(soup, source):
    events = []
    for c in tt_cards(soup, source):
        if c["end"] and c["t2"]:
            fallback = (f"{c['wd1']} {clock12(*c['t1'])} \u2013 "
                        f"{c['wd2']} {clock12(*c['t2'])}")
        else:
            fallback = clock12(*c["t1"])
        events.append(make_event(
            source, f"{gfs_title(c['title'])} ({GFS_SOCIETY})", c["start"],
            end_date=c["end"].isoformat() if c["end"] else None,
            time=gfs_screening_times(c["url"]) or fallback,
            url=c["url"], venue=c["venue"] or source["venue"],
            category="Film", sold_out=c["sold"]))
    return events


# ------------------------------------------------------- Galway Arts Centre
#
# galwayartscentre.ie/whats-on/ is a WordPress site. Its "Upcoming" list is
# one card per event - types, a date or date range, the venue, a title and a
# link to the event's own page - and each event page has an "Event Details"
# block (Date, Time, Location, Ticketing...) plus a booking link. The listing
# supplies the set of events and their dates; each upcoming event's own page
# then supplies the time, the location, the booking link and, if the card
# doesn't carry it, the title. Built from the pages' TEXT (links, separators,
# labels) rather than their CSS classes, so a theme tweak is less likely to
# break it.
#
# Standing programmes are left out. The centre lists multi-year 'Archive'
# projects (e.g. 2026-2031) and a weekly 'Gallery Lates' umbrella that runs
# for twenty months. Those aren't events you can attend, and as 'ongoing'
# cards they'd sit at the top of every day for years - so anything of type
# Archive, or lasting more than GAC_MAX_SPAN_DAYS, is skipped. (Individual
# Gallery Lates evenings are listed as their own events once scheduled.)

GAC_MAX_SPAN_DAYS = 180
GAC_NON_GENRE = {"festival", "event", "project", "tour", "archive"}
GAC_TYPE_NAMES = {"screening": "Film"}
GAC_DATE = r"\d{1,2}\s+[A-Za-z]{3,9}\s+\d{4}"
GAC_RANGE_RE = re.compile(
    "(" + GAC_DATE + r")(?:\s*[\u2013\u2014-]\s*(" + GAC_DATE + r"))?")
GAC_LABELS = {"Date", "Time", "Duration", "Location", "Ages", "Ticketing",
              "Event Type", "Additional Info", "Price", "Booking"}
GAC_BOOK_WORDS = {"BOOKING", "BOOK NOW", "BOOK TICKETS", "BOOK"}


def gac_date(text):
    m = re.match(r"\s*(\d{1,2})\s+([A-Za-z]{3})[A-Za-z]*\s+(\d{4})", text or "")
    mon = MONTHS.get(m.group(2).lower()) if m else None
    if not mon:
        return None
    try:
        return date(int(m.group(3)), mon, int(m.group(1)))
    except ValueError:
        return None


def gac_venue(text):
    """'Galway Arts Centre | 47 Dominick Street' and '47 Dominick Street'
    are the gallery; the Nuns Island theatre is its own space; 'Off-site:
    Please see event info' (or nothing) means unknown -> None."""
    low = (text or "").lower()
    if not low or "off-site" in low:
        return None
    if "nuns island" in low or "nun's island" in low:
        return "Nuns Island Theatre"
    if "dominick" in low or "galway arts centre" in low:
        return "Galway Arts Centre"
    return clean(text)


def gac_event_page(page):
    """(title, details, booking_url) from an event's own page. The Event
    Details block is a run of label/value text: 'Date' '15/10/2026' 'Time'
    '7pm-8pm' 'Location' ... up to 'Social Share'."""
    h1 = page.find("h1")
    title = clean(h1.get_text(" ")) if h1 else None
    strings = list(page.stripped_strings)
    details, label = {}, None
    try:
        start = strings.index("Event Details") + 1
    except ValueError:
        start = len(strings)
    for s in strings[start:]:
        if s == "Social Share":
            break
        if s in GAC_LABELS:
            label = s
            details[label] = []
        elif label:
            details[label].append(s)
    details = {k: clean(" ".join(v)) for k, v in details.items()}
    booking = next((a["href"] for a in page.find_all("a", href=True)
                    if clean(a.get_text()).upper() in GAC_BOOK_WORDS
                    and re.fullmatch(r"https?://\S+", a["href"].strip())), None)
    return title, details, booking


def parse_gac(soup, source):
    events, seen = [], set()
    for a in soup.find_all("a", href=True):
        path = urlparse(a["href"]).path
        if not re.fullmatch(r"/whats-on/[^/]+/?", path):
            continue    # the nav's own 'What's On' links have no slug
        text = clean(a.get_text(" "))
        if "read more" not in text.lower() or path in seen:
            continue    # image-only duplicates of a card link
        seen.add(path)
        dm = GAC_RANGE_RE.search(text)
        start = gac_date(dm.group(1)) if dm else None
        if not start:
            continue
        end = gac_date(dm.group(2)) if dm.group(2) else None
        types = [clean(t) for t in re.split(
            r"\s*\u2044\s*|\s+/\s+", text[:dm.start()].strip(" \u2044/|")) if clean(t)]
        # 'Types / date | venue | title description Read More'
        parts = [p.strip() for p in text[dm.end():].split("|")]
        if parts and parts[0] == "":
            parts = parts[1:]
        card_venue = parts[0] if len(parts) >= 2 else None
        heading = a.find(re.compile(r"^h[1-6]$"))
        title = clean(heading.get_text(" ")) if heading else None
        url = urljoin(source["url"], a["href"]).split("?")[0]

        if "archive" in (t.lower() for t in types):
            continue
        last = end or start
        if last < TODAY or (last - start).days > GAC_MAX_SPAN_DAYS:
            continue

        details, booking = {}, None
        try:
            page_title, details, booking = gac_event_page(fetch(url))
            title = title or page_title
        except Exception as exc:
            print(f"  could not read {url}: {exc}", file=sys.stderr)
        time.sleep(0.4)
        if not title:
            print(f"  skipping {url}: no title found", file=sys.stderr)
            continue

        # the event page's own date is the more reliable of the two
        page_dates = [date(int(y), int(m), int(d)) for d, m, y in
                      re.findall(r"(\d{1,2})/(\d{1,2})/(\d{4})", details.get("Date", ""))]
        if page_dates:
            start, end = page_dates[0], (page_dates[-1] if len(page_dates) > 1 else None)
        if end == start:
            end = None
        if (end or start) < TODAY:
            continue

        genres = []
        for t in types or [t.strip() for t in details.get("Event Type", "").split(",") if t.strip()]:
            if t.lower() in GAC_NON_GENRE:
                continue
            genres.append(GAC_TYPE_NAMES.get(t.lower(), t))
        t = details.get("Time")
        events.append(make_event(
            source, title, start,
            end_date=end.isoformat() if end else None,
            time=t if t and len(t) <= 40 else None,
            url=url, booking_url=booking,
            category=", ".join(dict.fromkeys(genres)) or None,
            venue=(gac_venue(details.get("Location")) or gac_venue(card_venue)
                   or THT_OFFSITE_VENUE)))
    if not seen:
        raise ValueError("no event cards found - the layout has changed, "
                         "or the page is empty or blocked")
    return events


# ---------------------------------------------------- Mick Lally Theatre
#
# druid.ie/about/the-mick-lally-theatre/whats-on lists "upcoming events by
# Druid and visiting companies" at the theatre: each is an image, a title and
# a date line like 'Fri 13 - Sat 14 February 2026' (the year is always given;
# the month appears once, at the END of a range). There are no times and no
# genres on the page. Druid's main site shows nothing for these because many
# of them are visiting companies' own shows.
#
# Built to need very little from the markup: it finds each DATE LINE, climbs
# from it to the largest element holding just that one date (the event's
# card), takes the title from a heading in the card or else the text just
# above the date, and the link from the card if it has one (otherwise the
# What's On page itself). The page's intro, nav and mailto links never get
# mistaken for an event's link.

MLT_DAY_RE = re.compile(
    r"(?:(?:Mon|Tue|Wed|Thu|Fri|Sat|Sun)[a-z]*\.?\s+)?(\d{1,2})"
    r"(?:\s+([A-Za-z]{3,9}))?(?:\s+(\d{4}))?")
MLT_MONTH_RE = re.compile(r"\b(?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\b", re.I)
MLT_COMEDY_RE = re.compile(r"\bcomed(?:y|ian|ians)\b|\bstand-?up\b", re.I)
MLT_MUSIC_RE = re.compile(
    r"\b(?:concert|gig|live music|album launch|trio|quartet|orchestra|sessions?)\b", re.I)


def mlt_is_date_line(t):
    return bool(len(t) <= 80
                and re.match(r"(?:Mon|Tue|Wed|Thu|Fri|Sat|Sun)[a-z]*\.?\s+\d{1,2}\b", t)
                and re.search(r"\b(?:19|20)\d{2}\b", t) and MLT_MONTH_RE.search(t))


def _mk_date(y, m, d):
    try:
        return date(y, m, d)
    except (TypeError, ValueError):
        return None


def mlt_parse_dates(line):
    """'Fri 13 - Sat 14 February 2026' -> [(Feb 13, Feb 14)]. A dash joins a
    run into one range; a comma, '&' or 'and' starts a separate run (one
    event each, rather than a false continuous range). A month or year
    missing from an earlier date is borrowed from the one after it, and
    corrected where that can't be right - 'Sat 31 - Mon 2 November' starts
    in October, '30 Dec - 2 Jan 2027' starts the year before."""
    toks, last = [], 0
    for m in MLT_DAY_RE.finditer(line):
        word = (m.group(2) or "")[:3].lower()
        toks.append({"d": int(m.group(1)), "m": MONTHS.get(word) if word else None,
                     "y": int(m.group(3)) if m.group(3) else None,
                     "sep": line[last:m.start()], "borrowed": False})
        last = m.end()
    cm = cy = None
    for t in reversed(toks):
        if t["m"] is None and cm:
            t["m"], t["borrowed"] = cm, True
        elif t["m"]:
            cm = t["m"]
        if t["y"] is None:
            t["y"] = cy
        else:
            cy = t["y"]
    groups, cur = [], []
    for t in toks:
        if cur and re.search(r"[,&]|\band\b", t["sep"], re.I):
            groups.append(cur)
            cur = []
        cur.append(t)
    if cur:
        groups.append(cur)
    runs = []
    for g in groups:
        first, lastt = g[0], g[-1]
        end = _mk_date(lastt["y"], lastt["m"], lastt["d"])
        if not end:
            continue
        if first is lastt:
            runs.append((end, None))
            continue
        start = _mk_date(first["y"], first["m"], first["d"])
        if (start is None or start > end) and first["y"] and first["m"]:
            y, m = first["y"], first["m"]
            if first["borrowed"]:
                m, y = (12, y - 1) if m == 1 else (m - 1, y)
            else:
                y -= 1
            start = _mk_date(y, m, first["d"])
        if start and start <= end:
            runs.append((start, end if end != start else None))
    return runs


def mlt_category(title):
    if MLT_COMEDY_RE.search(title):
        return "Comedy"
    if MLT_MUSIC_RE.search(title):
        return "Music"
    return "Theatre"


def _visible_strings(el):
    return [s for s in el.find_all(string=True)
            if not isinstance(s, Comment) and s.parent.name not in ("script", "style")]


def parse_mick_lally(soup, source):
    date_nodes = [s for s in _visible_strings(soup) if mlt_is_date_line(clean(s))]
    if not date_nodes:
        raise ValueError("no date lines found - the layout has changed, "
                         "or the page is empty or blocked")

    def dates_in(el):
        return sum(1 for s in _visible_strings(el) if mlt_is_date_line(clean(s)))

    events = []
    for node in date_nodes:
        card = node.parent
        while (card.parent is not None
               and card.parent.name not in ("body", "html", "[document]")
               and dates_in(card.parent) == 1):
            card = card.parent
        heading = card.find(re.compile(r"^h[1-6]$"))
        title = clean(heading.get_text(" ")) if heading else None
        if not title or mlt_is_date_line(title):
            strings = _visible_strings(card)
            idx = next((i for i, s in enumerate(strings) if s is node), 0)
            prev = [clean(s) for s in strings[:idx] if clean(s)]
            title = prev[-1] if prev else None
        if not title or len(title) < 2:
            continue
        link = (node.find_parent("a", href=True)
                or (card if card.name == "a" and card.get("href") else None)
                or card.find("a", href=True))
        href = link["href"].strip() if link else ""
        url = (urljoin(source["url"], href)
               if href and not re.match(r"(?i)(?:mailto|tel|javascript):|#", href)
               else source["url"])
        for start, end in mlt_parse_dates(clean(node)):
            events.append(make_event(
                source, title, start,
                end_date=end.isoformat() if end else None,
                url=url, category=mlt_category(title)))
    return events


SOURCES = [
    {"name": "tht", "venue": "Town Hall Theatre", "town": "Galway City",
     "county": "Galway", "url": "https://tht.ie/all",
     "parser": parse_tht},
    {"name": "monroes", "venue": "Monroe's Live", "town": "Galway City",
     "county": "Galway", "url": "https://monroes.ie/pages/gigs",
     "parser": parse_monroes},
    {"name": "roisindubh", "venue": "R\u00f3is\u00edn Dubh", "town": "Galway City",
     "county": "Galway", "url": "https://roisindubh.net/listings/",
     "parser": parse_roisindubh, "custom_fetch": True},
    {"name": "gfs", "venue": "Eye Cinema", "town": "Galway City",
     "county": "Galway",
     "url": "https://www.tickettailor.com/events/galwayfilmsociety",
     "parser": parse_gfs},
    {"name": "gac", "venue": "Galway Arts Centre", "town": "Galway City",
     "county": "Galway", "url": "https://www.galwayartscentre.ie/whats-on/",
     "parser": parse_gac, "quiet_if_empty": True},
    {"name": "mick_lally", "venue": "Mick Lally Theatre", "town": "Galway City",
     "county": "Galway",
     "url": "https://www.druid.ie/about/the-mick-lally-theatre/whats-on",
     "parser": parse_mick_lally},
]


# --------------------------------------------------- county/town whitelist

# Galway is being built one venue at a time, starting with the city
# itself. Add towns here as sources needing them get added, same
# incremental pattern as the North West's own whitelist.
ALLOWED_COUNTIES = {"Galway"}
COUNTY_ALIASES = {}
COUNTY_CANONICAL = {c.lower(): c for c in ALLOWED_COUNTIES}
COUNTY_CANONICAL.update({k.lower(): v for k, v in COUNTY_ALIASES.items()})

# Everything in Galway City - Salthill included, which is a suburb of the
# city - is shown as 'Galway City', so the Locations filter doesn't just
# repeat the county ('Galway' / 'Galway'). Towns and villages further out
# get their own entry here, added as sources need them (the same incremental
# pattern as the North West's whitelist).
TOWN_TO_COUNTY = {
    "galway": "Galway", "galway city": "Galway",
    "rosscahill": "Galway", "oranmore": "Galway", "barna": "Galway",
    "spiddal": "Galway", "an spid\u00e9al": "Galway", "kinvara": "Galway",
    "athenry": "Galway", "tuam": "Galway", "loughrea": "Galway",
    "ballinasloe": "Galway", "clifden": "Galway", "oughterard": "Galway",
    "gort": "Galway", "claregalway": "Galway", "moycullen": "Galway",
    "headford": "Galway", "portumna": "Galway",
    "inis o\u00edrr": "Galway", "inis m\u00f3r": "Galway", "inis me\u00e1in": "Galway",
}
_COUNTY_NAME_TOWNS = {"galway"}

# A venue can sit outside the city even though its source is based in it -
# e.g. THT's Baboro programme includes a forest-school workshop at Brigit's
# Garden in Rosscahill, about 40 minutes' drive away. Venue name
# (lower-case) -> the town to show for events there.
GALWAY_VENUE_TOWNS = {
    "brigid's garden": "Rosscahill",
}
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
# lowest number wins when two sources list the same event - the venue's own
# listing (the Arts Centre, Mick Lally Theatre, then THT) beats a promoter that cross-lists them
SOURCE_PRIORITY = {"gac": 0, "mick_lally": 1, "tht": 2, "monroes": 3,
                   "roisindubh": 4, "gfs": 5}
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
            if source.get("custom_fetch"):
                soup, found = None, source["parser"](source)
            else:
                soup = fetch(source["url"])
                found = source["parser"](soup, source)
            print(f"{source['venue']}: {len(found)} events")
            if not found and source.get("quiet_if_empty"):
                print("  (nothing upcoming - normal for this source, not a failure)")
                consecutive_failures[source["name"]] = 0
                continue
            if not found:
                extra = (f" Page preview: {soup.get_text(' ', strip=True)[:200]!r}"
                         if soup is not None else "")
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
        venue_town = GALWAY_VENUE_TOWNS.get((ev.get("venue") or "").strip().lower())
        ev["town"] = nearest_known_town(venue_town or ev.get("town"))
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
