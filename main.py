import os
import asyncio
import logging
import httpx
from fastapi import FastAPI, HTTPException, BackgroundTasks
from pydantic import BaseModel, HttpUrl, EmailStr
from playwright.async_api import async_playwright

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S"
)

app = FastAPI(title="Dynamic Production Engine")

# Strict Environment Validation (No hardcoded credentials or defaults)
TELEGRAM_TOKEN = os.getenv("TELEGRAMBOTTOKEN")
TARGET_CHAT_ID = os.getenv("TARGETCHATID")
PAYSTACK_SECRET = os.getenv("PAYSTACK_SECRET_KEY")

PARTNERSTACK_API_KEY = os.getenv("PARTNERSTACK_API_KEY")
IMPACT_ACCOUNT_SID = os.getenv("IMPACT_ACCOUNT_SID")
IMPACT_AUTH_TOKEN = os.getenv("IMPACT_AUTH_TOKEN")

SPORTYBET_URL = os.getenv("SPORTYBET_LIVE_URL")
BET9JA_URL = os.getenv("BET9JA_LIVE_URL")

BASE_URL = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}" if TELEGRAM_TOKEN else ""

class TaskPayload(BaseModel):
    title: str
    company: str
    budget_usd: float
    client_email: EmailStr
    direct_link: HttpUrl

async def send_telegram(client: httpx.AsyncClient, text: str, reply_markup: dict = None) -> bool:
    if not BASE_URL or not TARGET_CHAT_ID:
        logging.warning("⚠️ Telegram dispatcher skipped: Missing credentials.")
        return False
    payload = {
        "chat_id": str(TARGET_CHAT_ID),
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True
    }
    if reply_markup:
        payload["reply_markup"] = reply_markup
    try:
        res = await client.post(f"{BASE_URL}/sendMessage", json=payload, timeout=10.0)
        return res.status_code == 200
    except Exception as e:
        logging.error(f"❌ Telegram dispatch error: {e}")
        return False

# ==========================================
# 1. LIVE WEBHOOK INGESTION (Dynamic Inbound Tasks)
# ==========================================
@app.post("/webhook/task")
async def receive_external_task(task: TaskPayload, background_tasks: BackgroundTasks):
    if task.budget_usd < 2.0:
        raise HTTPException(status_code=400, detail="Task budget below dynamic minimum threshold ($2.00 USD).")
    
    background_tasks.add_task(process_live_task, task)
    return {"status": "success", "message": "Dynamic task ingested and queued."}

async def process_live_task(task: TaskPayload):
    async with httpx.AsyncClient() as client:
        payment_link = None
        if PAYSTACK_SECRET:
            try:
                res = await client.post(
                    "https://api.paystack.co/transaction/initialize",
                    headers={
                        "Authorization": f"Bearer {PAYSTACK_SECRET.strip()}",
                        "Content-Type": "application/json"
                    },
                    json={
                        "email": task.client_email,
                        "amount": 100000,  # ₦1,000 Escrow Fee
                        "metadata": {"task_title": task.title, "target_url": str(task.direct_link)}
                    },
                    timeout=10.0
                )
                data = res.json()
                if res.status_code == 200 and data.get("status"):
                    payment_link = data["data"]["authorization_url"]
            except Exception as e:
                logging.error(f"❌ Paystack API error: {e}")

        keyboard = {"inline_keyboard": [[{"text": "⚡ UNLOCK USD MICRO-TASK (₦1,000)", "url": payment_link}]]} if payment_link else None
        
        msg = (
            f"🌍 <b>DYNAMIC VERIFIED TASK SIGNAL</b>\n"
            f"──────────────────────────────\n"
            f"📌 <b>Task:</b> <code>{task.title}</code>\n"
            f"🏢 <b>Employer:</b> {task.company}\n"
            f"💰 <b>Budget:</b> ${task.budget_usd:.2f} USD\n"
            f"──────────────────────────────"
        )
        await send_telegram(client, msg, keyboard)

