
#!/usr/bin/env python3
"""Football-tool: read-only 1X2 arbitrage scanner."""

import logging
import os
import re
import sys
import time
from datetime import datetime, timezone
from difflib import SequenceMatcher

import httpx

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s %(levelname)s %(message)s",
)
log = logging.getLogger("football-tool")

ODDS_API_KEY = os.getenv("ODDS_API_KEY", "").strip()
TELEGRAM_TOKEN = os.getenv("TELEGRAMBOTTOKEN", "").strip()
CHAT_ID = os.getenv("TARGETCHATID", "").strip()

SPORT_KEY = os.getenv("ODDS_SPORT_KEY", "soccer_epl")
REGIONS = os.getenv("ODDS_REGIONS", "uk,eu")
BANKROLL = float(os.getenv("BANKROLLPOOL", "20000"))
MIN_ROI = float(os.getenv("MIN_ARBITRAGE_ROI", "0.5"))
MAX_ROI = float(os.getenv("MAX_ARBITRAGE_ROI", "30"))
WINDOW_HOURS = float(os.getenv("TIMELINE_HOURS", "48"))

SPORTYBET_ENABLED = os.getenv(
    "SPORTYBET_ENABLED", "true"
).lower() in ("true", "1", "yes")

SPORTYBET_URL = os.getenv(
    "SPORTYBET_ODDS_URL",
    "https://www.sportybet.com/api/ng/factsCenter/pcUpcomingEvents",
)

SPORTYBET_TIMEOUT = float(
    os.getenv("SPORTYBET_TIMEOUT_SECONDS", "12")
)

# Only use documented/authorized API credentials.
# Do not use browser cookies or session tokens.
SPORTYBET_API_KEY = os.getenv("SPORTYBET_API_KEY", "").strip()
SPORTYBET_AUTH_MODE = os.getenv("SPORTYBET_AUTH_MODE", "none").lower()

DB_PATH = os.getenv("ARBITRAGE_DB_PATH", "arbitrage_state.sqlite3")
OUTCOMES = ("home", "draw", "away")


def normalize(value):
    return re.sub(r"[^a-z0-9]", "", str(value or "").lower())


def similarity(a, b):
    return SequenceMatcher(
        None, normalize(a), normalize(b)
    ).ratio()


def positive_float(value):
    try:
        number = float(value)
        return number if number > 0 else None
    except (TypeError, ValueError):
        return None


def parse_time(value):
    if not value:
        return None
    try:
        if isinstance(value, (int, float)):
            seconds = value / 1000 if value > 10_000_000_000 else value
            return datetime.fromtimestamp(seconds, timezone.utc)

        value = str(value).strip()
        if value.endswith("Z"):
            value = value[:-1] + "+00:00"

        result = datetime.fromisoformat(value)
        if result.tzinfo is None:
            result = result.replace(tzinfo=timezone.utc)
        return result.astimezone(timezone.utc)
    except (ValueError, OverflowError, OSError):
        return None


def telegram(message):
    if not TELEGRAM_TOKEN or not CHAT_ID:
        log.warning("Telegram secrets are not configured.")
        return False

    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"

    try:
        response = httpx.post(
            url,
            json={
                "chat_id": CHAT_ID,
                "text": message,
                "disable_web_page_preview": True,
            },
            timeout=15,
        )
        if response.status_code != 200:
            log.warning("Telegram delivery failed: HTTP %s", response.status_code)
            return False

        return response.json().get("ok", False)

    except (httpx.HTTPError, ValueError) as exc:
        log.warning("Telegram delivery error: %s", type(exc).__name__)
        return False


