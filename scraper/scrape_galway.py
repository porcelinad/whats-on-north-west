name: Scrape Galway events
on:
  schedule:
    # Same two-schedule trick as the North West workflow (GitHub's cron
    # is UTC-only with no daylight-saving awareness): one entry for
    # Apr-Oct (BST, UTC+1), one for Nov-Mar (GMT, UTC+0), each landing at
    # 00:30 Irish local time. Deliberately 15 minutes AFTER the North
    # West run (00:15), since both workflows commit and push to the same
    # branch - staggering them keeps them from racing each other. The
    # month-based approximation (rather than exact clock-change dates)
    # can be up to an hour off for the week or so around a clock change.
    - cron: "30 23 * 4-10 *"       # Apr-Oct (BST): 23:30 UTC = 00:30 BST
    - cron: "30 0 * 11,12,1,2,3 *" # Nov-Mar (GMT): 00:30 UTC = 00:30 GMT
  workflow_dispatch: {}    # manual "Run workflow" button still works too
permissions:
  contents: write
jobs:
  scrape:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with:
          python-version: "3.12"
      - name: Install dependencies
        run: pip install -r requirements.txt
      - name: Run Galway scraper
        env:
          # Both optional - safely resolve to empty strings if these
          # secrets/variables don't exist yet, which just means no push
          # notifications are sent (fine for now, not live yet). Add
          # NTFY_TOPIC_GALWAY / PAGE_URL_GALWAY as repo secrets/variables
          # whenever Galway notifications are wanted.
          NTFY_TOPIC_GALWAY: ${{ secrets.NTFY_TOPIC_GALWAY }}
          PAGE_URL_GALWAY: ${{ vars.PAGE_URL_GALWAY }}
        run: python scraper/scrape_galway.py
      - name: Commit updated events
        run: |
          git config user.name "events-bot"
          git config user.email "actions@users.noreply.github.com"
          git add docs/galway/events.json
          git diff --cached --quiet || git commit -m "Update Galway events ($(date -u +%Y-%m-%d))"
          # If the North West workflow pushed while this one was running,
          # replay this commit on top of it first (the two only ever
          # touch different files, so this always applies cleanly).
          git pull --rebase origin "${GITHUB_REF_NAME}"
          git push
