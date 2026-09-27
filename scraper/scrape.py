"""
North West What's On - event scraper
Scrapes cultural venues in Donegal / Sligo / Derry into docs/nw/events.json
and sends an ntfy push notification when new events appear.

Each venue has its own small parser. They all work the same way:
walk the page top-to-bottom, spot event links and date text, and pair
them up. This avoids relying on fragile CSS class names, so minor site
redesigns are less likely to break things.

Shared, region-agnostic logic (date resolution, generic dedup, the
page-walking helpers) lives in common.py, reused by every region's
scraper. Everything below is specific to the North West: its venues,
its town/county whitelist, its category vocabulary.
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

from common import (
    HEADERS, TIMEOUT, NOW, TODAY, MONTHS, WEEKDAY_INDEX,
    fetch, fetch_text, clean, walk, make_event,
    infer_year, infer_range_years, resolve_date_tokens,
    make_town_resolver, cached_lookup,
    apply_generic_sold_out, looks_like_kids_family, apply_kids_family_tag,
    merge_cross_source_duplicates, event_key, load_previous, notify,
)

# ---------------------------------------------------------------- config

ROOT = Path(__file__).resolve().parent.parent
DATA_FILE = ROOT / "docs" / "nw" / "events.json"

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

NTFY_TOPIC = os.environ.get("NTFY_TOPIC", "").strip()
PAGE_URL = os.environ.get("PAGE_URL", "").strip()

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


def parse_an_grianan(soup, source):
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
        else:
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
    section = soup.find("section", class_="em-event-content")
    if not section:
        return None
    for p in section.find_all("p"):
        text = clean(p.get_text())
        if text:
            return text
    return None


def parse_balor(source):
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


def extract_server_data(html):
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

# Originally added for Eventbrite specifically, but the same GitHub-
# Actions-IP-blocking pattern (a site that works fine from anywhere else
# but blocks/empties out for GH Actions' own server IPs) has since shown
# up on other sites too (Abbey Arts Centre, The Playhouse), so this is
# now a general-purpose fallback, not an Eventbrite-only one.
PROXY_TEMPLATE = "https://api.allorigins.win/raw?url={}"


def fetch_eventbrite_page(url):
    try:
        return fetch_text(url, headers=EVENTBRITE_HEADERS)
    except Exception as direct_exc:
        proxy_url = PROXY_TEMPLATE.format(quote(url, safe=""))
        try:
            print(f"  direct fetch blocked ({direct_exc}); trying proxy...")
            return fetch_text(proxy_url, headers=EVENTBRITE_HEADERS)
        except Exception:
            raise direct_exc


def fetch_with_proxy_fallback(url):
    """Like fetch(), but retries through the same public proxy if the
    direct request is blocked - for any source hitting the same kind of
    GitHub-Actions-IP-blocking issue Eventbrite has, confirmed by the
    site loading fine from elsewhere (a browser, this codebase's own
    web-fetch tooling) while GH Actions' own server gets a 403 or an
    unexpectedly empty response. Returns parsed BeautifulSoup either
    way, so it's a drop-in replacement for fetch() at the call site."""
    try:
        return fetch(url)
    except Exception as direct_exc:
        proxy_url = PROXY_TEMPLATE.format(quote(url, safe=""))
        try:
            print(f"  direct fetch blocked ({direct_exc}); trying proxy...")
            html = fetch_text(proxy_url)
            return BeautifulSoup(html, "lxml")
        except Exception:
            raise direct_exc


def parse_eventbrite(source):
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


def parse_abbey_with_fallback(source):
    """Abbey's site loads fine from a browser or this codebase's own
    web-fetch tooling, but returns an empty page specifically to GitHub
    Actions' server IPs - the same kind of blocking Eventbrite has, just
    manifesting as an empty response rather than an explicit error."""
    soup = fetch_with_proxy_fallback(source["url"])
    return parse_abbey(soup, source)


MANUAL_CSV_PATH = ROOT / "scraper" / "manual-imports" / "eventbrite.csv"

