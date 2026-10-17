
#!/usr/bin/env python3
"""
SolveEase Global Football Scanner

Features:
- Global ESPN public scoreboard feeds
- 20 unique-match selections per accumulator by default
- Chronological fixture timeline
- 90-minute scan interval
- Telegram alerts
- FastAPI health and fixtures endpoints
- Bounded candidate selection and transparent feed diagnostics

IMPORTANT:
ESPN scoreboards do not supply bookmaker odds or a complete statistical
dataset. Candidate probabilities below are illustrative and unvalidated.
No profitability or betting edge is claimed.
"""

import hashlib
import itertools
import logging
import math
import os
import threading
import time
from datetime import datetime, timezone, timedelta

import httpx
from fastapi import FastAPI
import uvicorn

# ============================================================
# CONFIGURATION
# ============================================================

TELEGRAMBOTTOKEN = os.getenv("TELEGRAMBOTTOKEN", "").strip()
TARGETCHATID = os.getenv("TARGETCHATID", "").strip()

SCAN_INTERVAL_SECONDS = max(
    300, int(os.getenv("SCAN_INTERVAL_SECONDS", "5400"))
)
BANKROLLPOOL = max(0.0, float(os.getenv("BANKROLLPOOL", "10000")))

# Requested: 20 selections per accumulator.
ACCUMULATOR_LEGS = max(
    2, min(20, int(os.getenv("ACCUMULATOR_LEGS", "20")))
)

MAX_HOURS_AHEAD = max(
    1, min(240, int(os.getenv("MAX_HOURS_AHEAD", "72")))
)
MAX_CANDIDATES = max(
    ACCUMULATOR_LEGS,
    min(500, int(os.getenv("MAX_CANDIDATES", "150")))
)
MAX_ALERTS_PER_SCAN = max(
    0, min(5, int(os.getenv("MAX_ALERTS_PER_SCAN", "1")))
)

PORT = int(os.getenv("PORT", "10000"))
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()

logging.basicConfig(
    level=getattr(logging, LOG_LEVEL, logging.INFO),
    format="%(asctime)s %(levelname)s %(message)s",
)
log = logging.getLogger("football-global-tool")

app = FastAPI(title="SolveEase Global Football Scanner")
STATE_LOCK = threading.Lock()
SENT_ALERTS = set()

ESPN_SOURCES = {
    "eng.1": ("England — Premier League", "England"),
    "esp.1": ("Spain — La Liga", "Spain"),
    "ita.1": ("Italy — Serie A", "Italy"),
    "ger.1": ("Germany — Bundesliga", "Germany"),
    "fra.1": ("France — Ligue 1", "France"),
    "por.1": ("Portugal — Liga Portugal", "Portugal"),
    "ned.1": ("Netherlands — Eredivisie", "Netherlands"),
    "eng.2": ("England — Championship", "England"),
    "uefa.champions": ("Europe — UEFA Champions League", "Europe"),
    "uefa.europa": ("Europe — UEFA Europa League", "Europe"),
    "uefa.europa.conf": ("Europe — UEFA Conference League", "Europe"),
    "usa.1": ("USA — MLS", "USA"),
    "mex.1": ("Mexico — Liga MX", "Mexico"),
    "bra.1": ("Brazil — Série A", "Brazil"),
    "arg.1": ("Argentina — Primera División", "Argentina"),
    "conmebol.libertadores": (
        "South America — Copa Libertadores", "South America"
    ),
    "sau.1": ("Saudi Arabia — Saudi Pro League", "Saudi Arabia"),
    "jpn.1": ("Japan — J1 League", "Japan"),
    "afc.champions": ("Asia — AFC Champions League", "Asia"),
    "aus.1": ("Australia — A-League Men", "Australia"),
    "sco.1": ("Scotland — Premiership", "Scotland"),
    "bel.1": ("Belgium — Pro League", "Belgium"),
    "tur.1": ("Türkiye — Süper Lig", "Türkiye"),
    "bra.copa_do_brasil": ("Brazil — Copa do Brasil", "Brazil"),
}

