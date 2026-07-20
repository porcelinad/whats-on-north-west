"""
North West What's On - event scraper
Scrapes cultural venues in Donegal / Sligo / Derry into docs/events.json
and sends an ntfy push notification when new events appear.

Each venue has its own small parser. They all work the same way:
walk the page top-to-bottom, spot event links and date text, and pair
them up. This avoids relying on fragile CSS class names, so minor site
redesigns are less likely to break things.
"""

import csv
import hashlib
import json
import os
import re
import sys
import time
import unicodedata
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import quote

import requests
from bs4 import BeautifulSoup, NavigableString

# ---------------------------------------------------------------- config

ROOT = Path(__file__).resolve().parent.parent
DATA_FILE = ROOT / "docs" / "events.json"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-GB,en;q=0.9",
}

# Eventbrite specifically gets a fuller browser-like header set (used only
# by fetch_text, not the shared fetch() the WordPress venues use) - mixing
# these into every request made some sites' bot-protection MORE suspicious,
# since a Referer of google.com alongside Sec-Fetch-Site: none is actually
# self-contradictory and can look like a spoofed request.
EVENTBRITE_HEADERS = dict(HEADERS, **{
    "Accept-Encoding": "gzip, deflate, br",
    "Connection": "keep-alive",
    "Upgrade-Insecure-Requests": "1",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Sec-Fetch-User": "?1",
})
TIMEOUT = 30
NOW = datetime.now(timezone.utc)
TODAY = NOW.date()

NTFY_TOPIC = os.environ.get("NTFY_TOPIC", "").strip()
PAGE_URL = os.environ.get("PAGE_URL", "").strip()

MONTHS = {}
for i, name in enumerate(
    ["january", "february", "march", "april", "may", "june", "july",
     "august", "september", "october", "november", "december"], start=1
):
    MONTHS[name] = i
    MONTHS[name[:3]] = i

GENRE_WORDS = {
    "comedy", "dance", "drama", "exhibition", "family", "featured", "film",
    "in-house productions", "lasta", "music", "musical", "opera", "schools",
    "talks/spoken word", "spoken word", "theatre", "trad week", "variety",
    "workshop", "community arts", "earagail arts festival", "literature",
    "art lecture", "live event",
}

SKIP_LINK_TEXT = {
    "", "more info", "more", "less", "book now", "book online",
    "book online now", "view all", "view all events", "what's on",
    "whats on", "upcoming events", "events", "learn more",
}


# ---------------------------------------------------------------- helpers

def fetch(url):
    last_exc = None
    for attempt in range(3):
        try:
            r = requests.get(url, headers=HEADERS, timeout=TIMEOUT)
            r.raise_for_status()
            return BeautifulSoup(r.text, "lxml")
        except Exception as exc:
            last_exc = exc
            if attempt < 2:
                time.sleep(4 * (attempt + 1))
    raise last_exc


def clean(text):
    return " ".join(str(text).split())


def infer_year(month, day):
    """Venue listings only show current/upcoming events, so if a date
    without a year would fall well in the past, it means next year."""
    for year in (TODAY.year, TODAY.year + 1):
        try:
            d = date(year, month, day)
        except ValueError:
            continue
        if d >= TODAY - timedelta(days=90):
            return d
    return None


def genre_from_text(text):
    """Return 'Comedy, Music' etc. if a text node is purely a genre list."""
    t = clean(text).strip("|").strip()
    if not t or len(t) > 80:
        return None
    parts = [p.strip() for p in t.split(",") if p.strip()]
    if parts and all(p.lower() in GENRE_WORDS for p in parts):
        keep = [p for p in parts if p.lower() not in ("featured", "live event")]
        return ", ".join(keep) or None
    return None


def walk(soup):
    """Yield ('text', str) and ('link', href, text) in document order."""
    body = soup.body or soup
    for node in body.descendants:
        if isinstance(node, NavigableString):
            t = clean(node)
            if t:
                yield ("text", t, None)
        elif getattr(node, "name", None) == "a":
            yield ("link", node.get("href", ""), clean(node.get_text(" ")))


