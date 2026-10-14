
"""
Football-tool: read-only 1X2 arbitrage scanner
Python 3.11+

Required environment variables:
  ODDS_API_KEY
  TELEGRAMBOTTOKEN
  TARGETCHATID

Optional environment variables:
  SPORTYBET_ENABLED=true
  SPORTYBET_URL=https://www.sportybet.com/api/ng/factsCenter/pcUpcomingEvents
  SPORTYBET_AUTH_MODE=none
  SPORTYBET_TIMEOUT=15
  SPORTYBET_PAGE_SIZE=100
  SPORTYBET_TIMELINE=48
  SPORT_KEY=soccer_epl
  ODDS_REGIONS=uk,eu
  BANKROLLPOOL=20000
  MIN_ARBITRAGE_ROI=0.5
  MAX_ARBITRAGE_ROI=30
  TIMELINE_HOURS=48
  LOG_LEVEL=INFO

No bets are placed.
"""

import logging
import os
import re
import unicodedata
from datetime import datetime, timezone
from difflib import SequenceMatcher

import httpx


# ---------------- CONFIGURATION ----------------

ODDS_API_KEY = os.getenv("ODDS_API_KEY", "").strip()
TELEGRAM_TOKEN = os.getenv("TELEGRAMBOTTOKEN", "").strip()
TELEGRAM_CHAT_ID = os.getenv("TARGETCHATID", "").strip()

SPORTYBET_ENABLED = os.getenv(
    "SPORTYBET_ENABLED", "true"
).lower() in {"true", "1", "yes", "on"}

SPORTYBET_URL = os.getenv(
    "SPORTYBET_URL",
    "https://www.sportybet.com/api/ng/factsCenter/pcUpcomingEvents",
).strip()

SPORTYBET_AUTH_MODE = os.getenv(
    "SPORTYBET_AUTH_MODE", "none"
).lower().strip()

SPORTYBET_API_KEY = os.getenv("SPORTYBET_API_KEY", "").strip()
SPORTYBET_TIMEOUT = float(os.getenv("SPORTYBET_TIMEOUT", "15"))
SPORTYBET_PAGE_SIZE = min(
    100, max(1, int(os.getenv("SPORTYBET_PAGE_SIZE", "100")))
)
SPORTYBET_TIMELINE = min(
    720, max(1, int(os.getenv("SPORTYBET_TIMELINE", "48")))
)

SPORT_KEY = os.getenv("SPORT_KEY", "soccer_epl").strip()
ODDS_REGIONS = os.getenv("ODDS_REGIONS", "uk,eu").strip()

BANKROLL = float(os.getenv("BANKROLLPOOL", "20000"))
MIN_ROI = float(os.getenv("MIN_ARBITRAGE_ROI", "0.5"))
MAX_ROI = float(os.getenv("MAX_ARBITRAGE_ROI", "30"))
TIMELINE_HOURS = float(os.getenv("TIMELINE_HOURS", "48"))

ODDS_API_URL = (
    f"https://api.the-odds-api.com/v4/sports/{SPORT_KEY}/odds/"
)

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s %(levelname)s %(message)s",
)
log = logging.getLogger("football_tool")

HTTP_TIMEOUT = httpx.Timeout(20.0, connect=10.0)


# ---------------- GENERAL HELPERS ----------------

def normalize_team(name):
    if not isinstance(name, str):
        return ""

    name = unicodedata.normalize("NFKD", name)
    name = "".join(
        c for c in name if not unicodedata.combining(c)
    )
    name = name.lower().replace("&", " and ")
    name = re.sub(
        r"\b(fc|afc|cf|sc|club|football club|the)\b",
        " ",
        name,
    )
    return re.sub(r"[^a-z0-9]+", "", name)


def team_similarity(first, second):
    first = normalize_team(first)
    second = normalize_team(second)

    if not first or not second:
        return 0.0
    if first == second:
        return 1.0

    return SequenceMatcher(None, first, second).ratio()


def safe_float(value):
    try:
        number = float(value)
        if number > 1 and number != float("inf"):
            return number
    except (TypeError, ValueError, OverflowError):
        pass
    return None


