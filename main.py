
import os
import re
import json
import html
import hmac
import asyncio
import logging
import sqlite3
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse

import httpx
from fastapi import FastAPI, HTTPException, BackgroundTasks, Header
from pydantic import BaseModel, EmailStr, Field, HttpUrl, ConfigDict
from playwright.async_api import async_playwright


# ============================================================
# CONFIGURATION
# ============================================================

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s [%(levelname)s] %(message)s",
)

logger = logging.getLogger("production_engine")

TELEGRAM_TOKEN = os.getenv("TELEGRAMBOTTOKEN", "").strip()
TARGET_CHAT_ID = os.getenv("TARGETCHATID", "").strip()

# Required to enable /webhook/task.
TASK_WEBHOOK_SECRET = os.getenv("TASK_WEBHOOK_SECRET", "").strip()

PAYSTACK_SECRET = os.getenv("PAYSTACK_SECRET_KEY", "").strip()

# Amount charged to the client, in kobo.
# 100000 kobo = ₦1,000. Set this explicitly in GitHub Secrets.
PAYSTACK_CHARGE_KOBO = int(
    os.getenv("PAYSTACK_CHARGE_KOBO", "100000")
)

PARTNERSTACK_API_KEY = os.getenv("PARTNERSTACK_API_KEY", "").strip()

IMPACT_ACCOUNT_SID = os.getenv("IMPACT_ACCOUNT_SID", "").strip()
IMPACT_AUTH_TOKEN = os.getenv("IMPACT_AUTH_TOKEN", "").strip()

SPORTYBET_PREMATCH_URL = os.getenv("SPORTYBET_PREMATCH_URL", "").strip()
BET9JA_PREMATCH_URL = os.getenv("BET9JA_PREMATCH_URL", "").strip()

DATABASE_PATH = os.getenv(
    "ENGINE_DB_PATH", "engine_state.sqlite3"
).strip()

AFFILIATE_SCAN_SECONDS = max(
    300, int(os.getenv("AFFILIATE_SCAN_SECONDS", "21600"))
)

SPORTS_SCAN_SECONDS = max(
    300, int(os.getenv("SPORTS_SCAN_SECONDS", "900"))
)

TASK_MINIMUM_USD = float(os.getenv("TASK_MINIMUM_USD", "2.00"))

TELEGRAM_BASE_URL = (
    f"https://api.telegram.org/bot{TELEGRAM_TOKEN}"
    if TELEGRAM_TOKEN else ""
)


# ============================================================
# DATABASE: PERSISTENT DEDUPLICATION
# ============================================================

def init_database():
    db_path = Path(DATABASE_PATH)
    if str(db_path.parent) not in ("", "."):
        db_path.parent.mkdir(parents=True, exist_ok=True)

    with sqlite3.connect(DATABASE_PATH, timeout=30) as db:
        db.execute("PRAGMA journal_mode=WAL")
        db.execute("""
            CREATE TABLE IF NOT EXISTS sent_notifications (
                provider TEXT NOT NULL,
                item_id TEXT NOT NULL,
                sent_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (provider, item_id)
            )
        """)
        db.execute("""
            CREATE TABLE IF NOT EXISTS received_tasks (
                task_id TEXT PRIMARY KEY,
                title TEXT NOT NULL,
                company TEXT NOT NULL,
                budget_usd REAL NOT NULL,
                client_email TEXT NOT NULL,
                direct_link TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'received',
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
        """)
        db.commit()


def notification_was_sent(provider: str, item_id: str) -> bool:
    with sqlite3.connect(DATABASE_PATH, timeout=30) as db:
        row = db.execute(
            """
            SELECT 1 FROM sent_notifications
            WHERE provider = ? AND item_id = ?
            """,
            (provider, item_id),
        ).fetchone()
        return row is not None


def mark_notification_sent(provider: str, item_id: str):
    with sqlite3.connect(DATABASE_PATH, timeout=30) as db:
        db.execute(
            """
            INSERT OR IGNORE INTO sent_notifications (provider, item_id)
            VALUES (?, ?)
            """,
            (provider, item_id),
        )
        db.commit()