# ==========================================
# 2. DYNAMIC AFFILIATE API WORKER
# ==========================================
async def dynamic_affiliate_worker():
    """Polls live APIs dynamically without hardcoded campaign arrays"""
    logging.info("💻 Dynamic Affiliate API Worker Initialized.")
    sent_cache = set()

    while True:
        try:
            async with httpx.AsyncClient() as client:
                # Dynamic PartnerStack Fetch
                if PARTNERSTACK_API_KEY:
                    try:
                        res = await client.get(
                            "https://api.partnerstack.com/api/v2/rewards",
                            headers={"Authorization": f"Bearer {PARTNERSTACK_API_KEY}"},
                            timeout=15.0
                        )
                        if res.status_code == 200:
                            for reward in res.json().get("data", []):
                                item_id = reward.get("key")
                                if item_id in sent_cache:
                                    continue
                                sent_cache.add(item_id)
                                
                                title = reward.get("title")
                                amount = reward.get("amount", 0) / 100.0
                                
                                msg = (
                                    f"🚀 <b>PARTNERSTACK REWARD</b>\n"
                                    f"──────────────────────────────\n"
                                    f"📌 <b>Offer:</b> <code>{title}</code>\n"
                                    f"💰 <b>Payout:</b> ${amount:.2f} USD"
                                )
                                await send_telegram(client, msg)
                    except Exception as e:
                        logging.error(f"PartnerStack API Error: {e}")

                # Dynamic Impact Radius Fetch
                if IMPACT_ACCOUNT_SID and IMPACT_AUTH_TOKEN:
                    try:
                        res = await client.get(
                            f"https://api.impact.com/Mediapartners/{IMPACT_ACCOUNT_SID}/Campaigns",
                            auth=(IMPACT_ACCOUNT_SID, IMPACT_AUTH_TOKEN),
                            headers={"Accept": "application/json"},
                            timeout=15.0
                        )
                        if res.status_code == 200:
                            for camp in res.json().get("Campaigns", []):
                                camp_id = str(camp.get("Id"))
                                if camp_id in sent_cache:
                                    continue
                                sent_cache.add(camp_id)

                                name = camp.get("CampaignName")
                                tracking_link = camp.get("TrackingLink")

                                keyboard = {"inline_keyboard": [[{"text": "🔗 ACCESS LINK", "url": tracking_link}]]}
                                msg = (
                                    f"🚀 <b>IMPACT CAMPAIGN</b>\n"
                                    f"──────────────────────────────\n"
                                    f"🏢 <b>Brand:</b> <code>{name}</code>"
                                )
                                await send_telegram(client, msg, keyboard)
                    except Exception as e:
                        logging.error(f"Impact API Error: {e}")

        except Exception as e:
            logging.error(f"Affiliate Loop Error: {e}")

        await asyncio.sleep(21600)

# ==========================================
# 3. LIVE SPORTSBOOK NETWORK XHR SNIFFER
# ==========================================
async def live_sports_sniffer():
    """Intercepts live sports data streams from dynamically configured URLs"""
    if not SPORTYBET_URL and not BET9JA_URL:
        logging.warning("⚠️ Sports URLs not configured in environment variables. Sniffer inactive.")
        return

    logging.info("⚽ Live Sports Network Sniffer Initialized.")
    urls_to_monitor = [u for u in [SPORTYBET_URL, BET9JA_URL] if u]

    while True:
        try:
            async with async_playwright() as p:
                browser = await p.chromium.launch(headless=True, args=["--no-sandbox", "--disable-dev-shm-usage"])
                context = await browser.new_context(user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36")
                page = await context.new_page()

                async def handle_response(response):
                    url = response.url
                    if ("market" in url or "odds" in url or "fixture" in url) and response.status == 200:
                        try:
                            _ = await response.json()
                            logging.info(f"📡 Dynamic odds payload intercepted from {url[:50]}...")
                        except Exception:
                            pass

                page.on("response", handle_response)
                
                for target_url in urls_to_monitor:
                    try:
                        await page.goto(target_url, timeout=45000)
                        await asyncio.sleep(60)
                    except Exception as e:
                        logging.error(f"Navigation error on {target_url}: {e}")

                await browser.close()
        except Exception as e:
            logging.error(f"Sports Sniffer Error: {e}")
            await asyncio.sleep(60)

# ==========================================
# SYSTEM STARTUP
# ==========================================
@app.on_event("startup")
async def startup_event():
    asyncio.create_task(dynamic_affiliate_worker())
    asyncio.create_task(live_sports_sniffer())
    logging.info("🚀 Dynamic System Engine Online.")

@app.get("/")
def health_check():
    return {
        "status": "online",
        "system": "Pure Dynamic Production Engine",
        "config_status": {
            "telegram": bool(TELEGRAM_TOKEN and TARGET_CHAT_ID),
            "paystack": bool(PAYSTACK_SECRET),
            "partnerstack": bool(PARTNERSTACK_API_KEY),
            "impact": bool(IMPACT_ACCOUNT_SID and IMPACT_AUTH_TOKEN),
            "sports_monitored": bool(SPORTYBET_URL or BET9JA_URL)
        }
    }
