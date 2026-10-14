#!/usr/bin/env python3
"""Football-tool: read-only football odds scanner with Telegram alerts.
No bets are placed. Arbitrage calculations are theoretical and exclude costs.
"""

import hashlib
import logging
import os
import time
from collections import deque
from datetime import datetime, timedelta, timezone

import httpx

# ============================================================
# CONFIGURATION — set these in Render > Environment
# ============================================================

ODDS_API_KEY = os.getenv("ODDS_API_KEY", "").strip()
TELEGRAMBOTTOKEN = os.getenv("TELEGRAMBOTTOKEN", "").strip()
TARGETCHATID = os.getenv("TARGETCHATID", "").strip()

SPORT_KEY = os.getenv("SPORT_KEY", "soccer_epl").strip()
ODDS_REGIONS = os.getenv("ODDS_REGIONS", "uk,eu").strip()
TIMELINE_HOURS = max(1, int(os.getenv("TIMELINE_HOURS", "48")))
SCAN_INTERVAL_SECONDS = max(60, int(os.getenv("SCAN_INTERVAL_SECONDS", "900")))

BANKROLLPOOL = max(0.0, float(os.getenv("BANKROLLPOOL", "20000")))
MIN_ARBITRAGE_ROI = float(os.getenv("MIN_ARBITRAGE_ROI", "0.5"))
MAX_ARBITRAGE_ROI = float(os.getenv("MAX_ARBITRAGE_ROI", "30"))

SPORTYBET_ENABLED = os.getenv(
    "SPORTYBET_ENABLED", "false"
).strip().lower() in {"1", "true", "yes", "on"}

SPORTYBET_URL = os.getenv(
    "SPORTYBET_URL",
    "https://www.sportybet.com/api/ng/factsCenter/pcUpcomingEvents",
).strip()

SPORTYBET_TIMEOUT = max(3, int(os.getenv("SPORTYBET_TIMEOUT", "15")))
SPORTYBET_PAGE_SIZE = max(1, int(os.getenv("SPORTYBET_PAGE_SIZE", "100")))
SPORTYBET_TIMELINE = max(1, int(os.getenv("SPORTYBET_TIMELINE", "48")))

SPORTYBET_AUTH_MODE = os.getenv(
    "SPORTYBET_AUTH_MODE", "none"
).strip().lower()

SPORTYBET_BEARER_TOKEN = os.getenv(
    "SPORTYBET_BEARER_TOKEN", ""
).strip()

LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()

logging.basicConfig(
    level=getattr(logging, LOG_LEVEL, logging.INFO),
    format="%(asctime)s %(levelname)s %(message)s",
)
log = logging.getLogger("football-tool")

OUTCOMES = ("home", "draw", "away")

# In-memory duplicate-alert protection; resets when the worker restarts.
SENT_ALERTS = set()
ALERT_ORDER = deque()
MAX_REMEMBERED_ALERTS = 2000


# ============================================================
# GENERAL HELPERS
# ============================================================

def normalize_team(name):
    """Normalize team names for cautious event matching."""
    import re

    name = str(name or "").lower().replace("&", " and ")
    name = re.sub(r"\b(fc|afc|cf|sc|club)\b", " ", name)
    return re.sub(r"[^a-z0-9]+", "", name)


def parse_datetime(value):
    if not value:
        return None

    try:
        if isinstance(value, (int, float)):
            timestamp = float(value)
            if timestamp > 10_000_000_000:
                timestamp /= 1000
            return datetime.fromtimestamp(timestamp, tz=timezone.utc)

        value = str(value).strip()
        if value.endswith("Z"):
            value = value[:-1] + "+00:00"

        parsed = datetime.fromisoformat(value)

        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)

        return parsed.astimezone(timezone.utc)

    except (ValueError, TypeError, OverflowError, OSError):
        return None


def within_time_window(kickoff):
    if kickoff is None:
        return True

    now = datetime.now(timezone.utc)
    end = now + timedelta(hours=TIMELINE_HOURS)

    return now <= kickoff <= end


def event_key(home, away):
    return normalize_team(home), normalize_team(away)


# ============================================================
# THE ODDS API
# ============================================================

