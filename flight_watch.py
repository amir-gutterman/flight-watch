"""Scrapes Google Flights (via the fast-flights library) for round-trip
fares across a date window and emails alerts when the cheapest fare found
drops to or below a configured threshold.

Run manually with: python flight_watch.py
Config lives in watches.json (one or more independent route/date/price watches).
Per-watch alert history lives in state.json so repeat runs don't re-spam the
same price.
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


def search_offer(watch, out_date, ret_date):
    query = create_query(
        flights=[
            FlightQuery(date=out_date.isoformat(), from_airport=watch["origin"], to_airport=watch["destination"]),
            FlightQuery(date=ret_date.isoformat(), from_airport=watch["destination"], to_airport=watch["origin"]),
        ],
        trip="round-trip",
        seat="economy",
        passengers=Passengers(adults=watch.get("adults", 1)),
        currency=watch.get("currency", "EUR"),
        max_stops=0 if watch.get("direct_only") else None,
    )
    try:
        results = get_flights(query)
    except (FlightsNotFound, Exception):
        return None
    if not results:
        return None
    cheapest = min(results, key=lambda f: f.price)
    return {
        "price": cheapest.price,
        "currency": watch.get("currency", "EUR"),
        "airlines": ", ".join(cheapest.airlines),
        "out_date": out_date.isoformat(),
        "ret_date": ret_date.isoformat(),
    }


def load_state():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE) as f:
            return json.load(f)
    return {}


def save_state(state):
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)


def should_alert(watch_name, price, state):
    prior = state.get(watch_name)
    if prior is None:
        return True
    days_since = (datetime.now() - datetime.fromisoformat(prior["alerted_at"])).days
    if price < prior["price"] * 0.95:
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


def run(dry_run=False):
    with open(WATCHES_FILE) as f:
        watches = json.load(f)
    state = load_state()

    for watch in watches:
        name = watch["name"]
        best = None
        for out_date, ret_date in candidate_date_pairs(watch):
            offer = search_offer(watch, out_date, ret_date)
            if offer and (best is None or offer["price"] < best["price"]):
                best = offer
            print(f"[{name}]   checked {out_date} -> {ret_date}: "
                  f"{offer['price'] if offer else 'no offers'}")

        if best is None:
            print(f"[{name}] no offers found")
            continue

        print(f"[{name}] cheapest found: {best['price']} {best['currency']} "
              f"({best['out_date']} -> {best['ret_date']}, {best['airlines']})")

        deal_hit = best["price"] <= watch["max_price"] and should_alert(name, best["price"], state)
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
            state[name] = {"price": best["price"], "alerted_at": datetime.now().isoformat()}
            print(f"[{name}] ALERT sent")

    if not dry_run:
        save_state(state)


if __name__ == "__main__":
    run(dry_run="--dry-run" in sys.argv)
