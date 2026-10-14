#!/usr/bin/env python3
"""Read-only football 1X2 odds comparison and arbitrage alert scanner.

Important:
- The Odds API is the supported external odds source.
- SportyBet's website endpoint is undocumented and may change. Use only where
  access is permitted by SportyBet's terms and applicable law.
- This tool never places bets. It reports theoretical opportunities only.
"""
from __future__ import annotations
import hashlib
import json
import os
import re
import sqlite3
import sys
import time
from datetime import datetime, timezone
from difflib import SequenceMatcher
from typing import Any

import httpx

ODDS_API_BASE = "https://api.the-odds-api.com/v4"
SPORTYBET_DEFAULT_URL = "https://www.sportybet.com/api/ng/factsCenter/pcUpcomingEvents"
DB_PATH = os.getenv("STATE_DB_PATH", "arbitrage_state.sqlite3")

def env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except ValueError:
        return default

def env_float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, str(default)))
    except ValueError:
        return default

def norm_name(value: str) -> str:
    value = value.lower().replace("&", " and ")
    value = re.sub(r"\b(fc|afc|cf|sc|club|women|wfc)\b", " ", value)
    return re.sub(r"[^a-z0-9]+", "", value)

def similar(a: str, b: str) -> float:
    na, nb = norm_name(a), norm_name(b)
    if not na or not nb:
        return 0.0
    if na == nb:
        return 1.0
    return SequenceMatcher(None, na, nb).ratio()

def send_telegram(text: str) -> bool:
    token = os.getenv("TELEGRAMBOTTOKEN", "").strip()
    chat_id = os.getenv("TARGETCHATID", "").strip()
    if not token or not chat_id:
        print("Telegram not configured; skipping message.")
        return False
    try:
        r = httpx.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            data={"chat_id": chat_id, "text": text, "disable_web_page_preview": True},
            timeout=20.0,
        )
        r.raise_for_status()
        result = r.json()
        if not result.get("ok"):
            print(f"Telegram API returned not-ok: {result}")
            return False
        return True
    except Exception as exc:
        print(f"Telegram send failed: {type(exc).__name__}: {exc}")
        return False

def init_db() -> None:
    with sqlite3.connect(DB_PATH) as con:
        con.execute("""CREATE TABLE IF NOT EXISTS sent_alerts (
            alert_key TEXT PRIMARY KEY, sent_at TEXT NOT NULL
        )""")

def already_sent(key: str) -> bool:
    with sqlite3.connect(DB_PATH) as con:
        return con.execute("SELECT 1 FROM sent_alerts WHERE alert_key=?", (key,)).fetchone() is not None

def mark_sent(key: str) -> None:
    with sqlite3.connect(DB_PATH) as con:
        con.execute("INSERT OR IGNORE INTO sent_alerts(alert_key, sent_at) VALUES (?, ?)",
                    (key, datetime.now(timezone.utc).isoformat()))

def get_odds_api_events(client: httpx.Client) -> list[dict[str, Any]]:
    key = os.getenv("ODDS_API_KEY", "").strip()
    if not key:
        raise RuntimeError("Missing ODDS_API_KEY secret.")
    sport = os.getenv("ODDS_SPORT_KEY", "soccer_epl").strip()
    regions = os.getenv("ODDS_REGIONS", "uk,eu").strip()
    url = f"{ODDS_API_BASE}/sports/{sport}/odds/"
    response = client.get(url, params={
        "apiKey": key, "regions": regions, "markets": "h2h",
        "oddsFormat": "decimal", "dateFormat": "iso",
    })
    if response.status_code == 401:
        raise RuntimeError("The Odds API rejected ODDS_API_KEY (HTTP 401).")
    if response.status_code == 429:
        raise RuntimeError("The Odds API rate limit/quota was reached (HTTP 429).")
    response.raise_for_status()
    data = response.json()
    if not isinstance(data, list):
        raise RuntimeError("Unexpected The Odds API response: expected a JSON list.")
    print(f"The Odds API: received {len(data)} events; remaining requests header="
          f"{response.headers.get('x-requests-remaining', 'not provided')}")
    return data

