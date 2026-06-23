"""Scrapes Google Flights (via the fast-flights library) for round-trip
fares across a date window and emails alerts when the cheapest fare found
drops to or below a configured threshold.

Run manually with: python flight_watch.py
Config lives in watches.json (one or more independent route/date/price watches,
each with its own check interval and optional expiry). Per-watch state
(last check time, price history, last alert) lives in state.json.
"""
import os
import sys
import json
import smtplib
from datetime import datetime, timedelta
from email.message import EmailMessage

from dotenv import load_dotenv
from fast_flights import create_query, get_flights, FlightQuery, Passengers
from fast_flights.exceptions import FlightsNotFound

load_dotenv()

WATCHES_FILE = os.path.join(os.path.dirname(__file__), "watches.json")
STATE_FILE = os.path.join(os.path.dirname(__file__), "state.json")

# Google Flights has no native "search a date range" mode for this library,
# so we sample outbound dates across the window and pair each with a few
# stay lengths, rather than checking every possible date combination.
OUTBOUND_SAMPLE_STEP_DAYS = 3
STAY_LENGTHS_DAYS = [7, 10, 14]
MAX_QUERIES_PER_WATCH = 20
HISTORY_CAP = 200


def daterange_step(start, end, step_days):
    d = start
    while d <= end:
        yield d
        d += timedelta(days=step_days)


def candidate_date_pairs(watch):
    out_start = datetime.strptime(watch["outbound_window"]["start"], "%Y-%m-%d").date()
    out_end = datetime.strptime(watch["outbound_window"]["end"], "%Y-%m-%d").date()
    ret_start = datetime.strptime(watch["return_window"]["start"], "%Y-%m-%d").date()
    ret_end = datetime.strptime(watch["return_window"]["end"], "%Y-%m-%d").date()
    min_stay = watch.get("min_stay_days", 3)
    max_stay = watch.get("max_stay_days", 21)

    pairs = []
    for out_date in daterange_step(out_start, out_end, OUTBOUND_SAMPLE_STEP_DAYS):
        for stay in STAY_LENGTHS_DAYS:
            if stay < min_stay or stay > max_stay:
                continue
            ret_date = out_date + timedelta(days=stay)
            if ret_start <= ret_date <= ret_end:
                pairs.append((out_date, ret_date))
    return pairs[:MAX_QUERIES_PER_WATCH]


SEARCH_RETRIES = 3


def search_offer(watch, out_date, ret_date):
    # Without an explicit currency, Google picks one based on route/locale
    # heuristics (observed ILS-like pricing for a Madrid->Tel Aviv search),
    # which would silently mislabel prices. Always force USD for consistent
    # units. Google Flights also intermittently returns a transient error
    # response (independent of route or currency) - retry a few times
    # before giving up on this date pair.
    query = create_query(
        flights=[
            FlightQuery(date=out_date.isoformat(), from_airport=watch["origin"], to_airport=watch["destination"]),
            FlightQuery(date=ret_date.isoformat(), from_airport=watch["destination"], to_airport=watch["origin"]),
        ],
        trip="round-trip",
        seat="economy",
        passengers=Passengers(adults=watch.get("adults", 1)),
        currency="USD",
        max_stops=0 if watch.get("direct_only") else None,
    )
    results = None
    for _ in range(SEARCH_RETRIES):
        try:
            results = get_flights(query)
            break
        except (FlightsNotFound, Exception):
            continue
    if not results:
        return None
    cheapest = min(results, key=lambda f: f.price)
    return {
        "price": cheapest.price,
        "currency": "USD",
        "airlines": ", ".join(cheapest.airlines),
        "out_date": out_date.isoformat(),
        "ret_date": ret_date.isoformat(),
    }


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
    """Parse an ISO timestamp from either Python (naive) or JS (Z-suffixed,
    timezone-aware) and normalize to a naive datetime for comparison."""
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


def should_alert(entry, price):
    prior = entry.get("alerted_price")
    alerted_at = entry.get("alerted_at")
    if prior is None:
        return True
    days_since = (datetime.now() - parse_dt(alerted_at)).days
    if price < prior * 0.95:
        return True
    if days_since >= 7:
        return True
    return False


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
        entry = state.setdefault(key, {})

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

        best = None
        for out_date, ret_date in candidate_date_pairs(watch):
            offer = search_offer(watch, out_date, ret_date)
            if offer and (best is None or offer["price"] < best["price"]):
                best = offer
            print(f"[{name}]   checked {out_date} -> {ret_date}: "
                  f"{offer['price'] if offer else 'no offers'}")

        entry["last_checked_at"] = datetime.now().isoformat()

        if best is None:
            print(f"[{name}] no offers found")
            continue

        print(f"[{name}] cheapest found: {best['price']} {best['currency']} "
              f"({best['out_date']} -> {best['ret_date']}, {best['airlines']})")

        history = entry.setdefault("history", [])
        history.append({"ts": entry["last_checked_at"], "price": best["price"],
                         "out_date": best["out_date"], "ret_date": best["ret_date"]})
        del history[:-HISTORY_CAP]

        deal_hit = best["price"] <= watch["max_price"] and should_alert(entry, best["price"])
        if deal_hit and dry_run:
            print(f"[{name}] DRY RUN: would send alert email (price under threshold)")
        elif deal_hit:
            send_email(
                subject=f"Flight deal: {name} - {best['price']} {best['currency']}",
                body=(
                    f"{watch['origin']} -> {watch['destination']}\n"
                    f"Outbound: {best['out_date']}\n"
                    f"Return: {best['ret_date']}\n"
                    f"Airline: {best['airlines']}\n"
                    f"Price: {best['price']} {best['currency']}\n"
                    f"Threshold: {watch['max_price']} {watch.get('currency', 'EUR')}\n"
                ),
            )
            entry["alerted_price"] = best["price"]
            entry["alerted_at"] = datetime.now().isoformat()
            print(f"[{name}] ALERT sent")

    if not dry_run:
        save_json(STATE_FILE, state)
        if watches_changed:
            save_json(WATCHES_FILE, watches)


if __name__ == "__main__":
    run(dry_run="--dry-run" in sys.argv, force="--force" in sys.argv)