def request_sportybet_json():
    """Return parsed JSON or None; a failed feed never crashes the scan."""

    if not SPORTYBET_ENABLED:
        log.info("SportyBet source is disabled.")
        return None

    headers = {
        "Accept": "application/json, text/plain, */*",
        "User-Agent": "Football-tool-readonly-scanner/1.0",
    }

    if SPORTYBET_API_KEY and SPORTYBET_AUTH_MODE == "bearer":
        headers["Authorization"] = f"Bearer {SPORTYBET_API_KEY}"
    elif SPORTYBET_API_KEY and SPORTYBET_AUTH_MODE == "x-api-key":
        headers["x-api-key"] = SPORTYBET_API_KEY

    params = {
        "sportId": os.getenv("SPORTYBET_SPORT_ID", "sr:sport:1"),
        "marketId": os.getenv("SPORTYBET_MARKET_ID", "1"),
        "pageSize": os.getenv("SPORTYBET_PAGE_SIZE", "100"),
        "pageNum": os.getenv("SPORTYBET_PAGE_NUM", "1"),
        "todayGames": os.getenv("SPORTYBET_TODAY_GAMES", "false"),
        "timeline": os.getenv("SPORTYBET_TIMELINE", "48"),
        "_": str(int(time.time() * 1000)),
    }

    try:
        response = httpx.get(
            SPORTYBET_URL,
            params=params,
            headers=headers,
            timeout=SPORTYBET_TIMEOUT,
            follow_redirects=True,
        )
    except httpx.HTTPError as exc:
        log.warning(
            "SportyBet request failed (%s); continuing without SportyBet.",
            type(exc).__name__,
        )
        return None

    content_type = response.headers.get(
        "content-type", "unknown"
    ).split(";")[0]

    if response.status_code != 200:
        log.warning(
            "SportyBet unavailable: HTTP %s, content-type=%s. "
            "Continuing without this source.",
            response.status_code,
            content_type,
        )
        return None

    try:
        data = response.json()
    except ValueError:
        log.warning(
            "SportyBet returned non-JSON: HTTP %s, content-type=%s, "
            "body_length=%s. Body omitted; continuing without SportyBet.",
            response.status_code,
            content_type,
            len(response.content),
        )
        return None

    if not isinstance(data, (dict, list)):
        log.warning("SportyBet returned an unsupported JSON structure.")
        return None

    return data


def walk_dicts(value):
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from walk_dicts(child)
    elif isinstance(value, list):
        for child in value:
            yield from walk_dicts(child)


def first_value(item, keys):
    for key in keys:
        value = item.get(key)
        if value is not None:
            return value
    return None


def get_teams(item):
    home = first_value(item, [
        "homeTeamName", "homeName", "home_team",
    ])
    away = first_value(item, [
        "awayTeamName", "awayName", "away_team",
    ])

    for key in ("homeTeam", "home"):
        if isinstance(item.get(key), dict):
            home = home or item[key].get("name") or item[key].get("teamName")

    for key in ("awayTeam", "away"):
        if isinstance(item.get(key), dict):
            away = away or item[key].get("name") or item[key].get("teamName")

    for key in ("competitors", "participants", "teams"):
        teams = item.get(key)
        if isinstance(teams, list) and len(teams) >= 2:
            def team_name(team):
                if isinstance(team, dict):
                    return team.get("name") or team.get("teamName")
                return str(team)

            home = home or team_name(teams[0])
            away = away or team_name(teams[1])

    return (
        str(home) if home else None,
        str(away) if away else None,
    )


def extract_sportybet_odds(event):
    """Only accept explicitly labelled Home/Draw/Away decimal odds."""

    labels = {
        "home": {"home", "1", "home win"},
        "draw": {"draw", "x", "tie"},
        "away": {"away", "2", "away win"},
    }

    found = {}

    for item in walk_dicts(event):
        label = first_value(
            item,
            ["name", "label", "outcome", "selectionName"],
        )
        odds_value = first_value(
            item,
            ["odds", "odd", "price", "decimalOdds", "currentOdds"],
        )

        if isinstance(odds_value, dict):
            odds_value = odds_value.get("value") or odds_value.get("decimal")

        odds = positive_float(odds_value)
        if odds is None or not isinstance(label, str):
            continue

        label = re.sub(r"\s+", " ", label.strip().lower())

        for outcome, variants in labels.items():
            if label in variants:
                found.setdefault(outcome, odds)

    return found if all(o in found for o in OUTCOMES) else {}