def sportybet_headers() -> dict[str, str]:
    # Optional headers are only for an authorized API provider. Do not paste browser
    # cookies or session authorization values into repository files.
    headers = {"Accept": "application/json", "User-Agent": "Football-tool-readonly/1.0"}
    token = os.getenv("SPORTYBET_API_KEY", "").strip()
    if token:
        mode = os.getenv("SPORTYBET_AUTH_MODE", "bearer").strip().lower()
        if mode == "bearer":
            headers["Authorization"] = f"Bearer {token}"
        elif mode == "x-api-key":
            headers["x-api-key"] = token
        else:
            raise RuntimeError("SPORTYBET_AUTH_MODE must be 'bearer' or 'x-api-key'.")
    return headers

def walk_dicts(obj: Any):
    if isinstance(obj, dict):
        yield obj
        for v in obj.values():
            yield from walk_dicts(v)
    elif isinstance(obj, list):
        for item in obj:
            yield from walk_dicts(item)

def pick_first(d: dict[str, Any], keys: tuple[str, ...]) -> Any:
    for k in keys:
        if k in d and d[k] not in (None, ""):
            return d[k]
    return None

def parse_sportybet_payload(payload: Any) -> list[dict[str, Any]]:
    """Best-effort parser; SportyBet response schema is not guaranteed."""
    found = []
    for d in walk_dicts(payload):
        home = pick_first(d, ("homeTeamName", "homeName", "homeTeam", "home", "team1Name"))
        away = pick_first(d, ("awayTeamName", "awayName", "awayTeam", "away", "team2Name"))
        if isinstance(home, dict):
            home = pick_first(home, ("name", "teamName", "enName"))
        if isinstance(away, dict):
            away = pick_first(away, ("name", "teamName", "enName"))
        if not isinstance(home, str) or not isinstance(away, str):
            continue
        kickoff = pick_first(d, ("estimateStartTime", "startTime", "kickoffTime", "matchTime", "eventTime"))
        try:
            kickoff_num = float(kickoff)
            if kickoff_num > 10_000_000_000:
                kickoff_num /= 1000
            kickoff_iso = datetime.fromtimestamp(kickoff_num, timezone.utc).isoformat()
        except (TypeError, ValueError, OSError, OverflowError):
            if isinstance(kickoff, str):
                kickoff_iso = kickoff
            else:
                kickoff_iso = ""
        odds = extract_sportybet_1x2(d)
        if all(odds.get(k) and odds[k] > 1.0 for k in ("home", "draw", "away")):
            found.append({"home": home, "away": away, "kickoff": kickoff_iso, "odds": odds})
    # Deduplicate repeated nested representations
    unique, seen = [], set()
    for e in found:
        k = (norm_name(e["home"]), norm_name(e["away"]), e["kickoff"], tuple(sorted(e["odds"].items())))
        if k not in seen:
            seen.add(k); unique.append(e)
    return unique

def extract_sportybet_1x2(d: dict[str, Any]) -> dict[str, float]:
    """Recognize only explicit 1/X/2 or home/draw/away outcome labels."""
    result: dict[str, float] = {}
    # Some response shapes include a market with outcomes; scan only dictionaries
    # containing outcome labels and plausible decimal odds.
    for child in walk_dicts(d):
        label = str(pick_first(child, ("outcomeName", "name", "desc", "marketName", "title", "specifier")) or "").strip().lower()
        raw = pick_first(child, ("odds", "odd", "decimalOdds", "price", "value"))
        try:
            odd = float(raw)
        except (TypeError, ValueError):
            odd = 0.0
        if not (1.001 <= odd <= 1000):
            continue
        if label in ("1", "home", "home win", "1x2_home", "home team"):
            result.setdefault("home", odd)
        elif label in ("x", "draw", "tie"):
            result.setdefault("draw", odd)
        elif label in ("2", "away", "away win", "1x2_away", "away team"):
            result.setdefault("away", odd)
    return result

