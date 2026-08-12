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
from zoneinfo import ZoneInfo
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
# (connect_timeout, read_timeout) - if a host is genuinely unreachable
# (e.g. blocking GitHub's server IPs, as Eventbrite did), that fails fast
# on the connect phase rather than hanging for a full 30s per attempt.
# A slow-but-working site still gets a generous 20s to actually respond.
TIMEOUT = (8, 20)
NOW = datetime.now(timezone.utc)
# TODAY must reflect the Irish calendar date, not the UTC one - Ireland is
# UTC+1 during summer (BST), so Irish midnight happens at 23:00 UTC the
# day before. Using raw UTC here meant that for roughly the first hour of
# every Irish day (00:00-01:00 IST), TODAY was still "yesterday" by UTC's
# clock, so that day's already-past single-day events weren't dropped yet.
TODAY = NOW.astimezone(ZoneInfo("Europe/Dublin")).date()

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
    for attempt in range(2):
        try:
            r = requests.get(url, headers=HEADERS, timeout=TIMEOUT)
            r.raise_for_status()
            return BeautifulSoup(r.text, "lxml")
        except Exception as exc:
            last_exc = exc
            if attempt < 1:
                time.sleep(2)
    raise last_exc


def clean(text):
    return " ".join(str(text).split())


def infer_year(month, day):
    """Venue listings only show current/upcoming events, so if a date
    without a year would fall in the past, it means next year. A small
    grace period (not e.g. 90 days) avoids wrongly rolling a date that's
    only just passed forward a whole year, while still correctly rolling
    forward dates many months out (some venues list up to a year ahead,
    so a wide grace period would wrongly keep those in the past year)."""
    for year in (TODAY.year, TODAY.year + 1):
        try:
            d = date(year, month, day)
        except ValueError:
            continue
        if d >= TODAY - timedelta(days=7):
            return d
    return None


def infer_range_years(tokens):
    """Resolves the year for a list of (month, day) tokens making up a
    single date RANGE together, rather than inferring each one
    independently against TODAY via infer_year(). A range's start can
    easily be more than infer_year()'s 7-day grace period in the past
    while the event is still genuinely ongoing (e.g. an exhibition
    running 1-31 August, checked on the 9th) - inferring the start on
    its own would wrongly roll it forward a full year even though the
    end date makes clear the event is still this year. Anchors on the
    LAST token (the one that actually determines whether the event is
    still relevant), then works backward assigning each earlier token
    the SAME year as the one after it, correcting back one year only if
    that would put it AFTER the following token (a genuine year-
    boundary-crossing range, e.g. 28 Dec - 3 Jan). Returns a list the
    same length as tokens, with None for any date that couldn't be
    resolved at all."""
    if not tokens:
        return []
    n = len(tokens)
    resolved = [None] * n
    last_mon, last_day = tokens[-1]
    resolved[-1] = infer_year(last_mon, last_day)
    if not resolved[-1]:
        return resolved
    for i in range(n - 2, -1, -1):
        mon, day = tokens[i]
        anchor = resolved[i + 1]
        try:
            d = date(anchor.year, mon, day)
        except ValueError:
            continue
        if d > anchor:
            try:
                d = date(anchor.year - 1, mon, day)
            except ValueError:
                continue
        resolved[i] = d
    return resolved


def resolve_date_tokens(tokens):
    """Like infer_range_years, but for tokens that may already carry an
    explicit year - (month, day, year_or_None) triples - rather than
    needing inference for all of them (EAF occasionally states a year
    explicitly, e.g. 'Saturday 9th January 2027', when a run crosses
    into next year). Resolves right-to-left: the last token uses its
    own explicit year if given, else infer_year(); each earlier token
    uses its own explicit year if given, else the same year as the
    token after it, correcting back one year only if that would put it
    after the following token (a genuine year-boundary-crossing
    range)."""
    if not tokens:
        return []
    n = len(tokens)
    resolved = [None] * n
    mon, day, yr = tokens[-1]
    if yr:
        try:
            resolved[-1] = date(yr, mon, day)
        except ValueError:
            resolved[-1] = None
    else:
        resolved[-1] = infer_year(mon, day)
    if not resolved[-1]:
        return resolved
    for i in range(n - 2, -1, -1):
        mon, day, yr = tokens[i]
        anchor = resolved[i + 1]
        if yr:
            try:
                resolved[i] = date(yr, mon, day)
            except ValueError:
                pass
            continue
        try:
            d = date(anchor.year, mon, day)
        except ValueError:
            continue
        if d > anchor:
            try:
                d = date(anchor.year - 1, mon, day)
            except ValueError:
                continue
        resolved[i] = d
    return resolved


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


BALOR_DATE_RE = re.compile(r"(\d{1,2})/(\d{1,2})/(\d{4})")
BALOR_TIME_RE = re.compile(r"\d{1,2}:\d{2}\s*[ap]m", re.I)


def parse_balor_listing(soup, source):
    """balorartscentre.com/?page_id=87 - redesigned as of ~July 2026.
    Each card: title (h3 link), a DD/MM/YYYY date (optionally a
    ' - DD/MM/YYYY' range), a separate time line, a genre category link,
    a description, then a duplicate 'More Info' link (same href as the
    title) which triggers finalising the event."""
    events = []
    title = url = genre = None
    dates_found, time_text = [], None

    def finalise():
        if title and url and dates_found:
            start = dates_found[0]
            end = dates_found[1] if len(dates_found) > 1 else None
            events.append(make_event(
                source, title, start,
                end_date=end.isoformat() if end and end != start else None,
                time=time_text, url=url, category=genre))

    for kind, a, b in walk(soup):
        if kind == "link":
            href, text = a, b
            if "event-categories=" in href and text:
                genre = text
                continue
            if "?event=" in href and text:
                if text.lower() == "more info":
                    finalise()
                    title = url = genre = None
                    dates_found, time_text = [], None
                elif text.lower() not in SKIP_LINK_TEXT:
                    title, url = text, href
        else:
            if title:
                for m in BALOR_DATE_RE.finditer(a):
                    try:
                        dates_found.append(
                            date(int(m.group(3)), int(m.group(2)), int(m.group(1))))
                    except ValueError:
                        pass
                if not dates_found:
                    continue
                if not time_text and BALOR_TIME_RE.search(a):
                    time_text = a
    return events


def parse_balor_ghostlight_lineup(soup):
    """The Ghostlight Sessions' own event page lists that month's lineup
    as the first plain-text paragraph inside <section class="em-event-
    content"> - the paragraph before it is just a 'book online' button
    (an image wrapped in a link, no text of its own), so the first
    paragraph with any actual text reliably is the lineup line."""
    section = soup.find("section", class_="em-event-content")
    if not section:
        return None
    for p in section.find_all("p"):
        text = clean(p.get_text())
        if text:
            return text
    return None


def parse_balor(source):
    """Same listing as parse_balor_listing, plus one extra: Balor's
    monthly 'Ghostlight Sessions' gets its lineup fetched from its own
    event page and appended to the title (e.g. 'The Ghostlight Sessions
    August 2026 — The Turfmen | Tanya McCole | Lorc D'), since the
    listing card alone never shows who's actually playing that month."""
    soup = fetch(source["url"])
    events = parse_balor_listing(soup, source)
    for ev in events:
        if "ghostlight sessions" not in ev["title"].lower():
            continue
        lineup = cached_lookup(
            GHOSTLIGHT_LINEUP_CACHE, ev["url"],
            lambda ev=ev: parse_balor_ghostlight_lineup(fetch(ev["url"])))
        if lineup:
            ev["title"] = f"{ev['title']} — {lineup}"
    return events


def fetch_text(url):
    """Like fetch(), but returns raw response text instead of parsed HTML -
    needed for Eventbrite, where we read an embedded JSON blob rather than
    the rendered markup. Uses EVENTBRITE_HEADERS, not the shared HEADERS."""
    last_exc = None
    for attempt in range(2):
        try:
            r = requests.get(url, headers=EVENTBRITE_HEADERS, timeout=TIMEOUT)
            r.raise_for_status()
            return r.text
        except Exception as exc:
            last_exc = exc
            if attempt < 1:
                time.sleep(2)
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

