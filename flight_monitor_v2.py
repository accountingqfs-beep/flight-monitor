#!/usr/bin/env python3
"""
Flight Price Monitor v2.0
Multi-trip automated price tracking with Discord push notifications.
Deploy on GitHub Actions (free) + SerpApi (free tier).
"""

import os
import sys
import json
import smtplib
import hashlib
import logging
from datetime import datetime, date
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from pathlib import Path

try:
    import requests
except ImportError:
    print("Installing requests...")
    import subprocess
    subprocess.check_call([sys.executable, "-m", "pip", "install", "requests", "-q"])
    import requests

# ─── CONFIGURATION ───────────────────────────────────────────────────────────

CONFIG = {
    # API
    "api_key": os.environ.get("SERPAPI_KEY", ""),

    # Discord (primary notification channel)
    "discord_webhook_url": os.environ.get("DISCORD_WEBHOOK", ""),

    # Email + SMS (optional backup)
    "email_enabled": os.environ.get("EMAIL_ENABLED", "false").lower() == "true",
    "email_smtp_server": "smtp.gmail.com",
    "email_smtp_port": 587,
    "email_sender": os.environ.get("EMAIL_SENDER", ""),
    "email_password": os.environ.get("EMAIL_PASSWORD", ""),
    "email_recipients": [x.strip() for x in os.environ.get("EMAIL_RECIPIENTS", "").split(",") if x.strip()],
    "sms_gateway": os.environ.get("SMS_GATEWAY", ""),  # e.g., 5135551234@txt.att.net

    # Data
    "data_dir": Path(os.environ.get("DATA_DIR", str(Path.home() / ".flight_monitor"))),
}

# ─── TRIPS ───────────────────────────────────────────────────────────────────
# Edit this list to add/remove/modify trips.
# Each trip is a dictionary with the fields below.

TRIPS = [
    {
        "id": "cvg-lga-jun26",
        "name": "NYC June Trip",
        "origin": "CVG",
        "destination": "LGA",
        "outbound_date": "2026-06-11",
        "return_date": "2026-06-14",
        "passengers": 2,
        "airline_filter": "DL",           # IATA code, or "" for all airlines
        "flight_numbers": "DL 5014 / DL 4987",
        "booked_price": None,             # SET THIS to your total booked price
        "alert_threshold": 100,           # $ drop to trigger Discord alert
        "active": True,
    },
    {
        "id": "cvg-ogg-nov26",
        "name": "Maui November Trip",
        "origin": "CVG",
        "destination": "OGG",
        "outbound_date": "2026-11-01",    # UPDATE with actual dates
        "return_date": "2026-11-08",      # UPDATE with actual dates
        "passengers": 2,
        "airline_filter": "",             # Track all airlines
        "flight_numbers": "",
        "booked_price": None,             # SET when booked
        "alert_threshold": 150,           # Higher threshold for longer/pricier route
        "active": True,
    },
    # ── Add more trips here ──
    # {
    #     "id": "unique-id",
    #     "name": "Trip Name",
    #     "origin": "CVG",
    #     "destination": "SJD",
    #     "outbound_date": "2026-05-25",
    #     "return_date": "2026-05-30",
    #     "passengers": 2,
    #     "airline_filter": "",
    #     "flight_numbers": "",
    #     "booked_price": None,
    #     "alert_threshold": 100,
    #     "active": True,
    # },
]

# ─── LOGGING ─────────────────────────────────────────────────────────────────

def setup_logging():
    data_dir = CONFIG["data_dir"]
    data_dir.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        handlers=[
            logging.FileHandler(data_dir / "monitor.log"),
            logging.StreamHandler(sys.stdout),
        ],
    )
    return logging.getLogger(__name__)

log = setup_logging()

# ─── SERPAPI FETCH ───────────────────────────────────────────────────────────