def fetch_sportybet_events():
    data = request_sportybet_json()
    if data is None:
        return []

    events = []
    seen = set()

    for item in walk_dicts(data):
        home, away = get_teams(item)
        if not home or not away:
            continue

        odds = extract_sportybet_odds(item)
        if len(odds) != 3:
            continue

        key = (normalize(home), normalize(away))
        if key in seen:
            continue
        seen.add(key)

        kickoff = first_value(
            item,
            ["estimateStartTime", "startTime", "kickoff", "eventTime"],
        )

        events.append({
            "home": home,
            "away": away,
            "odds": odds,
            "kickoff": parse_time(kickoff),
        })

    if events:
        log.info("Parsed %d SportyBet events.", len(events))
    else:
        log.warning(
            "SportyBet response contained no events matching the supported "
            "team and 1X2 odds schema."
        )

    return events


def fetch_odds_api_events():
    if not ODDS_API_KEY:
        raise RuntimeError("Missing ODDS_API_KEY secret.")

    url = (
        "https://api.the-odds-api.com/v4/sports/"
        f"{SPORT_KEY}/odds/"
    )

    try:
        response = httpx.get(
            url,
            params={
                "apiKey": ODDS_API_KEY,
                "regions": REGIONS,
                "markets": "h2h",
                "oddsFormat": "decimal",
                "dateFormat": "iso",
            },
            timeout=20,
        )
    except httpx.HTTPError as exc:
        raise RuntimeError(
            f"The Odds API request failed ({type(exc).__name__})."
        ) from None

    if response.status_code != 200:
        messages = {
            401: "The Odds API returned HTTP 401. Check the API key.",
            403: "The Odds API returned HTTP 403. Check plan and access.",
            429: "The Odds API returned HTTP 429. Check quota or rate limits.",
        }
        raise RuntimeError(
            messages.get(
                response.status_code,
                f"The Odds API returned HTTP {response.status_code}.",
            )
        )

    try:
        payload = response.json()
    except ValueError:
        raise RuntimeError("The Odds API returned invalid JSON.") from None

    if not isinstance(payload, list):
        raise RuntimeError("The Odds API response was not a list.")

    events = []

    for event in payload:
        home = event.get("home_team")
        away = event.get("away_team")
        if not home or not away:
            continue

        best = {o: None for o in OUTCOMES}

        for bookmaker in event.get("bookmakers", []):
            book_name = bookmaker.get("title", "Bookmaker")

            for market in bookmaker.get("markets", []):
                if market.get("key") != "h2h":
                    continue

                for outcome in market.get("outcomes", []):
                    name = outcome.get("name", "")
                    price = positive_float(outcome.get("price"))
                    if price is None:
                        continue

                    if normalize(name) == normalize(home):
                        key = "home"
                    elif normalize(name) == normalize(away):
                        key = "away"
                    elif name.strip().lower() in ("draw", "tie"):
                        key = "draw"
                    else:
                        continue

                    if best[key] is None or price > best[key]["odds"]:
                        best[key] = {
                            "odds": price,
                            "source": book_name,
                        }

        if all(best[o] for o in OUTCOMES):
            events.append({
                "home": home,
                "away": away,
                "kickoff": parse_time(event.get("commence_time")),
                "best": best,
            })

    log.info(
        "The Odds API returned %d events with complete 1X2 odds.",
        len(events),
    )
    return events


def find_sportybet_match(event, sporty_events):
    for candidate in sporty_events:
        direct = (
            similarity(event["home"], candidate["home"])
            + similarity(event["away"], candidate["away"])
        ) / 2

        if direct < 0.82:
            continue

        t1 = event.get("kickoff")
        t2 = candidate.get("kickoff")

        if t1 and t2 and abs((t1 - t2).total_seconds()) > 10800:
            continue

        return candidate

    return None


