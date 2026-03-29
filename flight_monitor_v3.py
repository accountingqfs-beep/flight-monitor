#!/usr/bin/env python3
"""
Flight Price Monitor v3.0
Multi-trip tracking with:
- Lowest price + shortest flight dual tracking
- Two one-way ticket comparison with per-direction airline filters
- Discord push notifications with rich embeds
- GitHub Actions scheduler (free tier)
- SerpApi Google Flights (free tier: 250/mo)
"""

import os, sys, json, smtplib, logging
from datetime import datetime, date
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from pathlib import Path

try:
    import requests
except ImportError:
    import subprocess
    subprocess.check_call([sys.executable, "-m", "pip", "install", "requests", "-q"])
    import requests

# ═══════════════════════════════════════════════════════════════════════════════
# CONFIGURATION
# ═══════════════════════════════════════════════════════════════════════════════
CONFIG = {
    "api_key": os.environ.get("SERPAPI_KEY", ""),
    "discord_webhook_url": os.environ.get("DISCORD_WEBHOOK", ""),
    "email_enabled": os.environ.get("EMAIL_ENABLED", "false").lower() == "true",
    "email_smtp_server": "smtp.gmail.com",
    "email_smtp_port": 587,
    "email_sender": os.environ.get("EMAIL_SENDER", ""),
    "email_password": os.environ.get("EMAIL_PASSWORD", ""),
    "email_recipients": [x.strip() for x in os.environ.get("EMAIL_RECIPIENTS", "").split(",") if x.strip()],
    "sms_gateway": os.environ.get("SMS_GATEWAY", ""),
    "data_dir": Path(os.environ.get("DATA_DIR", str(Path.home() / ".flight_monitor"))),
}

# ═══════════════════════════════════════════════════════════════════════════════
# TRIPS — Edit this list to add/remove trips
# ═══════════════════════════════════════════════════════════════════════════════
TRIPS = [
    {
        "id": "cvg-lga-jun26",
        "name": "NYC June Trip",
        "origin": "CVG",
        "destination": "LGA",
        "outbound_date": "2026-06-11",
        "return_date": "2026-06-14",
        "passengers": 2,
        "stops": "1",             # "1"=nonstop, "2"=1-stop-or-fewer, "0"=any
        "cabin": "1",             # "1"=economy, "2"=premium, "3"=business, "4"=first
        "booked_price": None,     # SET THIS to your total booked price
        "alert_threshold": 100,
        "active": True,

        # Round trip airline filter
        "airline_filter": "DL",

        # One-way comparison
        "search_one_ways": False,  # Set True to also search two one-ways
        "outbound_airline": "DL",  # Airline for outbound one-way ("" = all)
        "return_airline": "DL",    # Airline for return one-way ("" = all)
    },
    {
        "id": "cvg-ogg-nov26",
        "name": "Maui November Trip",
        "origin": "CVG",
        "destination": "OGG",
        "outbound_date": "2026-11-01",   # UPDATE with actual dates
        "return_date": "2026-11-08",     # UPDATE with actual dates
        "passengers": 2,
        "stops": "2",             # 1 stop or fewer (no nonstop CVG-OGG)
        "cabin": "1",
        "booked_price": None,
        "alert_threshold": 150,
        "active": True,

        "airline_filter": "",     # All airlines for round trip

        "search_one_ways": True,  # Compare two one-ways
        "outbound_airline": "DL", # Delta outbound
        "return_airline": "",     # All airlines return
    },
]

# ═══════════════════════════════════════════════════════════════════════════════
# LOGGING
# ═══════════════════════════════════════════════════════════════════════════════
def setup_logging():
    d = CONFIG["data_dir"]
    d.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s",
        handlers=[logging.FileHandler(d / "monitor.log"), logging.StreamHandler(sys.stdout)])
    return logging.getLogger(__name__)

log = setup_logging()

