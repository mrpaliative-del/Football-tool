import os
import asyncio
import logging
import random
from playwright.async_api import async_playwright
import httpx

# ─── LOGGING CONFIGURATION ───
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S"
)

class PlaywrightArbitrageEngine:
    def __init__(self):
        # Strictly alphabetic environment keys
        self.token = os.getenv("TELEGRAMBOTTOKEN")
        self.chatid = os.getenv("TARGETCHATID")
        self.baseurl = f"https://api.telegram.org/bot{self.token}" if self.token else ""
        
        # Dynamic Parameters
        self.bankroll = float(os.getenv("BANKROLLPOOL", 20000))
        self.minroi = float(os.getenv("MINROITHRESHOLD", 0.0))
        
        self.targeturl = "https://www.sportybet.com/ng/sport/football/live_list"
        self.sentcache = set()

    def validateenvironment(self) -> bool:
        """Verifies that mandatory GitHub Secrets exist."""
        if not self.token or not self.chatid:
            logging.critical("❌ CRITICAL ERROR: TELEGRAMBOTTOKEN or TARGETCHATID is missing in GitHub Secrets!")
            return False
        return True

    async def sendtelegramalert(self, client: httpx.AsyncClient, message: str) -> bool:
        """Asynchronously dispatches HTML formatted alerts to Telegram."""
        url = f"{self.baseurl}/sendMessage"
        payload = {
            "chat_id": str(self.chatid),
            "text": message,
            "parse_mode": "HTML"
        }
        try:
            response = await client.post(url, json=payload, timeout=10.0)
            if response.status_code == 200:
                logging.info("✅ Telegram notification successfully dispatched.")
                return True
            else:
                logging.error(f"❌ Telegram API rejected payload [{response.status_code}]: {response.text}")
        except httpx.RequestException as e:
            logging.error(f"❌ Network exception during Telegram dispatch: {e}")
        return False

    def generatebookingcode(self) -> str:
        """Generates a secure 6-character alphanumeric booking code."""
        chars = "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"
        return "".join(random.choices(chars, k=6))

    async def scrapeandcalculatespreads(self, page) -> dict:
        """Loads live SportyBet elements via headless browser, extracts odds, and computes arbitrage metrics."""
        try:
            # Navigate to live football list with stealth mobile viewport
            await page.goto(self.targeturl, timeout=40000, wait_until="domcontentloaded")
            
            # Allow time for live match cards to populate via JavaScript rendering
            await asyncio.sleep(6.0)

            # Extract active match blocks from the DOM layout
            match_elements = await page.locator(".match-card, .live-event-row, .events-list-item").all()
            
            if not match_elements:
                # Fallback selector targeting general live container cells if classes shift
                match_elements = await page.locator("tr, div[class*='event']").all()

            for match in match_elements[:15]: # Scan top active rows
                try:
                    text_content = await match.inner_text()
                    lines = [line.strip() for line in text_content.split("\n") if line.strip()]
                    
                    if len(lines) < 5:
                        continue
                        
                    matchname = f"{lines[0]} vs {lines[1]}"
                    matchid = "".join(filter(str.isalnum, matchname))

                    if matchid in self.sentcache:
                        continue

                    # Look for decimal odds patterns inside the row text
                    potential_odds = []
                    for token in lines:
                        try:
                            val = float(token)
                            if 1.05 <= val <= 15.0: # Valid decimal betting odds range
                                potential_odds.append(val)
                        except ValueError:
                            continue

                    if len(potential_odds) >= 2:
                        oddsa = potential_odds[0]
                        oddsb = potential_odds[1]

                        totalprob = (1.0 / oddsa) + (1.0 / oddsb)
                        roi = ((1.0 / totalprob) - 1.0) * 100

                        if roi >= self.minroi:
                            rawstakea = ((1.0 / oddsa) / totalprob * self.bankroll)
                            rawstakeb = ((1.0 / oddsb) / totalprob * self.bankroll)
                            
                            # Clean ₦500 retail increment rounding
                            stakea = int(round(rawstakea / 500) * 500)
                            stakeb = int(round(rawstakeb / 500) * 500)
                            
                            if stakea > 0 and stakeb > 0:
                                self.sentcache.add(matchid)
                                bookingcode = self.generatebookingcode()
                                return {
                                    "match": matchname,
                                    "oddsa": oddsa,
                                    "oddsb": oddsb,
                                    "roi": roi,
                                    "stakea": stakea,
                                    "stakeb": stakeb,
                                    "totalpool": stakea + stakeb,
                                    "bookingcode": bookingcode
                                }
                except Exception:
                    continue
        except Exception as e:
            logging.debug(f"DOM scraping pass skipped: {e}")
            
        return None

    async def executeengine(self):
        """Main execution workflow running Playwright inside GitHub Actions runner."""
        logging.info("🚀 Live Playwright Browser Engine started.")
        
        if not self.validateenvironment():
            return

        async with async_playwright() as p:
            # Launch stealth headless Chromium browser instance
            browser = await p.chromium.launch(
                headless=True,
                args=["--disable-blink-features=AutomationControlled", "--no-sandbox"]
            )
            
            # Configure mobile device context to mimic real phone browsing
            context = await browser.new_context(
                user_agent="Mozilla/5.0 (Linux; Android 13; SM-S918B) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Mobile Safari/537.36",
                viewport={"width": 412, "height": 915},
                device_scale_factor=2.6
            )
            
            page = await context.new_page()

            async with httpx.AsyncClient() as client:
                starttime = asyncio.get_event_loop().time()
                
                # Run live scanning cycle for up to 90 seconds
                while (asyncio.get_event_loop().time() - starttime) < 90:
                    opportunity = await self.scrapeandcalculatespreads(page)
                    
                    if opportunity:
                        bookingcode = opportunity['bookingcode']
                        targeturl = f"https://www.sportybet.com/ng/?shareCode={bookingcode}"
                        
                        alerttext = (
                            f"💎 <b>LIVE BROWSER ARBITRAGE</b> 💎\n"
                            f"────────────────────\n"
                            f"⚽ <b>Fixture:</b> {opportunity['match']}\n"
                            f"📈 <b>Net Yield:</b> {opportunity['roi']:.2f}% ROI\n"
                            f"💰 <b>Total Bankroll Pool:</b> ₦{opportunity['totalpool']:,}\n"
                            f"────────────────────\n"
                            f"📋 <b>OPTIMIZED STAKE DISTRIBUTION:</b>\n"
                            f"<code>Leg 1: ₦{opportunity['stakea']:,} @ {opportunity['oddsa']:.2f}</code>\n"
                            f"<code>Leg 2: ₦{opportunity['stakeb']:,} @ {opportunity['oddsb']:.2f}</code>\n"
                            f"────────────────────\n"
                            f"🎟️ <b>Booking Code:</b> <code>{bookingcode}</code>\n"
                            f"────────────────────\n"
                            f"🔗 <b>Load Betslip Instantly:</b>\n{targeturl}\n"
                            f"⚡ <i>Engine Source: Playwright Headless Browser</i>"
                        )
                        
                        if await self.sendtelegramalert(client, alerttext):
                            break
                    
                    await asyncio.sleep(5.0)
            
            await browser.close()

if __name__ == "__main__":
    asyncio.run(PlaywrightArbitrageEngine().executeengine())
