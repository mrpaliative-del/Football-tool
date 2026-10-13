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

app = FastAPI(title="Autonomous Production Engine")

# Strict Environment Loading (Alphanumeric GitHub/Render Standard)
TELEGRAM_TOKEN = os.getenv("TELEGRAMBOTTOKEN")
TARGET_CHAT_ID = os.getenv("TARGETCHATID")
PAYSTACK_SECRET = os.getenv("PAYSTACK_SECRET_KEY")

PARTNERSTACK_API_KEY = os.getenv("PARTNERSTACK_API_KEY")
IMPACT_ACCOUNT_SID = os.getenv("IMPACT_ACCOUNT_SID")
IMPACT_AUTH_TOKEN = os.getenv("IMPACT_AUTH_TOKEN")

SPORTYBET_PREMATCH_URL = os.getenv("SPORTYBET_PREMATCH_URL")
BET9JA_PREMATCH_URL = os.getenv("BET9JA_PREMATCH_URL")

BASE_URL = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}" if TELEGRAM_TOKEN else ""

class TaskPayload(BaseModel):
    title: str
    company: str
    budget_usd: float
    client_email: EmailStr
    direct_link: HttpUrl

async def send_telegram(client: httpx.AsyncClient, text: str, reply_markup: dict = None) -> bool:
    if not BASE_URL or not TARGET_CHAT_ID:
        logging.error("❌ Telegram dispatch failed: Missing credentials.")
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
        logging.error(f"❌ Telegram network error: {e}")
        return False

async def initialize_paystack_escrow(client: httpx.AsyncClient, email: str, title: str, target_url: str) -> str:
    if not PAYSTACK_SECRET:
        logging.error("❌ PAYSTACK_SECRET_KEY missing. Cannot initialize escrow.")
        raise HTTPException(status_code=500, detail="Payment gateway unconfigured.")
    
    res = await client.post(
        "https://api.paystack.co/transaction/initialize",
        headers={
            "Authorization": f"Bearer {PAYSTACK_SECRET.strip()}",
            "Content-Type": "application/json"
        },
        json={
            "email": email,
            "amount": 100000,  # ₦1,000 Escrow Fee
            "metadata": {"task_title": title, "target_url": target_url}
        },
        timeout=10.0
    )
    data = res.json()
    if res.status_code == 200 and data.get("status"):
        return data["data"]["authorization_url"]
    
    raise HTTPException(status_code=502, detail="Failed to initialize Paystack transaction session.")

@app.post("/webhook/task")
async def receive_external_task(task: TaskPayload, background_tasks: BackgroundTasks):
    if task.budget_usd < 2.0:
        raise HTTPException(status_code=400, detail="Task budget below required $2.00 minimum threshold.")
    
    background_tasks.add_task(process_live_task, task)
    return {"status": "success", "message": "Task ingested and queued for live processing."}

async def process_live_task(task: TaskPayload):
    async with httpx.AsyncClient() as client:
        payment_link = await initialize_paystack_escrow(
            client, task.client_email, task.title, str(task.direct_link)
        )

        keyboard = {
            "inline_keyboard": [
                [{"text": "⚡ UNLOCK USD MICRO-TASK (₦1,000)", "url": payment_link}]
            ]
        }
        
        msg = (
            f"🌍 <b>LIVE PRODUCTION TASK SIGNAL</b>\n"
            f"──────────────────────────────\n"
            f"📌 <b>Task:</b> <code>{task.title}</code>\n"
            f"🏢 <b>Employer:</b> {task.company}\n"
            f"💰 <b>Budget:</b> ${task.budget_usd:.2f} USD\n"
            f"──────────────────────────────"
        )
        await send_telegram(client, msg, keyboard)

async def enterprise_affiliate_worker():
    """Polls real PartnerStack and Impact Radius REST APIs continuously"""
    logging.info("💻 Enterprise Affiliate Worker Initialized.")
    sent_cache = set()

    while True:
        try:
            async with httpx.AsyncClient() as client:
                if PARTNERSTACK_API_KEY:
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
                                f"🚀 <b>LIVE PARTNERSTACK REWARD</b>\n"
                                f"──────────────────────────────\n"
                                f"📌 <b>Offer:</b> <code>{title}</code>\n"
                                f"💰 <b>Payout:</b> ${amount:.2f} USD"
                            )
                            await send_telegram(client, msg)

                if IMPACT_ACCOUNT_SID and IMPACT_AUTH_TOKEN:
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

                            keyboard = {"inline_keyboard": [[{"text": "🔗 ACCESS TRACKING LINK", "url": tracking_link}]]}
                            msg = (
                                f"🚀 <b>LIVE IMPACT CAMPAIGN</b>\n"
                                f"──────────────────────────────\n"
                                f"🏢 <b>Brand:</b> <code>{name}</code>"
                            )
                            await send_telegram(client, msg, keyboard)
        except Exception as e:
            logging.error(f"❌ Affiliate Worker Exception: {e}")

        await asyncio.sleep(21600)

async def production_fixtures_worker():
    """Sniffs real-time pre-match fixture JSON payloads via headless Playwright"""
    urls = [u for u in [SPORTYBET_PREMATCH_URL, BET9JA_PREMATCH_URL] if u]
    if not urls:
        logging.warning("⚠️ Pre-match URLs not defined. Fixtures worker on standby.")
        return

    logging.info("⚽ Production Pre-Match Fixtures Worker Initialized.")
    while True:
        try:
            async with async_playwright() as p:
                browser = await p.chromium.launch(headless=True, args=["--no-sandbox", "--disable-dev-shm-usage"])
                context = await browser.new_context(user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36")
                page = await context.new_page()

                async def intercept_xhr(response):
                    endpoint = response.url
                    if any(term in endpoint for term in ["schedule", "prematch", "odds", "market", "fixture"]) and response.status == 200:
                        try:
                            data = await response.json()
                            logging.info(f"📡 Successfully parsed live network stream from {endpoint[:50]}")
                        except Exception:
                            pass

                page.on("response", intercept_xhr)

                for target in urls:
                    try:
                        await page.goto(target, timeout=60000, wait_until="domcontentloaded")
                        await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
                        await asyncio.sleep(45)
                    except Exception as e:
                        logging.error(f"❌ Navigation failure on {target}: {e}")

                await browser.close()
        except Exception as e:
            logging.error(f"❌ Fixtures Worker Exception: {e}")

        await asyncio.sleep(900)

@app.on_event("startup")
async def startup_event():
    asyncio.create_task(enterprise_affiliate_worker())
    asyncio.create_task(production_fixtures_worker())
    logging.info("🚀 Autonomous Production Core Fully Online.")

@app.get("/")
def health_check():
    return {
        "status": "online",
        "system": "Autonomous Production Core",
        "active_integrations": {
            "telegram": bool(TELEGRAM_TOKEN and TARGET_CHAT_ID),
            "paystack": bool(PAYSTACK_SECRET),
            "partnerstack": bool(PARTNERSTACK_API_KEY),
            "impact": bool(IMPACT_ACCOUNT_SID and IMPACT_AUTH_TOKEN),
            "fixtures_sniffing": bool(SPORTYBET_PREMATCH_URL or BET9JA_PREMATCH_URL)
        }
    }