def fetch_odds_events(client):
    if not ODDS_API_KEY:
        log.error("ODDS_API_KEY is missing. Configure it in Render.")
        return []

    url = (
        "https://api.the-odds-api.com/v4/sports/"
        f"{SPORT_KEY}/odds"
    )

    params = {
        "apiKey": ODDS_API_KEY,
        "regions": ODDS_REGIONS,
        "markets": "h2h",
        "oddsFormat": "decimal",
        "dateFormat": "iso",
    }

    try:
        response = client.get(url, params=params, timeout=25)

        if response.status_code >= 400:
            log.error(
                "The Odds API HTTP %s: %s",
                response.status_code,
                response.text[:250],
            )
            return []

        payload = response.json()

        if not isinstance(payload, list):
            log.error("Unexpected The Odds API response format.")
            return []

        log.info("The Odds API events received: %d", len(payload))
        return payload

    except (httpx.HTTPError, ValueError) as exc:
        log.error("The Odds API request failed: %s", exc)
        return []


def extract_odds_api_prices(event):
    """Get the highest available decimal odds for each 1X2 outcome."""
    home = str(event.get("home_team", "")).strip()
    away = str(event.get("away_team", "")).strip()

    best = {}

    for bookmaker in event.get("bookmakers", []) or []:
        bookmaker_name = str(
            bookmaker.get("title") or bookmaker.get("key") or "Unknown"
        )

        for market in bookmaker.get("markets", []) or []:
            if market.get("key") != "h2h":
                continue

            for outcome in market.get("outcomes", []) or []:
                name = str(outcome.get("name", "")).strip()

                try:
                    price = float(outcome.get("price"))
                except (TypeError, ValueError):
                    continue

                if price <= 1:
                    continue

                if name == home:
                    key = "home"
                elif name == away:
                    key = "away"
                elif name.lower() in {"draw", "tie"}:
                    key = "draw"
                else:
                    continue

                if key not in best or price > best[key][0]:
                    best[key] = (price, bookmaker_name)

    return best


# ============================================================
# SPORTYBET — EXPERIMENTAL ADAPTER
# The endpoint and response schema must be verified against
# a current authorized response before relying on this parser.
# ============================================================

def walk_dicts(value):
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from walk_dicts(child)

    elif isinstance(value, list):
        for child in value:
            yield from walk_dicts(child)


def fetch_sportybet_events(client):
    if not SPORTYBET_ENABLED:
        log.info("SportyBet integration disabled.")
        return []

    headers = {
        "Accept": "application/json",
        "User-Agent": "Football-tool/1.0",
    }

    if SPORTYBET_AUTH_MODE == "bearer" and SPORTYBET_BEARER_TOKEN:
        headers["Authorization"] = f"Bearer {SPORTYBET_BEARER_TOKEN}"

    params = {
        "pageSize": SPORTYBET_PAGE_SIZE,
        "timeline": SPORTYBET_TIMELINE,
    }

    try:
        response = client.get(
            SPORTYBET_URL,
            headers=headers,
            params=params,
            timeout=SPORTYBET_TIMEOUT,
        )

        if response.status_code >= 400:
            log.warning(
                "SportyBet HTTP %s; endpoint or access may need updating.",
                response.status_code,
            )
            return []

        payload = response.json()

        if not isinstance(payload, (dict, list)):
            log.warning("Unexpected SportyBet response format.")
            return []

        parsed_events = []

        for item in walk_dicts(payload):
            home = (
                item.get("homeTeamName")
                or item.get("homeTeam")
                or item.get("home")
            )
            away = (
                item.get("awayTeamName")
                or item.get("awayTeam")
                or item.get("away")
            )

            if isinstance(home, dict):
                home = home.get("name") or home.get("teamName")

            if isinstance(away, dict):
                away = away.get("name") or away.get("teamName")

            if not isinstance(home, str) or not isinstance(away, str):
                continue

            if not normalize_team(home) or not normalize_team(away):
                continue

            kickoff = (
                item.get("estimateStartTime")
                or item.get("startTime")
                or item.get("eventTime")
                or item.get("kickoff")
            )

            markets = item.get("markets") or item.get("marketList") or []

            if isinstance(markets, dict):
                markets = list(markets.values())

            prices = {}

            for market in markets if isinstance(markets, list) else []:
                if not isinstance(market, dict):
                    continue

                market_name = str(
                    market.get("marketName")
                    or market.get("name")
                    or market.get("marketType")
                    or ""
                ).lower()

                if not any(
                    label in market_name
                    for label in ("1x2", "match result", "full time result")
                ):
                    continue

                selections = (
                    market.get("outcomes")
                    or market.get("selections")
                    or []
                )

                for selection in (
                    selections if isinstance(selections, list) else []
                ):
                    if not isinstance(selection, dict):
                        continue

                    label = str(
                        selection.get("outcomeName")
                        or selection.get("name")
                        or selection.get("desc")
                        or ""
                    ).strip().lower()

                    raw_price = (
                        selection.get("odds")
                        or selection.get("oddsValue")
                        or selection.get("price")
                    )

                    try:
                        price = float(raw_price)
                    except (TypeError, ValueError):
                        continue

                    if price <= 1:
                        continue

                    if label in {"1", "home", "home win"}:
                        prices["home"] = (price, "SportyBet")
                    elif label in {"x", "draw", "tie"}:
                        prices["draw"] = (price, "SportyBet")
                    elif label in {"2", "away", "away win"}:
                        prices["away"] = (price, "SportyBet")

            if prices:
                parsed_events.append({
                    "home_team": home,
                    "away_team": away,
                    "commence_time": kickoff,
                    "prices": prices,
                })

        log.info(
            "SportyBet candidate events parsed: %d (experimental parser)",
            len(parsed_events),
        )

        if not parsed_events:
            log.warning(
                "No SportyBet 1X2 events recognized. Verify the live schema."
            )

        return parsed_events

    except (httpx.HTTPError, ValueError) as exc:
        log.warning("SportyBet request failed: %s", exc)
        return []