def parse_datetime(value):
    if value is None:
        return None

    try:
        # SportyBet estimateStartTime is commonly Unix milliseconds.
        if isinstance(value, (int, float)):
            seconds = value / 1000 if value > 10_000_000_000 else value
            return datetime.fromtimestamp(seconds, tz=timezone.utc)

        text = str(value).strip()
        if text.isdigit():
            number = int(text)
            seconds = (
                number / 1000
                if number > 10_000_000_000
                else number
            )
            return datetime.fromtimestamp(seconds, tz=timezone.utc)

        text = text.replace("Z", "+00:00")
        parsed = datetime.fromisoformat(text)

        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)

        return parsed.astimezone(timezone.utc)

    except (ValueError, TypeError, OverflowError, OSError):
        return None


def event_in_window(event):
    kickoff = parse_datetime(event.get("commence_time"))

    # Keep unknown times rather than silently discarding them.
    if kickoff is None:
        return True

    hours = (
        kickoff - datetime.now(timezone.utc)
    ).total_seconds() / 3600

    return 0 <= hours <= TIMELINE_HOURS


# ---------------- SAFE HTTP REQUESTS ----------------

def get_json(url, params=None, headers=None, source="API", timeout=None):
    """
    Fetch JSON without logging credentials, full URLs, or response bodies.
    """
    try:
        with httpx.Client(
            timeout=timeout or HTTP_TIMEOUT,
            follow_redirects=True,
        ) as client:
            response = client.get(
                url,
                params=params,
                headers=headers or {},
            )

        content_type = response.headers.get("content-type", "")

        log.info(
            "%s HTTP status=%s content_type=%s response_bytes=%s",
            source,
            response.status_code,
            content_type.split(";")[0],
            len(response.content),
        )

        if not 200 <= response.status_code < 300:
            log.warning("%s returned a non-success HTTP status.", source)
            return None

        try:
            payload = response.json()
        except ValueError:
            log.warning("%s returned non-JSON content.", source)
            return None

        if isinstance(payload, dict):
            log.info(
                "%s top-level keys=%s",
                source,
                list(payload.keys())[:30],
            )
        elif isinstance(payload, list):
            log.info("%s top-level list length=%d", source, len(payload))
        else:
            log.info(
                "%s top-level type=%s",
                source,
                type(payload).__name__,
            )

        return payload

    except httpx.TimeoutException:
        log.warning("%s request timed out.", source)
    except httpx.HTTPError as exc:
        log.warning(
            "%s HTTP failure (%s).",
            source,
            type(exc).__name__,
        )
    except Exception as exc:
        log.error(
            "%s unexpected request failure (%s).",
            source,
            type(exc).__name__,
        )

    return None


def send_telegram(message):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        log.info("Telegram not configured; alert skipped.")
        return False

    url = (
        "https://api.telegram.org/bot"
        + TELEGRAM_TOKEN
        + "/sendMessage"
    )

    try:
        with httpx.Client(timeout=HTTP_TIMEOUT) as client:
            response = client.post(
                url,
                json={
                    "chat_id": TELEGRAM_CHAT_ID,
                    "text": message,
                    "disable_web_page_preview": True,
                },
            )

        if response.status_code != 200:
            log.warning(
                "Telegram send failed: HTTP %s",
                response.status_code,
            )
            return False

        result = response.json()

        if not result.get("ok"):
            log.warning("Telegram did not accept the message.")
            return False

        return True

    except (httpx.HTTPError, ValueError) as exc:
        log.warning(
            "Telegram request failed (%s).",
            type(exc).__name__,
        )
        return False


# ---------------- THE ODDS API ----------------

def fetch_odds_events():
    if not ODDS_API_KEY:
        log.error("ODDS_API_KEY is missing.")
        return []

    payload = get_json(
        ODDS_API_URL,
        params={
            "apiKey": ODDS_API_KEY,
            "regions": ODDS_REGIONS,
            "markets": "h2h",
            "oddsFormat": "decimal",
        },
        source="The Odds API",
    )

    if not isinstance(payload, list):
        log.error("The Odds API response is not an event list.")
        return []

    events = []

    for raw in payload:
        if not isinstance(raw, dict):
            continue

        home = raw.get("home_team")
        away = raw.get("away_team")

        if not home or not away:
            continue

        outcomes = {"home": [], "draw": [], "away": []}

        for bookmaker in raw.get("bookmakers", []) or []:
            if not isinstance(bookmaker, dict):
                continue

            book_name = (
                bookmaker.get("title")
                or bookmaker.get("key")
                or "Unknown bookmaker"
            )

            for market in bookmaker.get("markets", []) or []:
                if market.get("key") != "h2h":
                    continue

                for outcome in market.get("outcomes", []) or []:
                    name = str(outcome.get("name", "")).strip()
                    price = safe_float(outcome.get("price"))

                    if price is None:
                        continue

                    if name.casefold() == str(home).casefold():
                        outcomes["home"].append((price, book_name))
                    elif name.casefold() == str(away).casefold():
                        outcomes["away"].append((price, book_name))
                    elif name.casefold() in {"draw", "tie"}:
                        outcomes["draw"].append((price, book_name))

        events.append({
            "id": raw.get("id"),
            "home_team": home,
            "away_team": away,
            "commence_time": raw.get("commence_time"),
            "outcomes": outcomes,
        })

    log.info("The Odds API events: %d", len(events))
    return events


