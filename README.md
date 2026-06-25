# Flight Watch

Scrapes Google Flights (via the open-source `fast-flights` library) for
round-trip fares across a date window per route and emails you when the
cheapest fare found drops to/below a price threshold.

(Originally targeted Amadeus's self-service API, but that's being
decommissioned July 17, 2026. Tried Kiwi Tequila next, but it now requires
partner/affiliate approval rather than instant self-serve signup. Settled on
scraping Google Flights directly — no API key, no approval wait, but more
fragile if Google changes its page, and technically against Google's ToS for
automated use.)

## Setup

1. `pip install -r requirements.txt`
2. Copy `.env.example` to `.env` and fill in:
   - `GMAIL_USER` / `GMAIL_APP_PASSWORD` (Google Account > Security > App passwords)
   - `ALERT_TO` (where alerts get sent)
3. Edit `watches.json` to add/change routes. Each entry is independent:
   - `origin` / `destination`: IATA codes (e.g. `MAD`, `TLV`)
   - `outbound_window` / `return_window`: date ranges to search within
   - `min_stay_days` / `max_stay_days`: acceptable trip length
   - `direct_only`: true/false
   - `max_price`: alert threshold
   - `currency`, `adults`
4. Run once to test: `python flight_watch.py`

## How alerting works

Each run samples outbound dates across the window (every 3 days) paired with
stay lengths of 7/10/14 days (capped at 20 queries per watch per run, since
there's no native date-range search) and finds the cheapest non-stop fare.
You get an email when:
- the cheapest fare is at/below `max_price`, AND
- it's the first time that watch has triggered, OR the price dropped at
  least 5% further than the last alert, OR 7+ days have passed since the
  last alert (so you get an occasional reminder if the deal persists).

State is kept in `state.json` (auto-created).

## Scheduling

Runs automatically via the `.github/workflows/flight-watch.yml` GitHub Actions
workflow, on a cron schedule (currently twice an hour). Each per-watch
`check_interval_hours` then decides whether that particular watch is actually
due for a real search on a given tick.

**Known limitation:** GitHub Actions scheduled workflows are not guaranteed
to fire exactly on time — GitHub explicitly documents that scheduled runs can
be delayed during high platform load, especially right at the top of every
hour, and free-tier/public repos get lower scheduling priority. So a watch
configured to check "every 6h" may sometimes go 7-8h between real checks if
GitHub delays or skips a few ticks. The workflow's cron is intentionally set
to fire twice an hour at off-peak minutes (`:13` and `:43`, avoiding `:00`)
to reduce how often this happens, but it can't be eliminated entirely on the
free tier. You can trigger an immediate check anytime via the "Run now"
button on the webpage if you don't want to wait.