def get_sportybet_events(client: httpx.Client) -> list[dict[str, Any]]:
    url = os.getenv("SPORTYBET_FEED_URL", SPORTYBET_DEFAULT_URL).strip()
    # Query parameters used by some public website versions; undocumented and subject
    # to change. Override SPORTYBET_FEED_URL only with an endpoint you are authorized to use.
    params = {
        "sportId": os.getenv("SPORTYBET_SPORT_ID", "sr:sport:1"),
        "marketId": os.getenv("SPORTYBET_MARKET_ID", "1"),
        "pageSize": os.getenv("SPORTYBET_PAGE_SIZE", "100"),
        "pageNum": "1",
        "todayGames": "false",
        "timeline": os.getenv("SPORTYBET_TIMELINE_HOURS", "48"),
        "timestamp": str(int(time.time() * 1000)),
    }
    response = client.get(url, params=params, headers=sportybet_headers())
    if response.status_code in (401, 403):
        raise RuntimeError(f"SportyBet feed denied access (HTTP {response.status_code}). Check endpoint/authorization.")
    response.raise_for_status()
    try:
        payload = response.json()
    except json.JSONDecodeError:
        raise RuntimeError("SportyBet endpoint did not return JSON.")
    events = parse_sportybet_payload(payload)
    print(f"SportyBet feed: HTTP {response.status_code}; parsed {len(events)} complete 1X2 events.")
    if not events:
        print("WARNING: parser found no complete 1X2 events. The endpoint schema/market mapping is unverified; no cross-feed opportunities can be trusted.")
    return events

def best_foreign_1x2(event: dict[str, Any]) -> dict[str, Any] | None:
    outcomes: dict[str, tuple[float, str]] = {}
    home, away = str(event.get("home_team", "")), str(event.get("away_team", ""))
    for book in event.get("bookmakers", []) or []:
        for market in book.get("markets", []) or []:
            if market.get("key") != "h2h":
                continue
            for outcome in market.get("outcomes", []) or []:
                name = str(outcome.get("name", ""))
                try:
                    price = float(outcome.get("price"))
                except (TypeError, ValueError):
                    continue
                if price <= 1.0:
                    continue
                if similar(name, home) >= 0.8:
                    k = "home"
                elif similar(name, away) >= 0.8:
                    k = "away"
                elif name.strip().lower() in ("draw", "tie"):
                    k = "draw"
                else:
                    continue
                if k not in outcomes or price > outcomes[k][0]:
                    outcomes[k] = (price, str(book.get("title", "External bookmaker")))
    if not all(k in outcomes for k in ("home", "draw", "away")):
        return None
    return {k: {"price": outcomes[k][0], "bookmaker": outcomes[k][1]} for k in outcomes}

def parse_dt(value: str) -> float | None:
    if not value:
        return None
    try:
        s = value.replace("Z", "+00:00")
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.timestamp()
    except (ValueError, TypeError):
        try:
            v = float(value)
            if v > 10_000_000_000: v /= 1000
            return v
        except (ValueError, TypeError):
            return None

def match_events(sporty: dict[str, Any], foreign: dict[str, Any]) -> bool:
    if similar(sporty["home"], foreign.get("home_team", "")) < 0.82:
        return False
    if similar(sporty["away"], foreign.get("away_team", "")) < 0.82:
        return False
    st = parse_dt(sporty.get("kickoff", ""))
    ft = parse_dt(foreign.get("commence_time", ""))
    if st is not None and ft is not None and abs(st - ft) > env_int("MATCH_TIME_TOLERANCE_MINUTES", 180) * 60:
        return False
    return True

