
"""
Football-tool — read-only 1X2 arbitrage scanner
Python 3.11+

Required environment variables:
  ODDS_API_KEY
  TELEGRAMBOTTOKEN
  TARGETCHATID

Optional environment variables:
  SPORTYBET_ENABLED=true
  SPORTYBET_URL=<your verified SportyBet JSON endpoint>
  SPORTYBET_API_KEY=<if your endpoint requires an API key>
  SPORTYBET_AUTH_MODE=bearer   # bearer, x-api-key, or none
  SPORTYBET_TIMEOUT=15
  SPORTYBET_MIN_EVENTS=1
  SPORT_KEY=soccer_epl
  ODDS_REGIONS=uk,eu
  BANKROLLPOOL=20000
  MIN_ARBITRAGE_ROI=0.5
  MAX_ARBITRAGE_ROI=30
  TIMELINE_HOURS=48
  LOG_LEVEL=INFO

This program never places bets.
SportyBet endpoint and response schema must be verified.
"""

import os
import re
import json
import logging
import unicodedata
from datetime import datetime, timezone
from difflib import SequenceMatcher

import httpx


# ---------------- CONFIGURATION ----------------

ODDS_API_KEY = os.getenv("ODDS_API_KEY", "").strip()
TELEGRAM_TOKEN = os.getenv("TELEGRAMBOTTOKEN", "").strip()
TELEGRAM_CHAT_ID = os.getenv("TARGETCHATID", "").strip()

SPORTYBET_ENABLED = (
    os.getenv("SPORTYBET_ENABLED", "true").strip().lower()
    in {"1", "true", "yes", "on"}
)
SPORTYBET_URL = os.getenv("SPORTYBET_URL", "").strip()
SPORTYBET_API_KEY = os.getenv("SPORTYBET_API_KEY", "").strip()
SPORTYBET_AUTH_MODE = (
    os.getenv("SPORTYBET_AUTH_MODE", "bearer").strip().lower()
)
SPORTYBET_TIMEOUT = float(os.getenv("SPORTYBET_TIMEOUT", "15"))
SPORTYBET_MIN_EVENTS = int(os.getenv("SPORTYBET_MIN_EVENTS", "1"))

SPORT_KEY = os.getenv("SPORT_KEY", "soccer_epl").strip()
ODDS_REGIONS = os.getenv("ODDS_REGIONS", "uk,eu").strip()

BANKROLL = float(os.getenv("BANKROLLPOOL", "20000"))
MIN_ROI = float(os.getenv("MIN_ARBITRAGE_ROI", "0.5"))
MAX_ROI = float(os.getenv("MAX_ARBITRAGE_ROI", "30"))
TIMELINE_HOURS = float(os.getenv("TIMELINE_HOURS", "48"))

ODDS_API_URL = (
    "https://api.the-odds-api.com/v4/sports/"
    f"{SPORT_KEY}/odds/"
)

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s %(levelname)s %(message)s",
)
log = logging.getLogger("football_tool")

HTTP_TIMEOUT = httpx.Timeout(20.0, connect=10.0)


# ---------------- GENERAL HELPERS ----------------

def normalize_team(name):
    """Normalize team names for conservative event matching."""
    if not isinstance(name, str):
        return ""

    name = unicodedata.normalize("NFKD", name)
    name = "".join(c for c in name if not unicodedata.combining(c))
    name = name.lower().replace("&", " and ")
    name = re.sub(
        r"\b(fc|afc|cf|sc|club|football club|the)\b", " ", name
    )
    return re.sub(r"[^a-z0-9]+", "", name)


def team_similarity(a, b):
    a = normalize_team(a)
    b = normalize_team(b)
    if not a or not b:
        return 0.0
    if a == b:
        return 1.0
    return SequenceMatcher(None, a, b).ratio()


def safe_float(value):
    try:
        result = float(value)
        if result > 1.0 and result < float("inf"):
            return result
    except (TypeError, ValueError, OverflowError):
        pass
    return None


def parse_time(value):
    if not value:
        return None
    try:
        value = str(value).strip().replace("Z", "+00:00")
        dt = datetime.fromisoformat(value)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except (TypeError, ValueError):
        return None


def event_is_in_window(event):
    """Keep events with unknown times; filter known distant/past events."""
    kickoff = parse_time(event.get("commence_time"))
    if kickoff is None:
        return True

    now = datetime.now(timezone.utc)
    hours = (kickoff - now).total_seconds() / 3600
    return 0 <= hours <= TIMELINE_HOURS