# ---------------- SPORTYBET FETCH ----------------

def fetch_sportybet_payload():
    if not SPORTYBET_ENABLED:
        log.info("SportyBet integration disabled.")
        return None

    headers = {
        "Accept": "application/json",
        "Current-Country": "NG",
        "User-Agent": "Football-tool-readonly-scanner/1.0",
    }

    # This endpoint is reported as unauthenticated.
    # Do not supply a browser session token.
    if SPORTYBET_AUTH_MODE == "bearer" and SPORTYBET_API_KEY:
        headers["Authorization"] = f"Bearer {SPORTYBET_API_KEY}"
    elif SPORTYBET_AUTH_MODE == "x-api-key" and SPORTYBET_API_KEY:
        headers["x-api-key"] = SPORTYBET_API_KEY
    elif SPORTYBET_AUTH_MODE not in {"none", "bearer", "x-api-key"}:
        log.error("Unsupported SPORTYBET_AUTH_MODE.")
        return None

    params = {
        "sportId": "sr:sport:1",
        "marketId": "1",
        "pageSize": SPORTYBET_PAGE_SIZE,
        "pageNum": 1,
        "todayGames": "false",
        "timeline": SPORTYBET_TIMELINE,
        "_t": int(datetime.now(timezone.utc).timestamp() * 1000),
    }

    return get_json(
        SPORTYBET_URL,
        params=params,
        headers=headers,
        source="SportyBet",
        timeout=httpx.Timeout(
            SPORTYBET_TIMEOUT,
            connect=min(10.0, SPORTYBET_TIMEOUT),
        ),
    )


# ---------------- SPORTYBET PARSER ----------------

def iter_sportybet_events(payload):
    """
    Expected common response shape:
      {
        "bizCode": 10000,
        "data": {
          "tournaments": [
            {
              "name": "...",
              "events": [
                {
                  "eventId": "...",
                  "homeTeamName": "...",
                  "awayTeamName": "...",
                  "estimateStartTime": 1234567890000,
                  "markets": [...]
                }
              ]
            }
          ]
        }
      }

    Also tolerates a flat events list when present.
    """
    if not isinstance(payload, dict):
        return

    data = payload.get("data", payload)

    if isinstance(data, list):
        for item in data:
            if isinstance(item, dict):
                yield item
        return

    if not isinstance(data, dict):
        return

    tournaments = data.get("tournaments")

    if isinstance(tournaments, list):
        for tournament in tournaments:
            if not isinstance(tournament, dict):
                continue

            events = tournament.get("events", [])

            if not isinstance(events, list):
                continue

            for event in events:
                if isinstance(event, dict):
                    # Carry competition name as useful context.
                    event = dict(event)
                    event["_tournament_name"] = (
                        tournament.get("name")
                        or tournament.get("categoryName")
                        or ""
                    )
                    yield event

    # Some response versions use a flat list.
    for key in ("events", "matches", "eventList", "matchList"):
        events = data.get(key)

        if isinstance(events, list):
            for event in events:
                if isinstance(event, dict):
                    yield event


