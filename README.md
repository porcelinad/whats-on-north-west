# North West What's On

A one-stop listing of upcoming cultural events in the north west of Ireland,
scraped daily from each venue's own website, viewable on your phone, with
push notifications when new events are announced.

**Current sources:** An Grianán Theatre (Letterkenny) · Regional Cultural
Centre (Letterkenny) · Balor Arts Centre (Ballybofey) · Abbey Arts Centre
(Ballyshannon)

How it works: once a day, GitHub Actions (a free scheduler) runs
`scraper/scrape.py`, which reads each venue's public listings page, saves
everything to `docs/events.json`, and compares against yesterday. Anything
new triggers a push notification via [ntfy](https://ntfy.sh). GitHub Pages
serves `docs/index.html` as your website. Total cost: €0.

---

## Setup (one-time, ~20 minutes)

### 1. Create a GitHub account
Go to https://github.com/signup if you don't already have one.

### 2. Create the repository
1. Click the **+** in the top-right corner → **New repository**
2. Name it `nw-whats-on` (or anything you like)
3. Set it to **Public** (required for free GitHub Pages)
4. Tick **Add a README file**, then click **Create repository**

### 3. Upload these files
1. In your new repository, click **Add file → Upload files**
2. Drag the *contents* of this folder in — the `scraper` folder, `docs`
   folder, and `requirements.txt`. Folders can be dragged in whole and
   their structure is kept.
3. Click **Commit changes**
4. The `.github` folder often won't drag-and-drop (your computer hides
   folders starting with a dot). If it didn't upload: click
   **Add file → Create new file**, type exactly
   `.github/workflows/scrape.yml` as the filename (the slashes create the
   folders), paste in the contents of that file, and commit.

### 4. Pick your secret notification topic
1. Invent a topic name nobody would guess, e.g. `nw-culture-x7k2p-yourname`
   (ntfy topics are public to anyone who knows the name, so make it obscure)
2. In your repository: **Settings → Secrets and variables → Actions →
   New repository secret**
3. Name: `NTFY_TOPIC` — Value: your topic name → **Add secret**

### 5. Turn on the website
1. **Settings → Pages**
2. Under "Branch", choose `main`, folder `/docs`, click **Save**
3. After a minute or two your site is live at
   `https://YOUR-USERNAME.github.io/nw-whats-on/`
4. Optional, for a "tap the notification to open the site" shortcut:
   **Settings → Secrets and variables → Actions → Variables tab →
   New repository variable**, name `PAGE_URL`, value = that address.

### 6. Run the first scrape
1. Go to the **Actions** tab (approve/enable workflows if asked)
2. Click **Scrape events** in the left sidebar → **Run workflow** → green
   **Run workflow** button
3. Wait ~1 minute; a green tick means it worked. Refresh your website —
   events should now appear.
4. The first run never sends notifications (you'd get one giant blast of
   every event). From tomorrow, only genuinely new announcements notify.

### 7. Set up your phone
1. Install **ntfy** from the Play Store
2. Open it → **+** → subscribe to your exact topic name from step 4
3. Open your website in Chrome → menu (⋮) → **Add to Home screen**

Done. It now runs itself every morning at 8am Irish time.

---

## Day-to-day

- **The website** shows all upcoming events, newest announcements flagged
  **NEW**, filterable by venue, searchable. Tap through to book.
- **Notifications** arrive only when a venue announces something new.
- **If a venue redesigns its site**, that source will show a ⚠ warning at
  the top of the page and the daily run's log will say which venue failed.
  Its previously-found events are kept, so nothing vanishes. To fix it:
  open the failed run in the Actions tab, copy the log, and paste it to
  Claude along with the venue's URL — you'll get corrected parser code to
  paste into `scraper/scrape.py` (edit files on GitHub with the pencil icon).

## Adding a new venue later

Each venue is ~25 lines in `scraper/scrape.py`: a parser function plus an
entry in the `SOURCES` list. Give Claude the venue's what's-on URL and ask
for a parser in the same style; paste it in via the pencil-icon editor.

## Being a polite scraper

This project makes **one request per venue per day**, identifies itself in
its User-Agent, and links every event back to the venue's own site and box
office — it sends the venues traffic rather than taking it. If a venue ever
objects, remove it from `SOURCES`.