TOWN_TO_COUNTY = {
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
    "derry": "Derry", "londonderry": "Derry", "limavady": "Derry",
    "coleraine": "Derry", "magherafelt": "Derry", "maghera": "Derry",
    "garvagh": "Derry", "eglinton": "Derry",
    "omagh": "Tyrone", "strabane": "Tyrone", "dungannon": "Tyrone",
    "cookstown": "Tyrone", "castlederg": "Tyrone", "fintona": "Tyrone",
    "sion mills": "Tyrone",
    "carrick-on-shannon": "Leitrim", "carrick on shannon": "Leitrim",
    "manorhamilton": "Leitrim", "ballinamore": "Leitrim",
    "drumshanbo": "Leitrim", "mohill": "Leitrim", "kinlough": "Leitrim",
    "dromahair": "Leitrim", "rossinver": "Leitrim", "drumkeeran": "Leitrim",
    "newtowngore": "Leitrim", "aughavas": "Leitrim",
    "sligo": "Sligo", "tubbercurry": "Sligo", "ballymote": "Sligo",
    "enniscrone": "Sligo", "strandhill": "Sligo", "grange": "Sligo",
    "rosses point": "Sligo", "collooney": "Sligo", "coolaney": "Sligo",
    "riverstown": "Sligo", "dromore west": "Sligo", "easkey": "Sligo",
    "garrison": "Fermanagh", "enniskillen": "Fermanagh", "belleek": "Fermanagh",
    "kesh": "Fermanagh", "lisnaskea": "Fermanagh", "irvinestown": "Fermanagh",
    "belcoo": "Fermanagh", "derrygonnelly": "Fermanagh",
}


_COUNTY_NAME_TOWNS = {"donegal", "derry", "sligo", "leitrim", "tyrone", "fermanagh"}
find_specific_town, nearest_known_town = make_town_resolver(
    TOWN_TO_COUNTY, _COUNTY_NAME_TOWNS)


LOCATION_CACHE = {}
GHOSTLIGHT_LINEUP_CACHE = {}


def parse_eventbrite_date_text(text, trust_relative=True, reference_date=None):
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
    t = title.lower()
    t = unicodedata.normalize("NFKD", t).encode("ascii", "ignore").decode("ascii")
    t = t.replace("'", "")
    t = re.sub(r"[^a-z0-9]+", "-", t)
    return t.strip("-")


CSV_STALE_AFTER_DAYS = 1


def csv_freshness_check(prev_state):
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
    if not MANUAL_CSV_PATH.exists():
        return None
    trust_relative = (csv_freshness_check(prev_state)
                      if prev_state is not None else True)
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
                continue
            venue_text = clean(row.get("data5") or row.get("data13") or "")
            town = venue = None
            if "·" in venue_text:
                town, venue = (p.strip() for p in venue_text.split("·", 1))
            else:
                venue = venue_text or None
            county = TOWN_TO_COUNTY.get((town or "").strip().lower())
            if not county:
                continue
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
            pass
        time.sleep(0.4)
    return events


def parse_mcgrorys(soup, source):
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
                    continue
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
    return any(MONTHS.get(m.group(2).lower()[:3])
               for m in NERVE_DATE_TOKEN_RE.finditer(text))


def parse_nervecentre(soup, source):
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
            return
        if not date_line:
            return
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
    listing = fetch(source["url"])
    events = parse_hawkswell_listing(listing, source)
    for ev in events:
        try:
            detail = fetch(ev["url"])
            venue, town = parse_hawkswell_event_page(detail)
            ev["venue"] = venue
            ev["town"] = town
        except Exception:
            pass
        time.sleep(0.4)
    return events


CRAFTMONTH_DATE_RE = re.compile(
    r"(\d{1,2})\s+([A-Za-z]+)\s+to\s+(\d{1,2})\s+([A-Za-z]+)", re.I)


def parse_craftmonth_listing(soup, source):
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
        town = find_specific_town(title) or find_specific_town(fields.get("maker"))

        ev = make_event(
            source, title, start,
            end_date=end.isoformat() if end and end != start else None,
            url=href, category=", ".join(cat_bits) if cat_bits else None,
            venue=fields.get("maker"), county=county)
        ev["town"] = town
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
    texts = [a for kind, a, b in walk(soup) if kind == "text"]
    for i, t in enumerate(texts):
        if t.strip().lower() == "event address:" and i + 1 < len(texts):
            return find_specific_town(texts[i + 1])
    return None