LAST_SCAN_STATE = {
    "status": "starting",
    "service": "global football candidate scanner",
    "model_status": "illustrative baseline; not validated",
    "market_odds_status": "not fetched; bookmaker prices unverified",
    "last_scan_time": None,
    "last_successful_scan_time": None,
    "scan_interval_seconds": SCAN_INTERVAL_SECONDS,
    "requested_accumulator_legs": ACCUMULATOR_LEGS,
    "candidates_count": 0,
    "candidates": [],
    "accumulators_found": 0,
    "accumulators": [],
    "feeds": {},
    "telegram_configured": bool(TELEGRAMBOTTOKEN and TARGETCHATID),
    "last_error": None,
}


# ============================================================
# API
# ============================================================

@app.api_route("/", methods=["GET", "HEAD"])
def health_check():
    with STATE_LOCK:
        feeds = LAST_SCAN_STATE["feeds"]
        return {
            "status": LAST_SCAN_STATE["status"],
            "service": LAST_SCAN_STATE["service"],
            "last_scan_time": LAST_SCAN_STATE["last_scan_time"],
            "last_successful_scan_time":
                LAST_SCAN_STATE["last_successful_scan_time"],
            "feeds_successful": sum(
                1 for feed in feeds.values()
                if feed.get("status") == "ok"
            ),
            "feeds_configured": len(ESPN_SOURCES),
            "telegram_configured": bool(
                TELEGRAMBOTTOKEN and TARGETCHATID
            ),
            "model_status": LAST_SCAN_STATE["model_status"],
            "market_odds_status": LAST_SCAN_STATE["market_odds_status"],
            "last_error": LAST_SCAN_STATE["last_error"],
        }


@app.get("/fixtures")
def get_fixtures():
    with STATE_LOCK:
        return dict(LAST_SCAN_STATE)


# ============================================================
# BASELINE MODEL
# ============================================================

def estimate_candidate(home_team, away_team):
    """
    Temporary illustrative baseline.

    This function does not fetch current form, league tables, injuries,
    historical match results, or bookmaker prices. It is not a validated
    prediction model and must not be represented as one.
    """
    home_win = 0.42
    draw = 0.29
    away_win = 0.29
    over_15 = 0.68

    return {
        "home_dc_estimated_price": round(
            1.0 / (home_win + draw), 3
        ),
        "away_dc_estimated_price": round(
            1.0 / (away_win + draw), 3
        ),
        "over_15_estimated_price": round(1.0 / over_15, 3),
        "home_win_probability_baseline": home_win,
        "draw_probability_baseline": draw,
        "away_win_probability_baseline": away_win,
        "over_15_probability_baseline": over_15,
    }


def parse_datetime(value):
    if not value:
        return None

    try:
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if result.tzinfo is None:
            result = result.replace(tzinfo=timezone.utc)
        return result.astimezone(timezone.utc)
    except (ValueError, TypeError):
        return None


# ============================================================
# GLOBAL FIXTURE COLLECTION
# ============================================================