def fetch_prices(trip):
    """Fetch flight prices from SerpApi Google Flights."""
    params = {
        "engine": "google_flights",
        "departure_id": trip["origin"],
        "arrival_id": trip["destination"],
        "outbound_date": trip["outbound_date"],
        "return_date": trip["return_date"],
        "currency": "USD",
        "hl": "en",
        "type": "1",
        "travel_class": "1",
        "adults": str(trip["passengers"]),
        "stops": "0",  # nonstop only; change to "1" for 1-stop-or-fewer
        "api_key": CONFIG["api_key"],
    }
    if trip.get("airline_filter"):
        params["include_airlines"] = trip["airline_filter"]

    # Remove empty values
    params = {k: v for k, v in params.items() if v}

    log.info(f"[{trip['id']}] Fetching: {trip['origin']}→{trip['destination']} {trip['outbound_date']}")
    resp = requests.get("https://serpapi.com/search", params=params, timeout=45)
    resp.raise_for_status()
    data = resp.json()

    # Parse results
    flights = []
    for category in ["best_flights", "other_flights"]:
        for fg in data.get(category, []):
            entry = {
                "price": fg.get("price"),
                "airline": None,
                "flight_numbers": [],
                "duration": fg.get("total_duration"),
                "is_basic": False,
            }
            for seg in fg.get("flights", []):
                entry["flight_numbers"].append(seg.get("flight_number", ""))
                if not entry["airline"]:
                    entry["airline"] = seg.get("airline", "")
            for ext in fg.get("extensions", []):
                if isinstance(ext, str) and "Basic" in ext:
                    entry["is_basic"] = True
            if entry["price"]:
                flights.append(entry)

    all_prices = [f["price"] for f in flights]
    lowest = min(all_prices) if all_prices else None
    price_insights = data.get("price_insights", {})

    return {
        "timestamp": datetime.utcnow().isoformat() + "Z",
        "trip_id": trip["id"],
        "lowest_price": lowest,
        "num_results": len(flights),
        "top_flights": sorted(flights, key=lambda x: x["price"])[:5],
        "price_insights": price_insights,
        "api_status": data.get("search_metadata", {}).get("status", "unknown"),
    }

# ─── VALIDATION ──────────────────────────────────────────────────────────────

def validate(result):
    """Score reliability 0-100."""
    score = 100
    issues = []

    if result.get("api_status") not in ("Success", "success"):
        score -= 20; issues.append("API status not success")
    if result["num_results"] == 0:
        score -= 50; issues.append("No flights returned")
    elif result["num_results"] < 3:
        score -= 10; issues.append(f"Only {result['num_results']} results")

    lp = result.get("lowest_price")
    if lp is None:
        score -= 30; issues.append("No price found")
    elif lp < 50:
        score -= 25; issues.append(f"Suspiciously low: ${lp}")

    pi = result.get("price_insights", {})
    typical = pi.get("typical_price_range", [])
    if typical and lp and lp < typical[0] * 0.4:
        score -= 15; issues.append("Far below typical range")

    grade = "A" if score >= 90 else "B" if score >= 75 else "C" if score >= 60 else "F"
    return {"score": max(0, score), "grade": grade, "issues": issues, "passed": score >= 60}

# ─── PRICE HISTORY ───────────────────────────────────────────────────────────

def load_history():
    path = CONFIG["data_dir"] / "price_history.json"
    if path.exists():
        with open(path) as f:
            return json.load(f)
    return {}

def save_history(history):
    path = CONFIG["data_dir"] / "price_history.json"
    with open(path, "w") as f:
        json.dump(history, f, indent=2, default=str)

def add_to_history(history, trip_id, result):
    if trip_id not in history:
        history[trip_id] = {"checks": []}
    entry = {
        "timestamp": result["timestamp"],
        "price": result["lowest_price"],
        "num_results": result["num_results"],
        "price_level": result.get("price_insights", {}).get("price_level", ""),
        "typical_range": result.get("price_insights", {}).get("typical_price_range", []),
    }
    history[trip_id]["checks"].append(entry)
    save_history(history)
    return entry

