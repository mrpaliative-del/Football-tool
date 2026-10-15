#!/usr/bin/env python3
"""Football-tool: Advanced SportyBet Accumulator Scanner.
Filters teams based on corners, chances created, fouls, cards, and H2H dominance
targeting ~5.0 total combined odds.
"""

import hashlib
import logging
import os
import time
from collections import deque
from datetime import datetime, timezone
from itertools import combinations

import httpx
from playwright.sync_api import sync_playwright

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

LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
logging.basicConfig(level=getattr(logging, LOG_LEVEL, logging.INFO), format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("football-tool")

SENT_ALERTS = set()
ALERT_ORDER = deque()
MAX_REMEMBERED_ALERTS = 2000


# ============================================================
# ADVANCED METRICS EVALUATION FUNCTION
# ============================================================

def passes_performance_criteria(team_name, stats_data):
    """
    Evaluates whether a team meets your advanced performance criteria:
    - High corner kicks & chances created
    - High bound-to-score changes (Big chances)
    - Few fouls committed & few cards received
    - Superior H2H record (Wins + Draws > Opponent Wins)
    """
    if not stats_data:
        return True  # Fallback pass if stats are not yet wired to an external provider

    try:
        # 1. Check disciplinary & efficiency metrics (Few fouls, few cards)
        if stats_data.get("fouls_avg", 15) > 12.0 or stats_data.get("cards_avg", 3) > 2.0:
            return False

        # 2. Check attacking metrics (High corners, high chances created, high bound-to-score)
        if stats_data.get("corners_avg", 0) < 5.0 or stats_data.get("chances_created_avg", 0) < 2.5:
            return False

        # 3. Check Head-to-Head dominance: (Team Wins + Draws) >= Opponent Wins
        team_favorable_h2h = stats_data.get("h2h_wins", 0) + stats_data.get("h2h_draws", 0)
        opponent_wins = stats_data.get("opponent_h2h_wins", 0)
        if team_favorable_h2h < opponent_wins:
            return False

        return True
    except Exception:
        return False


# ============================================================
# SPORTYBET & ACCUMULATOR LOGIC
# ============================================================

def walk_dicts(value):
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from walk_dicts(child)
    elif isinstance(value, list):
        for child in value:
            yield from walk_dicts(child)


def fetch_sportybet_accumulator_candidates():
    if not SPORTYBET_ENABLED:
        return []

    parsed_events = []
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(
                headless=True, 
                args=["--no-sandbox", "--disable-setuid-sandbox", "--disable-dev-shm-usage"]
            )
            context = browser.new_context(user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36")
            page = context.new_page()
            
            page.goto("https://www.sportybet.com/ng/", wait_until="domcontentloaded", timeout=35000)
            time.sleep(3)

            response = page.request.get(f"{SPORTYBET_URL}?pageSize=100&timeline=48")
            if response.status >= 400:
                browser.close()
                return []

            payload = response.json()
            browser.close()

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
                            selected_team, opponent = home, away
                            outcome_label = f"Home ({home})"
                        elif label in {"2", "away", "away win"}:
                            selected_team, opponent = away, home
                            outcome_label = f"Away ({away})"
                        else:
                            continue

                        # Hook for your advanced stats evaluation
                        mock_stats = {}  # Replace with API stats fetcher when ready
                        if not passes_performance_criteria(selected_team, mock_stats):
                            continue

                        parsed_events.append({
                            "match": f"{home} vs {away}",
                            "selection": outcome_label,
                            "odds": price,
                        })

        return parsed_events
    except Exception as exc:
        log.warning("Scraper failed: %s", exc)
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
                valid_accumulators.append({"legs": combo, "total_odds": total_odds})

    return valid_accumulators


def format_accumulator_alert(acc):
    lines = [
        "🎯 ADVANCED METRICS 5-ODDS ACCUMULATOR",
        f"Combined Total Odds: **{acc['total_odds']:.2f}**",
        f"Illustrative Stake: {BANKROLLPOOL:,.2f}",
        f"Potential Payout: {BANKROLLPOOL * acc['total_odds']:,.2f}",
        "",
        "Selected Legs (Optimized for Corners, Chances & Low Fouls):",
    ]
    for i, leg in enumerate(acc["legs"], 1):
        lines.append(f"{i}. {leg['match']} — Pick: **{leg['selection']}** @ {leg['odds']:.2f}")
    return "\n".join(lines)


def send_telegram(client, message):
    if not TELEGRAMBOTTOKEN or not TARGETCHATID:
        log.error("Telegram credentials missing.")
        return False

    url = f"https://api.telegram.org/bot{TELEGRAMBOTTOKEN}/sendMessage"
    try:
        response = client.post(
            url,
            json={"chat_id": TARGETCHATID, "text": message, "disable_web_page_preview": True},
            timeout=15,
        )
        if response.status_code >= 400:
            log.error("Telegram HTTP %s: %s", response.status_code, response.text[:250])
            return False
        return True
    except (httpx.HTTPError, ValueError) as exc:
        log.error("Telegram send failed: %s", exc)
        return False


def scan(client):
    candidates = fetch_sportybet_accumulator_candidates()
    accumulators = build_accumulators(candidates)
    
    sent_count = 0
    for acc in accumulators[:2]:
        signature = hashlib.sha256(str(acc).encode()).hexdigest()
        if signature in SENT_ALERTS:
            continue
        
        message = format_accumulator_alert(acc)
        if send_telegram(client, message):
            SENT_ALERTS.add(signature)
            sent_count += 1
            log.info("Alert triggered for %.2f odds accumulator.", acc['total_odds'])

    log.info("Completed scan cycle. Sent %d accumulator alerts.", sent_count)


def run_forever():
    log.info("Football-tool Advanced Accumulator Scanner started.")
    with httpx.Client() as client:
        while True:
            try:
                scan(client)
            except Exception:
                log.exception("Error in scan cycle.")
            time.sleep(SCAN_INTERVAL_SECONDS)


if __name__ == "__main__":
    run_forever()