def parse_craftmonth(source):
    soup = fetch(source["url"])
    events = parse_craftmonth_listing(soup, source)
    for ev in events:
        town = cached_lookup(
            LOCATION_CACHE, ev["url"],
            lambda ev=ev: parse_craftmonth_event_page(fetch(ev["url"])))
        if town:
            ev["town"] = town
    return events


HERITAGEWEEK_DATE_RE = re.compile(r"^(\d{1,2})\s+([A-Za-z]+)\b")


def parse_heritageweek_page(soup, source):
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
        ev["town"] = town
        events.append(ev)
    return events


def parse_heritageweek_event_page(soup):
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
        town = cached_lookup(
            LOCATION_CACHE, ev["url"],
            lambda ev=ev: parse_heritageweek_event_page(fetch(ev["url"])))
        if town:
            ev["town"] = town
    return events


THEDOCK_DATE_TOKEN_RE = re.compile(r"(\d{1,2})\s+([A-Za-z]+)(?:\s+(\d{4}))?")


def parse_thedock_date(text):
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
    events = []
    for card in soup.select(".card-show"):
        title_tag = card.select_one("h2")
        link_tag = card.select_one("a.stretched-link")
        date_tag = card.select_one("p.published-date")
        if not (title_tag and link_tag and date_tag):
            continue
        date_text = clean(date_tag.get_text())
        if not date_text:
            continue
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
            continue
        events.append(make_event(
            source, clean(title_tag.get_text()), start,
            end_date=end.isoformat() if end else None,
            time=time_text, url=href))
    return events


PLAYHOUSE_DATE_RE = re.compile(r"(\d{1,2})(?:st|nd|rd|th)\s+([A-Za-z]+)\s+(\d{4})", re.I)


def parse_playhouse_derry(soup, source):
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


def parse_playhouse_derry_with_fallback(source):
    """derryplayhouse.co.uk loads fine from a browser or this codebase's
    own web-fetch tooling, but returns a 403 specifically to GitHub
    Actions' server IPs - the same kind of blocking Eventbrite has."""
    soup = fetch_with_proxy_fallback(source["url"])
    return parse_playhouse_derry(soup, source)


CRAFTMONTH_START = TODAY.strftime("%Y%m%d")
CRAFTMONTH_END = (TODAY + timedelta(days=120)).strftime("%Y%m%d")


def seasonal_interval(active_months, quiet_interval=3):
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
     "parser": parse_abbey_with_fallback, "custom_fetch": True},
    {"name": "eventbrite_donegal", "venue": "Eventbrite (Donegal)",
     "town": "Donegal", "county": "Donegal", "region_filter": "Donegal",
     "url": "https://www.eventbrite.ie/d/ireland--donegal/all-events/",
     "parser": parse_eventbrite_csv, "manual_csv": True},
    {"name": "eaf", "venue": "Earagail Arts Festival", "town": "Donegal",
     "county": "Donegal", "url": "https://eaf.ie/2026-events",
     "parser": parse_eaf, "custom_fetch": True,
     "min_interval_days": seasonal_interval({7, 8}, quiet_interval=7),
     "quiet_if_empty": True},
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
     "parser": parse_playhouse_derry_with_fallback, "custom_fetch": True},
]


ALLOWED_COUNTIES = {"Donegal", "Derry", "Sligo", "Leitrim", "Tyrone", "Fermanagh"}
COUNTY_ALIASES = {
    "Londonderry": "Derry",
    "Derry City": "Derry",
    "Derry/Londonderry": "Derry",
}
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
    "spectacle",
    "art deco",
}


def normalize_category(cat):
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


SOURCE_PRIORITY = {
    "an_grianan": 0, "rcc": 1, "balor": 2, "abbey": 3,
    "mcgrorys": 4, "st_columbs": 5,
    "eaf": 10, "eventbrite_donegal": 11,
}

DEDUP_NOISE_PREFIXES = [
    re.compile(r"^rcc kids:\s*", re.I),
    re.compile(r"^eaf:\s*", re.I),
    re.compile(r"^iadf 2026:\s*", re.I),
]
DEDUP_ALIASES = [
    (re.compile(r"\biadf\b", re.I), "irish aerial dance fest"),
]
DEDUP_THRESHOLD = 0.7


def main():
    global LOCATION_CACHE, GHOSTLIGHT_LINEUP_CACHE
    previous = load_previous(DATA_FILE, extra_defaults={
        "location_cache": {}, "ghostlight_lineup_cache": {}})
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
