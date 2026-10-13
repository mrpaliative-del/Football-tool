
import os
import json
import time
import hashlib
import logging
import sqlite3
from datetime import datetime, timezone

import httpx

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s %(levelname)s %(message)s",
)
log = logging.getLogger("surebet")

SPORTYBET_URL = os.getenv(
    "SPORTYBET_API_URL",
    "https://www.sportybet.com/api/ng/factsCenter/pcUpcomingEvents",
)
FOREIGN_ODDS_JSON_URL = os.getenv("FOREIGN_ODDS_JSON_URL", "").strip()

TELEGRAM_TOKEN = os.getenv("TELEGRAMBOTTOKEN", "").strip()
TELEGRAM_CHAT_ID = os.getenv("TARGETCHATID", "").strip()

BANKROLL = max(0.0, float(os.getenv("BANKROLLPOOL", "20000")))
MIN_ROI = max(0.0, float(os.getenv("MIN_ARBITRAGE_ROI", "0.5")))
MAX_ROI = max(MIN_ROI, float(os.getenv("MAX_ARBITRAGE_ROI", "30")))
DB_PATH = os.getenv("ARBITRAGE_DB_PATH", "arbitrage_state.sqlite3")
TIMELINE_HOURS = max(1, int(os.getenv("TIMELINE_HOURS", "48")))

OUTCOMES = ("home", "draw", "away")
LABELS = {"home": "Home", "draw": "Draw", "away": "Away"}


def parse_time(value):
    if value is None:
        return None
    try:
        if isinstance(value, (int, float)):
            return value / 1000 if value > 10_000_000_000 else value

        parsed = datetime.fromisoformat(
            str(value).replace("Z", "+00:00")
        )
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.timestamp()
    except (ValueError, TypeError, OverflowError):
        return None


def normalize_name(value):
    import re
    value = str(value or "").lower()
    value = re.sub(r"\b(fc|afc|sc|cf|club|the)\b", " ", value)
    return re.sub(r"[^a-z0-9]+", "", value)