def record_task(task_id: str, task: "TaskPayload"):
    with sqlite3.connect(DATABASE_PATH, timeout=30) as db:
        db.execute(
            """
            INSERT OR IGNORE INTO received_tasks
            (task_id, title, company, budget_usd, client_email, direct_link)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                task_id,
                task.title,
                task.company,
                task.budget_usd,
                str(task.client_email),
                str(task.direct_link),
            ),
        )
        db.commit()


# ============================================================
# TASK MODEL AND WEBHOOK AUTHENTICATION
# ============================================================

class TaskPayload(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    title: str = Field(min_length=1, max_length=250)
    company: str = Field(min_length=1, max_length=200)
    budget_usd: float = Field(gt=0, allow_inf_nan=False)
    client_email: EmailStr
    direct_link: HttpUrl


def safe_text(value) -> str:
    """Escape untrusted values for Telegram HTML."""
    return html.escape(str(value or ""), quote=True)


def valid_https_url(value: str) -> bool:
    try:
        parsed = urlparse(value)
        return parsed.scheme == "https" and bool(parsed.hostname)
    except Exception:
        return False


async def send_telegram(
    client: httpx.AsyncClient,
    text: str,
    reply_markup: Optional[dict] = None,
) -> bool:
    if not TELEGRAM_BASE_URL or not TARGET_CHAT_ID:
        logger.warning("Telegram is not configured.")
        return False

    payload = {
        "chat_id": TARGET_CHAT_ID,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }

    if reply_markup:
        payload["reply_markup"] = reply_markup

    try:
        response = await client.post(
            f"{TELEGRAM_BASE_URL}/sendMessage",
            json=payload,
            timeout=15,
        )
        response.raise_for_status()

        result = response.json()
        if not result.get("ok"):
            logger.error("Telegram API returned ok=false.")
            return False

        return True

    except (httpx.HTTPError, ValueError) as exc:
        logger.error("Telegram delivery failed: %s", type(exc).__name__)
        return False


def check_webhook_secret(provided: Optional[str]) -> bool:
    if not TASK_WEBHOOK_SECRET or not provided:
        return False

    return hmac.compare_digest(
        TASK_WEBHOOK_SECRET,
        provided,
    )


# ============================================================
# PAYSTACK: CHECKOUT INITIALIZATION ONLY
# ============================================================

async def initialize_paystack_checkout(
    client: httpx.AsyncClient,
    task: TaskPayload,
    task_id: str,
) -> Optional[str]:
    """
    Creates a checkout request only.

    This does NOT verify that a payment was received.
    A separate verified Paystack webhook is required for that.
    """
    if not PAYSTACK_SECRET:
        logger.info("Paystack is not configured; checkout skipped.")
        return None

    if PAYSTACK_CHARGE_KOBO <= 0:
        logger.error("PAYSTACK_CHARGE_KOBO must be positive.")
        return None

    try:
        response = await client.post(
            "https://api.paystack.co/transaction/initialize",
            headers={
                "Authorization": f"Bearer {PAYSTACK_SECRET}",
                "Content-Type": "application/json",
            },
            json={
                "email": str(task.client_email),
                "amount": PAYSTACK_CHARGE_KOBO,
                "currency": "NGN",
                "metadata": {
                    "task_id": task_id,
                    "task_title": task.title[:200],
                },
            },
            timeout=20,
        )

        response.raise_for_status()
        body = response.json()

        if body.get("status") is not True:
            logger.warning("Paystack did not initialize checkout.")
            return None

        data = body.get("data") or {}
        checkout_url = data.get("authorization_url")

        if checkout_url and valid_https_url(checkout_url):
            return checkout_url

        logger.warning("Paystack returned no valid HTTPS checkout URL.")
        return None

    except (httpx.HTTPError, ValueError) as exc:
        logger.error(
            "Paystack initialization failed: %s",
            type(exc).__name__,
        )
        return None


# ============================================================
# TASK PROCESSING
# ============================================================

async def process_live_task(task_id: str, task: TaskPayload):
    async with httpx.AsyncClient(
        follow_redirects=False,
        timeout=20,
    ) as client:

        payment_link = await initialize_paystack_checkout(
            client, task, task_id
        )

        title = safe_text(task.title)
        company = safe_text(task.company)
        budget = f"{task.budget_usd:.2f}"

        message = (
            "🌍 <b>LIVE TASK SIGNAL</b>\n"
            "────────────────────\n"
            f"📌 <b>Task:</b> <code>{title}</code>\n"
            f"🏢 <b>Company:</b> {company}\n"
            f"💰 <b>Listed budget:</b> ${budget} USD\n"
            f"🆔 <b>Task ID:</b> <code>{safe_text(task_id)}</code>\n"
            "────────────────────\n"
            "⚠️ A listed budget is not proof of payment."
        )

        keyboard = None
        if payment_link:
            keyboard = {
                "inline_keyboard": [[
                    {
                        "text": "Open Checkout",
                        "url": payment_link,
                    }
                ]]
            }

        sent = await send_telegram(client, message, keyboard)

        if not sent:
            logger.warning("Task notification could not be delivered.")


@app.post("/webhook/task")
async def receive_external_task(
    task: TaskPayload,
    background_tasks: BackgroundTasks,
    x_webhook_secret: Optional[str] = Header(
        default=None,
        alias="X-Webhook-Secret",
    ),
):
    if not check_webhook_secret(x_webhook_secret):
        raise HTTPException(
            status_code=401,
            detail="Invalid or missing webhook credentials.",
        )

    if task.budget_usd < TASK_MINIMUM_USD:
        raise HTTPException(
            status_code=400,
            detail=f"Budget is below ${TASK_MINIMUM_USD:.2f} USD.",
        )

    # Stable key allows a sender to retry the same payload safely
    # when it reuses the same task identifier.
    # Supply task_id in a future signed payload if your provider has one.
    import hashlib

    task_id = hashlib.sha256(
        (
            str(task.title) + "|" +
            str(task.company) + "|" +
            str(task.client_email) + "|" +
            str(task.direct_link)
        ).encode("utf-8")
    ).hexdigest()

    record_task(task_id, task)

    background_tasks.add_task(process_live_task, task_id, task)

    return {
        "status": "accepted",
        "task_id": task_id,
        "message": "Task accepted for processing; payment is not confirmed.",
    }


# ============================================================
# AFFILIATE PROVIDER HELPERS
# ============================================================

def extract_records(body, key: str) -> list:
    """Handle common list/dict response shapes without inventing records."""
    if not isinstance(body, dict):
        return []

    records = body.get(key, [])
    if isinstance(records, list):
        return records

    if isinstance(records, dict):
        return [records]

    return []


async def notify_once(
    provider: str,
    item_id: str,
    message: str,
    keyboard: Optional[dict] = None,
):
    if not item_id:
        return

    if notification_was_sent(provider, item_id):
        return

    async with httpx.AsyncClient(timeout=15) as client:
        delivered = await send_telegram(client, message, keyboard)

    # Mark only after Telegram confirms delivery. If delivery fails,
    # a later scan can retry.
    if delivered:
        mark_notification_sent(provider, item_id)


async def scan_partnerstack():
    if not PARTNERSTACK_API_KEY:
        return

    async with httpx.AsyncClient(timeout=20) as client:
        response = await client.get(
            "https://api.partnerstack.com/api/v2/rewards",
            headers={
                "Authorization": f"Bearer {PARTNERSTACK_API_KEY}",
                "Accept": "application/json",
            },
            params={"limit": 100},
        )
        response.raise_for_status()
        body = response.json()

    records = extract_records(body, "data")

    for reward in records:
        if not isinstance(reward, dict):
            continue

        item_id = str(reward.get("key") or "").strip()
        if not item_id:
            continue

        # PartnerStack reward amounts are commonly expressed in cents,
        # but don't treat an absent or malformed amount as earnings.
        try:
            amount_raw = reward.get("amount")
            amount = float(amount_raw) / 100.0
        except (TypeError, ValueError):
            amount = None

        title = safe_text(
            reward.get("description") or reward.get("title") or "Reward"
        )
        currency = safe_text(reward.get("currency") or "USD")
        status = safe_text(reward.get("status") or "unknown")

        amount_text = (
            f"{amount:.2f} {currency}"
            if amount is not None
            else "Amount unavailable"
        )

        message = (
            "🚀 <b>PARTNERSTACK REWARD RECORD</b>\n"
            "────────────────────\n"
            f"📌 <b>Details:</b> {title}\n"
            f"💰 <b>Recorded amount:</b> {amount_text}\n"
            f"📊 <b>Status:</b> {status}\n"
            f"🆔 <b>Record:</b> <code>{safe_text(item_id)}</code>\n"
            "This record is not proof of a completed payout."
        )

        await notify_once("partnerstack", item_id, message)


async def scan_impact():
    if not IMPACT_ACCOUNT_SID or not IMPACT_AUTH_TOKEN:
        return

    async with httpx.AsyncClient(timeout=20) as client:
        response = await client.get(
            f"https://api.impact.com/Mediapartners/"
            f"{IMPACT_ACCOUNT_SID}/Campaigns",
            auth=(IMPACT_ACCOUNT_SID, IMPACT_AUTH_TOKEN),
            headers={"Accept": "application/json"},
        )
        response.raise_for_status()
        body = response.json()

    records = extract_records(body, "Campaigns")

    for campaign in records:
        if not isinstance(campaign, dict):
            continue

        item_id = str(
            campaign.get("Id") or campaign.get("CampaignId") or ""
        ).strip()

        if not item_id:
            continue

        name = safe_text(
            campaign.get("CampaignName")
            or campaign.get("Name")
            or "Unnamed campaign"
        )

        tracking_link = campaign.get("TrackingLink")
        keyboard = None

        if isinstance(tracking_link, str) and valid_https_url(tracking_link):
            keyboard = {
                "inline_keyboard": [[
                    {"text": "Open Affiliate Link", "url": tracking_link}
                ]]
            }

        message = (
            "🚀 <b>IMPACT CAMPAIGN</b>\n"
            "────────────────────\n"
            f"🏢 <b>Campaign:</b> {name}\n"
            f"🆔 <b>Campaign ID:</b> <code>{safe_text(item_id)}</code>\n"
            "A campaign listing is not a confirmed commission."
        )

        await notify_once("impact", item_id, message, keyboard)


async def enterprise_affiliate_worker():
    logger.info("Affiliate worker started.")

    while True:
        try:
            await scan_partnerstack()
        except Exception:
            logger.exception("PartnerStack scan failed.")

        try:
            await scan_impact()
        except Exception:
            logger.exception("Impact scan failed.")

        await asyncio.sleep(AFFILIATE_SCAN_SECONDS)


# ============================================================
# OPTIONAL SPORTS PAGE OBSERVER
# ============================================================

async def live_network_sniffer():
    urls = [
        url for url in (
            SPORTYBET_PREMATCH_URL,
            BET9JA_PREMATCH_URL,
        )
        if url
    ]

    if not urls:
        logger.info("Sports observer disabled: no URLs configured.")
        return

    for url in urls:
        if not valid_https_url(url):
            logger.error("Ignoring a sports URL that is not HTTPS.")
            return

    logger.info("Sports page observer started.")

    while True:
        try:
            async with async_playwright() as p:
                browser = await p.chromium.launch(
                    headless=True,
                    args=["--no-sandbox", "--disable-dev-shm-usage"],
                )

                try:
                    context = await browser.new_context()
                    page = await context.new_page()

                    async def inspect_response(response):
                        if response.status != 200:
                            return

                        if not any(
                            word in response.url.lower()
                            for word in (
                                "schedule", "odds", "market", "fixture"
                            )
                        ):
                            return

                        content_type = response.headers.get(
                            "content-type", ""
                        ).lower()

                        if "json" not in content_type:
                            return

                        try:
                            payload = await response.json()
                            if isinstance(payload, (dict, list)):
                                logger.info(
                                    "Observed JSON response from sports page; "
                                    "no verified fixture parser is configured."
                                )
                        except Exception:
                            logger.debug("Could not parse response JSON.")

                    page.on("response", inspect_response)

                    for url in urls:
                        try:
                            await page.goto(
                                url,
                                timeout=60000,
                                wait_until="domcontentloaded",
                            )
                            await asyncio.sleep(15)
                        except Exception:
                            logger.exception("Sports page navigation failed.")

                    await context.close()
                finally:
                    await browser.close()

        except Exception:
            logger.exception("Sports observer cycle failed.")

        await asyncio.sleep(SPORTS_SCAN_SECONDS)


# ============================================================
# FASTAPI LIFECYCLE
# ============================================================

@asynccontextmanager
async def lifespan(app: FastAPI):
    init_database()

    workers = [
        asyncio.create_task(
            enterprise_affiliate_worker(),
            name="affiliate-worker",
        ),
        asyncio.create_task(
            live_network_sniffer(),
            name="sports-observer",
        ),
    ]

    logger.info("Production engine started.")

    try:
        yield
    finally:
        for worker in workers:
            worker.cancel()

        await asyncio.gather(*workers, return_exceptions=True)
        logger.info("Production engine stopped.")


app = FastAPI(
    title="Pure Dynamic Production Engine",
    lifespan=lifespan,
)


@app.get("/")
async def health_check():
    return {
        "status": "online",
        "mode": "configured_integrations",
        "telegram_configured": bool(
            TELEGRAM_TOKEN and TARGET_CHAT_ID
        ),
        "paystack_configured": bool(PAYSTACK_SECRET),
        "partnerstack_configured": bool(PARTNERSTACK_API_KEY),
        "impact_configured": bool(
            IMPACT_ACCOUNT_SID and IMPACT_AUTH_TOKEN
        ),
        "sports_observer_configured": bool(
            SPORTYBET_PREMATCH_URL or BET9JA_PREMATCH_URL
        ),
    }


@app.get("/health")
async def health():
    return {"status": "ok"}
