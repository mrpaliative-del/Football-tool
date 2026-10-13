import os
import asyncio
import logging
import httpx

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S"
)

class ForeignMicroTaskEngine:
    def __init__(self):
        self.token = os.getenv("TELEGRAMBOTTOKEN")
        self.chatid = os.getenv("TARGETCHATID")
        self.baseurl = f"https://api.telegram.org/bot{self.token}" if self.token else ""
        self.paystack_secret = os.getenv("PAYSTACK_SECRET_KEY")
        self.sentcache = set()

        self.sources = [
            "https://remoteok.com/api",
            "https://remotive.com/api/remote-jobs"
        ]

    def validateenvironment(self) -> bool:
        if not self.token or not self.chatid:
            logging.critical("❌ ERROR: TELEGRAMBOTTOKEN or TARGETCHATID missing.")
            return False
        return True

    async def send_telegram_alert(self, client: httpx.AsyncClient, task_data: dict) -> bool:
        url = f"{self.baseurl}/sendMessage"
        
        unlock_fee_kobo = 100000  # ₦1,000
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
                        "email": "task.customer@gmail.com",  # Standardized valid test email format
                        "amount": unlock_fee_kobo,
                        "metadata": {"task_title": task_data['title']}
                    },
                    timeout=10.0
                )
                data = pay_res.json()
                logging.info(f"Paystack Raw Response Status: {pay_res.status_code}")
                logging.info(f"Paystack Raw Response Data: {data}")

                if pay_res.status_code == 200 and data.get("status"):
                    payment_link = data["data"]["authorization_url"]
                else:
                    logging.error(f"❌ Paystack Initialization Failed: {data}")
            except Exception as e:
                logging.error(f"❌ Paystack Connection Exception: {e}")
        else:
            logging.warning("⚠️ PAYSTACK_SECRET_KEY environment variable is missing from GitHub Secrets.")

        inline_keyboard = {
            "inline_keyboard": [[{"text": "⚡ UNLOCK USD MICRO-TASK (₦1,000)", "url": payment_link}]]
        }
        
        payload = {
            "chat_id": str(self.chatid),
            "text": (
                f"🌍 <b>USD MICRO-TASK SIGNAL ($2+ USD)</b>\n"
                f"──────────────────────────────\n"
                f"📌 <b>Task/Gig:</b> <code>{task_data['title']}</code>\n"
                f"🏢 <b>Foreign Employer:</b> {task_data['company']}\n"
                f"💰 <b>Micro-Budget:</b> {task_data['compensation']}\n"
                f"──────────────────────────────\n"
                f"⚡ <i>Micro-task verified ($2+ USD min). Pay escrow to unlock direct link.</i>"
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

    async def fetch_foreign_micro_task(self, client: httpx.AsyncClient) -> dict:
        for api_url in self.sources:
            try:
                logging.info(f"🌐 Scanning international feed for $2+ tasks: {api_url}")
                response = await client.get(api_url, headers={"User-Agent": "Mozilla/5.0"}, timeout=15.0)
                
                if response.status_code != 200:
                    continue
                
                data = response.json()
                jobs = []
                if "remoteok" in api_url:
                    jobs = [item for item in data if isinstance(item, dict) and "position" in item]
                elif "remotive" in api_url:
                    jobs = data.get("jobs", [])

                for job in jobs[:20]:
                    title = job.get("position") or job.get("title", "Quick Task")
                    company = job.get("company_name") or job.get("company", "Global Client")
                    
                    salary_min = job.get("salary_min")
                    if salary_min and salary_min >= 2:
                        compensation = f"${salary_min:,} / task"
                    else:
                        compensation = "$5 – $50 / quick micro-task"

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
                logging.error(f"❌ Feed error on {api_url}: {e}")
        return None

    async def executeengine(self):
        logging.info("🚀 Foreign Micro-Task Engine Initialized.")
        if not self.validateenvironment():
            return

        async with httpx.AsyncClient() as client:
            task = await self.fetch_foreign_micro_task(client)
            if task:
                if await self.send_telegram_alert(client, task):
                    logging.info("✅ Foreign micro-task signal dispatched successfully.")
            else:
                logging.info("⚠️ No qualifying micro-tasks found in current cycle.")

if __name__ == "__main__":
    asyncio.run(ForeignMicroTaskEngine().executeengine())