# ============================================================
# ARBITRAGE CALCULATIONS
# ============================================================

def calculate_arbitrage(prices):
    if not all(outcome in prices for outcome in OUTCOMES):
        return None

    try:
        inverse_sum = sum(1.0 / prices[outcome][0] for outcome in OUTCOMES)
    except (ZeroDivisionError, TypeError):
        return None

    if not 0 < inverse_sum < 1:
        return None

    roi_pct = (1.0 / inverse_sum - 1.0) * 100.0

    if not MIN_ARBITRAGE_ROI <= roi_pct <= MAX_ARBITRAGE_ROI:
        return None

    stakes = {
        outcome: (
            BANKROLLPOOL
            * (1.0 / prices[outcome][0])
            / inverse_sum
        )
        for outcome in OUTCOMES
    }

    returns = {
        outcome: stakes[outcome] * prices[outcome][0]
        for outcome in OUTCOMES
    }

    equalized_return = min(returns.values())
    theoretical_profit = equalized_return - BANKROLLPOOL

    return {
        "roi_pct": roi_pct,
        "stakes": stakes,
        "return": equalized_return,
        "profit": theoretical_profit,
        "inverse_sum": inverse_sum,
    }


# ============================================================
# TELEGRAM ALERTS
# ============================================================

def alert_signature(event, prices):
    parts = [
        normalize_team(event.get("home_team")),
        normalize_team(event.get("away_team")),
        str(event.get("commence_time") or ""),
    ]

    for outcome in OUTCOMES:
        odd, source = prices[outcome]
        parts.append(f"{outcome}:{odd:.3f}:{source}")

    return hashlib.sha256("|".join(parts).encode()).hexdigest()


def remember_alert(signature):
    SENT_ALERTS.add(signature)
    ALERT_ORDER.append(signature)

    while len(ALERT_ORDER) > MAX_REMEMBERED_ALERTS:
        oldest = ALERT_ORDER.popleft()
        SENT_ALERTS.discard(oldest)


def send_telegram(client, message):
    if not TELEGRAMBOTTOKEN or not TARGETCHATID:
        log.error(
            "Telegram credentials missing. Configure TELEGRAMBOTTOKEN "
            "and TARGETCHATID in Render."
        )
        return False

    url = f"https://api.telegram.org/bot{TELEGRAMBOTTOKEN}/sendMessage"

    try:
        response = client.post(
            url,
            json={
                "chat_id": TARGETCHATID,
                "text": message,
                "disable_web_page_preview": True,
            },
            timeout=15,
        )

        if response.status_code >= 400:
            log.error(
                "Telegram HTTP %s: %s",
                response.status_code,
                response.text[:250],
            )
            return False

        payload = response.json()

        if not payload.get("ok"):
            log.error("Telegram did not confirm message delivery.")
            return False

        return True

    except (httpx.HTTPError, ValueError) as exc:
        log.error("Telegram send failed: %s", exc)
        return False