def get_stats(history, trip_id):
    checks = history.get(trip_id, {}).get("checks", [])
    prices = [c["price"] for c in checks if c.get("price")]
    if not prices:
        return None
    return {
        "count": len(prices),
        "current": prices[-1],
        "lowest": min(prices),
        "highest": max(prices),
        "average": sum(prices) / len(prices),
        "trend": "down" if len(prices) >= 2 and prices[-1] < prices[-2]
                 else "up" if len(prices) >= 2 and prices[-1] > prices[-2]
                 else "flat",
    }

# ─── DISCORD NOTIFICATIONS ──────────────────────────────────────────────────

def send_discord(trip, result, stats, validation, drop_amount=None):
    """Send a rich embedded message to Discord."""
    url = CONFIG.get("discord_webhook_url")
    if not url:
        log.warning("Discord webhook URL not configured — skipping")
        return

    lp = result.get("lowest_price", "?")
    pi = result.get("price_insights", {})
    price_level = pi.get("price_level", "unknown")
    typical = pi.get("typical_price_range", [])

    # Color: green for actionable drop, yellow for small, red for increase, blue for neutral
    if drop_amount and drop_amount >= trip.get("alert_threshold", 100):
        color = 0x10B981  # green
        title = f"🟢 PRICE DROP — {trip['origin']} → {trip['destination']}"
    elif drop_amount and drop_amount > 0:
        color = 0xF59E0B  # yellow
        title = f"🟡 Small drop — {trip['origin']} → {trip['destination']}"
    elif drop_amount and drop_amount < 0:
        color = 0xEF4444  # red
        title = f"🔴 Price up — {trip['origin']} → {trip['destination']}"
    else:
        color = 0x3B82F6  # blue
        title = f"✈️ Price check — {trip['origin']} → {trip['destination']}"

    fields = [
        {"name": "Current Price", "value": f"**${lp}**", "inline": True},
        {"name": "Price Level", "value": price_level.title() if price_level else "—", "inline": True},
    ]

    if drop_amount is not None:
        direction = "↓" if drop_amount > 0 else "↑"
        fields.append({
            "name": "vs. Booked",
            "value": f"**{direction} ${abs(drop_amount):.0f}**",
            "inline": True,
        })

    if typical:
        fields.append({"name": "Typical Range", "value": f"${typical[0]} – ${typical[1]}", "inline": True})

    if stats:
        fields.append({"name": "Lowest Seen", "value": f"${stats['lowest']}", "inline": True})
        trend_icon = "📉" if stats["trend"] == "down" else "📈" if stats["trend"] == "up" else "➡️"
        fields.append({"name": "Trend", "value": f"{trend_icon} {stats['trend']}", "inline": True})

    fields.append({
        "name": "Reliability",
        "value": f"{validation['grade']} ({validation['score']}/100)",
        "inline": True,
    })

    # Build embed
    embed = {
        "title": title,
        "color": color,
        "fields": fields,
        "footer": {
            "text": f"{trip.get('name', '')} · {trip['outbound_date']} → {trip['return_date']} · {trip['passengers']} pax"
                    + (f" · {trip['flight_numbers']}" if trip.get("flight_numbers") else ""),
        },
        "timestamp": result["timestamp"],
    }

    # Top flights detail
    top = result.get("top_flights", [])[:3]
    if top:
        detail_lines = []
        for f in top:
            basic_tag = " ⚠️Basic" if f.get("is_basic") else ""
            fns = ", ".join(f.get("flight_numbers", []))
            detail_lines.append(f"${f['price']} — {f.get('airline','?')} {fns}{basic_tag}")
        embed["description"] = "```\n" + "\n".join(detail_lines) + "\n```"

    payload = {
        "username": "Flight Monitor",
        "avatar_url": "https://em-content.zobj.net/source/apple/391/airplane_2708-fe0f.png",
        "embeds": [embed],
    }

    try:
        resp = requests.post(url, json=payload, timeout=10)
        if resp.status_code in (200, 204):
            log.info(f"[{trip['id']}] Discord notification sent")
        else:
            log.error(f"[{trip['id']}] Discord error {resp.status_code}: {resp.text[:200]}")
    except Exception as e:
        log.error(f"[{trip['id']}] Discord failed: {e}")

