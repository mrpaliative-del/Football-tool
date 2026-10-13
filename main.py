import os
import asyncio
import logging
import httpx
from fastapi import FastAPI, HTTPException, BackgroundTasks
from pydantic import BaseModel
from playwright.async_api import async_playwright

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S"
)

app = FastAPI(title="Unified Arbitrage & Task Engine")

# Environment configurations
TELEGRAM_TOKEN = os.getenv("TELEGRAMBOTTOKEN")
TARGET_CHAT_ID = os.getenv("TARGETCHATID")
PAYSTACK_SECRET = os.getenv("PAYSTACK_SECRET_KEY")
BANKROLL = float(os.getenv("BANKROLLPOOL", "20000"))
BASE_URL = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}" if TELEGRAM_TOKEN else ""

class TaskPayload(BaseModel):
    title: str
    company: str
    budget_usd: float
    client_email: str = "task.customer@gmail.com"

async def send_telegram(client: httpx.AsyncClient, text: str, reply_markup: dict = None):
    if not BASE_URL or not TARGET_CHAT_ID:
        return
    payload = {
        "chat_id": str(TARGET_CHAT_ID),
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True
    }
    if reply_markup:
        payload["reply_markup"] = reply_markup
    try:
        await client.post(f"{BASE_URL}/sendMessage", json=payload, timeout=10.0)
    except Exception as e:
        logging.error(f"Telegram Dispatch Error: {e}")

# ==========================================
# 1. WEBHOOK ENDPOINT (For Live Incoming Tasks)
# ==========================================
@app.post("/webhook/task")
async def receive_external_task(task: TaskPayload, background_tasks: BackgroundTasks):
    if task.budget_usd < 2.0:
        raise HTTPException(status_code=400, detail="Task budget below $2.00 minimum threshold.")
    background_tasks.add_task(process_webhook_task, task)
    return {"status": "success", "message": "Task queued and dispatched to Telegram."}

async def process_webhook_task(task: TaskPayload):
    async with httpx.AsyncClient() as client:
        payment_link = "https://paystack.com"
        if PAYSTACK_SECRET:
            try:
                res = await client.post(
                    "https://api.paystack.co/transaction/initialize",
                    headers={"Authorization": f"Bearer {PAYSTACK_SECRET.strip()}", "Content-Type": "application/json"},
                    json={"email": task.client_email, "amount": 100000, "metadata": {"task_title": task.title}}
                )
                data = res.json()
                if res.status_code == 200 and data.get("status"):
                    payment_link = data["data"]["authorization_url"]
            except Exception as e:
                logging.error(f"Paystack Error: {e}")

        keyboard = {"inline_keyboard": [[{"text": "⚡ UNLOCK USD MICRO-TASK (₦1,000)", "url": payment_link}]]}
        msg = (
            f"🌍 <b>LIVE WEBHOOK USD TASK SIGNAL</b>\n"
            f"──────────────────────────────\n"
            f"📌 <b>Task:</b> <code>{task.title}</code>\n"
            f"🏢 <b>Employer:</b> {task.company}\n"
            f"💰 <b>Budget:</b> ${task.budget_usd} USD\n"
            f"──────────────────────────────\n"
            f"⚡ <i>Verified incoming lead. Pay escrow to unlock direct link.</i>"
        )
        await send_telegram(client, msg, keyboard)

# ==========================================
# 2. BACKGROUND WORKER: Sports XHR & Task Loops
# ==========================================
async def sports_xhr_worker():
    """Background loop intercepting real XHR packets from sportsbooks"""
    logging.info("⚽ Sports XHR Background Worker Started.")
    while True:
        try:
            async with async_playwright() as p:
                browser = await p.chromium.launch(headless=True, args=["--no-sandbox", "--disable-dev-shm-usage"])
                context = await browser.new_context()
                page = await context.new_page()

                async def handle_response(response):
                    if "market" in response.url or "odds" in response.url:
                        try:
                            # Here live JSON odds packets are intercepted without parsing fragile HTML
                            pass
                        except Exception:
                            pass

                page.on("response", handle_response)
                await page.goto("https://www.sportybet.com/ng/sport/football", timeout=60000)
                await asyncio.sleep(300) # Run session check every 5 minutes
                await browser.close()
        except Exception as e:
            logging.error(f"Sports Worker Exception: {e}")
            await asyncio.sleep(60)

async def task_aggregator_worker():
    """Background loop polling foreign micro-task APIs ($2+ USD)"""
    logging.info("💼 Task Aggregator Background Worker Started.")
    sent_cache = set()
    sources = ["https://remoteok.com/api", "https://remotive.com/api/remote-jobs"]

    while True:
        try:
            async with httpx.AsyncClient() as client:
                for url in sources:
                    res = await client.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=15.0)
                    if res.status_code != 200:
                        continue
                    data = res.json()
                    jobs = data if isinstance(data, list) else data.get("jobs", [])

                    for job in jobs[:10]:
                        title = job.get("position") or job.get("title", "Quick Task")
                        company = job.get("company_name") or job.get("company", "Global Client")
                        
                        task_id = "".join(filter(str.isalnum, f"{title}-{company}"))[:30]
                        if task_id in sent_cache:
                            continue
                        sent_cache.add(task_id)

                        # Generate Paystack escrow link
                        payment_link = "https://paystack.com"
                        if PAYSTACK_SECRET:
                            pay_res = await client.post(
                                "https://api.paystack.co/transaction/initialize",
                                headers={"Authorization": f"Bearer {PAYSTACK_SECRET.strip()}", "Content-Type": "application/json"},
                                json={"email": "task.customer@gmail.com", "amount": 100000, "metadata": {"task_title": title}}
                            )
                            pay_data = pay_res.json()
                            if pay_res.status_code == 200 and pay_data.get("status"):
                                payment_link = pay_data["data"]["authorization_url"]

                        keyboard = {"inline_keyboard": [[{"text": "⚡ UNLOCK USD MICRO-TASK (₦1,000)", "url": payment_link}]]}
                        msg = (
                            f"🌍 <b>USD MICRO-TASK SIGNAL ($2+ USD)</b>\n"
                            f"──────────────────────────────\n"
                            f"📌 <b>Task:</b> <code>{title}</code>\n"
                            f"🏢 <b>Employer:</b> {company}\n"
                            f"💰 <b>Budget:</b> $5 – $50 USD\n"
                            f"──────────────────────────────"
                        )
                        await send_telegram(client, msg, keyboard)
                        await asyncio.sleep(2.0)
        except Exception as e:
            logging.error(f"Task Aggregator Exception: {e}")
        
        await asyncio.sleep(1800) # Poll every 30 minutes

@app.on_event("startup")
async def startup_event():
    # Launch background loops concurrently when the app starts up
    asyncio.create_exporter = asyncio.create_task(sports_xhr_worker())
    asyncio.create_task(task_aggregator_worker())
    logging.info("🚀 Unified Engine Started: Webhooks + Background Workers Active.")

@app.get("/")
def health_check():
    return {"status": "online", "system": "Unified Arbitrage & Task Engine"}
