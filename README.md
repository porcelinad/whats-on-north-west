# Out and About

A one-stop listing of upcoming cultural events, scraped daily from each
venue's own website, viewable on your phone, with push notifications
when new events are announced.

**Live at:** [outandabout.ie](https://outandabout.ie)

The site is organised by region, each with its own path:

- **[outandabout.ie/nw](https://outandabout.ie/nw)** — North West Ireland
  (Donegal, Derry, Sligo, Leitrim, Tyrone, Fermanagh). The original,
  most complete region, with 20+ sources covering theatres, arts
  centres, festivals, and county-wide aggregators.
- **outandabout.ie/galway** — Galway, being built incrementally, one
  venue at a time. Currently just getting started.

Each region has its own scraper, its own `events.json`, and its own
page, but shares the same look, the same underlying helper code (date
parsing, deduplication, genre normalisation), and the same daily
GitHub Actions schedule.

How it works: once a day, GitHub Actions (a free scheduler) runs each
region's scraper script, which reads every venue's public listings
page, saves everything to that region's `events.json`, and compares
against the previous run. Anything new triggers a push notification via
[ntfy](https://ntfy.sh). GitHub Pages serves the whole `docs/` folder as
the website, with `outandabout.ie` pointed at it via custom domain DNS.
Total cost: €0 (aside from the domain registration itself).

---

## Repository structure

```
docs/
  index.html              <- root landing page ("choose your city")
  nw/
    index.html            <- North West site
    events.json           <- North West's scraped data
    outandabout_logo.svg  <- shared site icon
  galway/
    index.html            <- Galway site (once built)
    events.json           <- Galway's scraped data
scraper/
  scrape.py               <- North West's scraper
  manual-imports/
    eventbrite.csv         <- hand-exported Eventbrite data (see below)
.github/workflows/
  scrape.yml              <- runs the scraper(s) on a schedule
```

## Setup (one-time)

### 1. Create a GitHub account
Go to https://github.com/signup if you don't already have one.

### 2. Create the repository
1. Click the **+** in the top-right corner → **New repository**
2. Set it to **Public** (required for free GitHub Pages)
3. Tick **Add a README file**, then click **Create repository**

### 3. Upload the project files
1. In your new repository, click **Add file → Upload files**
2. Drag the *contents* of this project in — the `scraper` folder, `docs`
   folder, and `requirements.txt`. Folders can be dragged in whole and
   their structure is kept.
3. Click **Commit changes**
4. The `.github` folder often won't drag-and-drop (your computer hides
   folders starting with a dot). If it didn't upload: click
   **Add file → Create new file**, type exactly
   `.github/workflows/scrape.yml` as the filename (the slashes create the
   folders), paste in the contents of that file, and commit.

### 4. Pick your secret notification topic
1. Invent a topic name nobody would guess, e.g. `oaa-x7k2p-yourname`
   (ntfy topics are public to anyone who knows the name, so make it obscure)
2. In your repository: **Settings → Secrets and variables → Actions →
   New repository secret**
3. Name: `NTFY_TOPIC` — Value: your topic name → **Add secret**

### 5. Turn on the website
1. **Settings → Pages**
2. Under "Branch", choose `main`, folder `/docs`, click **Save**
3. After a minute or two your site is live at
   `https://YOUR-USERNAME.github.io/YOUR-REPO-NAME/`
4. To use a custom domain instead (like `outandabout.ie`): still in
   **Settings → Pages**, enter it under "Custom domain". At your domain
   registrar's DNS settings, add four **A records** for the bare domain
   pointing at `185.199.108.153`, `185.199.109.153`, `185.199.110.153`,
   and `185.199.111.153`, plus (optional) a **CNAME record** for `www`
   pointing at `YOUR-USERNAME.github.io`. DNS changes can take anywhere
   from minutes to a few hours to take effect; GitHub shows a checkmark
   here once it's verified, and offers to enable HTTPS automatically.
5. Optional, for a "tap the notification to open the site" shortcut:
   **Settings → Secrets and variables → Actions → Variables tab →
   New repository variable**, name `PAGE_URL`, value = your site's address.

### 6. Run the first scrape
1. Go to the **Actions** tab (approve/enable workflows if asked)
2. Click **Scrape events** in the left sidebar → **Run workflow** → green
   **Run workflow** button
3. Wait ~1 minute; a green tick means it worked. Refresh your website —
   events should now appear.
4. The first run never sends notifications (you'd get one giant blast of
   every event). From the next run on, only genuinely new announcements
   notify.

### 7. Set up your phone
1. Install **ntfy** from the Play Store (or App Store on iOS)
2. Open it → **+** → subscribe to your exact topic name from step 4
3. Open your website in Chrome → menu (⋮) → **Add to Home screen**

Done. It now runs itself automatically.

---

## When the scraper actually runs

GitHub Actions cron is always specified in UTC with no daylight-saving
awareness, so the schedule uses **two** cron entries to land at
"shortly after midnight" Irish local time year-round:

- **April–October** (BST, Irish clocks at UTC+1): fires at 23:15 UTC,
  landing at 00:15 BST
- **November–March** (GMT, Irish clocks at UTC+0): fires at 00:15 UTC,
  landing at 00:15 GMT

This is approximated by calendar month rather than the exact
last-Sunday transition dates, so the week or so either side of the
actual clock change may land up to an hour off — a minor tradeoff
against being off by 7+ hours every single day, which is what a single
fixed time would mean for at least one of the two seasons.

You can always trigger a manual run any time via **Actions → Scrape
events → Run workflow**, regardless of the schedule.

## Day-to-day

- **The website** shows all upcoming events, newest announcements
  flagged **NEW**, filterable by county, location, and genre, searchable.
  Tap through to book.
- **Notifications** arrive only when a venue announces something new.
- **Some sources refresh more often than daily during their own active
  season** (e.g. Earagail Arts Festival and August Craft Month check
  every single day through July/August, since that's when new listings
  actually appear, then drop back to a quieter check the rest of the
  year) — this is intentional, not a bug if you notice a heavier or
  lighter workflow run at different times of year.
- **If a venue redesigns its site**, that source will show a ⚠ warning
  at the top of the page and the daily run's log will say which venue
  failed. Its previously-found events are kept, so nothing vanishes. To
  fix it: open the failed run in the Actions tab, copy the log, and
  paste it to Claude along with the venue's URL — you'll get corrected
  parser code to paste into the relevant `scrape.py` (edit files on
  GitHub with the pencil icon).

## Adding a new venue later

Each venue is typically 20-60 lines in the relevant `scrape.py`: a
parser function plus an entry in the `SOURCES` list. Give Claude the
venue's what's-on URL and ask for a parser in the same style; paste it
in via the pencil-icon editor. For a venue whose listing page is
rendered by JavaScript (so a plain fetch shows an empty page), Claude
can walk you through pulling the real HTML via a small Python notebook
instead.

## Adding a new region

Each region (North West, Galway, and any future one) gets its own
scraper script, its own county/town whitelist, and its own
`events.json`, output to its own `docs/<region>/` folder — while
reusing shared logic (date inference, cross-source deduplication,
genre normalisation) rather than duplicating it outright. The
front-end (`index.html`) needs no region-specific code at all: every
filter is built dynamically from whatever's in that region's
`events.json`, so a new region's page is mostly a copy of an existing
one with a fresh coat of branding.

## Being a polite scraper

This project makes at most a handful of requests per venue per day
(more only for a source with its own per-event detail pages, and even
then, capped and gently paced), identifies itself in its User-Agent,
and links every event back to the venue's own site and box office — it
sends venues traffic rather than taking it. If a venue ever objects,
remove it from `SOURCES`.

---

# Manual Eventbrite import

Eventbrite blocks requests from GitHub's servers, so this one source is
updated by hand instead of automatically.

## How to refresh it

1. Open Chrome, go to Eventbrite's search page for your region (e.g.
   `https://www.eventbrite.ie/d/ireland--donegal/all-events/`)
2. Open DevTools → **Web Scraper** tab (your existing sitemap should be there)
3. Run the scrape, then **Export data as CSV**
4. In this repo, go to `scraper/manual-imports/eventbrite.csv`
5. Click the pencil (✏️) to edit, or use **Add file → Upload files** and
   drag your new export in — always keep the filename **exactly**
   `eventbrite.csv`, overwriting the old one each time
6. Commit, then trigger a workflow run (Eventbrite isn't throttled, so
   it re-reads the CSV fresh on every run — no special "full scrape"
   needed)

Do this whenever convenient - weekly is plenty. If you skip a week,
nothing breaks: the site just keeps showing last week's Eventbrite
events until you upload a fresh file.

## How dates are handled

Explicit dates ("Fri 31 Jul, 18:30") always work correctly. Relative
dates ("Today", "Tomorrow", or a bare weekday like "Thursday at 11:00")
are resolved against the day the CSV was actually **captured** (tracked
automatically), not whenever the scraper happens to run afterwards —
this matters because a bare weekday can otherwise silently roll a full
week forward if even a day or two passes between exporting the CSV and
it being processed. Once a CSV goes more than a day stale, only its
unambiguous explicit dates keep being used; relative-only rows are
dropped rather than risking the wrong day.

## Why some irrelevant events might show up

The CSV has no category information, so non-cultural events (markets,
sports, workshops) that happen to appear in the search results will
show up too. Either delete those rows from the CSV before uploading, or
just leave them - they're fully searchable/filterable on the site like
everything else.