def extract_sportybet_1x2(event):
    """
    SportyBet 1X2 outcome IDs commonly used by the reference:
      1 = Home, 2 = Draw, 3 = Away

    Market ID 1 is the requested market. Descriptions are also checked.
    """
    result = {"home": None, "draw": None, "away": None}
    markets = event.get("markets", [])

    if not isinstance(markets, list):
        return result

    for market in markets:
        if not isinstance(market, dict):
            continue

        market_id = str(market.get("id", market.get("marketId", "")))
        market_desc = str(
            market.get("desc", market.get("name", ""))
        ).casefold()

        # Market ID 1 is the commonly reported 1X2 market.
        # Description fallback helps when the provider changes IDs.
        is_1x2 = (
            market_id == "1"
            or "1x2" in market_desc
            or "match result" in market_desc
            or "full time result" in market_desc
        )

        if not is_1x2:
            continue

        market_status = str(market.get("status", "")).casefold()
        if market_status in {"0", "closed", "suspended", "inactive"}:
            continue

        outcomes = market.get("outcomes", [])

        if not isinstance(outcomes, list):
            continue

        for outcome in outcomes:
            if not isinstance(outcome, dict):
                continue

            active = outcome.get("isActive", True)
            if active is False or str(active).casefold() in {
                "false", "0", "no"
            }:
                continue

            odds = safe_float(outcome.get("odds"))
            if odds is None:
                continue

            outcome_id = str(
                outcome.get("id", outcome.get("outcomeId", ""))
            ).strip()

            desc = str(
                outcome.get("desc", outcome.get("name", ""))
            ).strip().casefold()

            if outcome_id == "1" or desc in {
                "home", "1", "home win", "1x2 home"
            }:
                result["home"] = max(
                    result["home"] or 0, odds
                )
            elif outcome_id == "2" or desc in {
                "draw", "x", "tie"
            }:
                result["draw"] = max(
                    result["draw"] or 0, odds
                )
            elif outcome_id == "3" or desc in {
                "away", "2", "away win", "1x2 away"
            }:
                result["away"] = max(
                    result["away"] or 0, odds
                )

    return result


def fetch_sportybet_events():
    payload = fetch_sportybet_payload()

    if payload is None:
        log.warning("SportyBet payload unavailable.")
        return []

    if isinstance(payload, dict):
        biz_code = payload.get("bizCode")
        if biz_code is not None and str(biz_code) != "10000":
            log.warning(
                "SportyBet reported a non-success business code: %s",
                biz_code,
            )

    raw_events = list(iter_sportybet_events(payload) or [])
    log.info("SportyBet raw event records found: %d", len(raw_events))

    if not raw_events:
        if isinstance(payload, dict):
            data = payload.get("data")
            if isinstance(data, dict):
                log.warning(
                    "SportyBet data keys: %s",
                    list(data.keys())[:30],
                )
        log.warning(
            "No SportyBet events found. Response schema may have changed."
        )
        return []

    parsed = []
    seen = set()
    missing_1x2 = 0
    missing_teams = 0

    for raw in raw_events:
        home = raw.get("homeTeamName") or raw.get("home_team")
        away = raw.get("awayTeamName") or raw.get("away_team")

        if not isinstance(home, str) or not isinstance(away, str):
            missing_teams += 1
            continue

        home = home.strip()
        away = away.strip()

        if not home or not away:
            missing_teams += 1
            continue

        prices = extract_sportybet_1x2(raw)

        if not all(prices.get(k) for k in ("home", "draw", "away")):
            missing_1x2 += 1
            continue

        key = (normalize_team(home), normalize_team(away))
        if key in seen:
            continue
        seen.add(key)

        parsed.append({
            "id": raw.get("eventId") or raw.get("id"),
            "home_team": home,
            "away_team": away,
            "commence_time": raw.get(
                "estimateStartTime",
                raw.get("startTime", raw.get("commence_time")),
            ),
            "tournament": raw.get("_tournament_name", ""),
            "outcomes": prices,
        })

    log.info("SportyBet events parsed: %d", len(parsed))
    log.info(
        "SportyBet skipped: missing_teams=%d missing_complete_1x2=%d",
        missing_teams,
        missing_1x2,
    )

    return parsed


# ---------------- MATCHING / ARBITRAGE ----------------

def events_match(odds_event, sporty_event):
    # Conservative matching: home must match home and away must match away.
    home_score = team_similarity(
        odds_event["home_team"], sporty_event["home_team"]
    )
    away_score = team_similarity(
        odds_event["away_team"], sporty_event["away_team"]
    )

    return home_score >= 0.84 and away_score >= 0.84


def best_prices(odds_event, sporty_event):
    best = {}

    for outcome in ("home", "draw", "away"):
        candidates = []

        for price, bookmaker in odds_event["outcomes"].get(outcome, []):
            if price and price > 1:
                candidates.append((price, bookmaker))

        sporty_price = sporty_event["outcomes"].get(outcome)
        if sporty_price and sporty_price > 1:
            candidates.append((sporty_price, "SportyBet"))

        if not candidates:
            return None

        best[outcome] = max(candidates, key=lambda item: item[0])

    return best


