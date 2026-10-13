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

app = FastAPI(title="Unified Big-Tech Affiliate & Arbitrage Engine")

# Environment configurations
TELEGRAM_TOKEN = os.getenv("TELEGRAMBOTTOKEN")
TARGET_CHAT_ID = os.getenv("TARGETCHATID")
PAYSTACK_SECRET = os.getenv("PAYSTACK_SECRET_KEY")
AFFILIATE_ID = os.getenv("TECH_AFFILIATE_ID", "your_ref_tag_2026") # Your tracking reference
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
# 2. BACKGROUND WORKERS: Tech Affiliates & Sports
# ==========================================
async def tech_affiliate_worker():
    """Background loop tracking high-tier tech company affiliate programs and tools"""
    logging.info("💻 Big-Tech Affiliate Worker Started.")
    
    # Curated registry of top tech software programs with affiliate parameters
    tech_programs = [
        {"name": "DigitalOcean Cloud Infrastructure", "payout": "$25 - $100 Credit Bonus", "base_url": "https://www.digitalocean.com/?ref="},
        {"name": "HubSpot CRM & Marketing Automation", "payout": "30% Recurring Commission", "base_url": "https://www.hubspot.com/?ref="},
        {"name": "Semrush SEO & Analytics Suite", "payout": "$200 Per Sale Bounty", "base_url": "https://www.semrush.com/?ref="},
        {"name": "ActiveCampaign Cloud Infrastructure", "payout": "Up to 50% Recurring", "base_url": "https://www.activecampaign.com/?ref="}
    ]

    sent_cache = set()
    while True:
        try:
            async with httpx.AsyncClient() as client:
                for prog in tech_programs:
                    prog_id = f"tech_{prog['name']}"
                    if prog_id in sent_cache:
                        continue
                    sent_cache.add(prog_id)

                    tracking_url = f"{prog['base_url']}{AFFILIATE_ID}"
                    
                    # Generate Paystack checkout link for secure info access
                    payment_link = "https://paystack.com"
                    if PAYSTACK_SECRET:
                        pay_res = await client.post(
                            "https://api.paystack.co/transaction/initialize",
                            headers={"Authorization": f"Bearer {PAYSTACK_SECRET.strip()}", "Content-Type": "application/json"},
                            json={"email": "task.customer@gmail.com", "amount": 100000, "metadata": {"program": prog['name']}}
                        )
                        pay_data = pay_res.json()
                        if pay_res.status_code == 200 and pay_data.get("status"):
                            payment_link = pay_data["data"]["authorization_url"]

                    keyboard = {
                        "inline_keyboard": [
                            [{"text": "🔗 UNLOCK DIRECT AFFILIATE LINK", "url": tracking_url}],
                            [{"text": "⚡ VERIFY ESCROW ACCESS (₦1,000)", "url": payment_link}]
                        ]
                    }
                    
                    msg = (
                        f"🚀 <b>BIG-TECH AFFILIATE OPPORTUNITY</b>\n"
                        f"──────────────────────────────\n"
                        f"🏢 <b>Tech Platform:</b> <code>{prog['name']}</code>\n"
                        f"💰 <b>Commission Model:</b> {prog['payout']}\n"
                        f"──────────────────────────────\n"
                        f"⚡ <i>Verified enterprise partner ecosystem. Unlock tracked referral stream.</i>"
                    )
                    await send_telegram(client, msg, keyboard)
                    await asyncio.sleep(5.0)

        except Exception as e:
            logging.error(f"Tech Affiliate Worker Exception: {e}")
        
        # Rotate check cycle every 12 hours for high-value enterprise campaigns
        await asyncio.sleep(43200)

async def sports_xhr_worker():
    """Background loop intercepting live sportsbook XHR streams"""
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
                            pass
                        except Exception:
                            pass

                page.on("response", handle_response)
                await page.goto("https://www.sportybet.com/ng/sport/football", timeout=60000)
                await asyncio.sleep(300)
                await browser.close()
        except Exception as e:
            logging.error(f"Sports Worker Exception: {e}")
            await asyncio.sleep(60)

@app.on_event("startup")
async def startup_event():
    asyncio.create_task(tech_affiliate_worker())
    asyncio.create_task(sports_xhr_worker())
    logging.info("🚀 Unified Engine Started: Webhooks + Tech Affiliates + Sports Active.")

@app.get("/")
def health_check():
    return {"status": "online", "system": "Unified Big-Tech Affiliate & Arbitrage Engine"}