def make_event(source, title, start, **extra):
    ev = {
        "source": source["name"],
        "venue": source["venue"],
        "town": source["town"],
        "county": source["county"],
        "title": title,
        "date": start.isoformat(),
    }
    ev.update({k: v for k, v in extra.items() if v})
    return ev


# ---------------------------------------------------------------- parsers

def parse_an_grianan(soup, source):
    """angrianan.com/events/ - date lines appear BEFORE each title link."""
    date_re = re.compile(
        r"(?:Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday)\s+"
        r"([A-Za-z]+)\s+(\d{1,2})\b", re.I)
    events, dates, booking, genre = [], [], None, None
    for kind, a, b in walk(soup):
        if kind == "text":
            g = genre_from_text(a)
            if g:
                genre = g
            for m in date_re.finditer(a):
                mon = MONTHS.get(m.group(1).lower())
                if mon:
                    d = infer_year(mon, int(m.group(2)))
                    if d:
                        dates.append(d)
        else:  # link
            href, text = a, b
            if "ticketsolve.com" in href:
                booking = href
            elif "/event/" in href and text.lower() not in SKIP_LINK_TEXT:
                if dates:
                    events.append(make_event(
                        source, text, dates[0],
                        end_date=dates[1].isoformat() if len(dates) > 1 else None,
                        url=href, booking_url=booking, category=genre))
                dates, booking, genre = [], None, None
    return events


def parse_rcc(soup, source):
    """regionalculturalcentre.com/whats-on/ - dates come AFTER each title."""
    date_re = re.compile(
        r"(?:Mon|Tue|Wed|Thu|Fri|Sat|Sun)\s+([A-Za-z]{3})\s+(\d{1,2}),\s*"
        r"(\d{1,2}:\d{2}\s*[ap]m)", re.I)
    link_re = re.compile(r"/(events|exhibitions)/[^/]+/?$")
    events, current, genre = [], None, None

    def finalise():
        if current and current.get("_start"):
            events.append(make_event(
                source, current["title"], current["_start"],
                end_date=current.get("_end"), time=current.get("_time"),
                url=current["url"], category=current.get("cat")))

    for kind, a, b in walk(soup):
        if kind == "text":
            g = genre_from_text(a)
            if g:
                genre = g
            m = date_re.search(a)
            if m and current:
                mon = MONTHS.get(m.group(1).lower())
                d = infer_year(mon, int(m.group(2))) if mon else None
                if d and not current.get("_start"):
                    current["_start"] = d
                    current["_time"] = m.group(3).lower()
                elif d:
                    current["_end"] = d.isoformat()
        else:
            href, text = a, b
            if link_re.search(href) and text.lower() not in SKIP_LINK_TEXT:
                finalise()
                current = {"title": text, "url": href, "cat": genre}
                genre = None
    finalise()
    return events


def parse_balor(soup, source):
    """balorartscentre.com homepage - '10 Jul 26' dates AFTER title links."""
    date_re = re.compile(r"\b(\d{1,2})\s+([A-Za-z]{3})\s+(\d{2})\b")
    events, current = [], None
    for kind, a, b in walk(soup):
        if kind == "text":
            m = date_re.search(a)
            if m and current:
                mon = MONTHS.get(m.group(2).lower())
                if mon:
                    try:
                        d = date(2000 + int(m.group(3)), mon, int(m.group(1)))
                    except ValueError:
                        d = None
                    if d:
                        events.append(make_event(
                            source, current["title"], d, url=current["url"]))
                current = None
        else:
            href, text = a, b
            if "?event=" in href and text.lower() not in SKIP_LINK_TEXT:
                current = {"title": text, "url": href}
    return events


