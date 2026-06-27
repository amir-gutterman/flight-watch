"""Scrapes Google Hotels for the cheapest matching hotels in a destination
across an exact date range, and emails alerts when any tracked hotel's price
drops 20%+ below the lowest price ever recorded for it.

Run manually with: python hotel_watch.py
Config lives in hotel_watches.json (one or more independent destination/date
watches, each with its own check interval and optional expiry). Per-watch,
per-hotel state (price history, last alert) lives in hotel_state.json.

How it works:
- The first time a watch is checked, we drive a real Google Hotels search
  (via Playwright) to resolve the destination + exact check-in/check-out
  dates into a `ts=` URL parameter Google uses to encode the search. This
  step requires UI interaction (typing the destination, clicking calendar
  cells) because Google's router strips plain query-string dates.
- That resolved URL is cached in state. Every check (including the first)
  then does a *fresh* page load of that URL - Google server-renders an
  embedded JSON blob (`AF_initDataCallback({key: 'ds:0', ...})`) containing
  real, per-date prices, which we parse directly instead of scraping the
  rendered DOM.
- Review scores from Google Hotels are on a 5-point scale; `min_score` in
  the config should be on that same 5-point scale (e.g. 4.0, not 8).
- Prices are always converted to EUR-per-night (forced via the currency
  picker during URL resolution, then divided by the stay's night count) so
  `max_price` and price history are comparable across watches and runs.
"""
import os
import sys
import json
import re
import smtplib
from datetime import datetime, timedelta
from email.message import EmailMessage

from dotenv import load_dotenv
from playwright.sync_api import sync_playwright

load_dotenv()

# Hotel prices print with non-ASCII currency symbols (e.g. shekel); Windows'
# default console encoding can't display them, so force UTF-8 stdout.
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

WATCHES_FILE = os.path.join(os.path.dirname(__file__), "hotel_watches.json")
STATE_FILE = os.path.join(os.path.dirname(__file__), "hotel_state.json")

DATE_CELL_RE = re.compile(r"^[A-Za-z]+, [A-Za-z]+ \d+, \d{4}$")
PRICE_RE = re.compile(r"[\d,]+")
HISTORY_CAP = 200

# Google's per-hotel data has no clean "property type" field (the small int
# arrays attached to each entry are shared amenity tags, not type markers -
# confirmed by inspecting real hostel vs. hotel entries side by side), so
# excluding hostels/shared-room properties has to go by name. This combines
# generic wording with known hostel-chain brand names (e.g. "Generator",
# which has no "hostel" in its own name) - it's a best-effort heuristic, not
# a guaranteed match against Google's internal classification.
HOSTEL_KEYWORDS = [
    "hostel", "hostal", "backpacker",
    "generator", "wombat", "st christopher", "meininger", "selina",
    "safestay", "clink", "yha ", "a&o ", "easyhostel",
]


def is_hostel(name):
    lowered = name.lower()
    return any(kw in lowered for kw in HOSTEL_KEYWORDS)

# Finds the scrollable ancestor of any visible calendar day cell and scrolls
# it - used to bring later months into view without relying on a "next
# month" button (Google's calendar here is an infinite-scroll list).
SCROLL_CALENDAR_JS = """(amount) => {
    const re = /^[A-Za-z]+, [A-Za-z]+ \\d+, \\d{4}$/;
    const el = Array.from(document.querySelectorAll('[aria-label]'))
        .find(e => re.test(e.getAttribute('aria-label')));
    if (!el) return false;
    let p = el;
    while (p && p.scrollHeight <= p.clientHeight) p = p.parentElement;
    if (p) { p.scrollTop += amount; return true; }
    return false;
}"""


def to_cell_label(date):
    return date.strftime("%A, %B ") + str(date.day) + date.strftime(", %Y")


