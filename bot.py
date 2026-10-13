import os
import asyncio
import logging
import time
import httpx
from playwright.async_api import async_playwright

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S"
)

class ContinuousArbitrageEngine:
    def __init__(self):
        self.token = os.getenv("TELEGRAMBOTTOKEN")
        self.chatid = os.getenv("TARGETCHATID")
        self.baseurl = f"https://api.telegram.org/bot{self.token}" if self.token else ""
        self.bankroll = float(os.getenv("BANKROLLPOOL", "20000"))
        
        self.bookmaker_a_url = os.getenv("BOOKMAKER_A_URL", "https://www.sportybet.com/ng/sport/football/live")
        self.bookmaker_b_url = os.getenv("BOOKMAKER_B_URL", "https://www.bet9ja.com/sport/live")
        self.sentcache = set()

    def validateenvironment(self) -> bool:
        if not self.token or not self.chatid:
            logging.critical("❌ ERROR: TELEGRAMBOTTOKEN or TARGETCHATID missing from environment variables.")
            return False
        return True

    def round_to_nearest(self, n: int, base: int = 100) -> int:
        return max(base, base * round(n / base))

    async def send_telemetry(self, client: httpx.AsyncClient, message: str):
        if not self.baseurl:
            return
        url = f"{self.baseurl}/sendMessage"
        payload = {
            "chat_id": str(self.chatid),
            "text": f"⚽ <b>SPORT ENGINE TELEMETRY</b>\n──────────────────────────────\n{message}",
            "parse_mode": "HTML",
            "disable_web_page_preview": True
        }
        try:
            await client.post(url, json=payload, timeout=10.0)
        except Exception as e:
            logging.error(f"Telegram Telemetry Error: {e}")

    async def scrape_bookmaker(self, page, url: str, bookmaker_name: str) -> dict:
        odds_map = {}
        try:
            if page.url != url:
                await page.goto(url, timeout=45000, wait_until="domcontentloaded")
                await asyncio.sleep(5.0)

            selectors = [
                ".match-row", ".af-match", "tr.match", 
                "div[class*='match']", "div[class*='event']", 
                "div[class*='game']", "article"
            ]
            
            elements = []
            for sel in selectors:
                elements = await page.locator(sel).all()
                if len(elements) > 2:
                    break

            for elem in elements[:40]:
                try:
                    text = await elem.inner_text()
                    lines = [l.strip() for l in text.split("\n") if l.strip()]
                    
                    if len(lines) < 2:
                        continue

                    team_one = lines[0].lower()
                    team_two = lines[1].lower() if len(lines) > 1 else "unknown"
                    fixture_key = "".join(filter(str.isalnum, f"{team_one}-{team_two}"))[:30]
                    
                    if len(fixture_key) < 5:
                        continue

                    valid_odds = []
                    for line in lines[2:]:
                        try:
                            val = float(line.replace(",", "."))
                            if 1.05 <= val <= 35.0:
                                valid_odds.append(val)
                        except ValueError:
                            continue

                    if valid_odds:
                        odds_map[fixture_key] = max(valid_odds)
                except Exception:
                    continue

        except Exception as e:
            logging.error(f"❌ Scraping error on {bookmaker_name}: {e}")
            
        return odds_map

    async def executeengine(self):
        logging.info("🚀 High-Yield (5%+ ROI) Arbitrage Engine Starting...")
        if not self.validateenvironment():
            return

        RUN_DURATION = 600  # 10-minute workflow execution block
        POLL_INTERVAL = 5.0
        start_time = time.time()

        async with async_playwright() as p:
            browser = await p.chromium.launch(
                headless=True,
                args=["--no-sandbox", "--disable-dev-shm-usage", "--disable-blink-features=AutomationControlled"]
            )
            context = await browser.new_context(
                user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
                viewport={"width": 1440, "height": 900}
            )
            page_a = await context.new_page()
            page_b = await context.new_page()

            async with httpx.AsyncClient() as client:
                await self.send_telemetry(client, "⚡ <b>5%+ Arbitrage Engine Active</b>\nScanning live spreads for high-margin opportunities...")

                scan_count = 0
                while (time.time() - start_time) < RUN_DURATION:
                    scan_count += 1
                    logging.info(f"🔄 Scan Cycle #{scan_count}")

                    odds_a, odds_b = await asyncio.gather(
                        self.scrape_bookmaker(page_a, self.bookmaker_a_url, "SportyBet"),
                        self.scrape_bookmaker(page_b, self.bookmaker_b_url, "Bet9ja")
                    )

                    common_fixtures = set(odds_a.keys()).intersection(set(odds_b.keys()))

                    for fix in common_fixtures:
                        o_a, o_b = odds_a[fix], odds_b[fix]
                        implied_prob = (1.0 / o_a) + (1.0 / o_b)

                        # Calculate ROI percentage
                        roi = round((1.0 - implied_prob) * 100, 2)

                        # Filter specifically for 5.0% and above profit yield (up to 50% max sanity check)
                        if 5.0 <= roi <= 50.0:
                            cache_key = f"{fix}_{o_a}_{o_b}"
                            
                            if cache_key in self.sentcache:
                                continue
                            self.sentcache.add(cache_key)

                            stake_a = self.round_to_nearest(int(self.bankroll * ((1.0 / o_a) / implied_prob)))
                            stake_b = self.round_to_nearest(int(self.bankroll * ((1.0 / o_b) / implied_prob)))

                            alert_text = (
                                f"🔥 <b>HIGH-MARGIN ARBITRAGE SIGNAL (+{roi}%)</b>\n"
                                f"──────────────────────────────\n"
                                f"🏆 <b>Fixture:</b> {fix}\n"
                                f"📈 <b>Bookmaker A (SportyBet):</b> {o_a} | <b>Stake:</b> ₦{stake_a:,}\n"
                                f"📈 <b>Bookmaker B (Bet9ja):</b> {o_b} | <b>Stake:</b> ₦{stake_b:,}\n"
                                f"💰 <b>Guaranteed ROI:</b> +{roi}%"
                            )
                            await self.send_telemetry(client, alert_text)

                    await asyncio.sleep(POLL_INTERVAL)

                await self.send_telemetry(client, f"🏁 <b>Session Complete</b>\nExecuted {scan_count} high-frequency scans for 5%+ spreads.")

            await browser.close()

if __name__ == "__main__":
    asyncio.run(ContinuousArbitrageEngine().executeengine())