# Eventbrite's destination search pads results with "nearby" events once
# genuine local listings run thin, which is how places like Warrenpoint
# (Co. Down) or Belfast (Co. Antrim) end up in a "Donegal" search. Rather
# than trust Eventbrite's own geography, every row's town is checked
# against this list and assigned its real county; anything not
# recognised is dropped entirely rather than mislabelled. This list is
# inherently incomplete - if a genuine local event ever gets wrongly
# excluded because its town isn't here yet, add it.
TOWN_TO_COUNTY = {
    # Donegal
    "letterkenny": "Donegal", "ballybofey": "Donegal", "stranorlar": "Donegal",
    "ballyshannon": "Donegal", "bundoran": "Donegal", "donegal": "Donegal",
    "donegal town": "Donegal", "killybegs": "Donegal", "glenties": "Donegal",
    "ardara": "Donegal", "dungloe": "Donegal", "gaoth dobhair": "Donegal",
    "ghaoth dobhair": "Donegal",
    "gweedore": "Donegal", "falcarragh": "Donegal", "dunfanaghy": "Donegal",
    "milford": "Donegal", "ramelton": "Donegal", "rathmullan": "Donegal",
    "portsalon": "Donegal", "dunkineely": "Donegal", "tory island": "Donegal",
    "raphoe": "Donegal", "convoy": "Donegal", "carndonagh": "Donegal",
    "buncrana": "Donegal", "moville": "Donegal", "culdaff": "Donegal",
    "malin": "Donegal", "clonmany": "Donegal", "ballyliffin": "Donegal",
    "kilcar": "Donegal", "carrick": "Donegal", "mountcharles": "Donegal",
    "pettigo": "Donegal", "lettermacaward": "Donegal", "churchill": "Donegal",
    "gortahork": "Donegal", "derrybeg": "Donegal", "dunlewey": "Donegal",
    "linsfort": "Donegal", "burtonport": "Donegal", "creeslough": "Donegal",
    "kilmacrenan": "Donegal", "manorcunningham": "Donegal",
    "newtowncunningham": "Donegal", "lifford": "Donegal", "muff": "Donegal",
    "greencastle": "Donegal", "fahan": "Donegal",
    # Derry
    "derry": "Derry", "londonderry": "Derry", "limavady": "Derry",
    "coleraine": "Derry", "magherafelt": "Derry", "maghera": "Derry",
    "garvagh": "Derry", "eglinton": "Derry",
    # Tyrone
    "omagh": "Tyrone", "strabane": "Tyrone", "dungannon": "Tyrone",
    "cookstown": "Tyrone", "castlederg": "Tyrone", "fintona": "Tyrone",
    "sion mills": "Tyrone",
    # Leitrim
    "carrick-on-shannon": "Leitrim", "carrick on shannon": "Leitrim",
    "manorhamilton": "Leitrim", "ballinamore": "Leitrim",
    "drumshanbo": "Leitrim", "mohill": "Leitrim", "kinlough": "Leitrim",
    "dromahair": "Leitrim", "rossinver": "Leitrim", "drumkeeran": "Leitrim",
    "newtowngore": "Leitrim", "aughavas": "Leitrim",
    # Sligo
    "sligo": "Sligo", "tubbercurry": "Sligo", "ballymote": "Sligo",
    "enniscrone": "Sligo", "strandhill": "Sligo", "grange": "Sligo",
    "rosses point": "Sligo", "collooney": "Sligo", "coolaney": "Sligo",
    "riverstown": "Sligo", "dromore west": "Sligo", "easkey": "Sligo",
    # Fermanagh
    "garrison": "Fermanagh", "enniskillen": "Fermanagh", "belleek": "Fermanagh",
    "kesh": "Fermanagh", "lisnaskea": "Fermanagh", "irvinestown": "Fermanagh",
    "belcoo": "Fermanagh", "derrygonnelly": "Fermanagh",
}


def _proper_town_case(key):
    """'carrick-on-shannon' -> 'Carrick-on-Shannon', 'sion mills' ->
    'Sion Mills', 'gaoth dobhair' -> 'Gaoth Dobhair'."""
    lower_words = {"on", "of"}
    parts = re.split(r"(-|\s+)", key)
    out = []
    for i, p in enumerate(parts):
        if p == "-" or p.isspace():
            out.append(p)
        elif p.lower() in lower_words and i != 0:
            out.append(p.lower())
        else:
            out.append(p.capitalize())
    return "".join(out)


TOWN_DISPLAY_CASE = {k: _proper_town_case(k) for k in TOWN_TO_COUNTY}
# Bare county names (Donegal, Derry, etc) are also valid town values in
# their own right (e.g. Eventbrite's "Donegal" meaning Donegal Town), but
# must only match as a LAST resort - otherwise 'Co. Donegal' inside a
# longer address (e.g. Fahan's) would wrongly win over the real, more
# specific town name just because 'donegal' happens to be longer.
_COUNTY_NAME_TOWNS = {"donegal", "derry", "sligo", "leitrim", "tyrone", "fermanagh"}
_SPECIFIC_TOWN_ORDER = sorted(
    (k for k in TOWN_TO_COUNTY if k not in _COUNTY_NAME_TOWNS),
    key=len, reverse=True)
_FALLBACK_TOWN_ORDER = sorted(
    (k for k in TOWN_TO_COUNTY if k in _COUNTY_NAME_TOWNS),
    key=len, reverse=True)


def find_specific_town(text):
    """Like nearest_known_town, but only matches a genuinely specific
    town - NEVER a bare county name - and returns None (not the
    original text) when nothing matches, so callers can tell 'found a
    real town' apart from 'found nothing'. Useful for checking a
    title/venue string for a town mention, where falling back to a
    bare county name would be actively misleading rather than just
    imprecise."""
    if not text:
        return None
    low = text.lower()
    for key in _SPECIFIC_TOWN_ORDER:
        if re.search(r"\b" + re.escape(key) + r"\b", low):
            return TOWN_DISPLAY_CASE[key]
    return None


def nearest_known_town(raw_town):
    """Reduces a raw, possibly overly-specific or compound town/address
    string (e.g. Heritage Week's 'Rossinver Community Centre, Co.
    Leitrim', or EAF's 'Linsfort, Buncrana') down to the nearest REAL,
    recognised town from TOWN_TO_COUNTY, so the town filter doesn't end
    up cluttered with one-off venue names and townlands. Matches a known
    town anywhere in the string as a whole word, not just as an exact
    full-string match, since the real town name is often just one part
    of a longer address. Falls back to the original value unchanged if
    no known town is found anywhere in it."""
    if not raw_town:
        return raw_town
    found = find_specific_town(raw_town)
    if found:
        return found
    low = raw_town.lower()
    for key in _FALLBACK_TOWN_ORDER:
        if re.search(r"\b" + re.escape(key) + r"\b", low):
            return TOWN_DISPLAY_CASE[key]
    return raw_town


# Populated from persisted state at the start of main() and written back
# at the end. Some sources (August Craft Month, Heritage Week) only give
# a county on their listing pages - the real town is only on each event's
# own page. Since that town never changes once an event exists, it's
# fetched once and cached by URL forever after, rather than being
# re-fetched on every single run.
LOCATION_CACHE = {}


def cached_town_lookup(url, fetch_and_extract_fn):
    """Returns the cached town for this event URL if known; otherwise
    calls fetch_and_extract_fn() to fetch the event's own page and derive
    one. Only caches the result if the fetch actually succeeded (even if
    it found nothing) - a fetch that merely failed (timeout, blocked,
    etc) is left uncached so it's retried on the next run, rather than
    being permanently remembered as 'no location available'."""
    if url in LOCATION_CACHE:
        return LOCATION_CACHE[url]
    try:
        town = fetch_and_extract_fn()
    except Exception:
        return None  # don't cache - retry next run
    LOCATION_CACHE[url] = town  # a genuine "found nothing" IS worth caching
    return town


# Same pattern as LOCATION_CACHE, but for Balor's Ghostlight Sessions
# lineup, which is also permanent once an event exists.
GHOSTLIGHT_LINEUP_CACHE = {}


def cached_lookup(cache, key, fetch_and_extract_fn):
    """Generic version of cached_town_lookup, for any per-event value
    that's fetched once and permanent thereafter. Only caches a genuine
    'fetched fine, computed a value (possibly None)' result - a fetch
    that failed outright is left uncached so it's retried next run."""
    if key in cache:
        return cache[key]
    try:
        value = fetch_and_extract_fn()
    except Exception:
        return None
    cache[key] = value
    return value