# ─── EMAIL / SMS (BACKUP) ───────────────────────────────────────────────────

def send_email_sms(trip, result, stats, validation, drop_amount=None):
    """Send email and optional SMS as backup notification."""
    if not CONFIG.get("email_enabled"):
        return

    lp = result.get("lowest_price", "?")
    drop_text = f" [-${drop_amount:.0f}]" if drop_amount and drop_amount > 0 else ""
    subject = f"Flight Alert: {trip['origin']}→{trip['destination']} ${lp}{drop_text}"

    body_lines = [
        f"{trip['origin']} → {trip['destination']} — {trip.get('name', '')}",
        f"{'=' * 40}",
        f"Current Lowest:  ${lp}",
    ]
    if drop_amount and drop_amount > 0:
        body_lines.append(f"DROP:            -${drop_amount:.0f} vs booked")
    if stats:
        body_lines.extend([
            f"Lowest Seen:     ${stats['lowest']}",
            f"Average:         ${stats['average']:.0f}",
            f"Trend:           {stats['trend']}",
        ])
    body_lines.extend([
        f"Reliability:     {validation['grade']} ({validation['score']}/100)",
        f"Dates:           {trip['outbound_date']} → {trip['return_date']}",
        f"Checked:         {result['timestamp'][:19]}",
    ])
    body = "\n".join(body_lines)

    recipients = list(CONFIG.get("email_recipients", []))
    if CONFIG.get("sms_gateway"):
        recipients.append(CONFIG["sms_gateway"])

    if not recipients:
        return

    try:
        msg = MIMEMultipart()
        msg["Subject"] = subject
        msg["From"] = CONFIG["email_sender"]
        msg["To"] = ", ".join(recipients)
        msg.attach(MIMEText(body, "plain"))

        with smtplib.SMTP(CONFIG["email_smtp_server"], CONFIG["email_smtp_port"]) as server:
            server.starttls()
            server.login(CONFIG["email_sender"], CONFIG["email_password"])
            server.send_message(msg)
        log.info(f"[{trip['id']}] Email/SMS sent to {len(recipients)} recipients")
    except Exception as e:
        log.error(f"[{trip['id']}] Email failed: {e}")

# ─── MAIN ────────────────────────────────────────────────────────────────────

def check_trip(trip, history):
    """Run a full price check for one trip."""
    log.info(f"[{trip['id']}] Starting check: {trip['origin']}→{trip['destination']}")

    # Fetch
    try:
        result = fetch_prices(trip)
    except Exception as e:
        log.error(f"[{trip['id']}] API error: {e}")
        return {"trip_id": trip["id"], "error": str(e)}

    # Validate
    val = validate(result)
    log.info(f"[{trip['id']}] Validation: {val['grade']} ({val['score']}/100)")

    # Save to history
    add_to_history(history, trip["id"], result)
    stats = get_stats(history, trip["id"])

    # Calculate drop
    drop = None
    if trip.get("booked_price") and result.get("lowest_price"):
        drop = trip["booked_price"] - result["lowest_price"]

    # Log summary
    log.info(
        f"[{trip['id']}] Price: ${result.get('lowest_price', '?')} | "
        f"Results: {result['num_results']} | "
        f"Drop: {'$' + str(round(drop)) if drop else 'N/A'} | "
        f"Grade: {val['grade']}"
    )

    # Notify — always send to Discord so you have a log; email/SMS only on threshold
    always_notify = os.environ.get("ALWAYS_NOTIFY", "true").lower() == "true"
    threshold_met = drop is not None and drop >= trip.get("alert_threshold", 100)

    if always_notify or threshold_met:
        send_discord(trip, result, stats, val, drop)

    if threshold_met:
        send_email_sms(trip, result, stats, val, drop)
        log.info(f"[{trip['id']}] *** ALERT THRESHOLD MET: -${drop:.0f} ***")

    return {
        "trip_id": trip["id"],
        "price": result.get("lowest_price"),
        "drop": drop,
        "alert": threshold_met,
        "grade": val["grade"],
    }

