import os
import asyncio
import logging
import time
import httpx
from playwright.async_api import async_playwright

# ─── LOGGING CONFIGURATION ───
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S"
)

class Polling5GEngine:
    def __init__(self):
        self.token = os.getenv("TELEGRAMBOTTOKEN")
        self.chatid = os.getenv("TARGETCHATID")
        self.baseurl = f"https://api.telegram.org/bot{self.token}" if self.token else ""
        self.paystack_secret = os.getenv("PAYSTACK_SECRET_KEY")
        
        # Target recurring task feed or micro-gig board URL
        self.targeturl = os.getenv("TARGETTASKURL", "https://remoteok.com/remote-dev-jobs")
        self.sentcache = set()

    def validateenvironment(self) -> bool:
        """Verifies mandatory GitHub Secrets exist before executing."""
        if not self.token or not self.chatid:
            logging.critical("❌ ERROR: TELEGRAMBOTTOKEN or TARGETCHATID missing from GitHub Secrets.")
            return False
        return True

    async def verify_paystack_payment(self, client: httpx.AsyncClient, reference: str) -> bool:
        """Directly polls Paystack API to check if payment was completed successfully."""
        if not self.paystack_secret:
            logging.warning("⚠️ PAYSTACK_SECRET_KEY missing. Simulating verification pass for testing.")
            return False # Set to True for dry runs if needed

        url = f"https://api.paystack.co/transaction/verify/{reference}"
        headers = {"Authorization": f"Bearer {self.paystack_secret}"}
        
        try:
            response = await client.get(url, headers=headers, timeout=10.0)
            if response.status_code == 200:
                data = response.json()
                status = data.get("data", {}).get("status")
                if status == "success":
                    return True
        except httpx.RequestException as e:
            logging.error(f"❌ Paystack Verification Network Error: {e}")
        return False

    async def execute_5g_fulfillment(self, task_data: dict):
        """[5G Autonomous Execution] Triggered instantly once payment poll confirms success."""
        logging.info(f"⚡ [5G TRIGGERED] Executing automated fulfillment for: {task_data['title']}")
        
        async with httpx.AsyncClient() as client:
            try:
                # Add your fulfillment endpoint, API call, or automation sequence here
                response = await client.post(
                    "https://httpbin.org/post",
                    json={"status": "fulfilled", "task_title": task_data['title']},
                    timeout=5.0
                )
                if response.status_code in [200, 201]:
                    logging.info("🎯 5G Task Successfully Fulfilled & Settled via Direct Polling!")
            except Exception as e:
                logging.error(f"❌ 5G Fulfillment Error: {e}")

    async def sendtelegramalert(self, client: httpx.AsyncClient, task_data: dict, reference: str) -> bool:
        """Dispatches formatted HTML task alerts with a payment link."""
        url = f"{self.baseurl}/sendMessage"
        
        # Generate a sample Paystack checkout or initialization link using the reference
        # (Users can pay via standard Paystack payment link format or custom gateway)
        payment_link = f"https://checkout.paystack.com/{reference}"

        inline_keyboard = {
            "inline_keyboard": [
                [
                    {
                        "text": "💳 PAY & UNLOCK VIA PAYSTACK", 
                        "url": payment_link
                    }
                ]
            ]
        }

        payload = {
            "chat_id": str(self.chatid),
            "text": (
                f"🔄 <b>RECURRING TASK SIGNAL + PAYSTACK</b> 🔄\n"
                f"────────────────────\n"
                f"📌 <b>Task/Gig:</b> {task_data['title']}\n"
                f"📝 <b>Summary:</b> {task_data['snippet']}\n"
                f"🔑 <b>Ref:</b> <code>{reference}</code>\n"
                f"────────────────────\n"
                f"⚡ <i>Awaiting direct payment confirmation...</i>"
            ),
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
            "reply_markup": inline_keyboard
        }

        try:
            response = await client.post(url, json=payload, timeout=10.0)
            if response.status_code == 200:
                logging.info("✅ Task alert with Paystack payment reference dispatched successfully.")
                return True
        except httpx.RequestException as e:
            logging.error(f"❌ Dispatch Network Error: {e}")
        return False

    async def scantaskgaps(self, page) -> dict:
        """Scans the board for fresh items."""
        try:
            await page.goto(self.targeturl, timeout=30000, wait_until="domcontentloaded")
            await asyncio.sleep(5.0)

            items = await page.locator("tr.job, .job-card, tr, article, div[data-id]").all()

            for item in items[:15]:
                try:
                    text_content = await item.inner_text()
                    lines = [l.strip() for l in text_content.split("\n") if l.strip()]

                    if len(lines) < 1:
                        continue

                    title = lines[0]
                    if len(title) < 5 or "remote" in title.lower() and len(lines) == 1:
                        continue

                    taskid = "".join(filter(str.isalnum, title))[:35]

                    if taskid in self.sentcache:
                        continue

                    self.sentcache.add(taskid)
                    return {
                        "title": title,
                        "snippet": lines[1] if len(lines) > 1 else "New recurring task entry detected."
                    }
                except Exception:
                    continue
        except Exception as e:
            logging.error(f"Task board scanning exception: {e}")

        return None

    async def executeengine(self):
        """Main runner execution cycle with integrated payment polling."""
        logging.info("🚀 Polling 5G + Paystack Task Engine Initialized.")

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
                opportunity = await self.scantaskgaps(page)

                if opportunity:
                    # Generate unique transaction reference for this signal
                    ref = f"task_{int(time.time())}"
                    
                    if await self.sendtelegramalert(client, opportunity, ref):
                        logging.info(f"⏳ Polling Paystack for payment confirmation on ref: {ref}...")
                        
                        # Poll Paystack for up to 5 minutes (30 attempts * 10 seconds)
                        for attempt in range(30):
                            await asyncio.sleep(10.0)
                            is_paid = await self.verify_paystack_payment(client, ref)
                            
                            if is_paid:
                                logging.info("🎉 Payment verified successfully via polling loop!")
                                await self.execute_5g_fulfillment(opportunity)
                                break
                            else:
                                logging.info(f"⏳ Attempt {attempt+1}/30: Payment pending...")

            await browser.close()

if __name__ == "__main__":
    asyncio.run(Polling5GEngine().executeengine())