WEEKDAY_INDEX = {name: i for i, name in enumerate(
    ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"])}


def parse_eventbrite_date_text(text, trust_relative=True, reference_date=None):
    """Parses Eventbrite's date text as found in a webscraper.io export:
    an explicit date ('Fri 31 Jul, 18:30'), or a relative one ('Today at
    09:00', 'Tomorrow at 19:30', 'Thursday at 11:00' - a bare weekday
    means the next occurrence of that day). Relative dates are only
    parsed when trust_relative is True - see csv_freshness_check below
    for why that matters.

    reference_date anchors what 'today'/'tomorrow'/a bare weekday
    actually MEANS - it should be the day the CSV was captured, NOT
    necessarily today. If even a day or two passes between capture and
    this actually being processed (easily possible: the CSV stays
    'fresh' for a day, and the workflow runs daily), resolving 'Friday'
    against the CURRENT day rather than the capture day can roll a full
    week past the already-happened, originally-intended Friday - which
    is exactly what happened to two Ballyshannon Festival events that
    were meant as 31 Jul/1 Aug and came out a week later as 7/8 Aug.
    Defaults to TODAY if not given, for compatibility."""
    if reference_date is None:
        reference_date = TODAY
    if not text:
        return None, None
    t = clean(text)

    if trust_relative:
        m = re.match(r"today at (\d{1,2}:\d{2})", t, re.I)
        if m:
            return reference_date, m.group(1)

        m = re.match(r"tomorrow at (\d{1,2}:\d{2})", t, re.I)
        if m:
            return reference_date + timedelta(days=1), m.group(1)

        m = re.match(r"([A-Za-z]+)\s+at\s+(\d{1,2}:\d{2})", t)
        if m:
            wd = WEEKDAY_INDEX.get(m.group(1).lower())
            if wd is not None:
                delta = (wd - reference_date.weekday()) % 7 or 7
                return reference_date + timedelta(days=delta), m.group(2)

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
    # relative date text ('Friday at 18:30') must be resolved against the
    # day the CSV was actually captured, not today - see
    # parse_eventbrite_date_text's docstring for why that distinction
    # matters
    reference_date = TODAY
    if prev_state is not None:
        since = prev_state.get("eventbrite_csv_since")
        if since:
            try:
                reference_date = datetime.strptime(
                    since, "%Y-%m-%dT%H:%MZ").date()
            except ValueError:
                pass
    events = []
    with MANUAL_CSV_PATH.open(encoding="utf-8-sig", newline="") as f:
        for row in csv.DictReader(f):
            title = clean(row.get("data") or row.get("data6") or "")
            if not title:
                continue
            start_date, time_str = parse_eventbrite_date_text(
                row.get("data2"), trust_relative, reference_date)
            if not start_date:
                start_date, time_str = parse_eventbrite_date_text(
                    row.get("data11"), trust_relative, reference_date)
            if not start_date:
                continue  # stale relative date, or missing entirely
            venue_text = clean(row.get("data5") or row.get("data13") or "")
            town = venue = None
            if "·" in venue_text:
                town, venue = (p.strip() for p in venue_text.split("·", 1))
            else:
                venue = venue_text or None
            county = TOWN_TO_COUNTY.get((town or "").strip().lower())
            if not county:
                continue  # not a recognized Donegal/Derry/Sligo/Leitrim/Tyrone town
            # Eventbrite's own bare "Donegal" location tag specifically and
            # reliably means Donegal Town itself (confirmed against real
            # listings), unlike other sources where a bare "Donegal"
            # fallback usually just means "we only know the county" - so
            # this upgrade belongs here, not as a blanket rule elsewhere
            if town and town.strip().lower() == "donegal":
                town = "Donegal Town"
            slug = slugify(title)
            url = (f"https://www.eventbrite.ie/d/ireland--donegal/{slug}/"
                   if slug else source["url"])
            events.append(make_event(
                source, title, start_date, time=time_str,
                venue=venue, town=town, county=county, url=url))
    return events


EAF_DATE_RE = re.compile(
    r"(?:Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday)?\s*"
    r"(\d{1,2})(?:st|nd|rd|th)\s+([A-Za-z]+)(?:\s+(\d{4}))?", re.I)
EAF_TYPE_WORDS = {"live event", "exhibition", "project"}


def parse_eaf_date_text(text):
    """Extracts every date found in a line like 'Monday 13th - Friday
    17th July' or 'Saturday 9th January 2027', as raw (month, day,
    explicit_year_or_None) tuples - year resolution is deferred until
    all of an event's date tokens are collected together (see
    resolve_date_tokens in finalise() below), since resolving each one
    independently can wrongly roll an already-passed-but-still-relevant
    start date a full year forward while the event is genuinely still
    ongoing."""
    found = []
    for m in EAF_DATE_RE.finditer(text):
        mon = MONTHS.get(m.group(2).lower()[:3])
        if not mon:
            continue
        day = int(m.group(1))
        year = int(m.group(3)) if m.group(3) else None
        found.append((mon, day, year))
    return found


EAF_SOLD_OUT_RE = re.compile(
    r"\s*[-–—]\s*(?:d[ií]olta amach\s*/\s*)?sold\s*out\s*$", re.I)


def parse_eaf_listing(soup, source):
    """eaf.ie/2026-events/ lists every festival event on one page: a
    genre link, then a title link, then date/time bullet lines, then an
    event-type label (Live Event / Exhibition / Project). 'Project'
    entries (artist residencies with no attendable date) are skipped.
    A sold-out show has 'DÍOLTA AMACH / SOLD OUT' (or just 'SOLD OUT')
    appended to its title on the page - that's stripped out and turned
    into a sold_out flag instead of being shown twice."""
    events = []
    genre = None
    title = url = None
    pending_dates, pending_time = [], None

    def finalise(type_word):
        nonlocal title, url, genre, pending_dates, pending_time
        if title and url and type_word != "project" and pending_dates:
            resolved = resolve_date_tokens(pending_dates)
            start = resolved[0]
            end = resolved[-1] if len(resolved) == 2 else None
            if start:
                clean_title = title
                sold_out = False
                m = EAF_SOLD_OUT_RE.search(title)
                if m:
                    clean_title = title[:m.start()].strip()
                    sold_out = True
                events.append(make_event(
                    source, clean_title, start,
                    end_date=end.isoformat() if end and end != start else None,
                    time=pending_time, url=url, category=genre,
                    sold_out=sold_out))
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
            availability = (item.get("offers") or {}).get("availability", "")
            sold_out = "soldout" in availability.lower().replace(" ", "")
            events.append(make_event(
                source, name, start_date, end_date=end_date, url=url,
                sold_out=sold_out))
    return events


NERVE_EVENT_LINK_RE = re.compile(r"/whats-on/[a-z0-9-]+/?$", re.I)
NERVE_DATE_TOKEN_RE = re.compile(r"(\d{1,2})\s+([A-Za-z]+)(?:\s+(\d{4}))?")
NERVE_TIME_RE = re.compile(r"\|\s*(\d{1,2}:\d{2}\s*[AP]M)", re.I)


def parse_nerve_date_line(text):
    """Parses date lines like '31 July 2026 | 5:00PM', '20 July - 24
    July 2026' (first date missing a year, borrowed from the second),
    or '1 October - 30 June 2027' - a course spanning into the next
    year, where naively borrowing the end date's year would make the
    start date come AFTER the end date; in that case the start year is
    stepped back by one to keep the range chronological."""
    time_text = None
    m_time = NERVE_TIME_RE.search(text)
    if m_time:
        time_text = m_time.group(1)
        text = text[:m_time.start()]

    parsed = []
    for m in NERVE_DATE_TOKEN_RE.finditer(text):
        mon = MONTHS.get(m.group(2).lower()[:3])
        if not mon:
            continue
        year = int(m.group(3)) if m.group(3) else None
        parsed.append([int(m.group(1)), mon, year])
    if not parsed:
        return None, None, None

    for i in range(len(parsed) - 1):
        if parsed[i][2] is None:
            later_years = [p[2] for p in parsed[i + 1:] if p[2] is not None]
            if later_years:
                parsed[i][2] = later_years[0]

    dates = []
    for day, mon, year in parsed:
        if year is None:
            d = infer_year(mon, day)
        else:
            try:
                d = date(year, mon, day)
            except ValueError:
                d = None
        if d:
            dates.append(d)
    if not dates:
        return None, None, time_text

    if len(dates) >= 2 and dates[0] > dates[-1]:
        try:
            fixed = date(dates[0].year - 1, dates[0].month, dates[0].day)
            if fixed <= dates[-1]:
                dates[0] = fixed
        except ValueError:
            pass

    start = dates[0]
    end = dates[-1] if len(dates) > 1 and dates[-1] != dates[0] else None
    return start, end, time_text


def _nerve_has_date_token(text):
    """NERVE_DATE_TOKEN_RE alone is too loose - '7-8 Magazine Street'
    matches 'digit + word' just like a real date would. Only treat a
    line as a date line if at least one match's word is an actual
    month name."""
    return any(MONTHS.get(m.group(2).lower()[:3])
               for m in NERVE_DATE_TOKEN_RE.finditer(text))


def parse_nervecentre(soup, source):
    """nervecentre.org/whats-on - Nerve Centre runs events across Derry,
    Belfast, Bangor and even Wales, so only events whose venue text
    mentions Derry (and NOT Belfast, ruling out the one dual-city
    workshop) are kept. Listings with no date at all, or with no venue
    line (e.g. a book for sale), are skipped - not real attendable
    events for this site. The button text ('Sold Out' vs 'Book Now' /
    'Sign Up' / 'Apply Now' / 'Purchase Now') sets a sold_out flag,
    which naturally refreshes on each day's rescrape.

    A date RANGE is marked up as two separate <time> tags with a bare
    '-' text node between them (e.g. '10 August' / '-' / '13 August
    2026'), not one combined string like a single date is - so the
    leading run of date-shaped-or-separator text nodes is accumulated
    into one date_line before parsing, rather than only keeping the
    last one seen."""
    events = []
    url = genre = None
    raw_lines = []
    skip_next = False

    def finalise(sold_out):
        if not (url and raw_lines):
            return
        i = 0
        date_parts = []
        while i < len(raw_lines) and (
                _nerve_has_date_token(raw_lines[i])
                or re.fullmatch(r"[-–—\s]+", raw_lines[i])):
            date_parts.append(raw_lines[i])
            i += 1
        date_line = " ".join(date_parts) if date_parts else None
        leftover = raw_lines[i:]
        if not leftover:
            return
        title = leftover[0]
        venue_text = leftover[-1] if len(leftover) > 1 else ""
        vt_lower = venue_text.lower()
        if "derry" not in vt_lower or "belfast" in vt_lower:
            return  # not a strictly-Derry event
        if not date_line:
            return  # no date at all - not schedulable
        start, end, time_text = parse_nerve_date_line(date_line)
        if not start:
            return
        venue = venue_text.split(",")[0].strip()
        events.append(make_event(
            source, title, start, end_date=end.isoformat() if end else None,
            time=time_text, url=url, category=genre, venue=venue,
            town="Derry", sold_out=sold_out))

    for kind, a, b in walk(soup):
        if kind == "link":
            href, text = a, b
            if "topic=" in href and text:
                genre = text
                continue
            if NERVE_EVENT_LINK_RE.search(href):
                if href == url and text:
                    finalise(sold_out=(text.strip().lower() == "sold out"))
                    url = genre = None
                    raw_lines = []
                    skip_next = False
                else:
                    url = href
        else:
            if not url:
                continue
            if skip_next:
                skip_next = False
                continue
            if a.lower().startswith("admission:"):
                skip_next = True
                continue
            raw_lines.append(a)
    return events


HAWKSWELL_TIME_RE = re.compile(r"\d{1,2}([.:]\d{2})?\s*(?:am|pm)", re.I)
HAWKSWELL_DATE_TOKEN_RE = re.compile(r"\b(\d{1,2})\b(?:\s+([A-Za-z]+))?(?:\s+(\d{4}))?")


def parse_hawkswell_date_line(text):
    """Parses Hawk's Well's date lines, which come in several shapes:
    a single date+time ('Wed 22 July 2026, 1.10pm'), a genuine range
    ('Tues 21 - Sat 25 July 2026, 5pm & 10.30pm' - dash-joined, day-only
    on the first token borrows month/year from the second), or a list of
    genuinely separate non-contiguous performances ('Fri 6 March & Fri
    10 April 2026, 1pm' - '&'/',' joined). Only a dash-only join is
    treated as a real range; any '&' or ',' between date tokens means
    'take the first occurrence only', so we don't imply a false
    continuous run across unrelated nights."""
    time_matches = [m.group(0) for m in HAWKSWELL_TIME_RE.finditer(text)]
    time_text = " & ".join(time_matches) if time_matches else None
    if not time_text:
        m_various = re.search(r"various\s*times?", text, re.I)
        if m_various:
            time_text = "Various times"

    date_part = HAWKSWELL_TIME_RE.sub("", text)
    matches = list(HAWKSWELL_DATE_TOKEN_RE.finditer(date_part))
    if not matches:
        return None, None, time_text

    is_list = False
    for i in range(len(matches) - 1):
        between = date_part[matches[i].end():matches[i + 1].start()]
        if "&" in between or "," in between:
            is_list = True
            break

    parsed = [[int(m.group(1)),
               MONTHS.get(m.group(2).lower()[:3]) if m.group(2) else None,
               int(m.group(3)) if m.group(3) else None]
              for m in matches]

    for i in range(len(parsed) - 1):
        if parsed[i][1] is None:
            later = next((p for p in parsed[i + 1:] if p[1] is not None), None)
            if later:
                parsed[i][1] = later[1]
                if parsed[i][2] is None:
                    parsed[i][2] = later[2]
        if parsed[i][2] is None:
            later_year = next((p[2] for p in parsed[i + 1:] if p[2] is not None), None)
            if later_year:
                parsed[i][2] = later_year

    resolved = resolve_date_tokens(
        [(mon, day, year) for day, mon, year in parsed if mon])
    dates = [d for d in resolved if d]

    if not dates:
        return None, None, time_text
    if is_list or len(dates) == 1:
        return dates[0], None, time_text
    start, end = dates[0], dates[-1]
    return start, (end if end != start else None), time_text


def parse_hawkswell_listing(soup, source):
    """hawkswell.com/whats-on/shows - each card is a genre tag (plain
    div, not a link), then a single <a> wrapping the whole card (image,
    title, optional subline, date). The genre 'Filter' buttons at the
    top of the page are real links pointing at the same URL pattern as
    individual shows, so they're explicitly excluded via the #wwd-tags
    container rather than guessed at."""
    filter_hrefs = set()
    filter_div = soup.find(id="wwd-tags")
    if filter_div:
        filter_hrefs = {a.get("href") for a in filter_div.find_all("a", href=True)}

    events = []
    genre = None
    url = None
    buf = []

    def finalise():
        if not (url and buf):
            return
        date_line = None
        text_parts = []
        for line in buf:
            if HAWKSWELL_DATE_TOKEN_RE.search(HAWKSWELL_TIME_RE.sub("", line)) \
                    or HAWKSWELL_TIME_RE.search(line) \
                    or re.search(r"various\s*times?", line, re.I):
                date_line = line
            else:
                text_parts.append(line)
        if not date_line or not text_parts:
            return
        start, end, time_text = parse_hawkswell_date_line(date_line)
        if not start:
            return
        title = " – ".join(text_parts)
        events.append(make_event(
            source, title, start, end_date=end.isoformat() if end else None,
            time=time_text, url=url, category=genre))

    for kind, a, b in walk(soup):
        if kind == "link":
            href, text = a, b
            if href in filter_hrefs:
                continue
            if href != url:
                # the most recently buffered text (if any) is actually
                # the NEW event's genre tag, captured while the OLD
                # event's href was still active - reclaim it before
                # finalising the old event
                next_genre = buf.pop() if buf else None
                finalise()
                url = href
                genre = next_genre
                buf = []
        else:
            buf.append(a)
    finalise()
    return events


def parse_hawkswell_event_page(soup):
    """Each event's own page has a clean 'Location' table row: either
    the main theatre ('Hawk's Well Theatre') or their second venue in
    Ballymote ('Art Deco Theatre, Ballymote')."""
    for th in soup.find_all("th"):
        if clean(th.get_text()).lower() == "location":
            td = th.find_next_sibling("td")
            if td:
                text = clean(td.get_text())
                if "," in text:
                    venue, town = (p.strip() for p in text.split(",", 1))
                else:
                    venue, town = text, "Sligo"
                return venue, town
    return "Hawk's Well Theatre", "Sligo"


def parse_hawkswell(source):
    """Two-stage, same pattern as EAF: scrape the listing page for
    what/when/genre, then visit each event's own page for its real
    venue (the listing page has no reliable per-event location - only
    the 'Art Deco' genre tag, which is also a style label used at the
    main venue too, not exclusively a Ballymote marker)."""
    listing = fetch(source["url"])
    events = parse_hawkswell_listing(listing, source)
    for ev in events:
        try:
            detail = fetch(ev["url"])
            venue, town = parse_hawkswell_event_page(detail)
            ev["venue"] = venue
            ev["town"] = town
        except Exception:
            pass  # keep the default Hawk's Well Theatre / Sligo
        time.sleep(0.4)
    return events


CRAFTMONTH_DATE_RE = re.compile(
    r"(\d{1,2})\s+([A-Za-z]+)\s+to\s+(\d{1,2})\s+([A-Za-z]+)", re.I)


def parse_craftmonth_listing(soup, source):
    """augustcraftmonth.org/events/?search_loc=X - each event card is
    `<a class="acm-venue-item">` inside `#results` (there's also an
    unrelated 'featured' carousel elsewhere on the page showing events
    from ALL counties regardless of the filter, which is skipped by
    only looking inside #results). The date range span has a <br/> in
    the middle splitting it into two text nodes, so it's read directly
    via BeautifulSoup rather than the generic text-walk."""
    results = soup.find(id="results")
    if not results:
        return []
    events = []
    for card in results.find_all("a", class_="acm-venue-item"):
        href = card.get("href")
        title_tag = card.find("h3")
        date_tab = card.find("span", class_="date_tab")
        if not (href and title_tag and date_tab):
            continue
        title = clean(title_tag.get_text())
        date_text = clean(date_tab.get_text(" "))
        m = CRAFTMONTH_DATE_RE.search(date_text)
        if not m:
            continue
        d1, mon1, d2, mon2 = m.groups()
        mon1n = MONTHS.get(mon1.lower()[:3])
        mon2n = MONTHS.get(mon2.lower()[:3])
        if not (mon1n and mon2n):
            continue
        start, end = infer_range_years([(mon1n, int(d1)), (mon2n, int(d2))])
        if not start:
            continue

        fields = {}
        for p in card.select(".text_items p"):
            strong = p.find("strong")
            if not strong:
                continue
            label = clean(strong.get_text()).rstrip(":").lower()
            value = clean(strong.next_sibling or "")
            fields[label] = value

        county = COUNTY_ALIASES.get(fields.get("location", ""), fields.get("location"))
        cat_bits = [fields[k] for k in ("event type", "craft type") if fields.get(k)]
        # the site's own 'Location:' field is county-level only, not a
        # specific town - check the title and maker/venue name for an
        # actual town mention (e.g. 'Rathmullan Makers Market'). If
        # nothing specific turns up here OR from the per-event page
        # fetch (see parse_craftmonth), town is left unset rather than
        # falling back to the bare county name, which downstream would
        # get misread as a specific place ("Donegal" -> "Donegal Town")
        town = find_specific_town(title) or find_specific_town(fields.get("maker"))

        ev = make_event(
            source, title, start,
            end_date=end.isoformat() if end and end != start else None,
            url=href, category=", ".join(cat_bits) if cat_bits else None,
            venue=fields.get("maker"), county=county)
        ev["town"] = town  # explicit, bypassing make_event's default-from-source
        events.append(ev)

    next_link = soup.select_one("a.next, a[rel='next']")
    if next_link and next_link.get("href") and len(events) > 0:
        try:
            more_soup = fetch(next_link["href"])
            events.extend(parse_craftmonth_listing(more_soup, source))
        except Exception:
            pass
    return events


def parse_craftmonth_event_page(soup):
    """Individual event pages have an 'Event Address:' label followed by
    the real street address as a separate text node right after it (e.g.
    'Front Street Ardara Co Donegal F94 E4E4') - much more specific than
    the always-county-level 'Event Location:' field also present."""
    texts = [a for kind, a, b in walk(soup) if kind == "text"]
    for i, t in enumerate(texts):
        if t.strip().lower() == "event address:" and i + 1 < len(texts):
            return find_specific_town(texts[i + 1])
    return None


def parse_craftmonth(source):
    """Two-stage: the listing gives title/date/genre/a rough town, then
    each event's own page is fetched once (ever - see LOCATION_CACHE)
    for a more precise town from its real street address."""
    soup = fetch(source["url"])
    events = parse_craftmonth_listing(soup, source)
    for ev in events:
        town = cached_town_lookup(
            ev["url"],
            lambda ev=ev: parse_craftmonth_event_page(fetch(ev["url"])))
        if town:
            ev["town"] = town
    return events


HERITAGEWEEK_DATE_RE = re.compile(r"^(\d{1,2})\s+([A-Za-z]+)\b")


def parse_heritageweek_page(soup, source):
    """heritageweek.ie/event-listings - each card is <article
    class="item-summary">, with an inner <ul class="list-details">
    whose <li> items are, in order: venue (bold), 'Co. County', a
    town/address fragment, then one <li> PER DAY for multi-day events
    ('17 August, 9:30am - 5:30pm', '18 August, ...' etc - each its own
    list item, not one combined string)."""
    events = []
    for article in soup.select("article.item-summary"):
        link = article.select_one("a.link-block")
        if not link:
            continue
        href = link.get("href")
        title_tag = link.select_one("h3.title")
        if not (href and title_tag):
            continue
        title = clean(title_tag.get_text())

        items = []
        for li in link.select("ul.list-details li"):
            for piece in li.get_text("\n").split("\n"):
                piece = clean(piece)
                if piece:
                    items.append(piece)
        date_entries = [i for i in items if HERITAGEWEEK_DATE_RE.match(i)]
        info_entries = [i for i in items if not HERITAGEWEEK_DATE_RE.match(i)]
        if not date_entries:
            continue

        venue = info_entries[0] if info_entries else None
        county = None
        town_candidate = None
        bare_county_re = re.compile(r"^co\.\s+[a-z\s]+$", re.I)
        for item in info_entries[1:]:
            m = re.search(r"co\.\s*([a-z\s]+?)(?:,|$)", item, re.I)
            if m and county is None:
                county = clean(m.group(1))
            if town_candidate is None and not bare_county_re.match(item.strip()):
                town_candidate = item
        county = COUNTY_ALIASES.get(county, county)
        # resolved now (not left as raw address text) so an unresolved
        # candidate doesn't later fall through to a bare county-name
        # match in the main pipeline's normalisation and get misread as
        # a specific place ("Donegal" -> "Donegal Town")
        town = find_specific_town(town_candidate) if town_candidate else None

        date_tokens, time_text = [], None
        for de in date_entries:
            m = HERITAGEWEEK_DATE_RE.match(de)
            mon = MONTHS.get(m.group(2).lower()[:3])
            if not mon:
                continue
            date_tokens.append((mon, int(m.group(1))))
            if not time_text:
                rest = de[m.end():].lstrip(", ").strip()
                if rest:
                    time_text = rest
        dates = [d for d in infer_range_years(date_tokens) if d]
        if not dates:
            continue
        start = dates[0]
        end = dates[-1] if len(dates) > 1 and dates[-1] != start else None

        ev = make_event(
            source, title, start,
            end_date=end.isoformat() if end else None,
            time=time_text, url=href, venue=venue, county=county)
        ev["town"] = town  # explicit, bypassing make_event's default-from-source
        events.append(ev)
    return events


def parse_heritageweek_event_page(soup):
    """Individual event pages have a cleaner address breakdown than the
    listing card: <ul class="event-details"> with a few specific-to-
    general <li> lines ending in 'Co. County' (e.g. 'Port Arthur, An
    Luinnigh' / 'Luinnigh, Gaoth Dobhair' / 'Co. Donegal') - the county
    line itself is skipped since it's no more useful than what the
    listing page already gave us."""
    ul = soup.select_one("ul.event-details")
    if not ul:
        return None
    for li in ul.select("li"):
        text = clean(li.get_text())
        if text.lower().startswith("co."):
            continue
        found = find_specific_town(text)
        if found:
            return found
    return None


def parse_heritageweek(source):
    """Follows 'Next' pagination links (which preserve our county
    filter query params) until no more pages remain. Capped as a
    safety net against a genuinely broken/looping 'Next' link, not as
    a normal termination path - the real result set is large (Donegal
    alone has ~150 events, ~13 pages, and this query combines six
    counties into one alphabetically-sorted list), so the cap must sit
    well above that or events sorting late alphabetically (anything
    from roughly S onward) silently never get fetched at all. Each
    event's own page is then fetched once (ever - see LOCATION_CACHE)
    for a more precise town than the listing card's address fragment
    gives."""
    events = []
    url = source["url"]
    for _ in range(60):
        soup = fetch(url)
        found = parse_heritageweek_page(soup, source)
        events.extend(found)
        next_link = soup.find("a", string=lambda s: s and s.strip().lower() == "next")
        if not next_link or not next_link.get("href"):
            break
        url = next_link["href"]

    for ev in events:
        town = cached_town_lookup(
            ev["url"],
            lambda ev=ev: parse_heritageweek_event_page(fetch(ev["url"])))
        if town:
            ev["town"] = town
    return events


THEDOCK_DATE_TOKEN_RE = re.compile(r"(\d{1,2})\s+([A-Za-z]+)(?:\s+(\d{4}))?")


def parse_thedock_date(text):
    """Parses dates like '5 September 2026' or a range like '25 — 28
    November 2026' (first date missing month/year, borrowed from the
    second, same approach as Hawk's Well and Nerve Centre)."""
    matches = [[int(m.group(1)),
                MONTHS.get(m.group(2).lower()[:3]),
                int(m.group(3)) if m.group(3) else None]
               for m in THEDOCK_DATE_TOKEN_RE.finditer(text)]
    for i in range(len(matches) - 1):
        if matches[i][1] is None:
            later = next((p for p in matches[i + 1:] if p[1] is not None), None)
            if later:
                matches[i][1] = later[1]
                if matches[i][2] is None:
                    matches[i][2] = later[2]
        if matches[i][2] is None:
            later_year = next((p[2] for p in matches[i + 1:] if p[2] is not None), None)
            if later_year:
                matches[i][2] = later_year
    resolved = resolve_date_tokens(
        [(mon, day, year) for day, mon, year in matches if mon])
    dates = [d for d in resolved if d]
    if not dates:
        return None, None
    start = dates[0]
    end = dates[-1] if len(dates) > 1 and dates[-1] != start else None
    return start, end


def parse_thedock(soup, source):
    """thedock.ie/whats-on/upcoming-events - each event is <article
    class="item-event">, with genre (.btn), title (h3.title), an
    optional subtitle (h4.sub-title), and a date (p.date), all in
    clean, directly-selectable elements. A single physical venue, so
    no per-event detail fetch is needed."""
    events = []
    for article in soup.select("article.item-event"):
        link = article.select_one("a.link-block")
        if not link:
            continue
        href = link.get("href")
        title_tag = link.select_one("h3.title")
        date_tag = link.select_one("p.date")
        if not (href and title_tag and date_tag):
            continue
        title = clean(title_tag.get_text())
        sub_tag = link.select_one("h4.sub-title")
        if sub_tag:
            sub = clean(sub_tag.get_text())
            if sub:
                title = f"{title} – {sub}"
        genre_tag = link.select_one(".btn")
        genre = clean(genre_tag.get_text()) if genre_tag else None
        start, end = parse_thedock_date(clean(date_tag.get_text()))
        if not start:
            continue
        events.append(make_event(
            source, title, start, end_date=end.isoformat() if end else None,
            url=href, category=genre))
    return events


STRULE_DATE_RE = re.compile(
    r"(\d{1,2})\s+([A-Za-z]+)(?:\s+at\s+(\d{1,2}[:.]\d{2}\s*[ap]m))?", re.I)


def parse_strule(soup, source):
    """struleartscentre.co.uk/whats-on/shows/ - each card (.card-show)
    has a genre (.primary-category), title (h2), a date (p.published-
    date), and a Book/Sold Out button. For a sold-out show, the site
    drops the date from the listing entirely rather than showing it
    alongside a Sold Out badge - those are skipped, since there's no
    way to schedule an event with no visible date on this page."""
    events = []
    for card in soup.select(".card-show"):
        title_tag = card.select_one("h2")
        link_tag = card.select_one("a.stretched-link")
        date_tag = card.select_one("p.published-date")
        if not (title_tag and link_tag and date_tag):
            continue
        date_text = clean(date_tag.get_text())
        if not date_text:
            continue  # sold out - no date shown on this page
        m = STRULE_DATE_RE.search(date_text)
        if not m:
            continue
        mon = MONTHS.get(m.group(2).lower()[:3])
        if not mon:
            continue
        start = infer_year(mon, int(m.group(1)))
        if not start:
            continue
        genre_tag = card.select_one(".primary-category")
        sold_out = bool(card.select_one("a.btn.disabled"))
        events.append(make_event(
            source, clean(title_tag.get_text()), start,
            time=m.group(3), url=link_tag.get("href"),
            category=clean(genre_tag.get_text()) if genre_tag else None,
            sold_out=sold_out))
    return events


THEMODEL_DATE_RE = re.compile(
    r"([A-Za-z]{3,9})\s+(\d{1,2}),\s+(\d{4})"
    r"(?:\s*[-–—]\s*([A-Za-z]{3,9})\s+(\d{1,2}),\s+(\d{4}))?")
THEMODEL_TIME_RE = re.compile(r"\d{1,2}:\d{2}\s*[ap]m", re.I)
THEMODEL_MAX_SPAN_DAYS = 90


def parse_themodel_date(text):
    """Parses lines like 'Sun., 11:00 am Jun 13, 2026 – Aug 22, 2026' or
    'Open all day. Jan 1, 2026 – Dec 31, 2026'."""
    time_m = THEMODEL_TIME_RE.search(text)
    time_text = time_m.group(0) if time_m else None
    if not time_text and re.search(r"open all day", text, re.I):
        time_text = "Open all day"

    m = THEMODEL_DATE_RE.search(text)
    if not m:
        return None, None, time_text
    mon1, d1, y1, mon2, d2, y2 = m.groups()
    mon1n = MONTHS.get(mon1.lower()[:3])
    if not mon1n:
        return None, None, time_text
    try:
        start = date(int(y1), mon1n, int(d1))
    except ValueError:
        return None, None, time_text
    end = None
    if mon2:
        mon2n = MONTHS.get(mon2.lower()[:3])
        if mon2n:
            try:
                end_d = date(int(y2), mon2n, int(d2))
                if end_d != start:
                    end = end_d
            except ValueError:
                pass
    return start, end, time_text


def parse_themodel(soup, source):
    """themodel.ie/whats-on/ - each event is <li class="grid-item">,
    title in h3>a, and a combined weekday/time/date-range string in
    .grid-item-meta. Standing year-round programmes that aren't real
    discrete events (e.g. 'Gallery Tours for Groups' running Jan-Dec, or
    'Guided Tours for Schools' running Sep-June) are skipped via a
    90-day span cutoff, rather than a title-based denylist, so any
    similar catchall programme is caught automatically."""
    events = []
    seen_urls = set()
    for li in soup.select("li.grid-item"):
        title_tag = li.select_one("h3 a")
        meta_tag = li.select_one(".grid-item-meta")
        if not (title_tag and meta_tag):
            continue
        href = title_tag.get("href")
        if not href or href in seen_urls:
            continue
        seen_urls.add(href)
        start, end, time_text = parse_themodel_date(clean(meta_tag.get_text(" ")))
        if not start:
            continue
        if end and (end - start).days > THEMODEL_MAX_SPAN_DAYS:
            continue  # standing/catchall programme, not a real event
        events.append(make_event(
            source, clean(title_tag.get_text()), start,
            end_date=end.isoformat() if end else None,
            time=time_text, url=href))
    return events


PLAYHOUSE_DATE_RE = re.compile(r"(\d{1,2})(?:st|nd|rd|th)\s+([A-Za-z]+)\s+(\d{4})", re.I)


def parse_playhouse_derry(soup, source):
    """derryplayhouse.co.uk/events - each card is a div.group with a
    title (.title.font-title-bold), a date range separated by '~'
    (.title.font-title - a DIFFERENT class combo to the title, since
    'font-title-bold' and 'font-title' are distinct CSS tokens, not a
    substring match), and a link (a.gradient). No genre is exposed per
    card here (the promo badges like 'NEW SHOW'/'LIMITED RUN' aren't
    genres), so category is left unset, same as a few other sources."""
    events = []
    for card in soup.select("div.group"):
        title_tag = card.select_one("div.title.font-title-bold")
        date_tag = card.select_one("div.title.font-title")
        link_tag = card.select_one("a.gradient")
        if not (title_tag and date_tag and link_tag):
            continue
        href = link_tag.get("href")
        if not href:
            continue
        date_text = clean(date_tag.get_text(" "))
        matches = PLAYHOUSE_DATE_RE.findall(date_text)
        if not matches:
            continue
        dates = []
        for day, mon_name, year in matches:
            mon = MONTHS.get(mon_name.lower()[:3])
            if not mon:
                continue
            try:
                dates.append(date(int(year), mon, int(day)))
            except ValueError:
                pass
        if not dates:
            continue
        start = dates[0]
        end = dates[-1] if len(dates) > 1 and dates[-1] != start else None
        events.append(make_event(
            source, clean(title_tag.get_text()), start,
            end_date=end.isoformat() if end else None, url=href))
    return events


# ---------------------------------------------------------------- sources

CRAFTMONTH_START = TODAY.strftime("%Y%m%d")
CRAFTMONTH_END = (TODAY + timedelta(days=120)).strftime("%Y%m%d")


def seasonal_interval(active_months, quiet_interval=3):
    """Returns 0 (no throttle - refresh on every run, same as an
    unthrottled source) during active_months, or quiet_interval
    otherwise. For genuinely seasonal sources (a festival, a themed
    week) where new events appear frequently during their real active
    window but the source is otherwise dormant - checking daily only
    when it's actually worth checking daily."""
    return 0 if TODAY.month in active_months else quiet_interval


def craftmonth_url(loc):
    return (f"https://augustcraftmonth.org/events/?search_loc={loc}"
            f"&event_type=&discipline=&start_date={CRAFTMONTH_START}"
            f"&end_date={CRAFTMONTH_END}")


SOURCES = [
    {"name": "an_grianan", "venue": "An Grianán Theatre", "town": "Letterkenny",
     "county": "Donegal", "url": "https://angrianan.com/events/",
     "parser": parse_an_grianan},
    {"name": "rcc", "venue": "Regional Cultural Centre", "town": "Letterkenny",
     "county": "Donegal", "url": "https://regionalculturalcentre.com/whats-on/",
     "parser": parse_rcc},
    {"name": "balor", "venue": "Balor Arts Centre", "town": "Ballybofey",
     "county": "Donegal", "url": "https://www.balorartscentre.com/?page_id=87",
     "parser": parse_balor, "custom_fetch": True},
    {"name": "abbey", "venue": "Abbey Arts Centre", "town": "Ballyshannon",
     "county": "Donegal", "url": "https://abbeycentre.ie/",
     "parser": parse_abbey},
    {"name": "eventbrite_donegal", "venue": "Eventbrite (Donegal)",
     "town": "Donegal", "county": "Donegal", "region_filter": "Donegal",
     "url": "https://www.eventbrite.ie/d/ireland--donegal/all-events/",
     "parser": parse_eventbrite_csv, "manual_csv": True},
    {"name": "eaf", "venue": "Earagail Arts Festival", "town": "Donegal",
     "county": "Donegal", "url": "https://eaf.ie/2026-events/",
     "parser": parse_eaf, "custom_fetch": True,
     "min_interval_days": seasonal_interval({7, 8}, quiet_interval=7)},
    {"name": "mcgrorys", "venue": "McGrory's Hotel", "town": "Culdaff",
     "county": "Donegal", "url": "https://www.mcgrorys.ie/entertainment",
     "parser": parse_mcgrorys},
    {"name": "st_columbs", "venue": "St Columb's Hall", "town": "Derry",
     "county": "Derry", "url": "https://www.saintcolumbshall.com/whatson/",
     "parser": parse_st_columbs},
    {"name": "nervecentre", "venue": "Nerve Centre", "town": "Derry",
     "county": "Derry", "url": "https://nervecentre.org/whats-on",
     "parser": parse_nervecentre},
    {"name": "hawkswell", "venue": "Hawk's Well Theatre", "town": "Sligo",
     "county": "Sligo", "url": "https://www.hawkswell.com/whats-on/shows",
     "parser": parse_hawkswell, "custom_fetch": True, "min_interval_days": 3},
    {"name": "craftmonth_donegal", "venue": "August Craft Month", "town": "",
     "county": "Donegal", "url": craftmonth_url("donegal"),
     "parser": parse_craftmonth, "custom_fetch": True,
     "min_interval_days": seasonal_interval({7, 8}), "quiet_if_empty": True},
    {"name": "craftmonth_derry", "venue": "August Craft Month", "town": "",
     "county": "Derry", "url": craftmonth_url("derry"),
     "parser": parse_craftmonth, "custom_fetch": True,
     "min_interval_days": seasonal_interval({7, 8}), "quiet_if_empty": True},
    {"name": "craftmonth_derry_city", "venue": "August Craft Month", "town": "",
     "county": "Derry", "url": craftmonth_url("derry_city"),
     "parser": parse_craftmonth, "custom_fetch": True,
     "min_interval_days": seasonal_interval({7, 8}), "quiet_if_empty": True},
    {"name": "craftmonth_leitrim", "venue": "August Craft Month", "town": "",
     "county": "Leitrim", "url": craftmonth_url("leitrim"),
     "parser": parse_craftmonth, "custom_fetch": True,
     "min_interval_days": seasonal_interval({7, 8}), "quiet_if_empty": True},
    {"name": "craftmonth_sligo", "venue": "August Craft Month", "town": "",
     "county": "Sligo", "url": craftmonth_url("sligo"),
     "parser": parse_craftmonth, "custom_fetch": True,
     "min_interval_days": seasonal_interval({7, 8}), "quiet_if_empty": True},
    {"name": "craftmonth_tyrone", "venue": "August Craft Month", "town": "",
     "county": "Tyrone", "url": craftmonth_url("tyrone"),
     "parser": parse_craftmonth, "custom_fetch": True,
     "min_interval_days": seasonal_interval({7, 8}), "quiet_if_empty": True},
    {"name": "craftmonth_fermanagh", "venue": "August Craft Month", "town": "",
     "county": "Fermanagh", "url": craftmonth_url("fermanagh"),
     "parser": parse_craftmonth, "custom_fetch": True,
     "min_interval_days": seasonal_interval({7, 8}), "quiet_if_empty": True},
    {"name": "heritageweek", "venue": "Heritage Week", "town": "",
     "county": "Donegal",
     "url": "https://www.heritageweek.ie/event-listings?q=&where%5B%5D=derry"
            "&where%5B%5D=donegal&where%5B%5D=leitrim&where%5B%5D=sligo"
            "&where%5B%5D=tyrone&where%5B%5D=fermanagh",
     "parser": parse_heritageweek, "custom_fetch": True,
     "min_interval_days": seasonal_interval({7, 8}), "quiet_if_empty": True},
    {"name": "thedock", "venue": "The Dock", "town": "Carrick-on-Shannon",
     "county": "Leitrim", "url": "https://www.thedock.ie/whats-on/upcoming-events",
     "parser": parse_thedock},
    {"name": "strule", "venue": "Strule Arts Centre", "town": "Omagh",
     "county": "Tyrone", "url": "https://struleartscentre.co.uk/whats-on/shows/",
     "parser": parse_strule},
    {"name": "ardhowen", "venue": "Ardhowen Theatre", "town": "Enniskillen",
     "county": "Fermanagh", "url": "https://ardhowen.com/whats-on/shows/",
     "parser": parse_strule},
    {"name": "themodel", "venue": "The Model", "town": "Sligo",
     "county": "Sligo", "url": "https://www.themodel.ie/whats-on/",
     "parser": parse_themodel},
    {"name": "playhouse", "venue": "The Playhouse", "town": "Derry",
     "county": "Derry", "url": "https://www.derryplayhouse.co.uk/events",
     "parser": parse_playhouse_derry},
]


ALLOWED_COUNTIES = {"Donegal", "Derry", "Sligo", "Leitrim", "Tyrone", "Fermanagh"}
COUNTY_ALIASES = {
    "Londonderry": "Derry",
    "Derry City": "Derry",
    "Derry/Londonderry": "Derry",
}
# Case-insensitive lookup covering both the six real county names and all
# known Derry variants, so a source producing "derry city", "DERRY", etc
# (rather than our exact expected capitalisation) is still normalised
# correctly instead of silently falling through unaliased or, worse,
# being wrongly excluded from the region entirely.
COUNTY_CANONICAL = {c.lower(): c for c in ALLOWED_COUNTIES}
COUNTY_CANONICAL.update({k.lower(): v for k, v in COUNTY_ALIASES.items()})

CATEGORY_ALIASES = {
    "family": "Kids/Family",
    "family friendly": "Kids/Family",
    "cinema": "Film",
    "spoken word & conversations": "Spoken Word",
    "talks/spoken word": "Spoken Word",
    "talk": "Spoken Word",
    "talks/literary": "Spoken Word",
    "masterclass/workshop": "Workshop",
    "workshops & programmes": "Workshop",
    "visual arts & film": "Visual Arts",
    "classical music": "Music",
    "musical theatre": "Musical",
    "maker talk": "Meet the Maker",
    # fine-grained craft disciplines (mostly August Craft Month's own
    # "Craft Type" taxonomy) - individually each only ever tags a
    # handful of events, cluttering the genre list; grouped into one
    # broader bucket instead
    "ceramics": "Craft/Hobbies",
    "glass making": "Craft/Hobbies",
    "felt": "Craft/Hobbies",
    "basketry & willow": "Craft/Hobbies",
    "blacksmithing": "Craft/Hobbies",
    "furniture making": "Craft/Hobbies",
    "jewellery making": "Craft/Hobbies",
    "music instrument making": "Craft/Hobbies",
    "textile making": "Craft/Hobbies",
    "lettering": "Craft/Hobbies",
    "mixed media construction": "Craft/Hobbies",
    "mosaics": "Craft/Hobbies",
    "printing": "Craft/Hobbies",
    "soap making": "Craft/Hobbies",
    "spinning": "Craft/Hobbies",
    "woodworking": "Craft/Hobbies",
    "fashion design": "Craft/Hobbies",
    "candle making": "Craft/Hobbies",
    "interior furnishings": "Craft/Hobbies",
    "multiple": "Craft/Hobbies",
}
CATEGORY_DROP = {
    "spectacle",  # "Street Arts & Circus" alone covers this well
    "art deco",  # a venue marker (Hawk's Well's Ballymote location), not a genre
}

AGE_RANGE_RE = re.compile(r"\b(\d{1,2})\s*-\s*(\d{1,2})\s*(?:yrs?|years?)\b", re.I)
KIDS_KEYWORDS_RE = re.compile(r"\b(kids?|children'?s?|junior)\b", re.I)


GENERIC_SOLD_OUT_RE = re.compile(r"\(?\bsold\s*out\b\)?", re.I)


def apply_generic_sold_out(ev):
    """Catches 'SOLD OUT' (any capitalisation) appearing anywhere in a
    title from ANY source, stripping it out and setting sold_out - the
    same treatment already built in for EAF and Nerve Centre, just
    generalised as a safety net for every other source too. Skips
    events a source-specific check already handled, so nothing gets
    double-processed."""
    if ev.get("sold_out"):
        return
    title = ev.get("title", "")
    if not re.search(r"\bsold\s*out\b", title, re.I):
        return
    cleaned = GENERIC_SOLD_OUT_RE.sub("", title)
    cleaned = re.sub(r"^[\s\-–—:|/]+|[\s\-–—:|/]+$", "", cleaned)
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    ev["title"] = cleaned or title
    ev["sold_out"] = True


def normalize_category(cat):
    """Renames some genre tags to consolidate near-duplicates (e.g.
    'Cinema' -> 'Film'), and drops a few tokens entirely (CATEGORY_DROP)
    rather than renaming them, where an existing tag already covers the
    same ground well enough on its own.

    Children's content specifically gets a SUBSTRING match ('child' or
    'kids' appearing anywhere in the tag), not just another entry in the
    exact-match alias table below - sources phrase this wildly
    differently ('Children', "Children's Event", "Children's Theatre",
    'For Children', "Kids' Workshop" have all shown up separately), and
    a substring catch-all means a NEW phrasing from some future venue
    gets folded in automatically instead of silently slipping through
    unconsolidated until someone happens to notice and add it by hand."""
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
            out.append(CATEGORY_ALIASES.get(low, p))
    seen, deduped = set(), []
    for p in out:
        if p not in seen:
            seen.add(p)
            deduped.append(p)
    return ", ".join(deduped) if deduped else None


def looks_like_kids_family(title):
    """Catches events with no genre data at all (mainly Eventbrite, which
    has no category field in our pipeline) that are clearly for children
    based on the title: 'kids'/'children's'/'junior', or a hyphenated age
    range whose upper bound is under 18 ('6-11yrs' but not '18+yrs' or
    '25 Years On', neither of which is a hyphenated range at all)."""
    if KIDS_KEYWORDS_RE.search(title):
        return True
    m = AGE_RANGE_RE.search(title)
    return bool(m and int(m.group(2)) < 18)


def apply_kids_family_tag(ev):
    if not looks_like_kids_family(ev["title"]):
        return
    existing = [p.strip() for p in (ev.get("category") or "").split(",") if p.strip()]
    if "Kids/Family" not in existing:
        existing.append("Kids/Family")
    ev["category"] = ", ".join(existing)


SOURCE_PRIORITY = {
    "an_grianan": 0, "rcc": 1, "balor": 2, "abbey": 3,
    "mcgrorys": 4, "st_columbs": 5,
    "eaf": 10, "eventbrite_donegal": 11,
}

DEDUP_STOPWORDS = {"the", "a", "an", "with", "and", "at", "in", "on", "of",
                    "by", "to", "for"}
DEDUP_NOISE_PREFIXES = [
    re.compile(r"^rcc kids:\s*", re.I),
    re.compile(r"^eaf:\s*", re.I),
    re.compile(r"^iadf 2026:\s*", re.I),
]
DEDUP_ALIASES = [
    (re.compile(r"\biadf\b", re.I), "irish aerial dance fest"),
]
DEDUP_THRESHOLD = 0.7


def _dedup_words(title):
    t = title.lower()
    for pat in DEDUP_NOISE_PREFIXES:
        t = pat.sub("", t)
    for pat, repl in DEDUP_ALIASES:
        t = pat.sub(repl, t)
    t = re.sub(r"[^a-z0-9 ]+", " ", t)
    return {w for w in t.split() if w and w not in DEDUP_STOPWORDS}


def _title_containment(a, b):
    """What fraction of the SHORTER title's (stopword-stripped) words
    appear in the longer one - robust to one source truncating or
    padding a title, unlike plain string-similarity ratios."""
    wa, wb = _dedup_words(a), _dedup_words(b)
    if not wa or not wb:
        return 0.0
    smaller, larger = (wa, wb) if len(wa) <= len(wb) else (wb, wa)
    return len(smaller & larger) / len(smaller)


def merge_cross_source_duplicates(events):
    """Events from different sources describing the same real-world
    happening (e.g. a show at An Grianán that's also promoted by the
    Earagail Arts Festival) are merged into one entry. Only ever
    compares events sharing the exact same venue AND date - deliberately
    conservative, so two genuinely different events at the same venue on
    the same day are never wrongly merged, at the cost of occasionally
    missing a real duplicate (e.g. if two sources disagree on an
    exhibition's exact opening date by a few days). Whichever source is
    closer to the venue itself wins the merged details (SOURCE_PRIORITY);
    other sources only backfill fields the winner is missing."""
    groups = {}
    for ev in events:
        groups.setdefault((ev["venue"], ev["date"]), []).append(ev)

    result = []
    for group in groups.values():
        if len(group) == 1:
            result.append(group[0])
            continue

        parent = list(range(len(group)))

        def find(i):
            while parent[i] != i:
                parent[i] = parent[parent[i]]
                i = parent[i]
            return i

        for i in range(len(group)):
            for j in range(i + 1, len(group)):
                if _title_containment(group[i]["title"],
                                       group[j]["title"]) >= DEDUP_THRESHOLD:
                    ri, rj = find(i), find(j)
                    if ri != rj:
                        parent[rj] = ri

        clusters = {}
        for i in range(len(group)):
            clusters.setdefault(find(i), []).append(group[i])

        for members in clusters.values():
            if len(members) == 1:
                result.append(members[0])
                continue
            members.sort(key=lambda e: SOURCE_PRIORITY.get(e["source"], 99))
            winner = dict(members[0])
            for loser in members[1:]:
                for k, v in loser.items():
                    if v and not winner.get(k):
                        winner[k] = v
            winner["merged_from"] = sorted({m["source"] for m in members})
            result.append(winner)
    return result


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
            data.setdefault("location_cache", {})
            data.setdefault("ghostlight_lineup_cache", {})
            return data
        except Exception:
            pass
    return {"events": [], "source_last_run": {}, "consecutive_failures": {},
            "location_cache": {}, "ghostlight_lineup_cache": {}}


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
    global LOCATION_CACHE, GHOSTLIGHT_LINEUP_CACHE
    previous = load_previous()
    prev_by_key = {event_key(e): e for e in previous.get("events", [])}
    LOCATION_CACHE = dict(previous.get("location_cache", {}))
    GHOSTLIGHT_LINEUP_CACHE = dict(previous.get("ghostlight_lineup_cache", {}))

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
            if not found and source.get("quiet_if_empty"):
                print(f"  (zero results - treated as normal for a seasonal "
                      f"source, not a failure)")
                if interval:
                    source_last_run[source["name"]] = NOW.strftime("%Y-%m-%dT%H:%MZ")
                consecutive_failures[source["name"]] = 0
                continue
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
    for ev in merge_cross_source_duplicates(all_events):
        canon = COUNTY_CANONICAL.get((ev.get("county") or "").strip().lower())
        if not canon:
            continue  # not a recognized Donegal/Derry/Sligo/Leitrim/Tyrone/Fermanagh county
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

    DATA_FILE.parent.mkdir(parents=True, exist_ok=True)
    DATA_FILE.write_text(json.dumps({
        "generated_at": NOW.strftime("%Y-%m-%dT%H:%MZ"),
        "failed_sources": failed,
        "source_last_run": source_last_run,
        "consecutive_failures": consecutive_failures,
        "location_cache": LOCATION_CACHE,
        "ghostlight_lineup_cache": GHOSTLIGHT_LINEUP_CACHE,
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
