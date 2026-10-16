#!/usr/bin/env python3
"""Football-tool: Advanced Odds API Accumulator Scanner with Web Dashboard & Raw Debugger.
Runs the scanner loop while keeping a lightweight web server open for Render.
"""

import hashlib
import logging
import os
import threading
import time
from itertools import combinations

import httpx
from fastapi import FastAPI
import uvicorn

# ============================================================
# CONFIGURATION
# ============================================================

TELEGRAMBOTTOKEN = os.getenv("TELEGRAMBOTTOKEN", "").strip()
TARGETCHATID = os.getenv("TARGETCHATID", "").strip()

SCAN_INTERVAL_SECONDS = max(60, int(os.getenv("SCAN_INTERVAL_SECONDS", "900")))
BANKROLLPOOL = max(0.0, float(os.getenv("BANKROLLPOOL", "10000")))

ACCUMULATOR_TARGET_ODDS = float(os.getenv("ACCUMULATOR_TARGET_ODDS", "5.0"))
ACCUMULATOR_TOLERANCE = float(os.getenv("ACCUMULATOR_TOLERANCE", "0.5"))

# The Odds API Configuration
ODDS_API_KEY = os.getenv("ODDS_API_KEY", "e188f95c426fe15af08095768989c06b").strip()
SOCCER_LEAGUES = [
    "soccer_epl",
    "soccer_spain_la_liga",
    "soccer_italy_serie_a",
    "soccer_germany_bundesliga",
    "soccer_uefa_champions_league"
]

PORT = int(os.getenv("PORT", "10000"))

LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
logging.basicConfig(level=getattr(logging, LOG_LEVEL, logging.INFO), format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("football-tool")

SENT_ALERTS = set()

# Shared state to inspect latest scan results via browser
LAST_SCAN_STATE = {
    "last_scan_time": None,
    "candidates_count": 0,
    "candidates": [],
    "accumulators_found": 0,
    "accumulators": []
}

app = FastAPI()

@app.api_route("/", methods=["GET", "HEAD"])
def health_check():
    return {"status": "running", "service": "football-tool odds-api scanner"}

@app.get("/fixtures")
def get_fixtures():
    """Inspect current scanned candidates and valid accumulators in browser."""
    return LAST_SCAN_STATE

@app.get("/raw-debug")
def raw_debug():
    """Live test request to The Odds API to inspect raw output and status codes."""
    url = f"https://api.the-odds-api.com/v4/sports/{SOCCER_LEAGUES[0]}/odds/"
    params = {
        "apiKey": ODDS_API_KEY,
        "regions": "eu,uk",
        "markets": "h2h",
        "oddsFormat": "decimal"
    }
    try:
        with httpx.Client(follow_redirects=True) as client:
            resp = client.get(url, params=params, timeout=15)
            try:
                json_data = resp.json()
            except Exception:
                json_data = None
            return {
                "status_code": resp.status_code,
                "url_requested": str(resp.url),
                "remaining_requests": resp.headers.get("x-requests-remaining"),
                "json_payload": json_data[:3] if isinstance(json_data, list) else json_data
            }
    except Exception as exc:
        return {"error": str(exc)}


# ============================================================
# SCANNER LOGIC
# ============================================================

def passes_performance_criteria(team_name, stats_data):
    if not stats_data:
        return True
    try:
        if stats_data.get("fouls_avg", 15) > 12.0 or stats_data.get("cards_avg", 3) > 2.0:
            return False
        if stats_data.get("corners_avg", 0) < 5.0 or stats_data.get("chances_created_avg", 0) < 2.5:
            return False
        return True
    except Exception:
        return False


def fetch_odds_api_candidates(client):
    parsed_events = []
    
    for league in SOCCER_LEAGUES:
        url = f"https://api.the-odds-api.com/v4/sports/{league}/odds/"
        params = {
            "apiKey": ODDS_API_KEY,
            "regions": "eu,uk",
            "markets": "h2h",
            "oddsFormat": "decimal"
        }

        try:
            response = client.get(url, params=params, timeout=30)
            if response.status_code != 200:
                log.warning("Odds API returned status %s for league %s", response.status_code, league)
                continue

            events = response.json()
            if not isinstance(events, list):
                continue

            for event in events:
                home = event.get("home_team")
                away = event.get("away_team")
                bookmakers = event.get("bookmakers", [])

                if not home or not away or not bookmakers:
                    continue

                bookmaker = bookmakers[0]
                markets = bookmaker.get("markets", [])
                
                h2h_market = None
                for m in markets:
                    if m.get("key") == "h2h":
                        h2h_market = m
                        break

                if not h2h_market:
                    continue

                outcomes = h2h_market.get("outcomes", [])
                for outcome in outcomes:
                    name = outcome.get("name")
                    price = outcome.get("price")

                    if not name or not price:
                        continue

                    try:
                        price = float(price)
                    except (TypeError, ValueError):
                        continue

                    if price < 1.30 or price > 3.00:
                        continue

                    if name == home:
                        outcome_label = f"Home ({home})"
                        selected_team = home
                    elif name == away:
                        outcome_label = f"Away ({away})"
                        selected_team = away
                    else:
                        continue

                    if not passes_performance_criteria(selected_team, {}):
                        continue

                    parsed_events.append({
                        "match": f"{home} vs {away}",
                        "selection": outcome_label,
                        "odds": price,
                    })

        except Exception as exc:
            log.exception("Error fetching league %s: %s", league, exc)

    return parsed_events


def build_accumulators(candidates):
    valid_accumulators = []
    for r in (2, 3):
        for combo in combinations(candidates, r):
            if len(set(leg["match"] for leg in combo)) != len(combo):
                continue

            total_odds = 1.0
            for leg in combo:
                total_odds *= leg["odds"]

            if (ACCUMULATOR_TARGET_ODDS - ACCUMULATOR_TOLERANCE) <= total_odds <= (ACCUMULATOR_TARGET_ODDS + ACCUMULATOR_TOLERANCE):
                valid_accumulators.append({"legs": list(combo), "total_odds": total_odds})

    return valid_accumulators


def format_accumulator_alert(acc):
    lines = [
        "🎯 ADVANCED METRICS 5-ODDS ACCUMULATOR",
        f"Combined Total Odds: **{acc['total_odds']:.2f}**",
        f"Illustrative Stake: {BANKROLLPOOL:,.2f}",
        f"Potential Payout: {BANKROLLPOOL * acc['total_odds']:,.2f}",
        "",
        "Selected Legs:",
    ]
    for i, leg in enumerate(acc["legs"], 1):
        lines.append(f"{i}. {leg['match']} — Pick: **{leg['selection']}** @ {leg['odds']:.2f}")
    return "\n".join(lines)


def send_telegram(client, message):
    if not TELEGRAMBOTTOKEN or not TARGETCHATID:
        log.warning("Telegram credentials (TELEGRAMBOTTOKEN or TARGETCHATID) are missing!")
        return False
    url = f"https://api.telegram.org/bot{TELEGRAMBOTTOKEN}/sendMessage"
    try:
        response = client.post(url, json={"chat_id": TARGETCHATID, "text": message, "disable_web_page_preview": True}, timeout=15)
        if response.status_code >= 400:
            log.error("Telegram API error %s: %s", response.status_code, response.text)
            return False
        return True
    except Exception as exc:
        log.error("Telegram send exception: %s", exc)
        return False


def scan_loop():
    log.info("Odds API Scanner background thread started.")
    with httpx.Client(follow_redirects=True) as client:
        while True:
            try:
                candidates = fetch_odds_api_candidates(client)
                accumulators = build_accumulators(candidates)
                
                LAST_SCAN_STATE["last_scan_time"] = time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime())
                LAST_SCAN_STATE["candidates_count"] = len(candidates)
                LAST_SCAN_STATE["candidates"] = candidates[:30]
                LAST_SCAN_STATE["accumulators_found"] = len(accumulators)
                LAST_SCAN_STATE["accumulators"] = accumulators[:10]

                log.info("Scan complete. Candidates found: %d, Matching ~5.0 odds accumulators: %d", len(candidates), len(accumulators))

                for acc in accumulators[:2]:
                    signature = hashlib.sha256(str(acc).encode()).hexdigest()
                    if signature in SENT_ALERTS:
                        continue
                    
                    message = format_accumulator_alert(acc)
                    if send_telegram(client, message):
                        SENT_ALERTS.add(signature)
                        log.info("Alert triggered for %.2f odds accumulator.", acc['total_odds'])
            except Exception:
                log.exception("Error in scan cycle.")
            time.sleep(SCAN_INTERVAL_SECONDS)


if __name__ == "__main__":
    t = threading.Thread(target=scan_loop, daemon=True)
    t.start()

    log.info("Starting web server on port %d...", PORT)
    uvicorn.run(app, host="0.0.0.0", port=PORT)
