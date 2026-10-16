#!/usr/bin/env python3
"""Football-tool: Advanced SportyBet Accumulator Scanner with Web Dashboard & Raw Debugger.
Runs the scanner loop while keeping a lightweight web server open for Render.
"""

import hashlib
import logging
import os
import threading
import time
from collections import deque
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

SPORTYBET_ENABLED = os.getenv("SPORTYBET_ENABLED", "true").strip().lower() in {"1", "true", "yes", "on"}
SPORTYBET_URL = os.getenv("SPORTYBET_URL", "https://www.sportybet.com/api/ng/factsCenter/pcUpcomingEvents").strip()

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
    return {"status": "running", "service": "football-tool accumulator scanner"}

@app.get("/fixtures")
def get_fixtures():
    """Inspect current scanned candidates and valid accumulators in browser."""
    return LAST_SCAN_STATE

@app.get("/raw-debug")
def raw_debug():
    """Live test request to SportyBet API to inspect raw output and status codes."""
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
        "Accept": "application/json, text/plain, */*",
        "Referer": "https://www.sportybet.com/ng/",
    }
    params = {"pageSize": "10", "timeline": "48"}
    try:
        with httpx.Client(follow_redirects=True) as client:
            resp = client.get(SPORTYBET_URL, headers=headers, params=params, timeout=15)
            try:
                json_data = resp.json()
            except Exception:
                json_data = None
            return {
                "status_code": resp.status_code,
                "url_requested": str(resp.url),
                "headers_received": dict(resp.headers),
                "json_payload": json_data,
                "raw_text_preview": resp.text[:500] if not json_data else "Parsed successfully as JSON"
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
        team_favorable_h2h = stats_data.get("h2h_wins", 0) + stats_data.get("h2h_draws", 0)
        opponent_wins = stats_data.get("opponent_h2h_wins", 0)
        if team_favorable_h2h < opponent_wins:
            return False
        return True
    except Exception:
        return False


def walk_dicts(value):
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from walk_dicts(child)
    elif isinstance(value, list):
        for child in value:
            yield from walk_dicts(child)


def fetch_sportybet_accumulator_candidates(client):
    if not SPORTYBET_ENABLED:
        return []

    parsed_events = []
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
        "Accept": "application/json, text/plain, */*",
        "Referer": "https://www.sportybet.com/ng/",
    }
    params = {"pageSize": "100", "timeline": "48"}

    try:
        response = client.get(SPORTYBET_URL, headers=headers, params=params, timeout=30)
        if response.status_code >= 400:
            log.warning("SportyBet API returned status code: %s", response.status_code)
            return []

        try:
            payload = response.json()
        except Exception:
            log.warning("Failed to parse SportyBet payload as JSON.")
            return []

        if not isinstance(payload, (dict, list)):
            return []

        for item in walk_dicts(payload):
            home = item.get("homeTeamName") or item.get("homeTeam")
            away = item.get("awayTeamName") or item.get("awayTeam")
            if not isinstance(home, str) or not isinstance(away, str):
                continue

            markets = item.get("markets") or item.get("marketList") or []
            for market in markets if isinstance(markets, list) else []:
                if not isinstance(market, dict):
                    continue
                
                market_name = str(market.get("marketName") or market.get("name") or "").lower()
                if not any(label in market_name for label in ("1x2", "match result")):
                    continue

                for selection in market.get("outcomes", []) or []:
                    label = str(selection.get("outcomeName") or selection.get("name") or "").strip().lower()
                    try:
                        price = float(selection.get("odds") or selection.get("price"))
                    except (TypeError, ValueError):
                        continue

                    if price < 1.30 or price > 3.00:
                        continue

                    if label in {"1", "home", "home win"}:
                        selected_team = home
                        outcome_label = f"Home ({home})"
                    elif label in {"2", "away", "away win"}:
                        selected_team = away
                        outcome_label = f"Away ({away})"
                    else:
                        continue

                    if not passes_performance_criteria(selected_team, {}):
                        continue

                    parsed_events.append({
                        "match": f"{home} vs {away}",
                        "selection": outcome_label,
                        "odds": price,
                    })

        return parsed_events
    except Exception as exc:
        log.exception("SportyBet API error: %s", exc)
        return []


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
    log.info("Scanner background thread started.")
    with httpx.Client(follow_redirects=True) as client:
        while True:
            try:
                candidates = fetch_sportybet_accumulator_candidates(client)
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
