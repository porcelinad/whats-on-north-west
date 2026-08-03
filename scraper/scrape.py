name: Scrape events
on:
  schedule:
    # GitHub Actions cron is always UTC with no daylight-saving
    # awareness, so two schedules are needed to land at "shortly after
    # midnight" Irish local time year-round: Irish clocks run UTC+1
    # during BST (roughly late March - late October) and UTC+0 during
    # GMT (roughly late October - late March). This is approximated by
    # calendar month rather than the exact last-Sunday transition
    # dates, so the week or so either side of the actual clock change
    # may land up to an hour off - a minor tradeoff against being off
    # by 7+ hours every single day, which is what a single fixed time
    # would mean for at least one of the two seasons.
    - cron: "15 23 * 4-10 *"       # Apr-Oct (BST): 23:15 UTC = 00:15 BST
    - cron: "15 0 * 11,12,1,2,3 *" # Nov-Mar (GMT): 00:15 UTC = 00:15 GMT
  workflow_dispatch: {}    # adds a "Run workflow" button for manual runs
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
      - name: Run scraper
        env:
          NTFY_TOPIC: ${{ secrets.NTFY_TOPIC }}
          PAGE_URL: ${{ vars.PAGE_URL }}
        run: python scraper/scrape.py
      - name: Commit updated events
        run: |
          git config user.name "events-bot"
          git config user.email "actions@users.noreply.github.com"
          git add docs/events.json
          git diff --cached --quiet || git commit -m "Update events ($(date -u +%Y-%m-%d))"
          git push
