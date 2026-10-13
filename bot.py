import os
import asyncio
import logging
import math
import httpx
from playwright.async_api import async_playwright

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S"
)

class LiveArbitrageEngine:
    def __init__(self):
        self.token = os.getenv("TELEGRAMBOTTOKEN")
        self.chatid = os.getenv("TARGETCHATID")
        self.baseurl = f"https://api.telegram.org/bot{self.token}" if self.token else ""
        self.bankroll = float(os.getenv("BANKROLLPOOL", "20000"))
        
        # Live target endpoints
        self.bookmaker_a_url = os.getenv("BOOKMAKER_A_URL", "https://www.sportybet.com/ng/sport/football/live")
        self.bookmaker_b_url = os.getenv("BOOKMAKER_B_URL", "https://www.bet9ja.com/sport/live")
        self.sentcache = set()

    def validateenvironment(self) -> bool:
        if not self.token or not self.chatid:
            logging.critical("❌ ERROR: TELEGRAMBOTTOKEN or TARGETCHATID missing from environment variables.")
            return False
        return True

    def round_to_nearest(self, n: int, base: int = 100) -> int:
        """Rounds stakes to natural blocks to prevent risk-flagging patterns."""
        return max(base, base * round(n / base))

    async def sendtelegramalert(self, client: httpx.AsyncClient, arb_data: dict) -> bool:
        url = f"{self.baseurl}/sendMessage"
        
        raw_stake_a = self.bankroll * arb_data['stake_ratio_a']
        raw_stake_b = self.bankroll * arb_data['stake_ratio_b']
        stake_a = self.round_to_nearest(int(raw_stake_a))
        stake_b = self.round_to_nearest(int(raw_stake_b))
        
        payload = {
            "chat_id": str(self.chatid),
            "text": (
                f"⚖️ <b>LIVE ARBITRAGE SIGNAL DETECTED</b> ⚖️\n"
                f"────────────────────\n"
                f"🏆 <b>Fixture Matchup:</b> {arb_data['fixture']}\n"
                f"📈 <b>Bookmaker A Odds:</b> {arb_data['odds_a']} | <b>Stake:</b> ₦{stake_a}\n"
                f"📈 <b>Bookmaker B Odds:</b> {arb_data['odds_b']} | <b>Stake:</b> ₦{stake_b}\n"
                f"💰 <b>Guaranteed ROI Margin:</b> +{arb_data['roi']}%\n"
                f"────────────────────\n"
                f"⚡ <i>Autonomous Multi-Bookmaker Engine</i>"
            ),
            "parse_mode": "HTML",
            "disable_web_page_preview": True
        }
        try:
            response = await client.post(url, json=payload, timeout=10.0)
            if response.status_code == 200:
                logging.info("✅ Live arbitrage signal successfully dispatched to Telegram.")
                return True
        except httpx.RequestException as e:
            logging.error(f"❌ Telegram Dispatch Error: {e}")
        return False

    async def scrape_bookmaker_live_feed(self, page, url: str, bookmaker_name: str) -> dict:
        odds_map = {}
        try:
            logging.info(f"🌐 Connecting to {bookmaker_name} live feed: {url}")
            await page.goto(url, timeout=40000, wait_until="domcontentloaded")
            
            # Allow dynamic JavaScript payload rendering and odds streaming sockets to populate
            await asyncio.sleep(8.0)
            
            # Scroll down slightly to trigger lazy-loaded live fixture components
            await page.evaluate("window.scrollBy(0, 600)")
            await asyncio.sleep(3.0)

            # Universal multi-selector fallback strategy for live market rows
            selectors = [
                ".match-row", ".af-match", "tr.match", 
                "div[class*='match-item']", "div[class*='live-event']", 
                "div[class*='event-row']"
            ]
            
            elements = []
            for sel in selectors:
                elements = await page.locator(sel).all()
                if len(elements) > 0:
                    logging.info(f"Found {len(elements)} live nodes on {bookmaker_name} using selector: {sel}")
                    break

            if len(elements) == 0:
                # Fallback: grab all structural blocks containing text if standard selectors fail
                elements = await page.locator("article, section, div[class*='event']").all()

            for elem in elements[:35]:
                try:
                    text = await elem.inner_text()
                    lines = [l.strip() for l in text.split("\n") if l.strip()]
                    
                    if len(lines) < 3:
                        continue

                    # Construct normalized match key from team names (lines 0 and 1 typically)
                    team_one = lines[0].lower()
                    team_two = lines[1].lower()
                    fixture_key = "".join(filter(str.isalnum, f"{team_one}-{team_two}"))[:35]
                    
                    if len(fixture_key) < 6:
                        continue

                    # Scan remaining text lines for valid decimal odds (typically > 1.05 and < 50.0)
                    valid_odds = []
                    for line in lines[2:]:
                        # Clean potential formatting strings
                        clean_line = line.replace(",", ".")
                        try:
                            val = float(clean_line)
                            if 1.05 <= val <= 50.0:
                                valid_odds.append(val)
                        except ValueError:
                            continue

                    # If we successfully isolated odds for the fixture, store the highest/primary value
                    if valid_odds:
                        odds_map[fixture_key] = max(valid_odds)

                except Exception:
                    continue

        except Exception as e:
            logging.error(f"❌ Error scraping feed for {bookmaker_name}: {e}")
            
        logging.info(f"📊 Successfully indexed {len(odds_map)} live fixtures from {bookmaker_name}.")
        return odds_map

    async def executeengine(self):
        logging.info("🚀 Autonomous Live Arbitrage Engine Initialized.")
        if not self.validateenvironment():
            return

        async with async_playwright() as p:
            # Launch Chromium with anti-detection fingerprint evasion arguments
            browser = await p.chromium.launch(
                headless=True,
                args=[
                    "--no-sandbox",
                    "--disable-dev-shm-usage",
                    "--disable-blink-features=AutomationControlled",
                    "--disable-infobars",
                    "--start-maximized"
                ]
            )
            
            context = await browser.new_context(
                user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
                viewport={"width": 1440, "height": 900},
                device_scale_factor=1,
            )
            
            # Hide webdriver signatures from site scripts
            await context.add_init_script("Object.defineProperty(navigator, 'webdriver', {get: () => undefined})")
            page = await context.new_page()

            # Scrape Bookmaker A Live Feed
            odds_a = await self.scrape_bookmaker_live_feed(page, self.bookmaker_a_url, "Bookmaker A")
            
            # Scrape Bookmaker B Live Feed
            odds_b = await self.scrape_bookmaker_live_feed(page, self.bookmaker_b_url, "Bookmaker B")

            async with httpx.AsyncClient() as client:
                common_fixtures = set(odds_a.keys()).intersection(set(odds_b.keys()))
                logging.info(f"🔍 Cross-matched {len(common_fixtures)} overlapping live fixtures across bookmakers.")

                for fix in common_fixtures:
                    if fix in self.sentcache:
                        continue
                    
                    o_a = odds_a[fix]
                    o_b = odds_b[fix]

                    # Core Arbitrage Formula: Sum of inverse odds < 0.99 (Guaranteed Profit Margin)
                    implied_probability_sum = (1.0 / o_a) + (1.0 / o_b)
                    
                    if implied_probability_sum < 0.99:
                        roi = round((1.0 - implied_probability_sum) * 100, 2)
                        self.sentcache.add(fix)
                        
                        arb_data = {
                            "fixture": fix,
                            "odds_a": o_a,
                            "odds_b": o_b,
                            "roi": roi,
                            "stake_ratio_a": (1.0 / o_a) / implied_probability_sum,
                            "stake_ratio_b": (1.0 / o_b) / implied_probability_sum
                        }
                        
                        logging.info(f"🎯 Live Arbitrage Opportunity Confirmed on {fix}! Margin ROI: +{roi}%")
                        await self.sendtelegramalert(client, arb_data)

            await browser.close()
            logging.info("🏁 Live Arbitrage Execution Cycle Completed Successfully.")

if __name__ == "__main__":
    asyncio.run(LiveArbitrageEngine().executeengine())