# ═══════════════════════════════════════════════════════════════════════════════
# SERPAPI SEARCH
# ═══════════════════════════════════════════════════════════════════════════════
def serpapi_search(params):
    """Execute a single SerpApi search and return parsed flight list."""
    params["api_key"] = CONFIG["api_key"]
    params["engine"] = "google_flights"
    params["currency"] = "USD"
    params["hl"] = "en"
    params = {k: v for k, v in params.items() if v}

    resp = requests.get("https://serpapi.com/search", params=params, timeout=45)
    resp.raise_for_status()
    data = resp.json()

    flights = []
    for cat in ["best_flights", "other_flights"]:
        for fg in data.get(cat, []):
            entry = {
                "price": fg.get("price"),
                "duration": fg.get("total_duration"),
                "airline": None,
                "flight_numbers": [],
                "is_basic": False,
                "stops": len(fg.get("flights", [])) - 1,
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

    return {
        "flights": flights,
        "price_insights": data.get("price_insights", {}),
        "status": data.get("search_metadata", {}).get("status", "unknown"),
    }

def find_lowest(flights):
    """Find lowest price flight."""
    if not flights:
        return None
    f = min(flights, key=lambda x: x["price"])
    return {"price": f["price"], "duration": f.get("duration"), "airline": f.get("airline"),
            "flight_numbers": f.get("flight_numbers", []), "is_basic": f.get("is_basic", False)}

def find_shortest(flights, tolerance_min=30):
    """Find cheapest flight among the shortest-duration options (within tolerance)."""
    if not flights:
        return None
    valid = [f for f in flights if f.get("duration")]
    if not valid:
        return find_lowest(flights)
    min_dur = min(f["duration"] for f in valid)
    short_group = [f for f in valid if f["duration"] <= min_dur + tolerance_min]
    f = min(short_group, key=lambda x: x["price"])
    return {"price": f["price"], "duration": f.get("duration"), "airline": f.get("airline"),
            "flight_numbers": f.get("flight_numbers", []), "is_basic": f.get("is_basic", False)}

# ═══════════════════════════════════════════════════════════════════════════════
# TRIP CHECK — runs all searches for one trip
# ═══════════════════════════════════════════════════════════════════════════════
def check_trip_prices(trip):
    """Run all configured searches for a trip and return consolidated results."""
    results = {
        "timestamp": datetime.utcnow().isoformat() + "Z",
        "trip_id": trip["id"],
        "api_calls": 0,
    }

    # ── 1. Round trip search ──
    log.info(f"[{trip['id']}] Round trip: {trip['origin']}→{trip['destination']}")
    rt_params = {
        "type": "1",
        "departure_id": trip["origin"],
        "arrival_id": trip["destination"],
        "outbound_date": trip["outbound_date"],
        "return_date": trip["return_date"],
        "adults": str(trip["passengers"]),
        "stops": trip.get("stops", "0"),
        "travel_class": trip.get("cabin", "1"),
    }
    if trip.get("airline_filter"):
        rt_params["include_airlines"] = trip["airline_filter"]

    rt = serpapi_search(rt_params)
    results["api_calls"] += 1
    results["rt_status"] = rt["status"]
    results["rt_num_results"] = len(rt["flights"])
    results["price_insights"] = rt["price_insights"]

    rt_lowest = find_lowest(rt["flights"])
    rt_shortest = find_shortest(rt["flights"])

    results["rt_lowest"] = rt_lowest
    results["rt_shortest"] = rt_shortest
    results["rt_top5"] = sorted(rt["flights"], key=lambda x: x["price"])[:5]

    # ── 2. One-way searches (if enabled) ──
    results["ow_enabled"] = trip.get("search_one_ways", False)
    results["ow_outbound"] = None
    results["ow_return"] = None
    results["ow_combined"] = None

    if trip.get("search_one_ways"):
        # Outbound one-way
        log.info(f"[{trip['id']}] One-way out: {trip['origin']}→{trip['destination']}")
        ow_out_params = {
            "type": "2",
            "departure_id": trip["origin"],
            "arrival_id": trip["destination"],
            "outbound_date": trip["outbound_date"],
            "adults": str(trip["passengers"]),
            "stops": trip.get("stops", "0"),
            "travel_class": trip.get("cabin", "1"),
        }
        if trip.get("outbound_airline"):
            ow_out_params["include_airlines"] = trip["outbound_airline"]

        ow_out = serpapi_search(ow_out_params)
        results["api_calls"] += 1

        ow_out_lowest = find_lowest(ow_out["flights"])
        ow_out_shortest = find_shortest(ow_out["flights"])
        results["ow_outbound"] = {
            "lowest": ow_out_lowest,
            "shortest": ow_out_shortest,
            "num_results": len(ow_out["flights"]),
            "airline_filter": trip.get("outbound_airline", ""),
        }

        # Return one-way
        log.info(f"[{trip['id']}] One-way ret: {trip['destination']}→{trip['origin']}")
        ow_ret_params = {
            "type": "2",
            "departure_id": trip["destination"],
            "arrival_id": trip["origin"],
            "outbound_date": trip["return_date"],
            "adults": str(trip["passengers"]),
            "stops": trip.get("stops", "0"),
            "travel_class": trip.get("cabin", "1"),
        }
        if trip.get("return_airline"):
            ow_ret_params["include_airlines"] = trip["return_airline"]

        ow_ret = serpapi_search(ow_ret_params)
        results["api_calls"] += 1

        ow_ret_lowest = find_lowest(ow_ret["flights"])
        ow_ret_shortest = find_shortest(ow_ret["flights"])
        results["ow_return"] = {
            "lowest": ow_ret_lowest,
            "shortest": ow_ret_shortest,
            "num_results": len(ow_ret["flights"]),
            "airline_filter": trip.get("return_airline", ""),
        }

        # Combined one-way pricing
        if ow_out_lowest and ow_ret_lowest:
            combined_lowest = ow_out_lowest["price"] + ow_ret_lowest["price"]
            results["ow_combined"] = {
                "lowest_total": combined_lowest,
                "out_price": ow_out_lowest["price"],
                "out_airline": ow_out_lowest.get("airline", ""),
                "out_duration": ow_out_lowest.get("duration"),
                "ret_price": ow_ret_lowest["price"],
                "ret_airline": ow_ret_lowest.get("airline", ""),
                "ret_duration": ow_ret_lowest.get("duration"),
            }
        if ow_out_shortest and ow_ret_shortest:
            combined_short = ow_out_shortest["price"] + ow_ret_shortest["price"]
            results["ow_combined_shortest"] = {
                "total": combined_short,
                "out_price": ow_out_shortest["price"],
                "out_airline": ow_out_shortest.get("airline", ""),
                "out_duration": ow_out_shortest.get("duration"),
                "ret_price": ow_ret_shortest["price"],
                "ret_airline": ow_ret_shortest.get("airline", ""),
                "ret_duration": ow_ret_shortest.get("duration"),
            }

    return results

# ═══════════════════════════════════════════════════════════════════════════════
# VALIDATION
# ═══════════════════════════════════════════════════════════════════════════════
def validate(results):
    score, issues = 100, []
    if results.get("rt_status") not in ("Success", "success"):
        score -= 20; issues.append("API status not success")
    if results.get("rt_num_results", 0) == 0:
        score -= 50; issues.append("No round trip flights returned")
    elif results.get("rt_num_results", 0) < 3:
        score -= 10; issues.append(f"Only {results['rt_num_results']} RT results")
    lp = results.get("rt_lowest", {})
    if not lp or not lp.get("price"):
        score -= 30; issues.append("No RT price found")
    elif lp["price"] < 50:
        score -= 25; issues.append(f"Suspiciously low: ${lp['price']}")
    grade = "A" if score >= 90 else "B" if score >= 75 else "C" if score >= 60 else "F"
    return {"score": max(0, score), "grade": grade, "issues": issues, "passed": score >= 60}

# ═══════════════════════════════════════════════════════════════════════════════
# PRICE HISTORY
# ═══════════════════════════════════════════════════════════════════════════════
def load_history():
    p = CONFIG["data_dir"] / "price_history.json"
    return json.load(open(p)) if p.exists() else {}

def save_history(h):
    with open(CONFIG["data_dir"] / "price_history.json", "w") as f:
        json.dump(h, f, indent=2, default=str)

def add_to_history(history, trip_id, results):
    if trip_id not in history:
        history[trip_id] = {"checks": []}
    entry = {
        "timestamp": results["timestamp"],
        "rt_lowest_price": results.get("rt_lowest", {}).get("price") if results.get("rt_lowest") else None,
        "rt_lowest_duration": results.get("rt_lowest", {}).get("duration") if results.get("rt_lowest") else None,
        "rt_lowest_airline": results.get("rt_lowest", {}).get("airline") if results.get("rt_lowest") else None,
        "rt_shortest_price": results.get("rt_shortest", {}).get("price") if results.get("rt_shortest") else None,
        "rt_shortest_duration": results.get("rt_shortest", {}).get("duration") if results.get("rt_shortest") else None,
        "ow_combined_lowest": results.get("ow_combined", {}).get("lowest_total") if results.get("ow_combined") else None,
        "ow_combined_shortest": results.get("ow_combined_shortest", {}).get("total") if results.get("ow_combined_shortest") else None,
        "ow_out_price": results.get("ow_combined", {}).get("out_price") if results.get("ow_combined") else None,
        "ow_out_airline": results.get("ow_combined", {}).get("out_airline") if results.get("ow_combined") else None,
        "ow_ret_price": results.get("ow_combined", {}).get("ret_price") if results.get("ow_combined") else None,
        "ow_ret_airline": results.get("ow_combined", {}).get("ret_airline") if results.get("ow_combined") else None,
        "price_level": results.get("price_insights", {}).get("price_level", ""),
        "typical_range": results.get("price_insights", {}).get("typical_price_range", []),
        "api_calls": results.get("api_calls", 1),
    }
    history[trip_id]["checks"].append(entry)
    save_history(history)
    return entry

def get_stats(history, trip_id):
    checks = history.get(trip_id, {}).get("checks", [])
    rt_prices = [c["rt_lowest_price"] for c in checks if c.get("rt_lowest_price")]
    if not rt_prices:
        return None
    return {
        "count": len(checks),
        "rt_current": rt_prices[-1],
        "rt_lowest": min(rt_prices),
        "rt_highest": max(rt_prices),
        "rt_avg": sum(rt_prices) / len(rt_prices),
        "rt_trend": "down" if len(rt_prices) >= 2 and rt_prices[-1] < rt_prices[-2]
                    else "up" if len(rt_prices) >= 2 and rt_prices[-1] > rt_prices[-2] else "flat",
    }

# ═══════════════════════════════════════════════════════════════════════════════
# API USAGE TRACKING
# ═══════════════════════════════════════════════════════════════════════════════
def track_api_usage(calls):
    p = CONFIG["data_dir"] / "api_usage.json"
    usage = json.load(open(p)) if p.exists() else {}
    mk = datetime.utcnow().strftime("%Y-%m")
    usage[mk] = usage.get(mk, 0) + calls
    with open(p, "w") as f:
        json.dump(usage, f, indent=2)
    return usage[mk]

# ═══════════════════════════════════════════════════════════════════════════════
# DISCORD
# ═══════════════════════════════════════════════════════════════════════════════
def fmt_dur(mins):
    if not mins:
        return "?"
    return f"{mins // 60}h {mins % 60}m"

def send_discord(trip, results, stats, val):
    url = CONFIG.get("discord_webhook_url")
    if not url:
        log.warning("Discord webhook not configured"); return

    rt_lp = results.get("rt_lowest", {}).get("price") if results.get("rt_lowest") else None
    rt_sp = results.get("rt_shortest", {}).get("price") if results.get("rt_shortest") else None
    rt_sd = results.get("rt_shortest", {}).get("duration") if results.get("rt_shortest") else None
    ow = results.get("ow_combined")
    ow_short = results.get("ow_combined_shortest")
    booked = trip.get("booked_price")
    pi = results.get("price_insights", {})

    # Determine best price across all options
    prices = [p for p in [rt_lp, rt_sp, ow.get("lowest_total") if ow else None] if p]
    best = min(prices) if prices else rt_lp
    drop = booked - best if booked and best else None

    # Color
    if drop and drop >= trip.get("alert_threshold", 100):
        color, title_prefix = 0x10B981, "🟢 PRICE DROP"
    elif drop and drop > 0:
        color, title_prefix = 0xF59E0B, "🟡 Small drop"
    elif drop and drop < 0:
        color, title_prefix = 0xEF4444, "🔴 Price up"
    else:
        color, title_prefix = 0x3B82F6, "✈️ Price check"

    title = f"{title_prefix} — {trip['origin']} → {trip['destination']}"

    # Fields
    fields = []

    # Round trip section
    fields.append({"name": "🔄 ROUND TRIP", "value": "─────────────", "inline": False})
    if rt_lp:
        fields.append({"name": "Lowest", "value": f"**${rt_lp}**", "inline": True})
    if rt_sp:
        fields.append({"name": "Shortest", "value": f"**${rt_sp}** ({fmt_dur(rt_sd)})", "inline": True})
    if booked and rt_lp:
        d = booked - rt_lp
        fields.append({"name": "vs Booked", "value": f"{'↓' if d > 0 else '↑'} **${abs(d):.0f}**", "inline": True})

    # One-way section
    if ow:
        fields.append({"name": "🔀 TWO ONE-WAYS", "value": "─────────────", "inline": False})
        fields.append({"name": "Outbound", "value": f"${ow['out_price']} ({ow.get('out_airline','?')})", "inline": True})
        fields.append({"name": "Return", "value": f"${ow['ret_price']} ({ow.get('ret_airline','?')})", "inline": True})
        fields.append({"name": "Combined", "value": f"**${ow['lowest_total']}**", "inline": True})

        # Compare RT vs OW
        if rt_lp:
            diff = rt_lp - ow["lowest_total"]
            if diff > 0:
                fields.append({"name": "💡 Savings", "value": f"Two one-ways is **${diff:.0f} cheaper** than round trip", "inline": False})
            elif diff < 0:
                fields.append({"name": "📊 Compare", "value": f"Round trip is ${abs(diff):.0f} cheaper than two one-ways", "inline": False})

    # Price insights
    typical = pi.get("typical_price_range", [])
    if typical:
        fields.append({"name": "Typical Range", "value": f"${typical[0]} – ${typical[1]} ({pi.get('price_level','?')})", "inline": True})
    if stats:
        trend_icon = "📉" if stats["rt_trend"] == "down" else "📈" if stats["rt_trend"] == "up" else "➡️"
        fields.append({"name": "Trend", "value": f"{trend_icon} {stats['rt_trend']}", "inline": True})
    fields.append({"name": "Reliability", "value": f"{val['grade']} ({val['score']}/100)", "inline": True})

    # API usage
    month_total = track_api_usage(0)  # read without incrementing
    fields.append({"name": "API Usage", "value": f"{month_total}/250 this month", "inline": True})

    embed = {
        "title": title,
        "color": color,
        "fields": fields,
        "footer": {"text": f"{trip.get('name','')} · {trip['outbound_date']} → {trip.get('return_date','')} · {trip['passengers']}pax · {results.get('api_calls',1)} API calls used"},
        "timestamp": results["timestamp"],
    }

    payload = {
        "username": "Flight Monitor",
        "avatar_url": "https://em-content.zobj.net/source/apple/391/airplane_2708-fe0f.png",
        "embeds": [embed],
    }

    try:
        resp = requests.post(url, json=payload, timeout=10)
        if resp.status_code in (200, 204):
            log.info(f"[{trip['id']}] Discord sent")
        else:
            log.error(f"[{trip['id']}] Discord {resp.status_code}: {resp.text[:200]}")
    except Exception as e:
        log.error(f"[{trip['id']}] Discord failed: {e}")

# ═══════════════════════════════════════════════════════════════════════════════
# EMAIL/SMS BACKUP
# ═══════════════════════════════════════════════════════════════════════════════
def send_email_sms(trip, results, drop):
    if not CONFIG.get("email_enabled"):
        return
    rt_lp = results.get("rt_lowest", {}).get("price") if results.get("rt_lowest") else "?"
    drop_text = f" [-${drop:.0f}]" if drop and drop > 0 else ""
    subject = f"Flight: {trip['origin']}→{trip['destination']} ${rt_lp}{drop_text}"
    body = f"{trip['origin']}→{trip['destination']} — {trip.get('name','')}\nLowest: ${rt_lp}\n"
    if drop:
        body += f"Drop: ${drop:.0f}\n"
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
        with smtplib.SMTP(CONFIG["email_smtp_server"], CONFIG["email_smtp_port"]) as s:
            s.starttls()
            s.login(CONFIG["email_sender"], CONFIG["email_password"])
            s.send_message(msg)
        log.info(f"[{trip['id']}] Email/SMS sent")
    except Exception as e:
        log.error(f"[{trip['id']}] Email failed: {e}")

# ═══════════════════════════════════════════════════════════════════════════════
# MAIN
# ═══════════════════════════════════════════════════════════════════════════════
def process_trip(trip, history):
    log.info(f"[{trip['id']}] === Starting: {trip['origin']}→{trip['destination']} ===")

    try:
        results = check_trip_prices(trip)
    except Exception as e:
        log.error(f"[{trip['id']}] API error: {e}")
        return {"trip_id": trip["id"], "error": str(e)}

    val = validate(results)
    log.info(f"[{trip['id']}] Validation: {val['grade']} | API calls: {results['api_calls']}")

    # Track API usage
    month_used = track_api_usage(results["api_calls"])
    log.info(f"[{trip['id']}] API usage this month: {month_used}/250")

    # Save history
    add_to_history(history, trip["id"], results)
    stats = get_stats(history, trip["id"])

    # Determine best price and drop
    rt_lp = results.get("rt_lowest", {}).get("price") if results.get("rt_lowest") else None
    ow_lp = results.get("ow_combined", {}).get("lowest_total") if results.get("ow_combined") else None
    all_prices = [p for p in [rt_lp, ow_lp] if p]
    best = min(all_prices) if all_prices else None
    drop = trip["booked_price"] - best if trip.get("booked_price") and best else None

    # Log summary
    log.info(f"[{trip['id']}] RT Lowest: ${rt_lp or '?'} | RT Shortest: ${results.get('rt_shortest',{}).get('price','?') if results.get('rt_shortest') else '?'}")
    if ow_lp:
        log.info(f"[{trip['id']}] OW Combined: ${ow_lp}")
    if drop:
        log.info(f"[{trip['id']}] Drop vs booked: ${drop:.0f}")

    # Notify
    always = os.environ.get("ALWAYS_NOTIFY", "true").lower() == "true"
    threshold_met = drop is not None and drop >= trip.get("alert_threshold", 100)

    if always or threshold_met:
        send_discord(trip, results, stats, val)
    if threshold_met:
        send_email_sms(trip, results, drop)
        log.info(f"[{trip['id']}] *** ALERT: -${drop:.0f} ***")

    return {
        "trip_id": trip["id"],
        "rt_lowest": rt_lp,
        "rt_shortest": results.get("rt_shortest", {}).get("price") if results.get("rt_shortest") else None,
        "ow_combined": ow_lp,
        "best_price": best,
        "drop": drop,
        "alert": threshold_met,
        "grade": val["grade"],
        "api_calls": results["api_calls"],
    }

def run_all():
    if not CONFIG["api_key"]:
        log.error("SERPAPI_KEY not set"); sys.exit(1)

    active = [t for t in TRIPS if t.get("active", True)]
    log.info(f"Checking {len(active)} trip(s)")
    history = load_history()
    results = []
    total_calls = 0

    for trip in active:
        if trip.get("outbound_date") and trip["outbound_date"] < date.today().isoformat():
            log.info(f"[{trip['id']}] Skipping — past departure"); continue
        r = process_trip(trip, history)
        results.append(r)
        total_calls += r.get("api_calls", 0)

    log.info("=" * 55)
    log.info(f"DONE — {total_calls} total API calls")
    for r in results:
        s = "⚠️ ALERT" if r.get("alert") else "✓"
        log.info(f"  {s} {r['trip_id']}: RT=${r.get('rt_lowest','?')} Short=${r.get('rt_shortest','?')} OW=${r.get('ow_combined','N/A')}")
    log.info("=" * 55)
    return results

def show_status():
    history = load_history()
    if not history:
        print("No history yet."); return
    print(f"\n{'='*60}")
    print(f"  Flight Price Monitor v3 — Status")
    print(f"{'='*60}")
    for trip in TRIPS:
        stats = get_stats(history, trip["id"])
        if not stats:
            print(f"\n  {trip['origin']}→{trip['destination']} — no data"); continue
        d = (date.fromisoformat(trip["outbound_date"]) - date.today()).days
        print(f"\n  {trip['origin']}→{trip['destination']} ({trip['name']}) — {d}d out")
        print(f"    Checks:    {stats['count']}")
        print(f"    RT Lowest: ${stats['rt_current']} (low: ${stats['rt_lowest']}, avg: ${stats['rt_avg']:.0f})")
        print(f"    Trend:     {stats['rt_trend']}")
        if trip.get("booked_price"):
            diff = trip["booked_price"] - stats["rt_current"]
            print(f"    vs Booked: {'↓' if diff > 0 else '↑'}${abs(diff):.0f}")
    # API usage
    p = CONFIG["data_dir"] / "api_usage.json"
    if p.exists():
        usage = json.load(open(p))
        mk = datetime.utcnow().strftime("%Y-%m")
        print(f"\n  API Usage ({mk}): {usage.get(mk, 0)}/250")
    print(f"\n{'='*60}\n")

if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Flight Price Monitor v3")
    parser.add_argument("command", choices=["check", "status", "test"])
    args = parser.parse_args()

    if args.command == "check":
        r = run_all()
        print(json.dumps(r, indent=2, default=str))
    elif args.command == "status":
        show_status()
    elif args.command == "test":
        if not CONFIG["discord_webhook_url"]:
            print("DISCORD_WEBHOOK not set"); sys.exit(1)
        test_results = {
            "timestamp": datetime.utcnow().isoformat() + "Z", "trip_id": "test", "api_calls": 3,
            "rt_status": "Success", "rt_num_results": 12,
            "rt_lowest": {"price": 612, "duration": 490, "airline": "Delta", "flight_numbers": ["DL 2291", "DL 847"]},
            "rt_shortest": {"price": 689, "duration": 370, "airline": "Delta", "flight_numbers": ["DL 835", "DL 271"]},
            "rt_top5": [],
            "ow_enabled": True,
            "ow_outbound": {"lowest": {"price": 289}},
            "ow_return": {"lowest": {"price": 278}},
            "ow_combined": {"lowest_total": 567, "out_price": 289, "out_airline": "Delta", "out_duration": 370, "ret_price": 278, "ret_airline": "United", "ret_duration": 425},
            "ow_combined_shortest": {"total": 623, "out_price": 312, "out_airline": "Delta", "out_duration": 370, "ret_price": 311, "ret_airline": "American", "ret_duration": 390},
            "price_insights": {"price_level": "low", "typical_price_range": [550, 920]},
        }
        test_trip = {"id": "test", "name": "Test: CVG→OGG", "origin": "CVG", "destination": "OGG",
                     "outbound_date": "2026-11-01", "return_date": "2026-11-08", "passengers": 2,
                     "booked_price": 750, "alert_threshold": 100, "search_one_ways": True}
        test_val = {"score": 92, "grade": "A", "issues": [], "passed": True}
        test_stats = {"count": 8, "rt_current": 612, "rt_lowest": 580, "rt_highest": 750, "rt_avg": 665, "rt_trend": "down"}
        send_discord(test_trip, test_results, test_stats, test_val)
        print("Test Discord sent — check #general in Flight Alerts.")