# ---------------- HTTP / TELEGRAM ----------------

def request_json(url, params=None, headers=None, timeout=None, source="API"):
    """
    Fetch JSON safely. Never logs the full URL, query parameters,
    authentication headers, or response body.
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
        body = response.content

        log.info(
            "%s response: status=%s content_type=%s bytes=%s",
            source,
            response.status_code,
            content_type.split(";")[0],
            len(body),
        )

        if response.status_code < 200 or response.status_code >= 300:
            log.warning("%s returned a non-success HTTP status.", source)
            return None

        try:
            payload = response.json()
        except (json.JSONDecodeError, ValueError):
            log.warning(
                "%s response is not valid JSON. Check endpoint, "
                "redirects, access restrictions, and response format.",
                source,
            )
            return None

        if isinstance(payload, dict):
            log.info(
                "%s JSON top-level keys: %s",
                source,
                list(payload.keys())[:40],
            )
        elif isinstance(payload, list):
            log.info(
                "%s JSON top-level list length: %s",
                source,
                len(payload),
            )
        else:
            log.info(
                "%s JSON top-level type: %s",
                source,
                type(payload).__name__,
            )

        return payload

    except httpx.TimeoutException:
        log.warning("%s request timed out.", source)
    except httpx.HTTPError as exc:
        # Exception text can contain URLs. Do not print it verbatim.
        log.warning("%s HTTP request failed (%s).", source, type(exc).__name__)
    except Exception as exc:
        log.error("%s unexpected request error (%s).", source, type(exc).__name__)

    return None


def send_telegram(message):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        log.info("Telegram not configured; alert skipped.")
        return False

    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"

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

        try:
            result = response.json()
        except ValueError:
            log.warning("Telegram returned an unexpected response.")
            return False

        if not result.get("ok"):
            log.warning("Telegram did not accept the message.")
            return False

        return True

    except httpx.HTTPError as exc:
        log.warning("Telegram request failed (%s).", type(exc).__name__)
        return False


# ---------------- THE ODDS API ----------------

def fetch_odds_events():
    if not ODDS_API_KEY:
        log.error("Missing ODDS_API_KEY environment variable.")
        return []

    payload = request_json(
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
        log.error("The Odds API did not return an event list.")
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

            bookmaker_name = bookmaker.get("title") or bookmaker.get("key", "")

            for market in bookmaker.get("markets", []) or []:
                if market.get("key") != "h2h":
                    continue

                for outcome in market.get("outcomes", []) or []:
                    label = str(outcome.get("name", "")).strip()
                    price = safe_float(outcome.get("price"))

                    if price is None:
                        continue

                    if label.lower() == str(home).lower():
                        outcomes["home"].append(
                            (price, bookmaker_name)
                        )
                    elif label.lower() == str(away).lower():
                        outcomes["away"].append(
                            (price, bookmaker_name)
                        )
                    elif label.lower() in {"draw", "tie"}:
                        outcomes["draw"].append(
                            (price, bookmaker_name)
                        )

        events.append({
            "id": raw.get("id"),
            "home_team": home,
            "away_team": away,
            "commence_time": raw.get("commence_time"),
            "outcomes": outcomes,
        })

    log.info("The Odds API events: %s", len(events))
    return events


# ---------------- SPORTYBET DIAGNOSTICS ----------------

def fetch_sportybet_payload():
    if not SPORTYBET_ENABLED:
        log.info("SportyBet integration disabled.")
        return None

    if not SPORTYBET_URL:
        log.warning(
            "SPORTYBET_URL is not configured. SportyBet was not queried. "
            "Set it to a verified endpoint returning JSON."
        )
        return None

    headers = {
        "Accept": "application/json",
        "User-Agent": "Football-tool-readonly-scanner/1.0",
    }

    if SPORTYBET_API_KEY:
        if SPORTYBET_AUTH_MODE == "bearer":
            headers["Authorization"] = f"Bearer {SPORTYBET_API_KEY}"
        elif SPORTYBET_AUTH_MODE == "x-api-key":
            headers["x-api-key"] = SPORTYBET_API_KEY
        elif SPORTYBET_AUTH_MODE == "none":
            pass
        else:
            log.warning(
                "Unknown SPORTYBET_AUTH_MODE; no API-key header added."
            )

    payload = request_json(
        SPORTYBET_URL,
        headers=headers,
        timeout=httpx.Timeout(
            SPORTYBET_TIMEOUT,
            connect=min(SPORTYBET_TIMEOUT, 10.0),
        ),
        source="SportyBet",
    )

    if payload is None:
        log.warning(
            "SportyBet payload unavailable. The Odds API scan will continue."
        )

    return payload


# ---------------- SPORTYBET PARSER ----------------

HOME_KEYS = {
    "home", "hometeam", "home_team", "homeTeam",
    "homeName", "host", "hostName",
}
AWAY_KEYS = {
    "away", "awayteam", "away_team", "awayTeam",
    "awayName", "guest", "guestName",
}
DRAW_KEYS = {"draw", "tie", "x"}
EVENT_LIST_KEYS = {
    "events", "matches", "data", "fixtures", "results",
    "eventList", "matchList", "records",
}


def get_first(mapping, candidates):
    if not isinstance(mapping, dict):
        return None

    # Exact keys first, then case-insensitive matching.
    for key in candidates:
        if key in mapping:
            return mapping[key]

    lowered = {str(k).lower(): v for k, v in mapping.items()}
    for key in candidates:
        if str(key).lower() in lowered:
            return lowered[str(key).lower()]

    return None


def extract_team_names(event):
    home = get_first(event, HOME_KEYS)
    away = get_first(event, AWAY_KEYS)

    # Common nested forms: {"home": {"name": "..."}, ...}
    if isinstance(home, dict):
        home = get_first(home, {"name", "teamName", "label", "title"})
    if isinstance(away, dict):
        away = get_first(away, {"name", "teamName", "label", "title"})

    # Some feeds put teams in a two-element list.
    if (not home or not away) and isinstance(event.get("teams"), list):
        teams = event["teams"]
        if len(teams) >= 2:
            def team_name(item):
                if isinstance(item, str):
                    return item
                if isinstance(item, dict):
                    return get_first(
                        item, {"name", "teamName", "label", "title"}
                    )
                return None

            home = home or team_name(teams[0])
            away = away or team_name(teams[1])

    if not isinstance(home, str) or not isinstance(away, str):
        return None, None

    if not home.strip() or not away.strip():
        return None, None

    return home.strip(), away.strip()


def extract_event_time(event):
    return (
        get_first(
            event,
            {
                "commence_time", "startTime", "start_time", "matchTime",
                "kickoff", "kickOff", "eventDate", "date",
            },
        )
    )


def extract_market_container(event):
    """
    Return likely odds/markets containers without assuming one exact schema.
    """
    containers = []

    for key in (
        "odds", "markets", "market", "oddsList",
        "marketList", "betTypes", "selections",
    ):
        value = get_first(event, {key})
        if isinstance(value, (dict, list)):
            containers.append(value)

    return containers


def outcome_label(value):
    text = str(value or "").strip().lower()
    compact = re.sub(r"[^a-z0-9]+", "", text)

    if compact in {"1", "home", "hometeam", "1x2home"}:
        return "home"
    if compact in {"x", "draw", "tie"}:
        return "draw"
    if compact in {"2", "away", "awayteam", "1x2away"}:
        return "away"

    return None


def parse_outcome_price(item, key_hint=None):
    """
    Parse common representations such as:
      {"name": "Home", "odds": 2.2}
      {"label": "Draw", "price": 3.1}
      {"outcome": "Away", "value": "3.4"}
      {"home": 2.2, "draw": 3.1, "away": 3.4}
    """
    if isinstance(item, (int, float, str)):
        label = outcome_label(key_hint)
        price = safe_float(item)
        return (label, price) if label and price else (None, None)

    if not isinstance(item, dict):
        return None, None

    label = get_first(
        item,
        {
            "name", "label", "outcome", "selection", "selectionName",
            "title", "key", "type", "marketName",
        },
    )

    if label is None:
        label = key_hint

    # Market names can contain 1X2, so prefer explicit selection labels.
    label = outcome_label(label)

    price = get_first(
        item,
        {
            "odds", "odd", "price", "decimalOdds", "decimal_odd",
            "value", "oddsValue", "fixedOdds",
        },
    )
    price = safe_float(price)

    return (label, price) if label and price else (None, None)


def parse_sportybet_event(event):
    """
    Best-effort parser. Unrecognized event structures are skipped,
    not guessed. Log schema keys to help build a verified parser.
    """
    if not isinstance(event, dict):
        return None

    home, away = extract_team_names(event)
    if not home or not away:
        return None

    outcomes = {"home": None, "draw": None, "away": None}

    # First: direct 1X2 dictionaries.
    for container in extract_market_container(event):
        if isinstance(container, dict):
            for key, value in container.items():
                label = outcome_label(key)
                if label:
                    if isinstance(value, dict):
                        _, price = parse_outcome_price(value, key)
                    else:
                        price = safe_float(value)

                    if price:
                        outcomes[label] = max(
                            outcomes[label] or 0, price
                        )

        elif isinstance(container, list):
            for item in container:
                label, price = parse_outcome_price(item)
                if label and price:
                    outcomes[label] = max(
                        outcomes[label] or 0, price
                    )

                # A market may wrap its selections in a nested list.
                if isinstance(item, dict):
                    for nested_key in (
                        "outcomes", "selections", "options", "oddsList",
                    ):
                        nested = item.get(nested_key)
                        if isinstance(nested, list):
                            for selection in nested:
                                label, price = parse_outcome_price(selection)
                                if label and price:
                                    outcomes[label] = max(
                                        outcomes[label] or 0, price
                                    )

    # Also inspect simple direct outcome fields on the event itself.
    for label, aliases in {
        "home": {"homeOdds", "home_odds", "oddsHome"},
        "draw": {"drawOdds", "draw_odds", "oddsDraw"},
        "away": {"awayOdds", "away_odds", "oddsAway"},
    }.items():
        direct = safe_float(get_first(event, aliases))
        if direct:
            outcomes[label] = max(outcomes[label] or 0, direct)

    if not all(outcomes[k] and outcomes[k] > 1 for k in ("home", "draw", "away")):
        return None

    return {
        "id": get_first(event, {"eventId", "matchId", "id", "event_id"}),
        "home_team": home,
        "away_team": away,
        "commence_time": extract_event_time(event),
        "outcomes": outcomes,
    }


def find_event_lists(payload):
    """Find lists likely to contain events without recursively dumping payloads."""
    if isinstance(payload, list):
        if payload and all(isinstance(item, dict) for item in payload):
            return [payload]

        lists = []
        for item in payload:
            lists.extend(find_event_lists(item))
        return lists

    if isinstance(payload, dict):
        found = []

        for key, value in payload.items():
            if str(key).lower() in {k.lower() for k in EVENT_LIST_KEYS}:
                if isinstance(value, list):
                    found.append(value)
                elif isinstance(value, dict):
                    found.extend(find_event_lists(value))

        if found:
            return found

        # Some APIs wrap the response in an unnamed object.
        for value in payload.values():
            if isinstance(value, (dict, list)):
                found.extend(find_event_lists(value))
        return found

    return []


def fetch_sportybet_events():
    payload = fetch_sportybet_payload()
    if payload is None:
        return []

    candidate_lists = find_event_lists(payload)

    if not candidate_lists:
        if isinstance(payload, dict):
            log.warning(
                "SportyBet JSON had no recognizable event list. "
                "Top-level keys: %s",
                list(payload.keys())[:40],
            )
        else:
            log.warning(
                "SportyBet JSON structure did not contain a recognizable event list."
            )
        return []

    parsed = []
    seen = set()

    for candidate_list in candidate_lists:
        for raw_event in candidate_list:
            event = parse_sportybet_event(raw_event)
            if not event:
                continue

            key = (
                normalize_team(event["home_team"]),
                normalize_team(event["away_team"]),
            )
            if key in seen:
                continue

            seen.add(key)
            parsed.append(event)

    log.info("SportyBet events parsed: %s", len(parsed))

    if len(parsed) < SPORTYBET_MIN_EVENTS:
        log.warning(
            "SportyBet parsed fewer than the configured minimum (%s). "
            "Check the actual response schema and 1X2 market fields.",
            SPORTYBET_MIN_EVENTS,
        )

    return parsed


# ---------------- MATCHING / ARBITRAGE ----------------

def events_match(odds_event, sporty_event):
    """
    Require both home and away teams to match in the same orientation.
    Conservative matching avoids reversing home/away by mistake.
    """
    home_score = team_similarity(
        odds_event["home_team"], sporty_event["home_team"]
    )
    away_score = team_similarity(
        odds_event["away_team"], sporty_event["away_team"]
    )

    return home_score >= 0.84 and away_score >= 0.84


def best_market_prices(odds_event, sporty_event):
    """
    For each outcome, compare bookmaker prices from The Odds API
    against the parsed SportyBet price.
    """
    result = {}

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

        result[outcome] = max(candidates, key=lambda item: item[0])

    return result


def calculate_arbitrage(prices, bankroll):
    """
    Calculate theoretical dutching stakes.
    Positive ROI requires the sum of inverse odds to be below 1.
    """
    inverse_sum = sum(
        1.0 / prices[outcome][0]
        for outcome in ("home", "draw", "away")
    )

    if inverse_sum <= 0:
        return None

    roi_pct = ((1.0 / inverse_sum) - 1.0) * 100.0

    stakes = {
        outcome: bankroll * (1.0 / prices[outcome][0]) / inverse_sum
        for outcome in ("home", "draw", "away")
    }

    payouts = {
        outcome: stakes[outcome] * prices[outcome][0]
        for outcome in ("home", "draw", "away")
    }

    return {
        "roi_pct": roi_pct,
        "inverse_sum": inverse_sum,
        "stakes": stakes,
        "payouts": payouts,
        "minimum_payout": min(payouts.values()),
        "theoretical_profit": min(payouts.values()) - bankroll,
    }


def format_opportunity(odds_event, prices, result):
    kickoff = odds_event.get("commence_time") or "Time not supplied"

    lines = [
        "⚽ THEORETICAL 1X2 ARBITRAGE",
        f"{odds_event['home_team']} vs {odds_event['away_team']}",
        f"Kickoff: {kickoff}",
        f"Bankroll assumption: {BANKROLL:.2f}",
        f"Estimated ROI before fees: {result['roi_pct']:.2f}%",
        f"Theoretical profit: {result['theoretical_profit']:.2f}",
        "",
        "Outcome allocation:",
    ]

    for outcome, label in (
        ("home", "Home"),
        ("draw", "Draw"),
        ("away", "Away"),
    ):
        price, bookmaker = prices[outcome]
        lines.append(
            f"{label}: odds {price:.3f} at {bookmaker}; "
            f"stake {result['stakes'][outcome]:.2f}"
        )

    lines.extend([
        "",
        "READ-ONLY ESTIMATE. No bets placed.",
        "Verify live odds, limits, fees, and market settlement rules manually.",
    ])

    return "\n".join(lines)


def scan():
    log.info("Football-tool read-only scan started.")

    odds_events = fetch_odds_events()
    sporty_events = fetch_sportybet_events()

    log.info("The Odds API events: %s", len(odds_events))
    log.info("SportyBet events parsed: %s", len(sporty_events))

    if not odds_events:
        log.error("No reference events were retrieved.")
        return

    if not sporty_events:
        log.warning(
            "No SportyBet events are available for comparison. "
            "No cross-bookmaker arbitrage conclusion can be made."
        )
        log.info("Qualifying theoretical opportunities: 0")
        log.info("Read-only scanner. No bets placed.")
        return

    qualifying = 0
    checked_pairs = set()

    for odds_event in odds_events:
        if not event_is_in_window(odds_event):
            continue

        for sporty_event in sporty_events:
            if not events_match(odds_event, sporty_event):
                continue

            pair_key = (
                odds_event.get("id"),
                sporty_event.get("id")
                or (
                    sporty_event["home_team"],
                    sporty_event["away_team"],
                ),
            )
            if pair_key in checked_pairs:
                continue
            checked_pairs.add(pair_key)

            prices = best_market_prices(odds_event, sporty_event)
            if not prices:
                continue

            result = calculate_arbitrage(prices, BANKROLL)
            if not result:
                continue

            roi = result["roi_pct"]

            if MIN_ROI <= roi <= MAX_ROI:
                qualifying += 1
                message = format_opportunity(
                    odds_event, prices, result
                )
                log.info(
                    "Qualifying theoretical opportunity: %s vs %s; ROI %.2f%%",
                    odds_event["home_team"],
                    odds_event["away_team"],
                    roi,
                )
                log.info("%s", message)
                send_telegram(message)

    log.info(
        "Qualifying theoretical opportunities: %s",
        qualifying,
    )
    log.info("Read-only scanner. No bets placed.")


if __name__ == "__main__":
    try:
        scan()
    except KeyboardInterrupt:
        log.info("Scanner interrupted by user.")
    except Exception as exc:
        log.exception(
            "Scanner stopped due to an unexpected error (%s).",
            type(exc).__name__,
        )