def fetch_espn_global_candidates(client):
    candidates = []
    feeds = {}

    now = datetime.now(timezone.utc)
    horizon = now + timedelta(hours=MAX_HOURS_AHEAD)

    for slug, (league_label, country) in ESPN_SOURCES.items():
        url = (
            "https://site.api.espn.com/apis/site/v2/sports/"
            f"soccer/{slug}/scoreboard"
        )

        try:
            response = client.get(url, timeout=12)

            if response.status_code != 200:
                feeds[slug] = {
                    "status": "http_error",
                    "http_status": response.status_code,
                }
                continue

            data = response.json()
            events = data.get("events", [])
            fixture_count_before = len(candidates)

            for event in events:
                kickoff = parse_datetime(event.get("date"))
                competitions = event.get("competitions") or []

                if (
                    kickoff is None
                    or not competitions
                    or not (now <= kickoff <= horizon)
                ):
                    continue

                competitors = (
                    competitions[0].get("competitors") or []
                )
                home_team = None
                away_team = None

                for item in competitors:
                    team = (
                        (item.get("team") or {})
                        .get("displayName", "")
                        .strip()
                    )

                    if item.get("homeAway") == "home":
                        home_team = team
                    elif item.get("homeAway") == "away":
                        away_team = team

                if not home_team or not away_team:
                    continue

                stats = estimate_candidate(home_team, away_team)
                kickoff_label = kickoff.strftime("%Y-%m-%d %H:%M UTC")

                event_id = str(
                    event.get("id")
                    or hashlib.sha256(
                        (
                            f"{slug}|{kickoff.isoformat()}|"
                            f"{home_team}|{away_team}"
                        ).encode()
                    ).hexdigest()[:16]
                )

                # One candidate per fixture prevents multiple selections
                # from the same match entering the same accumulator.
                candidates.append({
                    "event_id": event_id,
                    "match": f"{home_team} vs {away_team}",
                    "home_team": home_team,
                    "away_team": away_team,
                    "league": league_label,
                    "country": country,
                    "kickoff": kickoff_label,
                    "kickoff_iso": kickoff.isoformat(),
                    "selection": "Double chance: home or draw",
                    "estimated_price_unverified":
                        stats["home_dc_estimated_price"],
                    "baseline_probability_unvalidated":
                        round(
                            stats["home_win_probability_baseline"]
                            + stats["draw_probability_baseline"],
                            4,
                        ),
                    "market_odds_verified": False,
                    "model_status":
                        "illustrative baseline; not validated",
                })

            feeds[slug] = {
                "status": "ok",
                "events_returned": len(events),
                "future_fixtures_in_window":
                    len(candidates) - fixture_count_before,
            }

        except Exception as exc:
            feeds[slug] = {
                "status": "error",
                "error": str(exc)[:250],
            }
            log.warning("ESPN feed failed for %s: %s", slug, exc)

    candidates.sort(
        key=lambda item: (
            item["kickoff_iso"],
            -item["baseline_probability_unvalidated"],
        )
    )

    # De-duplicate fixtures across competition feeds.
    unique = []
    seen_events = set()

    for candidate in candidates:
        if candidate["event_id"] in seen_events:
            continue

        seen_events.add(candidate["event_id"])
        unique.append(candidate)

    return unique[:MAX_CANDIDATES], feeds


# ============================================================
# 20-SELECTION ACCUMULATOR
# ============================================================

def build_accumulator(candidates):
    """
    Select up to the configured number of unique fixtures.

    Does not manufacture combined bookmaker odds. If fewer than 20
    candidates are available, no 20-selection accumulator is returned.
    """
    if len(candidates) < ACCUMULATOR_LEGS:
        return []

    ranked = sorted(
        candidates,
        key=lambda item: (
            -item["baseline_probability_unvalidated"],
            item["kickoff_iso"],
            item["event_id"],
        ),
    )

    selected = ranked[:ACCUMULATOR_LEGS]
    selected.sort(key=lambda item: item["kickoff_iso"])

    illustrative_joint_probability = math.prod(
        item["baseline_probability_unvalidated"]
        for item in selected
    )

    return [{
        "legs": selected,
        "leg_count": len(selected),
        "illustrative_joint_probability_unvalidated":
            round(illustrative_joint_probability, 8),
        "combined_bookmaker_odds": None,
        "odds_status":
            "not available; no bookmaker feed connected",
        "model_status": "illustrative baseline; not validated",
    }]


# ============================================================
# TELEGRAM
# ============================================================

def format_accumulator_alert(acc):
    lines = [
        f"⚽ GLOBAL FOOTBALL TIMELINE — {acc['leg_count']} SELECTIONS",
        "STATUS: UNVALIDATED STATISTICAL CANDIDATES",
        "Bookmaker prices: NOT FETCHED / NOT VERIFIED",
        "Combined odds: NOT CALCULATED",
        "Not guaranteed predictions or a betting recommendation.",
        "",
        "Illustrative joint probability (unvalidated): "
        f"{acc['illustrative_joint_probability_unvalidated']:.6%}",
        "Configured bankroll pool (not a stake recommendation): "
        f"{BANKROLLPOOL:,.2f}",
        "",
    ]

    for number, leg in enumerate(acc["legs"], 1):
        lines.extend([
            f"{number}. {leg['match']}",
            f"   Competition: {leg['league']} ({leg['country']})",
            f"   Kickoff: {leg['kickoff']}",
            f"   Candidate: {leg['selection']}",
            "   Illustrative price only: "
            f"{leg['estimated_price_unverified']:.3f}",
            "   NOT verified bookmaker odds",
            "",
        ])

    return "\n".join(lines)


