"""
Shared helpers used by every region's scraper (scrape.py for the North
West, scrape_galway.py for Galway, and any future region). Nothing in
here knows about a specific county, town, or venue - all of that stays
in each region's own script. Region-specific behaviour that this module
still needs (a town whitelist, source priorities, an output file path)
is always passed in as an argument, never hardcoded here.
"""

import hashlib
import json
import re
import sys
import time
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import requests
from bs4 import BeautifulSoup, NavigableString

# ---------------------------------------------------------------- config

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-GB,en;q=0.9",
}
TIMEOUT = (8, 20)
NOW = datetime.now(timezone.utc)
# TODAY reflects the Irish calendar date, not the UTC one - Ireland is
# UTC+1 during summer (BST), so Irish midnight happens at 23:00 UTC the
# day before. Using raw UTC here meant that for roughly the first hour of
# every Irish day (00:00-01:00 IST), TODAY was still "yesterday" by UTC's
# clock, so that day's already-past single-day events weren't dropped yet.
# This applies equally to every region, not just the North West.
TODAY = NOW.astimezone(ZoneInfo("Europe/Dublin")).date()

MONTHS = {}
for i, name in enumerate(
    ["january", "february", "march", "april", "may", "june", "july",
     "august", "september", "october", "november", "december"], start=1
):
    MONTHS[name] = i
    MONTHS[name[:3]] = i

