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

class GlobalTaskGapEngine:
    def __init__(self):
        self.token = os.getenv("TELEGRAMBOTTOKEN")
        self.chatid = os.getenv("TARGETCHATID")
        self.baseurl = f"https://api.telegram.org/bot{self.token}" if self.token else ""
        
        # Target recurring task feed or micro-gig board URL
        self.targeturl = os.getenv("TARGETTASKURL", "https://remoteok.com/remote-dev-jobs")
        self.sentcache = set()

    def validateenvironment(self) -> bool:
        """Verifies mandatory GitHub Secrets exist before executing."""
        logging.info("Checking task engine environment variables...")
        if not self.token or not self.chatid:
            logging.critical("❌ ERROR: TELEGRAMBOTTOKEN or TARGETCHATID missing from GitHub Secrets.")
            return False
        return True

    async def sendtelegramalert(self, client: httpx.AsyncClient, task_data: dict) -> bool:
        """Dispatches formatted HTML task alerts with optional 5G action buttons to Telegram."""
        url = f"{self.baseurl}/sendMessage"
        
        # Optional 5G Interactive Action Button for instant execution/opening
        inline_keyboard = {
            "inline_keyboard": [
                [
                    {
                        "text": "🔗 OPEN DIRECT TASK LINK", 
                        "url": task_data['source']
                    }
                ]
            ]
        }

        payload = {
            "chat_id": str(self.chatid),
            "text": (
                f"🔄 <b>RECURRING TASK SIGNAL</b> 🔄\n"
                f"────────────────────\n"
                f"📌 <b>Task/Gig:</b> {task_data['title']}\n"
                f"📝 <b>Summary:</b> {task_data['snippet']}\n"
                f"────────────────────\n"
                f"⚡ <i>Engine: Playwright Continuous Feed Scanner</i>"
            ),
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
            "reply_markup": inline_keyboard
        }

        try:
            response = await client.post(url, json=payload, timeout=10.0)
            if response.status_code == 200:
                logging.info("✅ Recurring task gap alert delivered successfully.")
                return True
            else:
                logging.error(f"❌ Telegram API Error [{response.status_code}]: {response.text}")
        except httpx.RequestException as e:
            logging.error(f"❌ Dispatch Network Error: {e}")
        return False

    async def scantaskgaps(self, page) -> dict:
        """Polls high-frequency task boards to capture fresh entries without sitting idle."""
        try:
            logging.info(f"Navigating to task stream: {self.targeturl}")
            await page.goto(self.targeturl, timeout=30000, wait_until="domcontentloaded")
            await asyncio.sleep(5.0)

            # Target dynamic list items that update frequently across boards
            items = await page.locator("tr.job, .job-card, tr.athing, .gig-item").all()
            logging.info(f"Found {len(items)} live task listings on the board.")

            for item in items[:15]:
                try:
                    text_content = await item.inner_text()
                    lines = [l.strip() for l in text_content.split("\n") if l.strip()]

                    if len(lines) < 1:
                        continue

                    title = lines[0]
                    taskid = "".join(filter(str.isalnum, title))[:35]

                    if taskid in self.sentcache:
                        continue

                    self.sentcache.add(taskid)
                    return {
                        "title": title,
                        "snippet": lines[1] if len(lines) > 1 else "New recurring task entry detected.",
                        "source": self.targeturl
                    }
                except Exception:
                    continue
        except Exception as e:
            logging.error(f"Task board scanning exception: {e}")

        return None

    async def executeengine(self):
        """Main runner execution cycle for the task engine."""
        logging.info("🚀 Global Task Gap Engine Initialized.")

        if not self.validateenvironment():
            return

        async with async_playwright() as p:
            browser = await p.chromium.launch(
                headless=True,
                args=["--no-sandbox", "--disable-dev-shm-usage"]
            )

            context = await browser.new_context(
                user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
                viewport={"width": 1280, "height": 800}
            )

            page = await context.new_page()

            async with httpx.AsyncClient() as client:
                starttime = asyncio.get_event_loop().time()

                while (asyncio.get_event_loop().time() - starttime) < 90:
                    opportunity = await self.scantaskgaps(page)

                    if opportunity:
                        if await self.sendtelegramalert(client, opportunity):
                            break

                    await asyncio.sleep(10.0)

            await browser.close()

if __name__ == "__main__":
    asyncio.run(GlobalTaskGapEngine().executeengine())