def fetch_text(url):
    """Like fetch(), but returns raw response text instead of parsed HTML -
    needed for Eventbrite, where we read an embedded JSON blob rather than
    the rendered markup. Uses EVENTBRITE_HEADERS, not the shared HEADERS."""
    last_exc = None
    for attempt in range(3):
        try:
            r = requests.get(url, headers=EVENTBRITE_HEADERS, timeout=TIMEOUT)
            r.raise_for_status()
            return r.text
        except Exception as exc:
            last_exc = exc
            if attempt < 2:
                time.sleep(4 * (attempt + 1))
    raise last_exc


def extract_server_data(html):
    """Eventbrite's destination-search pages embed the real results as
    `window.__SERVER_DATA__ = {...}` - a plain JSON object sitting in the
    raw HTML (rendered server-side for SEO), so no browser/JS execution
    is needed to read it. This walks the braces to find where that
    object ends, since it's followed by more JS, not a clean delimiter."""
    marker = "window.__SERVER_DATA__ = "
    start = html.index(marker) + len(marker)
    depth = 0
    in_str = False
    esc = False
    end = None
    for i in range(start, len(html)):
        c = html[i]
        if in_str:
            if esc:
                esc = False
            elif c == "\\":
                esc = True
            elif c == '"':
                in_str = False
            continue
        if c == '"':
            in_str = True
        elif c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                end = i + 1
                break
    return json.loads(html[start:end])


EVENTBRITE_MAX_PAGES = 10
EVENTBRITE_ALLOWED_CATEGORIES = {
    "Music", "Performing & Visual Arts", "Community & Culture", "Film & Media",
}

# GitHub Actions' server IPs appear to be blocked by Eventbrite specifically
# (a 405 even with full browser-like headers - consistent with an IP-range
# block rather than a header/UA check). As a fallback, route through a
# public raw-HTML proxy that has a different IP range. Direct is always
# tried first, so if Eventbrite's block ever lifts, this quietly stops
# being needed. This is an extra external dependency and could itself
# become unreliable - if so, dropping Eventbrite entirely is reasonable.
EVENTBRITE_PROXY_TEMPLATE = "https://api.allorigins.win/raw?url={}"


def fetch_eventbrite_page(url):
    try:
        return fetch_text(url)
    except Exception as direct_exc:
        proxy_url = EVENTBRITE_PROXY_TEMPLATE.format(quote(url, safe=""))
        try:
            print(f"  direct fetch blocked ({direct_exc}); trying proxy...")
            return fetch_text(proxy_url)
        except Exception:
            raise direct_exc  # the direct error is more informative to log


def parse_eventbrite(source):
    """Eventbrite 'discover' pages for a region (e.g. eventbrite.ie/d/
    ireland--donegal/all-events/) list thousands of results across many
    pages, most of it irrelevant (sports, recurring workshops, religious
    events, and nearby-but-out-of-county venues near the border). We page
    through a bounded number of pages and keep only events that are: in
    the target region specifically, not online-only, and tagged with a
    cultural category."""
    events = []
    for page in range(1, EVENTBRITE_MAX_PAGES + 1):
        page_url = source["url"] if page == 1 else f"{source['url']}?page={page}"
        html = fetch_eventbrite_page(page_url)
        data = extract_server_data(html)
        ev_block = data.get("search_data", {}).get("events", {})
        results = ev_block.get("results", [])
        if not results:
            break
        for r in results:
            if r.get("is_online_event"):
                continue
            cats = {t["display_name"] for t in r.get("tags", [])
                    if t.get("prefix") == "EventbriteCategory"}
            if not cats & EVENTBRITE_ALLOWED_CATEGORIES:
                continue
            venue = r.get("primary_venue") or {}
            addr = venue.get("address") or {}
            if addr.get("region") != source["region_filter"]:
                continue
            try:
                start_date = date.fromisoformat(r["start_date"])
            except (KeyError, ValueError, TypeError):
                continue
            end_date = None
            if r.get("end_date") and r["end_date"] != r["start_date"]:
                end_date = r["end_date"]
            events.append(make_event(
                source, r.get("name", "").strip(), start_date,
                end_date=end_date, time=r.get("start_time"),
                url=r.get("url"), category=", ".join(sorted(cats)),
                venue=venue.get("name"), town=addr.get("city")))
        pag = ev_block.get("pagination", {})
        if page >= pag.get("page_count", 1):
            break
    return events