WEEKDAY_INDEX = {name: i for i, name in enumerate(
    ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"])}


# ---------------------------------------------------------------- fetching

def fetch(url, headers=None):
    last_exc = None
    for attempt in range(2):
        try:
            r = requests.get(url, headers=headers or HEADERS, timeout=TIMEOUT)
            r.raise_for_status()
            return BeautifulSoup(r.text, "lxml")
        except Exception as exc:
            last_exc = exc
            if attempt < 1:
                time.sleep(2)
    raise last_exc


def fetch_text(url, headers=None):
    """Like fetch(), but returns raw response text instead of parsed HTML -
    useful for a source that embeds data as JSON in the raw page rather
    than in the rendered markup (e.g. Eventbrite's window.__SERVER_DATA__)."""
    last_exc = None
    for attempt in range(2):
        try:
            r = requests.get(url, headers=headers or HEADERS, timeout=TIMEOUT)
            r.raise_for_status()
            return r.text
        except Exception as exc:
            last_exc = exc
            if attempt < 1:
                time.sleep(2)
    raise last_exc


def clean(text):
    return " ".join(str(text).split())


def walk(soup):
    """Yield ('text', str, None) and ('link', href, text) in document
    order. This avoids relying on fragile CSS class names, so minor site
    redesigns are less likely to break a parser built on it."""
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


# ---------------------------------------------------------------- dates

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
    needing inference for all of them. Resolves right-to-left: the last
    token uses its own explicit year if given, else infer_year(); each
    earlier token uses its own explicit year if given, else the same
    year as the token after it, correcting back one year only if that
    would put it after the following token (a genuine year-boundary-
    crossing range)."""
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


# ---------------------------------------------------------------- town matching

def _proper_town_case(key):
    """'carrick-on-shannon' -> 'Carrick-on-Shannon', 'sion mills' ->
    'Sion Mills'."""
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


def make_town_resolver(town_to_county, county_name_towns):
    """Builds a (find_specific_town, nearest_known_town) pair bound to
    one region's own town whitelist - so each region can have its own
    town/county data while sharing the exact same matching logic.

    town_to_county: dict of {lowercase town name: county name}.
    county_name_towns: the subset of town_to_county's keys that are
    themselves bare county names (e.g. Donegal's whitelist includes
    "donegal" -> "Donegal", meaning the county name is ALSO a valid
    "town" value in its own right, e.g. Eventbrite's "Donegal" meaning
    Donegal Town) - these only match as a LAST resort, so a bare county
    name mentioned inside a longer address never wins over a real, more
    specific town name mentioned in the same text.

    find_specific_town(text) matches a genuinely specific town only -
    NEVER a bare county name - and returns None when nothing matches,
    so callers can tell "found a real town" apart from "found nothing".
    Useful for checking a title/venue string for a town mention, where
    falling back to a bare county name would be actively misleading
    rather than just imprecise.

    nearest_known_town(raw_town) reduces a raw, possibly overly-specific
    or compound town/address string down to the nearest real town from
    the whitelist, matching anywhere in the string as a whole word, not
    just an exact full match. Falls back to the original value unchanged
    if nothing is found anywhere in it."""
    display_case = {k: _proper_town_case(k) for k in town_to_county}
    specific_order = sorted(
        (k for k in town_to_county if k not in county_name_towns),
        key=len, reverse=True)
    fallback_order = sorted(
        (k for k in town_to_county if k in county_name_towns),
        key=len, reverse=True)

    def find_specific_town(text):
        if not text:
            return None
        low = text.lower()
        for key in specific_order:
            if re.search(r"\b" + re.escape(key) + r"\b", low):
                return display_case[key]
        return None

    def nearest_known_town(raw_town):
        if not raw_town:
            return raw_town
        found = find_specific_town(raw_town)
        if found:
            return found
        low = raw_town.lower()
        for key in fallback_order:
            if re.search(r"\b" + re.escape(key) + r"\b", low):
                return display_case[key]
        return raw_town

    return find_specific_town, nearest_known_town


# ---------------------------------------------------------------- caching

def cached_lookup(cache, key, fetch_and_extract_fn):
    """Returns the cached value for this key if known; otherwise calls
    fetch_and_extract_fn() to compute one. Only caches a genuine
    'fetched fine, computed a value (possibly None)' result - a fetch
    that failed outright is left uncached so it's retried next run,
    rather than being permanently remembered as unresolvable."""
    if key in cache:
        return cache[key]
    try:
        value = fetch_and_extract_fn()
    except Exception:
        return None
    cache[key] = value
    return value


# ---------------------------------------------------------------- genre/tags

AGE_RANGE_RE = re.compile(r"\b(\d{1,2})\s*-\s*(\d{1,2})\s*(?:yrs?|years?)\b", re.I)
KIDS_KEYWORDS_RE = re.compile(r"\b(kids?|children'?s?|junior)\b", re.I)
GENERIC_SOLD_OUT_RE = re.compile(r"\(?\bsold\s*out\b\)?", re.I)


def apply_generic_sold_out(ev):
    """Catches 'SOLD OUT' (any capitalisation) appearing anywhere in a
    title from ANY source, stripping it out and setting sold_out - a
    safety net for any source without its own dedicated sold-out
    detection. Skips events a source-specific check already handled."""
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


def looks_like_kids_family(title):
    """Catches events with no genre data at all that are clearly for
    children based on the title: 'kids'/'children's'/'junior', or a
    hyphenated age range whose upper bound is under 18 ('6-11yrs' but
    not '18+yrs' or '25 Years On', neither of which is a hyphenated
    range at all)."""
    if KIDS_KEYWORDS_RE.search(title):
        return True
    m = AGE_RANGE_RE.search(title)
    return bool(m and int(m.group(2)) < 18)


def apply_kids_family_tag(ev, label="Kids/Family"):
    if not looks_like_kids_family(ev["title"]):
        return
    existing = [p.strip() for p in (ev.get("category") or "").split(",") if p.strip()]
    if label not in existing:
        existing.append(label)
    ev["category"] = ", ".join(existing)


# ---------------------------------------------------------------- dedup

DEDUP_STOPWORDS = {"the", "a", "an", "with", "and", "at", "in", "on", "of",
                    "by", "to", "for"}


def _dedup_words(title, noise_prefixes=(), aliases=()):
    t = title.lower()
    for pat in noise_prefixes:
        t = pat.sub("", t)
    for pat, repl in aliases:
        t = pat.sub(repl, t)
    t = re.sub(r"[^a-z0-9 ]+", " ", t)
    return {w for w in t.split() if w and w not in DEDUP_STOPWORDS}


def _title_containment(a, b, noise_prefixes=(), aliases=()):
    """What fraction of the SHORTER title's (stopword-stripped) words
    appear in the longer one - robust to one source truncating or
    padding a title, unlike plain string-similarity ratios."""
    wa = _dedup_words(a, noise_prefixes, aliases)
    wb = _dedup_words(b, noise_prefixes, aliases)
    if not wa or not wb:
        return 0.0
    smaller, larger = (wa, wb) if len(wa) <= len(wb) else (wb, wa)
    return len(smaller & larger) / len(smaller)


def merge_cross_source_duplicates(events, source_priority=None,
                                   noise_prefixes=(), aliases=(),
                                   threshold=0.7, merge_same_source=True):
    """Events from different sources describing the same real-world
    happening (e.g. a show at a venue that's also promoted by a
    festival or a tourism-board aggregator) are merged into one entry.
    Only ever compares events sharing the exact same venue AND date -
    deliberately conservative, so two genuinely different events at the
    same venue on the same day are never wrongly merged, at the cost of
    occasionally missing a real duplicate. Whichever source has the
    lowest number in source_priority wins the merged details; sources
    not listed default to the lowest priority (99). Other sources only
    backfill fields the winner is missing.

    noise_prefixes/aliases let a region strip its own recurring title
    noise (e.g. 'RCC Kids: ') or normalise its own recurring
    abbreviations (e.g. 'IADF' -> 'irish aerial dance fest') before
    comparing titles for similarity - pass empty tuples for a region
    with no such patterns yet.

    merge_same_source: by default (kept for the North West scraper's
    existing behaviour) two events from the SAME source can also be
    merged if their titles are similar enough. That is wrong for a
    source that legitimately lists several similarly-titled events on
    one day at one venue - e.g. 'Beethoven Quartets - Saturday 3pm' and
    '... Saturday 7:30pm' are two separate ticketed concerts, but score
    0.75 against a 0.7 threshold and one silently vanishes. Pass False
    to only ever merge events from DIFFERENT sources."""
    source_priority = source_priority or {}
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
                if (not merge_same_source
                        and group[i]["source"] == group[j]["source"]):
                    continue
                if _title_containment(group[i]["title"], group[j]["title"],
                                       noise_prefixes, aliases) >= threshold:
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
            members.sort(key=lambda e: source_priority.get(e["source"], 99))
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


def load_previous(data_file, extra_defaults=None):
    """Loads a region's previous run's output. extra_defaults lets a
    region add its own top-level keys (e.g. a per-region cache) that
    should always exist with a sane empty default, without this shared
    function needing to know what they're called."""
    defaults = {"events": [], "source_last_run": {}, "consecutive_failures": {}}
    defaults.update(extra_defaults or {})
    if data_file.exists():
        try:
            data = json.loads(data_file.read_text(encoding="utf-8"))
            for key, default in defaults.items():
                data.setdefault(key, default)
            return data
        except Exception:
            pass
    return dict(defaults)


def notify(new_events, ntfy_topic, page_url=None):
    if not ntfy_topic or not new_events:
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
    if page_url:
        headers["Click"] = page_url
    try:
        requests.post(f"https://ntfy.sh/{ntfy_topic}",
                      data="\n".join(lines).encode("utf-8"),
                      headers=headers, timeout=TIMEOUT)
        print(f"Sent ntfy notification for {len(new_events)} new event(s)")
    except Exception as exc:  # never fail the run over a notification
        print(f"ntfy notification failed: {exc}", file=sys.stderr)
