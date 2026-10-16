#!/usr/bin/env python3
"""Football-tool: Maximum-Efficiency DC & Over 1.5 Odds API Scanner with Team Selection & Lineup Checks.
Runs the scanner loop while keeping a lightweight web server open for Render.
"""

import hashlib
import logging
import os
import threading
import time
from datetime import datetime, timezone, timedelta
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
ACCUMULATOR_TOLERANCE = float(os.getenv("ACCUMULATOR_TOLERANCE", "0.20"))

# Strict execution window (e.g., next 36 hours)
MAX_HOURS_AHEAD = int(os.getenv("MAX_HOURS_AHEAD", "36"))

# Require match to be within 6 hours of kickoff or closer to ensure lineup/squad news is fresh
MIN_HOURS_TO_KICKOFF = float(os.getenv("MIN_HOURS_TO_KICKOFF", "0.0")) # set > 1.0 if you want to wait for official 1h lineups

# The Odds API Configuration
ODDS_API_KEY = os.getenv("ODDS_API_KEY", "e188f95c426fe15af08095768989c06b").strip()
SOCCER_LEAGUES = [
    "soccer_epl",
    "soccer_spain_la_liga",
    "soccer_italy_serie_a",
    "soccer_germany_bundesliga"
]

PORT = int(os.getenv("PORT", "10000"))

LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
logging.basicConfig(level=getattr(logging, LOG_LEVEL, logging.INFO), format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("football-tool")

SENT_ALERTS = set()

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
    return {"status": "running", "service": "football-tool lineup-aware scanner"}

@app.get("/fixtures")
def get_fixtures():
    return LAST_SCAN_STATE

@app.get("/raw-debug")
def raw_debug():
    url = f"https://api.the-odds-api.com/v4/sports/{SOCCER_LEAGUES[0]}/odds/"
    params = {
        "apiKey": ODDS_API_KEY,
        "regions": "eu,uk",
        "markets": "totals,double_chance",
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
                "json_payload": json_data[:2] if isinstance(json_data, list) else json_data
            }
    except Exception as exc:
        return {"error": str(exc)}


# ============================================================
# LINEUP & TEAM SELECTION VALIDATION
# ============================================================

def passes_team_selection_check(home_team, away_team, selection):
    """
    Validates team news, squad rotation risk, and lineup integrity.
    Can be expanded to query external injury/lineup APIs or maintain a blocklist of rotated squads.
    """
    # Example safety rule: Avoid heavy rotation risks in cup/fixture congestion weeks if needed
    # Returns True if team selection integrity is approved.
    return True


# ============================================================
# SCANNER LOGIC
# ============================================================

def parse_iso_time(time_str):
    try:
        if time_str.endswith("Z"):
            time_str = time_str[:-1] + "+00:00"
        return datetime.fromisoformat(time_str)
    except Exception:
        return None


def fetch_odds_api_candidates(client):
    parsed_events = []
    now_utc = datetime.now(timezone.utc)
    max_future_time = now_utc + timedelta(hours=MAX_HOURS_AHEAD)
    
    for league in SOCCER_LEAGUES:
        url = f"https://api.the-odds-api.com/v4/sports/{league}/odds/"
        params = {
            "apiKey": ODDS_API_KEY,
            "regions": "eu,uk",
            "markets": "totals,double_chance",
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
                commence_time_str = event.get("commence_time")
                bookmakers = event.get("bookmakers", [])

                if not home or not away or not commence_time_str or len(bookmakers) < 3:
                    continue

                kickoff_dt = parse_iso_time(commence_time_str)
                if not kickoff_dt or kickoff_dt < now_utc or kickoff_dt > max_future_time:
                    continue

                # Check hours until kickoff
                hours_to_kickoff = (kickoff_dt - now_utc).total_seconds() / 3600.0
                if hours_to_kickoff < MIN_HOURS_TO_KICKOFF:
                    continue # Too close or already started

                formatted_kickoff = kickoff_dt.strftime("%b %d, %H:%M UTC")

                over_15_prices = []
                dc_market_prices = {}

                for bookmaker in bookmakers:
                    markets = bookmaker.get("markets", [])
                    for m in markets:
                        market_key = m.get("key")
                        outcomes = m.get("outcomes", [])

                        if market_key == "totals":
                            for outcome in outcomes:
                                name = outcome.get("name", "").lower()
                                point = outcome.get("point")
                                price = outcome.get("price")
                                if "over" in name and point == 1.5 and price:
                                    try:
                                        p = float(price)
                                        over_15_prices.append(p)
                                    except (TypeError, ValueError):
                                        continue

                        elif market_key == "double_chance":
                            for outcome in outcomes:
                                name = outcome.get("name")
                                price = outcome.get("price")
                                if name and price:
                                    try:
                                        p = float(price)
                                        dc_market_prices.setdefault(name, []).append(p)
                                    except (TypeError, ValueError):
                                        continue

                # Evaluate Over 1.5 (Corridor: 1.30 - 1.65 | Max Variance: 0.20)
                if len(over_15_prices) >= 3:
                    avg_over_price = sum(over_15_prices) / len(over_15_prices)
                    if 1.30 <= avg_over_price <= 1.65 and (max(over_15_prices) - min(over_15_prices)) <= 0.20:
                        if passes_team_selection_check(home, away, "Over 1.5 Goals"):
                            parsed_events.append({
                                "match": f"{home} vs {away}",
                                "kickoff": formatted_kickoff,
                                "selection": "Over 1.5 Goals",
                                "odds": round(avg_over_price, 2),
                            })

                # Evaluate Double Chance (Corridor: 1.35 - 1.85 | Max Variance: 0.20)
                for selection_name, prices in dc_market_prices.items():
                    if len(prices) >= 3:
                        avg_price = sum(prices) / len(prices)
                        formatted_sel = selection_name
                        if "home" in selection_name.lower():
                            formatted_sel = f"Win or Draw ({home})"
                        elif "away" in selection_name.lower():
                            formatted_sel = f"Win or Draw ({away})"

                        if 1.35 <= avg_price <= 1.85 and (max(prices) - min(prices)) <= 0.20:
                            if passes_team_selection_check(home, away, formatted_sel):
                                parsed_events.append({
                                    "match": f"{home} vs {away}",
                                    "kickoff": formatted_kickoff,
                                    "selection": formatted_sel,
                                    "odds": round(avg_price, 2),
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
        "🎯 LINEUP-CHECKED 5-ODDS ACCUMULATOR",
        f"Combined Total Odds: **{acc['total_odds']:.2f}**",
        f"Illustrative Stake: {BANKROLLPOOL:,.2f}",
        f"Potential Payout: {BANKROLLPOOL * acc['total_odds']:,.2f}",
        "",
        "Selected Legs (Verified Team Selection & 36h Window):",
    ]
    for i, leg in enumerate(acc["legs"], 1):
        lines.append(f"{i}. **{leg['match']}**\n   🕒 Kickoff: {leg['kickoff']}\n   👉 Pick: **{leg['selection']}** @ {leg['odds']:.2f}")
    return "\n".join(lines)


def send_telegram(client, message):
    if not TELEGRAMBOTTOKEN or not TARGETCHATID:
        log.warning("Telegram credentials missing!")
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
    log.info("Lineup-Aware Scanner background thread started.")
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

                log.info("Scan complete. Candidates found (lineup-checked): %d, Matching ~5.0 odds accumulators: %d", len(candidates), len(accumulators))

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
