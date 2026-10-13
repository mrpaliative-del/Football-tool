import os
import asyncio
import logging
import httpx

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S"
)

class InstantTaskEngine:
    def __init__(self):
        self.token = os.getenv("TELEGRAMBOTTOKEN")
        self.chatid = os.getenv("TARGETCHATID")
        self.baseurl = f"https://api.telegram.org/bot{self.token}" if self.token else ""
        self.paystack_secret = os.getenv("PAYSTACK_SECRET_KEY")
        self.api_url = "https://remoteok.com/api"
        self.sentcache = set()

    def validateenvironment(self) -> bool:
        if not self.token or not self.chatid:
            logging.critical("❌ ERROR: TELEGRAMBOTTOKEN or TARGETCHATID missing.")
            return False
        return True

    async def send_telegram_alert(self, client: httpx.AsyncClient, task_data: dict) -> bool:
        url = f"{self.baseurl}/sendMessage"
        payment_link = "https://paystack.com"
        
        if self.paystack_secret:
            try:
                pay_res = await client.post(
                    "https://api.paystack.co/transaction/initialize",
                    headers={
                        "Authorization": f"Bearer {self.paystack_secret.strip()}",
                        "Content-Type": "application/json"
                    },
                    json={
                        "email": "customer@taskengine.internal",
                        "amount": 250000,  # ₦2,500 escrow fee
                        "metadata": {"task_title": task_data['title']}
                    },
                    timeout=10.0
                )
                data = pay_res.json()
                if pay_res.status_code == 200 and data.get("status"):
                    payment_link = data["data"]["authorization_url"]
                else:
                    logging.error(f"❌ Paystack API Response Error: {data}")
            except Exception as e:
                logging.error(f"❌ Paystack Connection Exception: {e}")
        else:
            logging.warning("⚠️ PAYSTACK_SECRET_KEY environment variable is missing.")

        inline_keyboard = {
            "inline_keyboard": [[{"text": "💳 PAY & UNLOCK GIG DETAILS (₦2,500)", "url": payment_link}]]
        }
        
        payload = {
            "chat_id": str(self.chatid),
            "text": (
                f"🚀 <b>VERIFIED FREELANCE TASK SIGNAL</b>\n"
                f"──────────────────────────────\n"
                f"📌 <b>Position:</b> <code>{task_data['title']}</code>\n"
                f"🏢 <b>Company:</b> {task_data['company']}\n"
                f"💰 <b>Budget:</b> {task_data['compensation']}\n"
                f"──────────────────────────────\n"
                f"⚡ <i>Secure Paystack escrow gate active. Click to verify and unlock.</i>"
            ),
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
            "reply_markup": inline_keyboard
        }
        
        try:
            response = await client.post(url, json=payload, timeout=10.0)
            return response.status_code == 200
        except httpx.RequestException as e:
            logging.error(f"❌ Telegram Dispatch Error: {e}")
            return False

    async def fetch_latest_task(self, client: httpx.AsyncClient) -> dict:
        try:
            logging.info(f"⚡ Fetching live jobs via API: {self.api_url}")
            response = await client.get(self.api_url, headers={"User-Agent": "Mozilla/5.0"}, timeout=15.0)
            
            if response.status_code == 200:
                data = response.json()
                jobs = [item for item in data if isinstance(item, dict) and "position" in item]
                
                for job in jobs[:10]:
                    title = job.get("position", "Software Engineer")
                    company = job.get("company", "Verified Partner")
                    salary = job.get("salary_max", "")
                    
                    compensation = f"${salary:,} / year" if salary else "$3,500 – $7,000 / project"
                    
                    taskid = "".join(filter(str.isalnum, f"{title}-{company}"))[:35]
                    if taskid in self.sentcache:
                        continue
                    
                    self.sentcache.add(taskid)
                    return {
                        "title": title,
                        "company": company,
                        "compensation": compensation
                    }
        except Exception as e:
            logging.error(f"❌ API fetching exception: {e}")
        return None

    async def executeengine(self):
        logging.info("🚀 Instant API Task Engine Initialized.")
        if not self.validateenvironment():
            return

        async with httpx.AsyncClient() as client:
            opportunity = await self.fetch_latest_task(client)
            if opportunity:
                if await self.send_telegram_alert(client, opportunity):
                    logging.info("✅ Professional task signal dispatched successfully.")
            else:
                logging.info("⚠️ No new task captured from API feed.")

if __name__ == "__main__":
    asyncio.run(InstantTaskEngine().executeengine())
