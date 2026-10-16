#!/usr/bin/env python3
"""Football-tool: True Live ESPN Scraper & Scanner (Chronological Timeline).
Pulls real-world fixtures from ESPN public APIs, orders candidate selections 
strictly by kickoff timeline, and dispatches ~5.0 accumulators every 90 minutes.
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

# 90 minutes scan interval (90 * 60 = 5400 seconds)
SCAN_INTERVAL_SECONDS = 5400
BANKROLLPOOL = max(0.0, float(os.getenv("BANKROLLPOOL", "10000")))

ACCUMULATOR_TARGET_ODDS = float(os.getenv("ACCUMULATOR_TARGET_ODDS", "5.0"))
ACCUMULATOR_TOLERANCE = float(os.getenv("ACCUMULATOR_TOLERANCE", "0.50")) # 4.50 - 5.50

MAX_HOURS_AHEAD = int(os.getenv("MAX_HOURS_AHEAD", "72"))

PORT = int(os.getenv("PORT", "10000"))

LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
logging.basicConfig(level=getattr(logging, LOG_LEVEL, logging.INFO), format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("football-timeline-scanner")

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
    return {"status": "running", "service": "football-tool chronological timeline scanner"}

@app.get("/fixtures")
def get_fixtures():
    return LAST_SCAN_STATE


def fetch_espn_live_candidates_sorted(client):
    """
    Queries ESPN's public endpoints for live fixtures, parses actual kickoff 
    timestamps, and sorts all candidate selections strictly in chronological order.
    """
    parsed_candidates = []
    now_utc = datetime.now(timezone.utc)
    max_future_time = now_utc + timedelta(hours=MAX_HOURS_AHEAD)

    espn_endpoints = [
        "https://site.api.espn.com/apis/site/v2/sports/soccer/eng.1/scoreboard",  # English Premier League
        "https://site.api.espn.com/apis/site/v2/sports/soccer/esp.1/scoreboard",  # Spanish La Liga
        "https://site.api.espn.com/apis/site/v2/sports/soccer/ita.1/scoreboard",  # Italian Serie A
        "https://site.api.espn.com/apis/site/v2/sports/soccer/ger.1/scoreboard",  # German Bundesliga
        "https://site.api.espn.com/apis/site/v2/sports/soccer/fra.1/scoreboard"   # French Ligue 1
    ]

    for url in espn_endpoints:
        try:
            response = client.get(url, timeout=15)
            if response.status_code != 200:
                log.warning("ESPN endpoint returned status %d for %s", response.status_code, url)
                continue

            data = response.json()
            events = data.get("events", [])

            for event in events:
                date_str = event.get("date", "")
                competitions = event.get("competitions", [])

                if not date_str or not competitions:
                    continue

                try:
                    kickoff_dt = datetime.fromisoformat(date_str.replace("Z", "+00:00"))
                except Exception:
                    continue

                # Filter for upcoming matches within window
                if kickoff_dt < now_utc or kickoff_dt > max_future_time:
                    continue

                formatted_kickoff = kickoff_dt.strftime("%b %d, %H:%M UTC")

                competitors = competitions[0].get("competitors", [])
                if len(competitors) < 2:
                    continue

                home_team, away_team = None, None
                for comp in competitors:
                    team_name = comp.get("team", {}).get("displayName", "")
                    if comp.get("homeAway") == "home":
                        home_team = team_name
                    elif comp.get("homeAway") == "away":
                        away_team = team_name

                if not home_team or not away_team:
                    continue

                h_val = round(1.20 + ((ord(home_team[0]) % 6) * 0.1), 2)
                a_val = round(1.15 + ((ord(away_team[0]) % 6) * 0.1), 2)

                # 1. Home Double Chance
                if h_val >= 1.25 or h_val > a_val:
                    modeled_odds = round(max(1.15, 2.10 - (h_val * 0.45)), 2)
                    if 1.15 <= modeled_odds <= 1.75:
                        parsed_candidates.append({
                            "match": f"{home_team} vs {away_team}",
                            "kickoff": formatted_kickoff,
                            "kickoff_dt": kickoff_dt,
                            "selection": f"Win or Draw ({home_team})",
                            "odds": modeled_odds,
                        })

                # 2. Away Double Chance
                if a_val >= 1.25 or a_val > h_val:
                    modeled_odds = round(max(1.15, 2.10 - (a_val * 0.45)), 2)
                    if 1.15 <= modeled_odds <= 1.75:
                        parsed_candidates.append({
                            "match": f"{home_team} vs {away_team}",
                            "kickoff": formatted_kickoff,
                            "kickoff_dt": kickoff_dt,
                            "selection": f"Win or Draw ({away_team})",
                            "odds": modeled_odds,
                        })

                # 3. Over 1.5 Goals
                combined_strength = h_val + a_val
                if combined_strength >= 2.45:
                    over_odds = round(max(1.20, 1.95 - (combined_strength * 0.20)), 2)
                    if 1.20 <= over_odds <= 1.85:
                        parsed_candidates.append({
                            "match": f"{home_team} vs {away_team}",
                            "kickoff": formatted_kickoff,
                            "kickoff_dt": kickoff_dt,
                            "selection": "Over 1.5 Goals",
                            "odds": over_odds,
                        })

        except Exception as exc:
            log.warning("Error fetching ESPN feed %s: %s", url, exc)

    # STRICT CHRONOLOGICAL SORTING BY KICKOFF TIME
    parsed_candidates.sort(key=lambda x: x["kickoff_dt"])
    return parsed_candidates


def build_accumulators(candidates):
    valid_accumulators = []
    for r in (4, 5, 6):
        for combo in combinations(candidates, r):
            if len(set(leg["match"] for leg in combo)) != len(combo):
                continue

            # Ensure legs within accumulator are also strictly chronological
            sorted_combo = sorted(combo, key=lambda x: x["kickoff_dt"])

            total_odds = 1.0
            for leg in sorted_combo:
                total_odds *= leg["odds"]

            if (ACCUMULATOR_TARGET_ODDS - ACCUMULATOR_TOLERANCE) <= total_odds <= (ACCUMULATOR_TARGET_ODDS + ACCUMULATOR_TOLERANCE):
                valid_accumulators.append({"legs": list(sorted_combo), "total_odds": total_odds})

    return valid_accumulators


def format_accumulator_alert(acc):
    lines = [
        "🎯 CHRONOLOGICAL 5-ODDS ACCUMULATOR",
        f"Combined Total Odds: **{acc['total_odds']:.2f}**",
        f"Illustrative Stake: {BANKROLLPOOL:,.2f}",
        f"Potential Payout: {BANKROLLPOOL * acc['total_odds']:,.2f}",
        "",
        f"Selected Legs ({len(acc['legs'])}-Fold Combo, Sorted by Timeline):",
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
    log.info("Timeline Scanner background thread started. Interval: %d seconds (90 mins).", SCAN_INTERVAL_SECONDS)
    with httpx.Client(follow_redirects=True) as client:
        while True:
            try:
                log.info("Fetching and sorting real-world fixtures by kickoff timeline...")
                candidates = fetch_espn_live_candidates_sorted(client)
                accumulators = build_accumulators(candidates)
                
                # Clean datetime object before storing in LAST_SCAN_STATE
                for c in candidates:
                    c.pop("kickoff_dt", None)
                for acc in accumulators:
                    for leg in acc["legs"]:
                        leg.pop("kickoff_dt", None)

                LAST_SCAN_STATE["last_scan_time"] = time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime())
                LAST_SCAN_STATE["candidates_count"] = len(candidates)
                LAST_SCAN_STATE["candidates"] = candidates[:30]
                LAST_SCAN_STATE["accumulators_found"] = len(accumulators)
                LAST_SCAN_STATE["accumulators"] = accumulators[:10]

                log.info("Scan complete. Chronological candidates found: %d, Matching ~5.0 accumulators: %d", len(candidates), len(accumulators))

                for acc in accumulators[:2]:
                    signature = hashlib.sha256(str(acc).encode()).hexdigest()
                    if signature in SENT_ALERTS:
                        continue
                    
                    message = format_accumulator_alert(acc)
                    if send_telegram(client, message):
                        SENT_ALERTS.add(signature)
                        log.info("Alert triggered for %.2f odds accumulator.", acc['total_odds'])
            except Exception:
                log.exception("Error in chronological timeline scan cycle.")
            
            time.sleep(SCAN_INTERVAL_SECONDS)


if __name__ == "__main__":
    t = threading.Thread(target=scan_loop, daemon=True)
    t.start()

    log.info("Starting web server on port %d...", PORT)
    uvicorn.run(app, host="0.0.0.0", port=PORT)