def calculate_arbitrage(prices, bankroll):
    inverse_sum = sum(
        1.0 / prices[outcome][0]
        for outcome in ("home", "draw", "away")
    )

    if inverse_sum <= 0:
        return None

    # This is the mathematical margin before fees and execution risk.
    roi_pct = (1.0 / inverse_sum - 1.0) * 100.0

    stakes = {
        outcome: bankroll
        * (1.0 / prices[outcome][0])
        / inverse_sum
        for outcome in ("home", "draw", "away")
    }

    payouts = {
        outcome: stakes[outcome] * prices[outcome][0]
        for outcome in ("home", "draw", "away")
    }

    return {
        "roi_pct": roi_pct,
        "stakes": stakes,
        "minimum_payout": min(payouts.values()),
        "theoretical_profit": min(payouts.values()) - bankroll,
    }


def format_alert(event, prices, result):
    kickoff = parse_datetime(event.get("commence_time"))
    kickoff_text = (
        kickoff.strftime("%Y-%m-%d %H:%M UTC")
        if kickoff
        else "Unknown"
    )

    lines = [
        "THEORETICAL 1X2 ARBITRAGE",
        f"{event['home_team']} vs {event['away_team']}",
        f"Kickoff: {kickoff_text}",
        f"ROI before fees: {result['roi_pct']:.2f}%",
        f"Assumed total stake: {BANKROLL:.2f}",
        f"Theoretical profit: {result['theoretical_profit']:.2f}",
        "",
    ]

    for outcome, label in (
        ("home", "Home"),
        ("draw", "Draw"),
        ("away", "Away"),
    ):
        odds, bookmaker = prices[outcome]
        stake = result["stakes"][outcome]
        lines.append(
            f"{label}: {odds:.3f} @ {bookmaker}; stake {stake:.2f}"
        )

    lines.extend([
        "",
        "READ-ONLY ESTIMATE. No bets placed.",
        "Verify current odds, limits, settlement rules and fees.",
    ])

    return "\n".join(lines)


# ---------------- MAIN SCAN ----------------

def scan():
    log.info("Football-tool scan started.")

    odds_events = fetch_odds_events()
    sporty_events = fetch_sportybet_events()

    log.info("The Odds API events: %d", len(odds_events))
    log.info("SportyBet events parsed: %d", len(sporty_events))

    if not odds_events:
        log.error("No The Odds API events retrieved.")
        log.info("Qualifying theoretical opportunities: 0")
        log.info("Read-only scanner. No bets placed.")
        return

    if not sporty_events:
        log.warning(
            "No SportyBet events available for comparison; "
            "arbitrage opportunities cannot be assessed."
        )
        log.info("Qualifying theoretical opportunities: 0")
        log.info("Read-only scanner. No bets placed.")
        return

    qualifying = 0

    for odds_event in odds_events:
        if not event_in_window(odds_event):
            continue

        for sporty_event in sporty_events:
            if not events_match(odds_event, sporty_event):
                continue

            # Ignore events with different known kickoff times by > 6 hours.
            t1 = parse_datetime(odds_event.get("commence_time"))
            t2 = parse_datetime(sporty_event.get("commence_time"))

            if t1 and t2 and abs((t1 - t2).total_seconds()) > 6 * 3600:
                log.warning(
                    "Potential name match rejected due to kickoff mismatch: %s vs %s",
                    odds_event["home_team"],
                    odds_event["away_team"],
                )
                continue

            prices = best_prices(odds_event, sporty_event)
            if not prices:
                continue

            result = calculate_arbitrage(prices, BANKROLL)
            if not result:
                continue

            roi = result["roi_pct"]

            if MIN_ROI <= roi <= MAX_ROI:
                qualifying += 1
                alert = format_alert(odds_event, prices, result)

                log.info(
                    "Qualifying theoretical opportunity: %s vs %s, ROI %.2f%%",
                    odds_event["home_team"],
                    odds_event["away_team"],
                    roi,
                )
                log.info("%s", alert)
                send_telegram(alert)

    log.info("Qualifying theoretical opportunities: %d", qualifying)
    log.info("Read-only scanner. No bets placed.")


if __name__ == "__main__":
    try:
        scan()
    except KeyboardInterrupt:
        log.info("Scanner interrupted.")
    except Exception as exc:
        log.exception(
            "Scanner failed unexpectedly (%s).",
            type(exc).__name__,
        )