def calculate_arbitrage(event, sporty_match):
    choices = dict(event["best"])

    if sporty_match:
        for outcome in OUTCOMES:
            price = sporty_match["odds"].get(outcome)
            if price and (
                outcome not in choices
                or price > choices[outcome]["odds"]
            ):
                choices[outcome] = {
                    "odds": price,
                    "source": "SportyBet",
                }

    if not all(choices.get(o) for o in OUTCOMES):
        return None

    inverse_sum = sum(1 / choices[o]["odds"] for o in OUTCOMES)

    if inverse_sum >= 1:
        return None

    roi = (1 / inverse_sum - 1) * 100

    if not MIN_ROI <= roi <= MAX_ROI:
        return None

    stakes = {
        o: BANKROLL * (1 / choices[o]["odds"]) / inverse_sum
        for o in OUTCOMES
    }

    payouts = {
        o: stakes[o] * choices[o]["odds"]
        for o in OUTCOMES
    }

    guaranteed_payout = min(payouts.values())

    return {
        "choices": choices,
        "roi": roi,
        "stakes": stakes,
        "payout": guaranteed_payout,
        "profit": guaranteed_payout - BANKROLL,
    }


def format_alert(event, result):
    kickoff = event.get("kickoff")
    kickoff_text = (
        kickoff.strftime("%Y-%m-%d %H:%M UTC")
        if kickoff else "Kickoff unverified"
    )

    labels = {"home": "HOME", "draw": "DRAW", "away": "AWAY"}

    lines = [
        "⚽ FOOTBALL-TOOL: THEORETICAL 1X2 ARBITRAGE",
        f"{event['home']} vs {event['away']}",
        f"Kickoff: {kickoff_text}",
        f"Theoretical ROI: {result['roi']:.2f}%",
        f"Illustrative total stake: ₦{BANKROLL:,.2f}",
    ]

    for outcome in OUTCOMES:
        choice = result["choices"][outcome]
        lines.append(
            f"{labels[outcome]}: {choice['odds']:.3f} "
            f"({choice['source']}); stake "
            f"₦{result['stakes'][outcome]:,.2f}"
        )

    lines.extend([
        f"Illustrative payout: ₦{result['payout']:,.2f}",
        f"Illustrative profit: ₦{result['profit']:,.2f}",
        "",
        "Theoretical only. Verify current odds, identical markets, "
        "settlement rules, limits and fees manually. No bets are placed.",
    ])

    return "\n".join(lines)


def main():
    if not ODDS_API_KEY:
        raise RuntimeError("Missing ODDS_API_KEY secret.")

    if BANKROLL <= 0 or MIN_ROI < 0 or MAX_ROI < MIN_ROI:
        raise RuntimeError("Invalid bankroll or ROI configuration.")

    events = fetch_odds_api_events()

    # Crucial fix: a SportyBet failure returns an empty list, not a crash.
    sporty_events = fetch_sportybet_events()

    now = datetime.now(timezone.utc)
    found = 0

    for event in events:
        kickoff = event.get("kickoff")

        if kickoff:
            hours = (kickoff - now).total_seconds() / 3600
            if hours < -1 or hours > WINDOW_HOURS:
                continue

        sporty_match = find_sportybet_match(event, sporty_events)
        result = calculate_arbitrage(event, sporty_match)

        if result:
            found += 1
            telegram(format_alert(event, result))

    summary = (
        "Football-tool scan completed.\n"
        f"The Odds API events: {len(events)}\n"
        f"SportyBet events parsed: {len(sporty_events)}\n"
        f"Qualifying theoretical opportunities: {found}\n"
        "Read-only scanner. No bets placed."
    )

    log.info(summary.replace("\n", " | "))
    telegram(summary)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as exc:
        # Do not log environment variables, request URLs or response bodies.
        message = str(exc)
        log.error("Scan failed: %s", message)
        telegram(f"Football-tool scan error: {message}")
        sys.exit(1)