def resolve_search_url(page, destination, checkin, checkout):
    """One-time-per-watch UI flow: turns a destination + exact dates into a
    Google Hotels URL containing a `ts=` param, which encodes both into a
    form the server will honor on a fresh page load. Also forces EUR so
    prices are comparable across runs regardless of the runner's IP-based
    default currency (otherwise Google silently picks one per request)."""
    q = destination.replace(" ", "%20")
    page.goto(f"https://www.google.com/travel/search?q=hotels%20in%20{q}", timeout=30000)
    page.wait_for_timeout(3000)

    page.get_by_role("button", name="Change dates").click()
    page.wait_for_timeout(1000)

    for label in (to_cell_label(checkin), to_cell_label(checkout)):
        for _ in range(24):
            cell = page.locator(f'[aria-label="{label}"]')
            if cell.count() and cell.first.is_visible():
                break
            page.evaluate(SCROLL_CALENDAR_JS, 300)
            page.wait_for_timeout(250)
        page.locator(f'[aria-label="{label}"]').first.click()
        page.wait_for_timeout(500)

    page.locator("text=View prices").first.click(force=True)
    page.wait_for_timeout(2500)

    page.locator("span.twocKe").first.click()
    page.wait_for_timeout(800)
    currency_dialog = None
    dialogs = page.locator('[role="dialog"]')
    for i in range(dialogs.count()):
        try:
            if "Select currency" in dialogs.nth(i).inner_text(timeout=500):
                currency_dialog = dialogs.nth(i)
                break
        except Exception:
            continue
    if currency_dialog:
        currency_dialog.get_by_text("Euro", exact=False).first.click()
        page.wait_for_timeout(500)
        currency_dialog.get_by_role("button", name="Done").click()
        page.wait_for_timeout(2000)

    return page.url


def extract_ds0(html):
    m = re.search(r"AF_initDataCallback\(\{key: 'ds:0'.*?data:(\[.*?\]) *, sideChannel", html, re.S)
    if not m:
        return None
    return json.loads(m.group(1))


def walk_hotel_entries(node, out):
    """The ds:0 blob is deeply nested with no documented schema. Hotel
    entries are identifiable as lists whose [0] is a name string, [2] is a
    "<symbol><digits>" price string, and [4]/[5] are an int review count and
    float review score - this signature is specific enough not to false-
    positive on other nested arrays in the blob."""
    if isinstance(node, dict):
        for child in node.values():
            walk_hotel_entries(child, out)
        return
    if isinstance(node, list):
        if (
            len(node) > 5
            and isinstance(node[0], str)
            and isinstance(node[2], str)
            and PRICE_RE.search(node[2])
            and isinstance(node[4], int)
            and isinstance(node[5], (int, float))
        ):
            out.append(node)
            return
        for child in node:
            walk_hotel_entries(child, out)


def parse_price(price_str):
    m = PRICE_RE.search(price_str)
    if not m:
        return None
    return float(m.group(0).replace(",", ""))


SEARCH_RETRIES = 3


def search_hotels(destination, checkin, checkout, search_url=None):
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page(locale="en-US", viewport={"width": 1280, "height": 1400})
        try:
            if not search_url:
                search_url = resolve_search_url(page, destination, checkin, checkout)

            # The fresh fetch occasionally renders before the embedded data is
            # fully populated (or hits a transient empty response) - retry a
            # few times rather than treating a one-off as "no hotels found".
            raw_entries = []
            for _ in range(SEARCH_RETRIES):
                page.goto(search_url, timeout=30000)
                page.wait_for_timeout(3500)
                data = extract_ds0(page.content())
                if data is not None:
                    walk_hotel_entries(data, raw_entries)
                if raw_entries:
                    break
        finally:
            browser.close()

    nights = max((checkout - checkin).days, 1)

    hotels = []
    for e in raw_entries:
        total_price = parse_price(e[2])
        if total_price is None:
            continue
        per_night = total_price / nights
        hotels.append({
            "name": e[0],
            "price": per_night,
            "price_display": f"€{per_night:.0f}/night",
            "review_count": e[4],
            "review_score": e[5],
        })
    return search_url, hotels


def load_json(path, default):
    if os.path.exists(path):
        with open(path) as f:
            return json.load(f)
    return default


def save_json(path, data):
    with open(path, "w") as f:
        json.dump(data, f, indent=2)


def watch_key(watch):
    return watch.get("id") or watch["name"]


def parse_dt(s):
    dt = datetime.fromisoformat(s)
    if dt.tzinfo is not None:
        dt = dt.replace(tzinfo=None)
    return dt


def is_expired(watch):
    expires_after_days = watch.get("expires_after_days")
    created_at = watch.get("created_at")
    if not expires_after_days or not created_at:
        return False
    age_days = (datetime.now() - parse_dt(created_at)).days
    return age_days >= expires_after_days


