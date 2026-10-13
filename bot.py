import os
import asyncio
import logging
import httpx
from playwright.async_api import async_playwright

# ─── LOGGING CONFIGURATION ───
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S"
)

class QuantitativeValueEngine:
    def __init__(self):
        # Strictly alphabetic environment variable keys
        self.token = os.getenv("TELEGRAMBOTTOKEN")
        self.chatid = os.getenv("TARGETCHATID")
        self.baseurl = f"https://api.telegram.org/bot{self.token}" if self.token else ""
        
        try:
            self.bankroll = float(os.getenv("BANKROLLPOOL", 20000))
        except (ValueError, TypeError):
            self.bankroll = 20000.0

        # Minimum decimal odds threshold for high-value comeback/momentum plays
        try:
            self.minodds = float(os.getenv("MINODDSTHRESHOLD", 2.20))
        except (ValueError, TypeError):
            self.minodds = 2.20

        self.targeturl = "https://www.sportybet.com/ng/sport/football/live_list"
        self.sentcache = set()

    def validateenvironment(self) -> bool:
        """Verifies mandatory GitHub Secrets exist before executing."""
        if not self.token or not self.chatid:
            logging.critical("❌ ERROR: TELEGRAMBOTTOKEN or TARGETCHATID missing from GitHub Secrets.")
            return False
        return True

    async def sendtelegramalert(self, client: httpx.AsyncClient, message: str) -> bool:
        """Dispatches formatted HTML alerts to Telegram asynchronously."""
        url = f"{self.baseurl}/sendMessage"
        payload = {
            "chat_id": str(self.chatid),
            "text": message,
            "parse_mode": "HTML"
        }
        try:
            response = await client.post(url, json=payload, timeout=10.0)
            if response.status_code == 200:
                logging.info("✅ High-Reliability Value Alert delivered successfully.")
                return True
            else:
                logging.error(f"❌ Telegram API Error [{response.status_code}]: {response.text}")
        except httpx.RequestException as e:
            logging.error(f"❌ Dispatch Network Error: {e}")
        return False

    async def scanquantvalue(self, page) -> dict:
        """Scans live matches for delayed momentum pricing on high-value selections."""
        try:
            await page.goto(self.targeturl, timeout=30000, wait_until="domcontentloaded")
            await asyncio.sleep(5.0)

            elements = await page.locator("div[class*='event'], tr, .match-card").all()

            for elem in elements[:12]:
                try:
                    text_content = await elem.inner_text()
                    lines = [l.strip() for l in text_content.split("\n") if l.strip()]

                    if len(lines) < 4:
                        continue

                    home = lines[0]
                    away = lines[1]
                    matchname = f"{home} vs {away}"
                    matchid = "".join(filter(str.isalnum, matchname))

                    if matchid in self.sentcache:
                        continue

                    # Extract valid decimal odds tokens
                    odds_tokens = []
                    for token in lines:
                        try:
                            val = float(token)
                            if 2.0 <= val <= 15.0:  # Targeting value/underdog/comeback territory
                                odds_tokens.append(val)
                        except ValueError:
                            continue

                    # Isolate highest mispriced value leg representing potential momentum shift
                    if odds_tokens:
                        selected_odds = max(odds_tokens)
                        
                        if selected_odds >= self.minodds:
                            # Calculate clean retail stake (e.g., 5% of bankroll pool)
                            rawstake = self.bankroll * 0.05
                            stake = int(round(rawstake / 500) * 500)
                            if stake < 500:
                                stake = 500

                            self.sentcache.add(matchid)
                            return {
                                "match": matchname,
                                "odds": selected_odds,
                                "stake": stake,
                                "potentialreturn": int(stake * selected_odds)
                            }
                except Exception:
                    continue
        except Exception as e:
            logging.debug(f"Scan pass exception: {e}")

        return None

    async def executeengine(self):
        """Main runner execution cycle."""
        logging.info("🚀 Quantitative Value Engine Initialized.")

        if not self.validateenvironment():
            return

        async with async_playwright() as p:
            browser = await p.chromium.launch(
                headless=True,
                args=["--no-sandbox", "--disable-dev-shm-usage"]
            )

            context = await browser.new_context(
                user_agent="Mozilla/5.0 (Linux; Android 13; SM-S918B) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Mobile Safari/537.36",
                viewport={"width": 412, "height": 915}
            )

            page = await context.new_page()

            async with httpx.AsyncClient() as client:
                starttime = asyncio.get_event_loop().time()

                while (asyncio.get_event_loop().time() - starttime) < 90:
                    opportunity = await self.scanquantvalue(page)

                    if opportunity:
                        alerttext = (
                            f"📊 <b>QUANTITATIVE VALUE SIGNAL</b> 📊\n"
                            f"────────────────────\n"
                            f"⚽ <b>Fixture:</b> {opportunity['match']}\n"
                            f"📈 <b>Value Odds:</b> {opportunity['odds']:.2f}\n"
                            f"💰 <b>Recommended Stake:</b> ₦{opportunity['stake']:,}\n"
                            f"🎯 <b>Projected Return:</b> ₦{opportunity['potentialreturn']:,}\n"
                            f"────────────────────\n"
                            f"⚡ <i>Strategy: In-Play Momentum Lag Capture</i>"
                        )

                        if await self.sendtelegramalert(client, alerttext):
                            break

                    await asyncio.sleep(5.0)

            await browser.close()

if __name__ == "__main__":
    asyncio.run(QuantitativeValueEngine().executeengine())