def calculate_arbitrage(odds: dict[str, float], bankroll: float) -> dict[str, Any] | None:
    if bankroll <= 0 or not all(odds.get(k, 0) > 1.0 for k in ("home", "draw", "away")):
        return None
    inv_sum = sum(1.0 / odds[k] for k in ("home", "draw", "away"))
    roi = (1.0 / inv_sum - 1.0) * 100.0
    if inv_sum >= 1.0:
        return None
    stakes = {k: bankroll * (1.0 / odds[k]) / inv_sum for k in odds}
    returns = {k: stakes[k] * odds[k] for k in odds}
    return {"roi_pct": roi, "implied_sum": inv_sum, "stakes": stakes,
            "returns": returns, "min_return": min(returns.values()),
            "profit": min(returns.values()) - bankroll}

def alert_key(home: str, away: str, kickoff: str, odds: dict[str, float]) -> str:
    raw = json.dumps([norm_name(home), norm_name(away), kickoff, odds], sort_keys=True)
    return hashlib.sha256(raw.encode()).hexdigest()

def fmt_money(value: float) -> str:
    return f"₦{value:,.2f}"

def main() -> int:
    bankroll = env_float("BANKROLL_NGN", 20000.0)
    min_roi = env_float("MIN_ARBITRAGE_ROI", 0.5)
    max_roi = env_float("MAX_ARBITRAGE_ROI", 30.0)
    init_db()
    send_telegram("Football-tool scanner started. Read-only mode; no bets are placed.")
    try:
        with httpx.Client(timeout=httpx.Timeout(25.0), follow_redirects=True) as client:
            foreign_events = get_odds_api_events(client)
            sporty_events = get_sportybet_events(client)
    except Exception as exc:
        msg = f"Football-tool scan error: {type(exc).__name__}: {exc}"
        print(msg, file=sys.stderr)
        send_telegram(msg)
        return 1

    candidates = []
    for se in sporty_events:
        for fe in foreign_events:
            if not match_events(se, fe):
                continue
            best = best_foreign_1x2(fe)
            if not best:
                continue
            chosen = {}
            sources = {}
            # For each outcome, choose best price from SportyBet and external bookies.
            for outcome in ("home", "draw", "away"):
                sport_price = se["odds"][outcome]
                foreign_price = best[outcome]["price"]
                if sport_price >= foreign_price:
                    chosen[outcome] = sport_price
                    sources[outcome] = "SportyBet"
                else:
                    chosen[outcome] = foreign_price
                    sources[outcome] = best[outcome]["bookmaker"]
            calc = calculate_arbitrage(chosen, bankroll)
            if calc and min_roi <= calc["roi_pct"] <= max_roi:
                candidates.append((se, chosen, sources, calc))
    sent = 0
    for se, odds, sources, calc in candidates:
        key = alert_key(se["home"], se["away"], se.get("kickoff", ""), odds)
        if already_sent(key):
            continue
        text = (
            f"⚠️ THEORETICAL 1X2 ARBITRAGE — VERIFY BEFORE ACTION\n"
            f"{se['home']} vs {se['away']}\nKickoff: {se.get('kickoff') or 'unknown'}\n"
            f"Home: {odds['home']:.3f} ({sources['home']}) — stake {fmt_money(calc['stakes']['home'])}\n"
            f"Draw: {odds['draw']:.3f} ({sources['draw']}) — stake {fmt_money(calc['stakes']['draw'])}\n"
            f"Away: {odds['away']:.3f} ({sources['away']}) — stake {fmt_money(calc['stakes']['away'])}\n"
            f"Estimated theoretical ROI: {calc['roi_pct']:.2f}%\n"
            f"Bankroll assumed: {fmt_money(bankroll)}; theoretical min return: {fmt_money(calc['min_return'])}; "
            f"gross profit before limits/fees/settlement risk: {fmt_money(calc['profit'])}\n"
            f"Implied probability sum: {calc['implied_sum']*100:.3f}%\n"
            f"Confirm live prices, identical market/settlement rules, stake limits and availability manually."
        )
        if send_telegram(text):
            mark_sent(key); sent += 1
    summary = (f"Football-tool scan complete: SportyBet events={len(sporty_events)}, "
               f"external events={len(foreign_events)}, matched alerts candidates={len(candidates)}, "
               f"new Telegram alerts sent={sent}.")
    print(summary)
    send_telegram(summary)
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
