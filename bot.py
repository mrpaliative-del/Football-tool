
import os
import re
import time
import json
import html
import asyncio
import logging
import sqlite3
import hashlib
from pathlib import Path
from itertools import product
from typing import Optional

import httpx
from playwright.async_api import async_playwright


# ============================================================
# CONFIGURATION
# ============================================================

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s [%(levelname)s] %(message)s",
)

logger = logging.getLogger("multi_match_arbitrage")

TELEGRAM_TOKEN = os.getenv("TELEGRAMBOTTOKEN", "").strip()
TARGET_CHAT_ID = os.getenv("TARGETCHATID", "").strip()

BANKROLL = float(os.getenv("BANKROLLPOOL", "20000"))
MIN_ARBITRAGE_ROI = float(os.getenv("MIN_ARBITRAGE_ROI", "5"))
MAX_ARBITRAGE_ROI = float(os.getenv("MAX_ARBITRAGE_ROI", "50"))

RUN_DURATION = max(60, int(os.getenv("RUN_DURATION_SECONDS", "600")))
POLL_INTERVAL = max(10, int(os.getenv("POLL_INTERVAL_SECONDS", "15")))
PAGE_WAIT_SECONDS = max(1, int(os.getenv("PAGE_WAIT_SECONDS", "5")))

DATABASE_PATH = os.getenv(
    "ARBITRAGE_DB_PATH", "arbitrage_signals.sqlite3"
).strip()

BOOKMAKER_A_URL = os.getenv(
    "BOOKMAKER_A_URL",
    "https://www.sportybet.com/ng/sport/football",
).strip()

BOOKMAKER_B_URL = os.getenv(
    "BOOKMAKER_B_URL",
    "https://www.bet9ja.com/sport/soccer",
).strip()

TELEGRAM_BASE_URL = (
    f"https://api.telegram.org/bot{TELEGRAM_TOKEN}"
    if TELEGRAM_TOKEN
    else ""
)

# Broad selectors only. Actual bookmaker page structures may differ.
MATCH_SELECTORS = [
    ".match-row",
    ".af-match",
    "tr.match",
    "[data-testid*='event']",
    "[class*='match-row']",
    "[class*='event-row']",
    "article",
]

ODDS_PATTERN = re.compile(
    r"(?<![\w.])([1-9]\d?(?:[.,]\d{1,3})?)(?![\w.])"
)

OUTCOME_ALIASES = {
    "1": "home",
    "home": "home",
    "x": "draw",
    "draw": "draw",
    "tie": "draw",
    "2": "away",
    "away": "away",
}


# ============================================================
# DATABASE
# ============================================================

def initialize_database():
    path = Path(DATABASE_PATH)
    if str(path.parent) not in ("", "."):
        path.parent.mkdir(parents=True, exist_ok=True)

    with sqlite3.connect(DATABASE_PATH, timeout=30) as db:
        db.execute("PRAGMA journal_mode=WAL")
        db.execute("""
            CREATE TABLE IF NOT EXISTS sent_signals (
                signal_id TEXT PRIMARY KEY,
                fixture TEXT NOT NULL,
                roi REAL NOT NULL,
                sent_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
        """)
        db.commit()


def signal_already_sent(signal_id: str) -> bool:
    with sqlite3.connect(DATABASE_PATH, timeout=30) as db:
        return db.execute(
            "SELECT 1 FROM sent_signals WHERE signal_id = ?",
            (signal_id,),
        ).fetchone() is not None


def record_signal(signal_id: str, fixture: str, roi: float):
    with sqlite3.connect(DATABASE_PATH, timeout=30) as db:
        db.execute(
            """
            INSERT OR IGNORE INTO sent_signals
            (signal_id, fixture, roi)
            VALUES (?, ?, ?)
            """,
            (signal_id, fixture, roi),
        )
        db.commit()


# ============================================================
# UTILITIES
# ============================================================

def clean_text(value: str) -> str:
    return re.sub(r"\s+", " ", value or "").strip()


def escape_html(value) -> str:
    return html.escape(str(value), quote=True)


def normalize_team_name(value: str) -> str:
    value = clean_text(value).casefold()
    value = re.sub(r"\b(fc|sc|afc|cf|club)\b", "", value)
    return re.sub(r"[^a-z0-9]", "", value)


def fixture_key(home: str, away: str) -> str:
    """
    Preserve home/away order. Do not treat reversed fixtures as identical.
    """
    return f"{normalize_team_name(home)}__{normalize_team_name(away)}"


def parse_odds(value: str) -> Optional[float]:
    try:
        odds = float(value.replace(",", "."))
        if 1.01 <= odds <= 100:
            return odds
    except (TypeError, ValueError):
        pass

    return None


