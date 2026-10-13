import os
import asyncio
import logging
import httpx
from playwright.async_api import async_playwright

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S"
)

class DiagnosticArbitrageEngine:
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
        """Sends status telemetry to your Telegram monitor."""
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
            logging.info(f"🌐 Connecting to {bookmaker_name}: {url}")
            await page.goto(url, timeout=45000, wait_until="domcontentloaded")
            await asyncio.sleep(10.0)  # Allow sockets & JS to load
            
            # Scroll to trigger dynamic elements
            await page.evaluate("window.scrollBy(0, 800)")
            await asyncio.sleep(3.0)

            # Save diagnostic snapshot for debugging if selectors fail
            os.makedirs("debug_artifacts", exist_ok=True)
            await page.screenshot(path=f"debug_artifacts/{bookmaker_name.lower().replace(' ', '_')}.png")

            # Broad multi-selector search for match rows
            selectors = [
                ".match-row", ".af-match", "tr.match", 
                "div[class*='match']", "div[class*='event']", 
                "div[class*='game']", "article"
            ]
            
            elements = []
            for sel in selectors:
                elements = await page.locator(sel).all()
                if len(elements) > 2:
                    logging.info(f"Found {len(elements)} nodes on {bookmaker_name} using selector: {sel}")
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
                            if 1.05 <= val <= 40.0:
                                valid_odds.append(val)
                        except ValueError:
                            continue

                    if valid_odds:
                        odds_map[fixture_key] = max(valid_odds)
                except Exception:
                    continue

        except Exception as e:
            logging.error(f"❌ Scraping error on {bookmaker_name}: {e}")
            
        logging.info(f"📊 Extracted {len(odds_map)} valid fixtures from {bookmaker_name}.")
        return odds_map

    async def executeengine(self):
        logging.info("🚀 Diagnostic Sport Arbitrage Engine Initialized.")
        if not self.validateenvironment():
            return

        async with async_playwright() as p:
            browser = await p.chromium.launch(
                headless=True,
                args=[
                    "--no-sandbox",
                    "--disable-dev-shm-usage",
                    "--disable-blink-features=AutomationControlled"
                ]
            )
            context = await browser.new_context(
                user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
                viewport={"width": 1440, "height": 900}
            )
            await context.add_init_script("Object.defineProperty(navigator, 'webdriver', {get: () => undefined})")
            page = await context.new_page()

            async with httpx.AsyncClient() as client:
                odds_a = await self.scrape_bookmaker(page, self.bookmaker_a_url, "Bookmaker A")
                odds_b = await self.scrape_bookmaker(page, self.bookmaker_b_url, "Bookmaker B")

                common_fixtures = set(odds_a.keys()).intersection(set(odds_b.keys()))
                logging.info(f"🔍 Cross-matched {len(common_fixtures)} live common fixtures.")

                if len(odds_a) == 0 or len(odds_b) == 0:
                    await self.send_telemetry(client, "⚠️ <b>Warning:</b> Sport engine captured 0 odds. Bookmaker DOM structures or anti-bot firewalls may have changed.")

                signal_found = False
                for fix in common_fixtures:
                    o_a = odds_a[fix]
                    o_b = odds_b[fix]

                    implied_probability_sum = (1.0 / o_a) + (1.0 / o_b)
                    
                    if implied_probability_sum < 0.99:
                        signal_found = True
                        roi = round((1.0 - implied_probability_sum) * 100, 2)
                        
                        if fix in self.sentcache:
                            continue
                        self.sentcache.add(fix)
                        
                        stake_a = self.round_to_nearest(int(self.bankroll * ((1.0 / o_a) / implied_probability_sum)))
                        stake_b = self.round_to_nearest(int(self.bankroll * ((1.0 / o_b) / implied_probability_sum)))

                        alert_text = (
                            f"⚖️ <b>ARBITRAGE OPPORTUNITY DETECTED</b>\n"
                            f"──────────────────────────────\n"
                            f"🏆 <b>Fixture:</b> {fix}\n"
                            f"📈 <b>Odds A:</b> {o_a} | <b>Stake:</b> ₦{stake_a}\n"
                            f"📈 <b>Odds B:</b> {o_b} | <b>Stake:</b> ₦{stake_b}\n"
                            f"💰 <b>Guaranteed ROI:</b> +{roi}%"
                        )
                        await self.send_telemetry(client, alert_text)

                if not signal_found:
                    logging.info("ℹ️ Scan complete. No profitable arbitrage spreads (< 0.99) found in current live feed.")

            await browser.close()

if __name__ == "__main__":
    asyncio.run(DiagnosticArbitrageEngine().executeengine())