def due_for_check(watch, entry):
    interval_hours = watch.get("check_interval_hours", 6)
    last_checked_at = entry.get("last_checked_at")
    if not last_checked_at:
        return True
    elapsed_hours = (datetime.now() - parse_dt(last_checked_at)).total_seconds() / 3600
    return elapsed_hours >= interval_hours


def should_alert(hotel_entry, price):
    floor = hotel_entry.get("min_price")
    alerted_price = hotel_entry.get("alerted_price")
    if floor is None:
        return False  # first sighting - establish baseline, don't alert
    if price > floor * 0.8:
        return False  # not a 20%+ drop below the historical floor
    if alerted_price is not None and price >= alerted_price:
        return False  # already alerted at this price or lower
    return True


def send_email(subject, body):
    msg = EmailMessage()
    msg["From"] = os.environ["GMAIL_USER"]
    msg["To"] = os.environ["ALERT_TO"]
    msg["Subject"] = subject
    msg.set_content(body)
    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as smtp:
        smtp.login(os.environ["GMAIL_USER"], os.environ["GMAIL_APP_PASSWORD"])
        smtp.send_message(msg)


def run(dry_run=False, force=False):
    watches = load_json(WATCHES_FILE, [])
    state = load_json(STATE_FILE, {})
    watches_changed = False

    for watch in watches:
        name = watch["name"]
        key = watch_key(watch)
        entry = state.setdefault(key, {"hotels": {}})

        if not watch.get("enabled", True):
            print(f"[{name}] disabled, skipping")
            continue

        if is_expired(watch):
            print(f"[{name}] expired, disabling")
            watch["enabled"] = False
            watches_changed = True
            continue

        if not force and not due_for_check(watch, entry):
            print(f"[{name}] not due yet (checks every {watch.get('check_interval_hours', 6)}h)")
            continue

        checkin = datetime.strptime(watch["checkin"], "%Y-%m-%d").date()
        checkout = datetime.strptime(watch["checkout"], "%Y-%m-%d").date()
        search_url, hotels = search_hotels(
            watch["destination"], checkin, checkout, entry.get("search_url"),
        )
        entry["search_url"] = search_url
        entry["last_checked_at"] = datetime.now().isoformat()

        min_score = watch.get("min_score")
        max_price = watch.get("max_price")
        matching = [
            h for h in hotels
            if (min_score is None or h["review_score"] >= min_score)
            and (max_price is None or h["price"] <= max_price)
            and not is_hostel(h["name"])
        ]
        print(f"[{name}] {len(hotels)} hotels found, {len(matching)} match filters")

        hotels_state = entry.setdefault("hotels", {})
        for h in matching:
            hotel_entry = hotels_state.setdefault(h["name"], {})
            history = hotel_entry.setdefault("history", [])
            history.append({"ts": entry["last_checked_at"], "price": h["price"]})
            del history[:-HISTORY_CAP]

            deal_hit = should_alert(hotel_entry, h["price"])
            prior_floor = hotel_entry.get("min_price")
            hotel_entry["min_price"] = min(h["price"], hotel_entry.get("min_price", h["price"]))
            hotel_entry["last_price"] = h["price"]

            if deal_hit and dry_run:
                print(f"[{name}]   DRY RUN: would alert on {h['name']} - {h['price_display']}")
            elif deal_hit:
                drop_pct = round(100 - (h["price"] / prior_floor * 100), 0) if prior_floor else 0
                send_email(
                    subject=f"Hotel deal: {h['name']} - {h['price_display']} ({name})",
                    body=(
                        f"{h['name']}\n"
                        f"{watch['destination']}, {watch['checkin']} -> {watch['checkout']}\n"
                        f"Price: {h['price_display']} ({drop_pct:.0f}% below its previous low)\n"
                        f"Review score: {h['review_score']} ({h['review_count']} reviews)\n"
                        f"Search: {search_url}\n"
                    ),
                )
                hotel_entry["alerted_price"] = h["price"]
                hotel_entry["alerted_at"] = datetime.now().isoformat()
                print(f"[{name}]   ALERT sent for {h['name']} ({h['price_display']})")

    if not dry_run:
        save_json(STATE_FILE, state)
        if watches_changed:
            save_json(WATCHES_FILE, watches)


if __name__ == "__main__":
    run(dry_run="--dry-run" in sys.argv, force="--force" in sys.argv)