def round_stake(value: float) -> int:
    """Round to the nearest ₦100, allowing zero if the stake is too small."""
    return max(0, int(round(value / 100.0) * 100))


# ============================================================
# TELEGRAM
# ============================================================

async def send_telegram(
    client: httpx.AsyncClient,
    message: str,
) -> bool:
    if not TELEGRAM_BASE_URL or not TARGET_CHAT_ID:
        logger.error("Telegram credentials are missing.")
        return False

    try:
        response = await client.post(
            f"{TELEGRAM_BASE_URL}/sendMessage",
            json={
                "chat_id": TARGET_CHAT_ID,
                "text": message,
                "parse_mode": "HTML",
                "disable_web_page_preview": True,
            },
            timeout=15,
        )
        response.raise_for_status()

        result = response.json()
        return result.get("ok") is True

    except (httpx.HTTPError, ValueError) as exc:
        logger.error("Telegram delivery failed: %s", type(exc).__name__)
        return False


# ============================================================
# PAGE SCRAPING
# ============================================================

class MultiMatchArbitrageEngine:
    def __init__(self):
        self.bankroll = BANKROLL

        # Cache only within this process; the SQLite database tracks
        # signals successfully sent in previous runs when storage persists.
        self.processed = set()

    def validate_environment(self) -> bool:
        if not TELEGRAM_TOKEN or not TARGET_CHAT_ID:
            logger.critical(
                "TELEGRAMBOTTOKEN and TARGETCHATID are required."
            )
            return False

        if self.bankroll <= 0:
            logger.critical("BANKROLLPOOL must be greater than zero.")
            return False

        if not (
            0 < MIN_ARBITRAGE_ROI < MAX_ARBITRAGE_ROI <= 100
        ):
            logger.critical("Invalid arbitrage ROI thresholds.")
            return False

        for url in (BOOKMAKER_A_URL, BOOKMAKER_B_URL):
            if not url.startswith("https://"):
                logger.critical("Bookmaker URLs must use HTTPS.")
                return False

        return True

    async def extract_match_elements(self, page):
        """
        Try known structural selectors. A generic selector is not proof
        that an element represents one complete fixture.
        """
        for selector in MATCH_SELECTORS:
            try:
                locator = page.locator(selector)
                count = await locator.count()

                if 1 <= count <= 2000:
                    elements = []
                    for index in range(count):
                        elements.append(locator.nth(index))
                    logger.info(
                        "Using selector %s (%d elements)",
                        selector,
                        count,
                    )
                    return elements
            except Exception:
                continue

        return []

    async def scrape_bookmaker(
        self,
        page,
        url: str,
        bookmaker_name: str,
    ) -> dict:
        """
        Expected normalized output:
        {
          fixture_key: {
            "home": "...",
            "away": "...",
            "outcomes": {
              "home": 2.10,
              "draw": 3.20,
              "away": 3.50
            }
          }
        }

        This conservative parser accepts only labelled 1X2 outcomes.
        It deliberately does not infer outcomes from arbitrary numbers.
        """
        results = {}

        try:
            await page.goto(
                url,
                timeout=60000,
                wait_until="domcontentloaded",
            )
            await asyncio.sleep(PAGE_WAIT_SECONDS)

            try:
                await page.evaluate(
                    "window.scrollTo(0, document.body.scrollHeight)"
                )
                await asyncio.sleep(1)
            except Exception:
                pass

            elements = await self.extract_match_elements(page)

            for element in elements:
                try:
                    text = await element.inner_text(timeout=3000)
                    lines = [
                        clean_text(line)
                        for line in text.splitlines()
                        if clean_text(line)
                    ]

                    parsed = self.parse_match_lines(lines)
                    if not parsed:
                        continue

                    home = parsed["home"]
                    away = parsed["away"]
                    outcomes = parsed["outcomes"]

                    if len(outcomes) != 3:
                        continue

                    key = fixture_key(home, away)
                    if len(key) < 5:
                        continue

                    # Do not overwrite a complete fixture with an
                    # incomplete duplicate element.
                    results[key] = {
                        "home": home,
                        "away": away,
                        "outcomes": outcomes,
                        "bookmaker": bookmaker_name,
                        "source_url": url,
                        "observed_at": time.time(),
                    }

                except Exception:
                    continue

        except Exception as exc:
            logger.error(
                "%s page scan failed: %s",
                bookmaker_name,
                type(exc).__name__,
            )

        logger.info(
            "%s: parsed %d candidate fixtures",
            bookmaker_name,
            len(results),
        )
        return results

    def parse_match_lines(self, lines: list) -> Optional[dict]:
        """
        Conservative text parser.

        Recognizes explicit labels such as:
          Team A
          Team B
          1  2.10
          X  3.20
          2  3.50

        Bookmaker pages may use different DOM and label layouts.
        If a row does not expose unambiguous labels, it is skipped.
        """
        if len(lines) < 5:
            return None

        # Avoid mistaking a large container holding multiple matches
        # for a single fixture.
        if len(lines) > 35:
            return None

        home = lines[0]
        away = lines[1]

        if len(home) < 2 or len(away) < 2 or home == away:
            return None

        outcomes = {}
        i = 0

        while i < len(lines) - 1:
            label = lines[i].strip().casefold()
            outcome = OUTCOME_ALIASES.get(label)

            if outcome:
                # Label and odds on the same line, e.g. "1 2.10".
                same_line = re.fullmatch(
                    r"(1|x|2|home|draw|tie|away)\s*[:\-]?\s*"
                    r"(\d+(?:[.,]\d{1,3})?)",
                    label,
                    re.IGNORECASE,
                )

                if same_line:
                    odd = parse_odds(same_line.group(2))
                    if odd:
                        outcomes[outcome] = odd
                else:
                    odd = parse_odds(lines[i + 1])
                    if odd:
                        outcomes[outcome] = odd
                    i += 1

            else:
                # Also recognize labels and prices embedded together.
                match = re.fullmatch(
                    r"(1|x|2|home|draw|tie|away)\s*[:\-]?\s*"
                    r"(\d+(?:[.,]\d{1,3})?)",
                    lines[i],
                    re.IGNORECASE,
                )
                if match:
                    outcome = OUTCOME_ALIASES[match.group(1).casefold()]
                    odd = parse_odds(match.group(2))
                    if odd:
                        outcomes[outcome] = odd

            i += 1

        if set(outcomes) != {"home", "draw", "away"}:
            return None

        return {
            "home": home,
            "away": away,
            "outcomes": outcomes,
        }

    # ========================================================
    # ARBITRAGE CALCULATION
    # ========================================================

    def calculate_arbitrage(
        self,
        fixture_a: dict,
        fixture_b: dict,
    ) -> Optional[dict]:
        """
        Combine the best price for each outcome across two bookmakers.

        For a complete 1X2 market:
          S = 1 / best_home + 1 / best_draw + 1 / best_away

        A theoretical arbitrage exists when S < 1.
        This assumes the outcomes are mutually exclusive and exhaustive,
        the market rules match, and all quoted odds remain available.
        """
        best = {}

        for outcome in ("home", "draw", "away"):
            candidates = [
                (
                    fixture_a["outcomes"][outcome],
                    fixture_a["bookmaker"],
                ),
                (
                    fixture_b["outcomes"][outcome],
                    fixture_b["bookmaker"],
                ),
            ]

            best[outcome] = max(candidates, key=lambda item: item[0])

        implied_sum = sum(
            1.0 / best[outcome][0]
            for outcome in ("home", "draw", "away")
        )

        if implied_sum <= 0:
            return None

        roi = ((1.0 / implied_sum) - 1.0) * 100.0

        if not (MIN_ARBITRAGE_ROI <= roi <= MAX_ARBITRAGE_ROI):
            return None

        stakes = {}
        for outcome in ("home", "draw", "away"):
            odds = best[outcome][0]
            raw_stake = self.bankroll * (1.0 / odds) / implied_sum
            stakes[outcome] = round_stake(raw_stake)

        # Rounding can create small differences. Calculate the actual
        # total stake and estimated return using the rounded amounts.
        total_stake = sum(stakes.values())
        if total_stake <= 0:
            return None

        returns = [
            stakes[outcome] * best[outcome][0]
            for outcome in ("home", "draw", "away")
        ]

        minimum_return = min(returns)
        estimated_profit = minimum_return - total_stake
        actual_roi = (estimated_profit / total_stake) * 100.0

        return {
            "best": best,
            "implied_sum": implied_sum,
            "theoretical_roi": roi,
            "stakes": stakes,
            "total_stake": total_stake,
            "minimum_return": minimum_return,
            "estimated_profit": estimated_profit,
            "estimated_roi_after_rounding": actual_roi,
        }

    # ========================================================
    # SCAN AND ALERT
    # ========================================================

    async def execute_engine(self):
        logger.info("Multi-match arbitrage engine initialized.")

        if not self.validate_environment():
            return

        initialize_database()
        start_time = time.monotonic()
        scan_count = 0

        async with async_playwright() as p:
            browser = await p.chromium.launch(
                headless=True,
                args=[
                    "--no-sandbox",
                    "--disable-dev-shm-usage",
                ],
            )

            try:
                context = await browser.new_context(
                    viewport={"width": 1440, "height": 900},
                )

                try:
                    page_a = await context.new_page()
                    page_b = await context.new_page()

                    async with httpx.AsyncClient(timeout=15) as client:
                        await send_telegram(
                            client,
                            "⚽ <b>Multi-Match Arbitrage Engine Started</b>\n"
                            "Scanning for candidate 1X2 arbitrage signals.\n"
                            "Signals are estimates, not guaranteed profit.",
                        )

                        while (
                            time.monotonic() - start_time < RUN_DURATION
                        ):
                            scan_count += 1
                            logger.info(
                                "Scan cycle #%d",
                                scan_count,
                            )

                            odds_a, odds_b = await asyncio.gather(
                                self.scrape_bookmaker(
                                    page_a,
                                    BOOKMAKER_A_URL,
                                    "SportyBet",
                                ),
                                self.scrape_bookmaker(
                                    page_b,
                                    BOOKMAKER_B_URL,
                                    "Bet9ja",
                                ),
                            )

                            common = set(odds_a) & set(odds_b)
                            logger.info(
                                "Common normalized fixture keys: %d",
                                len(common),
                            )

                            for key in common:
                                a = odds_a[key]
                                b = odds_b[key]

                                # Do not assume fixture-key equality proves
                                # the underlying event and market are equal.
                                if (
                                    normalize_team_name(a["home"])
                                    != normalize_team_name(b["home"])
                                    or normalize_team_name(a["away"])
                                    != normalize_team_name(b["away"])
                                ):
                                    continue

                                opportunity = self.calculate_arbitrage(a, b)
                                if not opportunity:
                                    continue

                                best = opportunity["best"]

                                signal_data = {
                                    "fixture": key,
                                    "prices": {
                                        k: [best[k][0], best[k][1]]
                                        for k in best
                                    },
                                }
                                signal_id = hashlib.sha256(
                                    json.dumps(
                                        signal_data,
                                        sort_keys=True,
                                    ).encode()
                                ).hexdigest()

                                if signal_id in self.processed:
                                    continue

                                if signal_already_sent(signal_id):
                                    self.processed.add(signal_id)
                                    continue

                                title = (
                                    f"{a['home']} vs {a['away']}"
                                )

                                lines = [
                                    "⚠️ <b>CANDIDATE 1X2 ARBITRAGE</b>",
                                    "────────────────────────",
                                    f"🏆 <b>Fixture:</b> {escape_html(title)}",
                                    (
                                        "📊 <b>Theoretical ROI:</b> "
                                        f"{opportunity['theoretical_roi']:.2f}%"
                                    ),
                                    (
                                        "💵 <b>Estimated ROI after rounding:</b> "
                                        f"{opportunity['estimated_roi_after_rounding']:.2f}%"
                                    ),
                                    "",
                                ]

                                outcome_names = {
                                    "home": "Home win",
                                    "draw": "Draw",
                                    "away": "Away win",
                                }

                                for outcome in ("home", "draw", "away"):
                                    odds, bookmaker = best[outcome]
                                    stake = opportunity["stakes"][outcome]
                                    lines.append(
                                        f"• <b>{outcome_names[outcome]}:</b> "
                                        f"{odds:.3f} @ {escape_html(bookmaker)} "
                                        f"| Stake ₦{stake:,}"
                                    )

                                lines.extend([
                                    "",
                                    (
                                        "💰 <b>Total allocated:</b> "
                                        f"₦{opportunity['total_stake']:,}"
                                    ),
                                    (
                                        "📉 <b>Estimated minimum return:</b> "
                                        f"₦{opportunity['minimum_return']:,.2f}"
                                    ),
                                    (
                                        "🧮 <b>Estimated profit:</b> "
                                        f"₦{opportunity['estimated_profit']:,.2f}"
                                    ),
                                    "",
                                    "⚠️ Verify all prices, event identity,",
                                    "market rules, limits and availability",
                                    "before acting. Odds can change.",
                                ])

                                delivered = await send_telegram(
                                    client,
                                    "\n".join(lines),
                                )

                                if delivered:
                                    record_signal(
                                        signal_id,
                                        title,
                                        opportunity["theoretical_roi"],
                                    )
                                    self.processed.add(signal_id)
                                    await asyncio.sleep(1)

                            await asyncio.sleep(POLL_INTERVAL)

                        await send_telegram(
                            client,
                            "🏁 <b>Scan session complete</b>\n"
                            f"Cycles processed: {scan_count}",
                        )

                finally:
                    await context.close()

            finally:
                await browser.close()


if __name__ == "__main__":
    try:
        asyncio.run(MultiMatchArbitrageEngine().execute_engine())
    except KeyboardInterrupt:
        logger.info("Engine stopped by user.")