def init_db():
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS sent_signals (
                signal_key TEXT PRIMARY KEY,
                sent_at TEXT NOT NULL
            )
        """)


def sportybet_odds(event):
    """Extract complete 1X2 odds when the provider payload matches this schema."""
    for market in event.get("markets", []):
        market_id = str(market.get("id", ""))
        market_desc = str(market.get("desc", "")).lower().strip()

        if market_id != "1" and market_desc not in {
            "1x2", "match result", "full time result"
        }:
            continue

        result = {}

        for outcome in market.get("outcomes", []):
            if outcome.get("isActive") is False:
                continue

            oid = str(outcome.get("id", "")).strip()
            desc = str(outcome.get("desc", "")).lower().strip()

            if oid == "1" or desc in {"1", "home", "home win"}:
                key = "home"
            elif oid == "2" or desc in {"x", "draw", "tie"}:
                key = "draw"
            elif oid == "3" or desc in {"2", "away", "away win"}:
                key = "away"
            else:
                continue

            try:
                odd = float(outcome.get("odds"))
            except (ValueError, TypeError):
                continue

            if odd > 1:
                result[key] = odd

        if all(key in result for key in OUTCOMES):
            return result

    return None


def fetch_sportybet(client):
    params = {
        "sportId": "sr:sport:1",
        "marketId": "1",
        "pageSize": "100",
        "pageNum": "1",
        "todayGames": "false",
        "timeline": str(TIMELINE_HOURS),
        "_t": str(int(time.time() * 1000)),
    }

    response = client.get(
        SPORTYBET_URL,
        params=params,
        headers={
            "Accept": "application/json",
            "Current-Country": "NG",
            "User-Agent": "SportsValueEngine/1.0",
        },
    )
    response.raise_for_status()
    payload = response.json()

    data = payload.get("data") or {}
    results = []

    for tournament in data.get("tournaments", []):
        league = tournament.get("name", "")

        for event in tournament.get("events", []):
            home = event.get("homeTeamName", "")
            away = event.get("awayTeamName", "")
            odds = sportybet_odds(event)

            if not home or not away or not odds:
                continue

            results.append({
                "source": "SportyBet Nigeria",
                "home": home,
                "away": away,
                "league": league,
                "start_time": parse_time(
                    event.get("estimateStartTime")
                    or event.get("startTime")
                ),
                "market": "1X2",
                "odds": odds,
            })

    log.info("SportyBet events normalized: %d", len(results))
    return results


def fetch_foreign(client):
    if not FOREIGN_ODDS_JSON_URL:
        log.warning(
            "FOREIGN_ODDS_JSON_URL is not configured. "
            "Cross-bookmaker scanning is disabled."
        )
        return []

    response = client.get(FOREIGN_ODDS_JSON_URL)
    response.raise_for_status()
    payload = response.json()

    if isinstance(payload, dict):
        payload = payload.get("events", payload.get("data", []))

    if not isinstance(payload, list):
        raise ValueError("Foreign feed must return a JSON event list.")

    results = []

    for item in payload:
        odds = item.get("odds") or {}
        clean_odds = {}

        for key in OUTCOMES:
            try:
                odd = float(odds.get(key))
            except (ValueError, TypeError):
                continue

            if odd > 1:
                clean_odds[key] = odd

        home = item.get("home") or item.get("home_team")
        away = item.get("away") or item.get("away_team")

        if not home or not away:
            continue
        if not all(key in clean_odds for key in OUTCOMES):
            continue

        results.append({
            "source": item.get("source", "Foreign bookmaker"),
            "home": str(home),
            "away": str(away),
            "league": item.get("league", ""),
            "start_time": parse_time(
                item.get("start_time") or item.get("commence_time")
            ),
            "market": str(item.get("market", "1X2")).upper(),
            "odds": clean_odds,
        })

    log.info("Foreign bookmaker events normalized: %d", len(results))
    return results


def events_match(local, foreign):
    if normalize_name(local["home"]) != normalize_name(foreign["home"]):
        return False

    if normalize_name(local["away"]) != normalize_name(foreign["away"]):
        return False

    if local["market"] != "1X2" or foreign["market"] != "1X2":
        return False

    a = local.get("start_time")
    b = foreign.get("start_time")

    if a is not None and b is not None and abs(a - b) > 3 * 3600:
        return False

    return True


def calculate_surebet(local, foreign, bankroll):
    best = {}

    for outcome in OUTCOMES:
        available = []

        for event in (local, foreign):
            try:
                odd = float(event["odds"][outcome])
            except (KeyError, ValueError, TypeError):
                continue

            if odd > 1:
                available.append((odd, event["source"]))

        if not available:
            return None

        best[outcome] = max(available, key=lambda item: item[0])

    implied_sum = sum(1 / best[key][0] for key in OUTCOMES)

    if implied_sum >= 1:
        return None

    roi = (1 / implied_sum - 1) * 100

    stakes = {
        key: bankroll * (1 / best[key][0]) / implied_sum
        for key in OUTCOMES
    }

    returns = {
        key: stakes[key] * best[key][0]
        for key in OUTCOMES
    }

    min_return = min(returns.values())

    return {
        "best": best,
        "roi": roi,
        "stakes": stakes,
        "returns": returns,
        "implied_sum": implied_sum,
        "min_return": min_return,
        "profit": min_return - bankroll,
    }


def signal_key(local, calc):
    value = {
        "home": local["home"],
        "away": local["away"],
        "odds": {
            key: calc["best"][key][0]
            for key in OUTCOMES
        },
        "sources": {
            key: calc["best"][key][1]
            for key in OUTCOMES
        },
    }

    raw = json.dumps(value, sort_keys=True)
    return hashlib.sha256(raw.encode()).hexdigest()


def send_telegram(client, message):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        raise RuntimeError(
            "Set TELEGRAMBOTTOKEN and TARGETCHATID in GitHub Secrets."
        )

    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"

    response = client.post(url, json={
        "chat_id": TELEGRAM_CHAT_ID,
        "text": message,
        "disable_web_page_preview": True,
    })
    response.raise_for_status()

    data = response.json()
    if not data.get("ok"):
        raise RuntimeError("Telegram rejected the notification.")


def was_sent(key):
    with sqlite3.connect(DB_PATH) as conn:
        return conn.execute(
            "SELECT 1 FROM sent_signals WHERE signal_key=?",
            (key,),
        ).fetchone() is not None


def record_sent(key):
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            "INSERT OR IGNORE INTO sent_signals VALUES (?, ?)",
            (key, datetime.now(timezone.utc).isoformat()),
        )


def format_alert(local, foreign, calc):
    lines = [
        "⚠️ POTENTIAL 1X2 SURE BET — VERIFY FIRST",
        f"{local['home']} vs {local['away']}",
        f"League: {local.get('league') or 'Unknown'}",
        f"Theoretical ROI: {calc['roi']:.2f}%",
        f"Calculation bankroll: {BANKROLL:,.2f}",
        "",
    ]

    for outcome in OUTCOMES:
        odd, source = calc["best"][outcome]
        lines.append(
            f"{LABELS[outcome]}: {odd:.3f} @ {source} | "
            f"Stake: {calc['stakes'][outcome]:,.2f}"
        )

    lines.extend([
        "",
        f"Estimated return: {calc['min_return']:,.2f}",
        f"Estimated profit before fees: {calc['profit']:,.2f}",
        f"Sources: {local['source']} + {foreign['source']}",
        "Odds can change; verify all prices, rules and limits.",
        "No bets are placed automatically.",
    ])

    return "\n".join(lines)


def run_once():
    init_db()

    with httpx.Client(
        timeout=20,
        follow_redirects=True,
        headers={"User-Agent": "SportsValueEngine/1.0"},
    ) as client:
        local_events = fetch_sportybet(client)
        foreign_events = fetch_foreign(client)

        matches = 0
        alerts = 0

        for local in local_events:
            for foreign in foreign_events:
                if not events_match(local, foreign):
                    continue

                matches += 1
                calc = calculate_surebet(local, foreign, BANKROLL)

                if not calc:
                    continue

                if not MIN_ROI <= calc["roi"] <= MAX_ROI:
                    continue

                key = signal_key(local, calc)

                if was_sent(key):
                    continue

                send_telegram(client, format_alert(local, foreign, calc))
                record_sent(key)
                alerts += 1

        log.info(
            "Scan finished. Local=%d Foreign=%d Matched=%d Alerts=%d",
            len(local_events), len(foreign_events), matches, alerts,
        )


if __name__ == "__main__":
    try:
        run_once()
    except Exception:
        log.exception("Scanner failed.")
        raise
