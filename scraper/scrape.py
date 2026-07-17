"""
North West What's On - event scraper
Scrapes cultural venues in Donegal / Sligo / Derry into docs/events.json
and sends an ntfy push notification when new events appear.

Each venue has its own small parser. They all work the same way:
walk the page top-to-bottom, spot event links and date text, and pair
them up. This avoids relying on fragile CSS class names, so minor site
redesigns are less likely to break things.
"""

import hashlib
import json
import os
import re
import sys
import time
from datetime import date, timedelta
from pathlib import Path

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
TIMEOUT = 30
TODAY = date.today()

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
]


# ---------------------------------------------------------------- pipeline

def event_key(ev):
    raw = f"{ev['source']}|{ev['title'].lower()}|{ev['date']}"
    return hashlib.sha1(raw.encode()).hexdigest()[:12]


def load_previous():
    if DATA_FILE.exists():
        try:
            return json.loads(DATA_FILE.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {"events": []}


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
    for source in SOURCES:
        try:
            soup = fetch(source["url"])
            found = source["parser"](soup, source)
            print(f"{source['venue']}: {len(found)} events")
            if not found:
                snippet = soup.get_text(" ", strip=True)[:200]
                raise ValueError(
                    "parsed zero events - selectors may be stale, or the "
                    f"site blocked this request. Page preview: {snippet!r}")
            all_events.extend(found)
        except Exception as exc:
            failed.append(source["venue"])
            print(f"WARNING {source['venue']} failed: {exc}", file=sys.stderr)
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
        "generated_at": TODAY.isoformat(),
        "failed_sources": failed,
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