def run_all():
    """Check all active trips."""
    if not CONFIG["api_key"]:
        log.error("SERPAPI_KEY not set. Export it as an environment variable.")
        sys.exit(1)

    active = [t for t in TRIPS if t.get("active", True)]
    log.info(f"Running checks for {len(active)} active trip(s)")

    history = load_history()
    results = []

    for trip in active:
        # Skip past trips
        if trip.get("outbound_date") and trip["outbound_date"] < date.today().isoformat():
            log.info(f"[{trip['id']}] Skipping — departure date passed")
            continue
        result = check_trip(trip, history)
        results.append(result)

    # Summary
    log.info("=" * 50)
    log.info("CHECK COMPLETE")
    for r in results:
        status = "⚠️ ALERT" if r.get("alert") else "✓"
        price = f"${r['price']}" if r.get("price") else "error"
        log.info(f"  {status} {r['trip_id']}: {price}")
    log.info("=" * 50)

    return results

def show_status():
    """Print status of all trips from saved history."""
    history = load_history()
    if not history:
        print("No price history recorded yet. Run a check first.")
        return

    print(f"\n{'=' * 56}")
    print(f"  Flight Price Monitor — Status")
    print(f"{'=' * 56}")

    for trip in TRIPS:
        stats = get_stats(history, trip["id"])
        if not stats:
            print(f"\n  {trip['origin']}→{trip['destination']} ({trip['name']})")
            print(f"    No checks recorded")
            continue

        days_out = (date.fromisoformat(trip["outbound_date"]) - date.today()).days
        print(f"\n  {trip['origin']}→{trip['destination']} ({trip['name']}) — {days_out}d out")
        print(f"    Checks:   {stats['count']}")
        print(f"    Current:  ${stats['current']}")
        print(f"    Lowest:   ${stats['lowest']}")
        print(f"    Highest:  ${stats['highest']}")
        print(f"    Average:  ${stats['average']:.0f}")
        print(f"    Trend:    {stats['trend']}")
        if trip.get("booked_price"):
            diff = trip["booked_price"] - stats["current"]
            print(f"    vs Booked: {'↓' if diff > 0 else '↑'}${abs(diff):.0f} ({'BELOW' if diff > 0 else 'ABOVE'})")

    print(f"\n{'=' * 56}\n")

# ─── CLI ─────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Flight Price Monitor v2.0")
    parser.add_argument("command", choices=["check", "status", "test"],
                        help="check=run all trips, status=show history, test=send test Discord message")
    args = parser.parse_args()

    if args.command == "check":
        results = run_all()
        print(json.dumps(results, indent=2, default=str))

    elif args.command == "status":
        show_status()

    elif args.command == "test":
        if not CONFIG["discord_webhook_url"]:
            print("Error: DISCORD_WEBHOOK environment variable not set.")
            sys.exit(1)
        test_result = {
            "timestamp": datetime.utcnow().isoformat() + "Z",
            "lowest_price": 347,
            "num_results": 8,
            "top_flights": [
                {"price": 347, "airline": "Delta", "flight_numbers": ["DL 5014", "DL 4987"], "is_basic": False},
                {"price": 389, "airline": "Delta", "flight_numbers": ["DL 2291", "DL 1837"], "is_basic": True},
            ],
            "price_insights": {"price_level": "low", "typical_price_range": [320, 580]},
            "api_status": "Success",
            "trip_id": "test",
        }
        test_trip = {"id": "test", "name": "Test Alert", "origin": "CVG", "destination": "LGA",
                     "outbound_date": "2026-06-11", "return_date": "2026-06-14", "passengers": 2,
                     "flight_numbers": "DL 5014 / DL 4987", "booked_price": 498, "alert_threshold": 100}
        test_val = {"score": 95, "grade": "A", "issues": [], "passed": True}
        send_discord(test_trip, test_result, {"count": 5, "current": 347, "lowest": 312, "highest": 498, "average": 410, "trend": "down"}, test_val, drop_amount=151)
        print("Test Discord message sent — check your #flight-alerts channel.")