def parse_abbey(soup, source):
    """abbeycentre.ie homepage - titles link to Ticketsolve; the exact ISO
    date is embedded in each event's social-share links (/edate/YYYY-MM-DD)."""
    edate_re = re.compile(r"/edate/(\d{4}-\d{2}-\d{2})")
    eventer_re = re.compile(r"https?://abbeycentre\.ie/eventer/[^/&\s]+")
    events, current = [], None
    for kind, a, b in walk(soup):
        if kind != "link":
            continue
        href, text = a, b
        if ("ticketsolve.com/ticketbooth/shows/" in href
                and text.lower() not in SKIP_LINK_TEXT
                and re.search(r"shows/\d+", href)):
            current = {"title": text, "booking": href}
        elif current:
            m = edate_re.search(href)
            if m:
                try:
                    d = date.fromisoformat(m.group(1))
                except ValueError:
                    d = None
                page = eventer_re.search(href)
                if d:
                    events.append(make_event(
                        source, current["title"], d,
                        url=page.group(0) if page else current["booking"],
                        booking_url=current["booking"]))
                current = None
    return events


MANUAL_CSV_PATH = ROOT / "scraper" / "manual-imports" / "eventbrite.csv"


WEEKDAY_INDEX = {name: i for i, name in enumerate(
    ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"])}


def parse_eventbrite_date_text(text, trust_relative=True):
    """Parses Eventbrite's date text as found in a webscraper.io export:
    an explicit date ('Fri 31 Jul, 18:30'), or a relative one ('Today at
    09:00', 'Tomorrow at 19:30', 'Thursday at 11:00' - a bare weekday
    means the next occurrence of that day). Relative dates are only
    parsed when trust_relative is True - see csv_freshness_check below
    for why that matters."""
    if not text:
        return None, None
    t = clean(text)

    if trust_relative:
        m = re.match(r"today at (\d{1,2}:\d{2})", t, re.I)
        if m:
            return TODAY, m.group(1)

        m = re.match(r"tomorrow at (\d{1,2}:\d{2})", t, re.I)
        if m:
            return TODAY + timedelta(days=1), m.group(1)

        m = re.match(r"([A-Za-z]+)\s+at\s+(\d{1,2}:\d{2})", t)
        if m:
            wd = WEEKDAY_INDEX.get(m.group(1).lower())
            if wd is not None:
                delta = (wd - TODAY.weekday()) % 7 or 7
                return TODAY + timedelta(days=delta), m.group(2)

    m = re.match(r"[A-Za-z]+\s+(\d{1,2})\s+([A-Za-z]+),?\s+(\d{1,2}:\d{2})", t)
    if m:
        mon = MONTHS.get(m.group(2).lower()[:3])
        if mon:
            d = infer_year(mon, int(m.group(1)))
            if d:
                return d, m.group(3)

    return None, None


def slugify(title):
    """Guesses Eventbrite's own URL slug from a title, e.g. 'MacGill
    Summer School 2026' -> 'macgill-summer-school-2026'. Won't always be
    exactly right (Eventbrite occasionally adds words not in the visible
    title), but lands on the real event page far more often than not -
    and when it's wrong, eventbrite.ie/d/ireland--donegal/<slug>/ still
    lands on Eventbrite's Donegal search, which is what we'd link to
    anyway, so there's no downside to trying. Accented characters (common
    in Irish-language titles, e.g. 'Tír') are transliterated to their
    plain-ASCII equivalent rather than dropped, matching what Eventbrite
    itself does ('Tír' -> 'tir', not 't-r')."""
    t = title.lower()
    t = unicodedata.normalize("NFKD", t).encode("ascii", "ignore").decode("ascii")
    t = t.replace("'", "")
    t = re.sub(r"[^a-z0-9]+", "-", t)
    return t.strip("-")


CSV_STALE_AFTER_DAYS = 1


def csv_freshness_check(prev_state):
    """Tracks whether the manual Eventbrite CSV has changed since it was
    last read, using a content hash stored in our own persisted state -
    NOT file modification times, which git resets to checkout time on
    every run and are therefore useless for this. Returns True if the
    file is new or was last changed within CSV_STALE_AFTER_DAYS days -
    i.e. whether 'Today'/'Tomorrow'/bare-weekday text in it can still be
    trusted. Once a file goes stale, only its unambiguous explicit dates
    keep being used; relative-only rows are simply dropped rather than
    silently drifting onto the wrong day."""
    current_hash = hashlib.sha1(MANUAL_CSV_PATH.read_bytes()).hexdigest()[:12]
    prev_hash = prev_state.get("eventbrite_csv_hash")
    if current_hash != prev_hash:
        prev_state["eventbrite_csv_hash"] = current_hash
        prev_state["eventbrite_csv_since"] = NOW.strftime("%Y-%m-%dT%H:%MZ")
        return True
    since = prev_state.get("eventbrite_csv_since")
    if not since:
        prev_state["eventbrite_csv_since"] = NOW.strftime("%Y-%m-%dT%H:%MZ")
        return True
    since_date = datetime.strptime(since, "%Y-%m-%dT%H:%MZ").date()
    return (TODAY - since_date).days <= CSV_STALE_AFTER_DAYS


def parse_eventbrite_csv(source, prev_state=None):
    """Eventbrite blocks GitHub Actions' servers outright (see EVENTBRITE_*
    above), so this reads a CSV exported by hand from the webscraper.io
    browser extension instead - no network request, so nothing to block.
    Upload a fresh export to scraper/manual-imports/eventbrite.csv every
    so often (weekly is plenty), always overwriting the same filename.
    Returns None (not []) if no file has been uploaded yet, so the caller
    can tell 'nothing uploaded' apart from 'uploaded but empty/broken'."""
    if not MANUAL_CSV_PATH.exists():
        return None
    trust_relative = (csv_freshness_check(prev_state)
                      if prev_state is not None else True)
    events = []
    with MANUAL_CSV_PATH.open(encoding="utf-8-sig", newline="") as f:
        for row in csv.DictReader(f):
            title = clean(row.get("data") or row.get("data6") or "")
            if not title:
                continue
            start_date, time_str = parse_eventbrite_date_text(
                row.get("data2"), trust_relative)
            if not start_date:
                start_date, time_str = parse_eventbrite_date_text(
                    row.get("data11"), trust_relative)
            if not start_date:
                continue  # stale relative date, or missing entirely
            venue_text = clean(row.get("data5") or row.get("data13") or "")
            town = venue = None
            if "·" in venue_text:
                town, venue = (p.strip() for p in venue_text.split("·", 1))
            else:
                venue = venue_text or None
            slug = slugify(title)
            url = (f"https://www.eventbrite.ie/d/ireland--donegal/{slug}/"
                   if slug else source["url"])
            events.append(make_event(
                source, title, start_date, time=time_str,
                venue=venue, town=town, url=url))
    return events


EAF_DATE_RE = re.compile(
    r"(?:Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday)?\s*"
    r"(\d{1,2})(?:st|nd|rd|th)\s+([A-Za-z]+)(?:\s+(\d{4}))?", re.I)
EAF_TYPE_WORDS = {"live event", "exhibition", "project"}


def parse_eaf_date_text(text):
    """Extracts every date found in a line like 'Monday 13th - Friday
    17th July' or 'Saturday 9th January 2027', inferring the year when
    not stated explicitly."""
    found = []
    for m in EAF_DATE_RE.finditer(text):
        mon = MONTHS.get(m.group(2).lower()[:3])
        if not mon:
            continue
        day = int(m.group(1))
        if m.group(3):
            try:
                d = date(int(m.group(3)), mon, day)
            except ValueError:
                continue
        else:
            d = infer_year(mon, day)
        if d:
            found.append(d)
    return found


def parse_eaf_listing(soup, source):
    """eaf.ie/2026-events/ lists every festival event on one page: a
    genre link, then a title link, then date/time bullet lines, then an
    event-type label (Live Event / Exhibition / Project). 'Project'
    entries (artist residencies with no attendable date) are skipped."""
    events = []
    genre = None
    title = url = None
    pending_dates, pending_time = [], None

    def finalise(type_word):
        nonlocal title, url, genre, pending_dates, pending_time
        if title and url and type_word != "project" and pending_dates:
            start = pending_dates[0]
            end = pending_dates[-1] if len(pending_dates) == 2 else None
            events.append(make_event(
                source, title, start,
                end_date=end.isoformat() if end and end != start else None,
                time=pending_time, url=url, category=genre))
        title = url = None
        genre = None
        pending_dates, pending_time = [], None

    for kind, a, b in walk(soup):
        if kind == "link":
            href, text = a, b
            if "/genre/" in href and text:
                genre = text
            elif "/events/" in href and text and text.lower() not in SKIP_LINK_TEXT:
                title, url = text, href
        else:
            if a.lower() in EAF_TYPE_WORDS:
                finalise(a.lower())
                continue
            if title:
                dates = parse_eaf_date_text(a)
                if dates:
                    pending_dates.extend(dates)
                elif pending_dates and not pending_time:
                    pending_time = a
    return events


def parse_eaf_event_page(soup):
    """Each event's own page lists 'Location:' (town) and 'Venue:' (venue
    name) as plain labelled text - not present on the listing page."""
    town = venue = None
    pending_label = None
    for kind, a, b in walk(soup):
        if kind != "text":
            continue
        if a in ("Location:", "Venue:"):
            pending_label = a
            continue
        if pending_label == "Location:" and not town:
            town = a
        elif pending_label == "Venue:" and not venue:
            venue = a
        pending_label = None
    return town, venue


def parse_eaf(source):
    """Two-stage: scrape the listing page for what/when, then visit each
    event's own page for its venue (not shown on the listing page). This
    means ~1 + N requests where N is the number of live events/exhibitions
    - a small delay is added between the per-event requests to avoid
    hammering a small festival site's server all at once."""
    listing = fetch(source["url"])
    events = parse_eaf_listing(listing, source)
    for ev in events:
        try:
            detail = fetch(ev["url"])
            town, venue = parse_eaf_event_page(detail)
            if town:
                ev["town"] = town
            if venue:
                ev["venue"] = venue
        except Exception:
            pass  # keep the event with the festival's own name as venue
        time.sleep(0.4)
    return events


def parse_mcgrorys(soup, source):
    """mcgrorys.ie/entertainment - each card is an image link to the
    event's own page, immediately followed by its title as a heading
    (not itself a link), a description paragraph, a duplicate 'Read
    More' link, a booking link, then an 'Event Date DD Mon YY' line.
    The page never states a genre, but McGrory's is overwhelmingly a
    music venue, so every event is tagged category='Music' - if that
    ever stops being true, this is the line to revisit."""
    event_link_re = re.compile(r"/entertainment/\d+-\d+/?$")
    date_re = re.compile(r"(\d{1,2})\s+([A-Za-z]{3})\s+(\d{2})\b")
    events = []
    url = title = None
    awaiting_title = False
    for kind, a, b in walk(soup):
        if kind == "link":
            href, text = a, b
            if event_link_re.search(href) and href != url:
                url = href
                awaiting_title = True
        else:
            if awaiting_title and not title:
                if "read more" in a.lower():
                    continue  # hidden a11y label on the image link, not the title
                title = a
                awaiting_title = False
                continue
            m = date_re.search(a)
            if m and title and url:
                mon = MONTHS.get(m.group(2).lower())
                if mon:
                    try:
                        d = date(2000 + int(m.group(3)), mon, int(m.group(1)))
                    except ValueError:
                        d = None
                    if d:
                        events.append(make_event(
                            source, title, d, url=url, category="Music"))
                title = url = None
    return events


def parse_st_columbs(soup, source):
    """saintcolumbshall.com/whatson/ embeds full event data as JSON-LD
    (schema.org Event objects, exact ISO datetimes) - far more reliable
    than the visible text, which Tribe Events Calendar splits oddly
    across separate text nodes (the weekday and day number are two
    different nodes, for instance)."""
    events = []
    for script in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(script.string or script.get_text())
        except (json.JSONDecodeError, TypeError):
            continue
        items = data if isinstance(data, list) else data.get("@graph", [data])
        for item in items:
            if not isinstance(item, dict) or item.get("@type") != "Event":
                continue
            name, url, start = (item.get("name"), item.get("url"),
                                 item.get("startDate"))
            if not (name and url and start):
                continue
            try:
                start_date = date.fromisoformat(start[:10])
            except ValueError:
                continue
            end = item.get("endDate")
            end_date = end[:10] if end and end[:10] != start[:10] else None
            events.append(make_event(
                source, name, start_date, end_date=end_date, url=url))
    return events


# ---------------------------------------------------------------- sources

SOURCES = [
    {"name": "an_grianan", "venue": "An Grianán Theatre", "town": "Letterkenny",
     "county": "Donegal", "url": "https://angrianan.com/events/",
     "parser": parse_an_grianan},
    {"name": "rcc", "venue": "Regional Cultural Centre", "town": "Letterkenny",
     "county": "Donegal", "url": "https://regionalculturalcentre.com/whats-on/",
     "parser": parse_rcc},
    {"name": "balor", "venue": "Balor Arts Centre", "town": "Ballybofey",
     "county": "Donegal", "url": "https://www.balorartscentre.com/",
     "parser": parse_balor},
    {"name": "abbey", "venue": "Abbey Arts Centre", "town": "Ballyshannon",
     "county": "Donegal", "url": "https://abbeycentre.ie/",
     "parser": parse_abbey},
    {"name": "eventbrite_donegal", "venue": "Eventbrite (Donegal)",
     "town": "Donegal", "county": "Donegal", "region_filter": "Donegal",
     "url": "https://www.eventbrite.ie/d/ireland--donegal/all-events/",
     "parser": parse_eventbrite_csv, "manual_csv": True},
    {"name": "eaf", "venue": "Earagail Arts Festival", "town": "Donegal",
     "county": "Donegal", "url": "https://eaf.ie/2026-events/",
     "parser": parse_eaf, "custom_fetch": True, "min_interval_days": 7},
    {"name": "mcgrorys", "venue": "McGrory's Hotel", "town": "Culdaff",
     "county": "Donegal", "url": "https://www.mcgrorys.ie/entertainment",
     "parser": parse_mcgrorys},
    {"name": "st_columbs", "venue": "St Columb's Hall", "town": "Derry",
     "county": "Derry", "url": "https://www.saintcolumbshall.com/whatson/",
     "parser": parse_st_columbs},
]


# ---------------------------------------------------------------- pipeline

def event_key(ev):
    raw = f"{ev['source']}|{ev['title'].lower()}|{ev['date']}"
    return hashlib.sha1(raw.encode()).hexdigest()[:12]


def load_previous():
    if DATA_FILE.exists():
        try:
            data = json.loads(DATA_FILE.read_text(encoding="utf-8"))
            data.setdefault("source_last_run", {})
            data.setdefault("consecutive_failures", {})
            return data
        except Exception:
            pass
    return {"events": [], "source_last_run": {}, "consecutive_failures": {}}


def notify(new_events):
    if not NTFY_TOPIC or not new_events:
        return
    lines = [
        f"{e['title']} — {date.fromisoformat(e['date']).strftime('%a %d %b')}"
        f" — {e['venue']}"
        for e in sorted(new_events, key=lambda e: e["date"])[:12]
    ]
    if len(new_events) > 12:
        lines.append(f"...and {len(new_events) - 12} more")
    headers = {
        "Title": f"{len(new_events)} new event"
                 f"{'s' if len(new_events) != 1 else ''} announced",
        "Tags": "performing_arts",
    }
    if PAGE_URL:
        headers["Click"] = PAGE_URL
    try:
        requests.post(f"https://ntfy.sh/{NTFY_TOPIC}",
                      data="\n".join(lines).encode("utf-8"),
                      headers=headers, timeout=TIMEOUT)
        print(f"Sent ntfy notification for {len(new_events)} new event(s)")
    except Exception as exc:  # never fail the run over a notification
        print(f"ntfy notification failed: {exc}", file=sys.stderr)


def main():
    previous = load_previous()
    prev_by_key = {event_key(e): e for e in previous.get("events", [])}

    all_events, failed = [], []
    source_last_run = dict(previous.get("source_last_run", {}))
    consecutive_failures = dict(previous.get("consecutive_failures", {}))
    FAILURE_THRESHOLD = 5
    for source in SOURCES:
        interval = source.get("min_interval_days")
        if interval:
            last_run = source_last_run.get(source["name"])
            if last_run:
                last_date = datetime.strptime(
                    last_run, "%Y-%m-%dT%H:%MZ").date()
                if (TODAY - last_date).days < interval:
                    print(f"{source['venue']}: last refreshed {last_run}, "
                          f"refreshes every {interval}d - skipping today, "
                          f"keeping previous data")
                    all_events.extend(
                        e for e in prev_by_key.values()
                        if e["source"] == source["name"])
                    continue
        try:
            if source.get("manual_csv"):
                found = source["parser"](source, source_last_run)
                if found is None:
                    print(f"{source['venue']}: no manual CSV uploaded this "
                          f"run - keeping previously known events")
                    all_events.extend(
                        e for e in prev_by_key.values()
                        if e["source"] == source["name"])
                    continue
            elif source.get("custom_fetch"):
                found = source["parser"](source)
            else:
                soup = fetch(source["url"])
                found = source["parser"](soup, source)
            print(f"{source['venue']}: {len(found)} events")
            if not found:
                extra = ""
                if not source.get("custom_fetch") and not source.get("manual_csv"):
                    extra = f" Page preview: {soup.get_text(' ', strip=True)[:200]!r}"
                raise ValueError(
                    "parsed zero events - selectors may be stale, filters "
                    "may be too strict, or the site blocked this request."
                    + extra)
            all_events.extend(found)
            if interval:
                source_last_run[source["name"]] = NOW.strftime("%Y-%m-%dT%H:%MZ")
            consecutive_failures[source["name"]] = 0
        except Exception as exc:
            count = consecutive_failures.get(source["name"], 0) + 1
            consecutive_failures[source["name"]] = count
            print(f"WARNING {source['venue']} failed ({count} in a row): {exc}",
                  file=sys.stderr)
            if count >= FAILURE_THRESHOLD:
                failed.append(source["venue"])
            # keep this venue's previously-seen events so a one-day outage
            # doesn't wipe them (and re-announce them tomorrow)
            all_events.extend(
                e for e in prev_by_key.values() if e["source"] == source["name"])

    # drop past events, de-duplicate, stamp first_seen
    seen, final = set(), []
    for ev in all_events:
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

    DATA_FILE.parent.mkdir(parents=True, exist_ok=True)
    DATA_FILE.write_text(json.dumps({
        "generated_at": NOW.strftime("%Y-%m-%dT%H:%MZ"),
        "failed_sources": failed,
        "source_last_run": source_last_run,
        "consecutive_failures": consecutive_failures,
        "events": final,
    }, indent=1, ensure_ascii=False), encoding="utf-8")
    print(f"Wrote {len(final)} upcoming events to {DATA_FILE}")

    # notify only about genuinely new events (skip the very first run,
    # otherwise you'd get one giant notification for everything)
    if previous.get("events"):
        new = [e for e in final
               if e["id"] not in prev_by_key
               and e["source"] not in [s["name"] for s in SOURCES
                                       if s["venue"] in failed]]
        notify(new)

    if failed:
        print(f"Completed with failures: {', '.join(failed)}", file=sys.stderr)


if __name__ == "__main__":
    main()