def format_alert(event, prices, result):
    kickoff = parse_datetime(event.get("commence_time"))

    kickoff_text = (
        kickoff.strftime("%Y-%m-%d %H:%M UTC")
        if kickoff
        else str(event.get("commence_time") or "Unknown")
    )

    lines = [
        "⚽ FOOTBALL-TOOL — THEORETICAL ARBITRAGE",
        f"{event['home_team']} vs {event['away_team']}",
        f"Kickoff: {kickoff_text}",
        f"Theoretical ROI before costs: {result['roi_pct']:.2f}%",
        f"Illustrative total stake: {BANKROLLPOOL:,.2f}",
        "",
        "Outcome | Odds | Source | Suggested stake",
    ]

    for outcome, label in (
        ("home", "Home"),
        ("draw", "Draw"),
        ("away", "Away"),
    ):
        odd, source = prices[outcome]
        stake = result["stakes"][outcome]
        lines.append(
            f"{label}: {odd:.3f} | {source} | {stake:,.2f}"
        )

    lines.extend([
        "",
        f"Equalized return before costs: {result['return']:,.2f}",
        f"Theoretical profit before costs: {result['profit']:,.2f}",
        "",
        "READ-ONLY ALERT — no bet was placed.",
        "Prices may change or be stale. Limits, settlement rules, fees, "
        "currency conversion and rejected bets can eliminate the margin. "
        "Verify all three prices and event identity independently.",
    ])

    return "\n".join(lines)


# ============================================================
# SCAN
# ============================================================

def scan(client):
    odds_events = fetch_odds_events(client)
    sporty_events = fetch_sportybet_events(client)

    log.info("The Odds API events: %d", len(odds_events))
    log.info("SportyBet events parsed: %d", len(sporty_events))

    combined = {}

    for event in odds_events:
        home = str(event.get("home_team", "")).strip()
        away = str(event.get("away_team", "")).strip()

        if not home or not away:
            continue

        kickoff = parse_datetime(event.get("commence_time"))

        if kickoff and not within_time_window(kickoff):
            continue

        prices = extract_odds_api_prices(event)

        if prices:
            combined[event_key(home, away)] = {
                "home_team": home,
                "away_team": away,
                "commence_time": event.get("commence_time"),
                "prices": prices,
            }

    # Merge only matching home/away names. Skip obvious kickoff mismatches.
    for event in sporty_events:
        key = event_key(event["home_team"], event["away_team"])
        target = combined.get(key)

        if target is None:
            continue

        sporty_time = parse_datetime(event.get("commence_time"))
        odds_time = parse_datetime(target.get("commence_time"))

        if (
            sporty_time
            and odds_time
            and abs((sporty_time - odds_time).total_seconds()) > 10800
        ):
            log.warning(
                "Skipping likely fixture mismatch: %s vs %s",
                event["home_team"],
                event["away_team"],
            )
            continue

        for outcome, new_price in event["prices"].items():
            old_price = target["prices"].get(outcome)

            if old_price is None or new_price[0] > old_price[0]:
                target["prices"][outcome] = new_price

    qualifying = 0

    for event in combined.values():
        prices = event["prices"]
        result = calculate_arbitrage(prices)

        if result is None:
            continue

        qualifying += 1
        signature = alert_signature(event, prices)

        if signature in SENT_ALERTS:
            continue

        message = format_alert(event, prices, result)

        if send_telegram(client, message):
            remember_alert(signature)
            log.info(
                "Alert sent: %s vs %s; ROI %.2f%%",
                event["home_team"],
                event["away_team"],
                result["roi_pct"],
            )

    log.info("Qualifying theoretical opportunities: %d", qualifying)
    log.info("Read-only scanner. No bets placed.")


# ============================================================
# CONTINUOUS WORKER LOOP
# ============================================================

def run_forever():
    log.info(
        "Football-tool started: sport=%s, regions=%s, interval=%ss, "
        "SportyBet=%s",
        SPORT_KEY,
        ODDS_REGIONS,
        SCAN_INTERVAL_SECONDS,
        SPORTYBET_ENABLED,
    )

    with httpx.Client(
        headers={"User-Agent": "Football-tool/1.0"},
        follow_redirects=True,
    ) as client:
        while True:
            started = time.monotonic()

            try:
                scan(client)
            except Exception:
                log.exception("Unexpected scan failure; retrying next cycle.")

            elapsed = time.monotonic() - started
            time.sleep(max(1, SCAN_INTERVAL_SECONDS - elapsed))


if __name__ == "__main__":
    run_forever()