def send_telegram(client, message):
    if not TELEGRAMBOTTOKEN or not TARGETCHATID:
        log.warning("Telegram credentials missing; alert not sent.")
        return False

    url = (
        "https://api.telegram.org/bot"
        f"{TELEGRAMBOTTOKEN}/sendMessage"
    )

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
                "Telegram API error %s: %s",
                response.status_code,
                response.text[:500],
            )
            return False

        result = response.json()

        if not result.get("ok", False):
            log.error("Telegram returned non-ok response: %s", result)
            return False

        return True

    except Exception as exc:
        log.error("Telegram send exception: %s", exc)
        return False


# ============================================================
# SCAN ENGINE
# ============================================================

def scan_once(client):
    scan_time = datetime.now(timezone.utc).isoformat()

    with STATE_LOCK:
        LAST_SCAN_STATE["status"] = "scanning"
        LAST_SCAN_STATE["last_scan_time"] = scan_time
        LAST_SCAN_STATE["last_error"] = None

    candidates, feeds = fetch_espn_global_candidates(client)
    accumulators = build_accumulator(candidates)

    safe_candidates = [
        {key: value for key, value in item.items()
         if key != "kickoff_iso"}
        for item in candidates[:50]
    ]

    safe_accumulators = []

    for accumulator in accumulators:
        safe_accumulators.append({
            **{
                key: value
                for key, value in accumulator.items()
                if key != "legs"
            },
            "legs": [
                {
                    key: value
                    for key, value in leg.items()
                    if key != "kickoff_iso"
                }
                for leg in accumulator["legs"]
            ],
        })

    with STATE_LOCK:
        LAST_SCAN_STATE.update({
            "status": "running",
            "last_successful_scan_time": scan_time,
            "candidates_count": len(candidates),
            "candidates": safe_candidates,
            "accumulators_found": len(accumulators),
            "accumulators": safe_accumulators,
            "feeds": feeds,
            "telegram_configured": bool(
                TELEGRAMBOTTOKEN and TARGETCHATID
            ),
            "last_error": None,
        })

    log.info(
        "Scan complete: %d unique fixtures; %d accumulator(s); "
        "%d/%d feeds OK",
        len(candidates),
        len(accumulators),
        sum(
            1 for feed in feeds.values()
            if feed.get("status") == "ok"
        ),
        len(ESPN_SOURCES),
    )

    for accumulator in accumulators[:MAX_ALERTS_PER_SCAN]:
        signature_source = "|".join(
            f"{leg['event_id']}:{leg['selection']}"
            for leg in accumulator["legs"]
        )
        signature = hashlib.sha256(
            signature_source.encode()
        ).hexdigest()

        if signature in SENT_ALERTS:
            continue

        if send_telegram(client, format_accumulator_alert(accumulator)):
            SENT_ALERTS.add(signature)

            # Bound in-memory deduplication growth.
            if len(SENT_ALERTS) > 2000:
                SENT_ALERTS.clear()
                SENT_ALERTS.add(signature)

            log.info(
                "Telegram alert sent for %d selections.",
                accumulator["leg_count"],
            )


def scan_loop():
    log.info(
        "Scanner started: interval=%d seconds; selections=%d",
        SCAN_INTERVAL_SECONDS,
        ACCUMULATOR_LEGS,
    )

    with httpx.Client(
        follow_redirects=True,
        headers={"User-Agent": "SolveEaseFootballScanner/1.0"},
    ) as client:
        while True:
            started = time.monotonic()

            try:
                scan_once(client)

            except Exception as exc:
                log.exception("Scan cycle failed.")

                with STATE_LOCK:
                    LAST_SCAN_STATE["status"] = "error"
                    LAST_SCAN_STATE["last_error"] = str(exc)[:500]

            elapsed = time.monotonic() - started
            time.sleep(max(1, SCAN_INTERVAL_SECONDS - elapsed))


# ============================================================
# START SERVER
# ============================================================

if __name__ == "__main__":
    thread = threading.Thread(
        target=scan_loop,
        daemon=True,
        name="football-scan-loop",
    )
    thread.start()

    log.info("Starting API on port %d", PORT)
    uvicorn.run(app, host="0.0.0.0", port=PORT)
