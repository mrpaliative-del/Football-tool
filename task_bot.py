
import os
import re
import json
import html
import time
import asyncio
import hashlib
import logging
import sqlite3
from decimal import Decimal, InvalidOperation
from urllib.parse import urlparse

import httpx

# =========================================================
# CONFIGURATION
# =========================================================

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="%(asctime)s [%(levelname)s] %(message)s"
)
log = logging.getLogger("ForeignMicroTaskEngine")

MINIMUM_REWARD_USD = Decimal(
    os.getenv("MINIMUM_REWARD_USD", "0.75")
)

SCAN_INTERVAL = max(
    60, int(os.getenv("SCAN_INTERVAL_SECONDS", "900"))
)

DB_PATH = os.getenv(
    "TASK_DB_PATH", "task_engine.sqlite3"
)

TELEGRAM_TOKEN = os.getenv("TELEGRAMBOTTOKEN", "").strip()
TARGET_CHAT_ID = os.getenv("TARGETCHATID", "").strip()

# USD value of one unit of each currency.
# Maintain rates using a trusted exchange-rate provider.
try:
    FX_TO_USD = {
        currency.upper(): Decimal(str(rate))
        for currency, rate in json.loads(
            os.getenv("FX_TO_USD_JSON", '{"USD":"1"}')
        ).items()
        if Decimal(str(rate)) > 0
    }
except (ValueError, TypeError, InvalidOperation):
    FX_TO_USD = {"USD": Decimal("1")}
    log.error("Invalid FX_TO_USD_JSON; check environment configuration.")

SOURCES = {
    "remoteok": "https://remoteok.com/api",
    "remotive": "https://remotive.com/api/remote-jobs",
}


# =========================================================
# PERSISTENT DATABASE
# =========================================================

class Database:

    def __init__(self, path=DB_PATH):
        self.path = path
        self.initialize()

    def connect(self):
        conn = sqlite3.connect(self.path, timeout=20)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA busy_timeout=20000")
        return conn

    def initialize(self):
        with self.connect() as db:
            db.execute("PRAGMA journal_mode=WAL")

            db.executescript("""
                CREATE TABLE IF NOT EXISTS tasks (
                    task_id TEXT PRIMARY KEY,
                    source TEXT NOT NULL,
                    source_id TEXT NOT NULL,
                    title TEXT NOT NULL,
                    company TEXT NOT NULL,
                    url TEXT NOT NULL,
                    reward_amount TEXT,
                    reward_currency TEXT,
                    reward_usd TEXT,
                    reward_basis TEXT NOT NULL,
                    eligibility TEXT,
                    status TEXT NOT NULL,
                    created_at INTEGER NOT NULL,
                    updated_at INTEGER NOT NULL
                );

                CREATE TABLE IF NOT EXISTS submissions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    task_id TEXT NOT NULL,
                    adapter TEXT NOT NULL,
                    external_id TEXT,
                    status TEXT NOT NULL,
                    evidence TEXT NOT NULL DEFAULT '{}',
                    created_at INTEGER NOT NULL,
                    FOREIGN KEY(task_id) REFERENCES tasks(task_id),
                    UNIQUE(task_id, adapter, external_id)
                );

                CREATE TABLE IF NOT EXISTS payouts (
                    provider TEXT NOT NULL,
                    transaction_id TEXT NOT NULL,
                    currency TEXT NOT NULL,
                    amount TEXT NOT NULL,
                    task_id TEXT,
                    evidence TEXT NOT NULL,
                    received_at INTEGER NOT NULL,
                    PRIMARY KEY(provider, transaction_id)
                );

                CREATE TABLE IF NOT EXISTS earnings_ledger (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    provider TEXT NOT NULL,
                    transaction_id TEXT NOT NULL,
                    currency TEXT NOT NULL,
                    amount TEXT NOT NULL,
                    task_id TEXT,
                    credited_at INTEGER NOT NULL,
                    UNIQUE(provider, transaction_id)
                );

                CREATE TABLE IF NOT EXISTS task_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    task_id TEXT NOT NULL,
                    old_status TEXT,
                    new_status TEXT NOT NULL,
                    note TEXT NOT NULL,
                    created_at INTEGER NOT NULL
                );
            """)

    def save_task(self, task):
        now = int(time.time())

        with self.connect() as db:
            cursor = db.execute("""
                INSERT OR IGNORE INTO tasks (
                    task_id, source, source_id, title, company,
                    url, reward_amount, reward_currency, reward_usd,
                    reward_basis, eligibility, status,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                task["task_id"],
                task["source"],
                task["source_id"],
                task["title"],
                task["company"],
                task["url"],
                task["reward_amount"],
                task["reward_currency"],
                task["reward_usd"],
                task["reward_basis"],
                task["eligibility"],
                task["status"],
                now,
                now
            ))

            inserted = cursor.rowcount == 1

            if inserted:
                db.execute("""
                    INSERT INTO task_events (
                        task_id, old_status, new_status,
                        note, created_at
                    ) VALUES (?, NULL, ?, ?, ?)
                """, (
                    task["task_id"],
                    task["status"],
                    "Discovered from public feed",
                    now
                ))

            return inserted

    def set_status(self, task_id, new_status, note=""):
        allowed = {
            "discovered", "qualified", "solving",
            "ready_to_submit", "submitted", "accepted",
            "payout_pending", "paid", "rejected",
            "needs_review", "failed"
        }

        if new_status not in allowed:
            raise ValueError("Invalid task status")

        now = int(time.time())

        with self.connect() as db:
            row = db.execute(
                "SELECT status FROM tasks WHERE task_id=?",
                (task_id,)
            ).fetchone()

            if not row:
                raise KeyError("Task not found")

            db.execute(
                "UPDATE tasks SET status=?, updated_at=? WHERE task_id=?",
                (new_status, now, task_id)
            )

            db.execute("""
                INSERT INTO task_events (
                    task_id, old_status, new_status,
                    note, created_at
                ) VALUES (?, ?, ?, ?, ?)
            """, (
                task_id, row["status"], new_status,
                note[:1000], now
            ))

    def record_submission(
        self, task_id, adapter, external_id,
        status, evidence=None
    ):
        with self.connect() as db:
            db.execute("""
                INSERT OR IGNORE INTO submissions (
                    task_id, adapter, external_id,
                    status, evidence, created_at
                ) VALUES (?, ?, ?, ?, ?, ?)
            """, (
                task_id, adapter, external_id, status,
                json.dumps(evidence or {}),
                int(time.time())
            ))

    def credit_verified_payout(
        self,
        provider,
        transaction_id,
        currency,
        amount,
        verified,
        evidence,
        task_id=None
    ):
        """
        Call only after an official provider adapter verifies
        the actual incoming transaction.

        `verified` must come from trusted server-side verification,
        never directly from a user or an unverified webhook.
        """

        if verified is not True:
            raise ValueError("Unverified payment cannot be credited")

        amount = Decimal(str(amount))
        currency = currency.upper().strip()
        provider = provider.strip().lower()
        transaction_id = transaction_id.strip()

        if amount <= 0:
            raise ValueError("Payment amount must be positive")

        if not provider or not transaction_id or not currency:
            raise ValueError("Missing transaction identifiers")

        now = int(time.time())

        with self.connect() as db:
            # Atomic transaction prevents duplicate credits.
            db.execute("BEGIN IMMEDIATE")

            existing = db.execute("""
                SELECT 1 FROM earnings_ledger
                WHERE provider=? AND transaction_id=?
            """, (provider, transaction_id)).fetchone()

            if existing:
                return False

            db.execute("""
                INSERT INTO payouts (
                    provider, transaction_id, currency,
                    amount, task_id, evidence, received_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(provider, transaction_id) DO NOTHING
            """, (
                provider, transaction_id, currency,
                str(amount), task_id,
                json.dumps(evidence or {}), now
            ))

            db.execute("""
                INSERT INTO earnings_ledger (
                    provider, transaction_id, currency,
                    amount, task_id, credited_at
                ) VALUES (?, ?, ?, ?, ?, ?)
            """, (
                provider, transaction_id, currency,
                str(amount), task_id, now
            ))

            if task_id:
                row = db.execute(
                    "SELECT status FROM tasks WHERE task_id=?",
                    (task_id,)
                ).fetchone()

                if row:
                    db.execute("""
                        UPDATE tasks SET status='paid', updated_at=?
                        WHERE task_id=?
                    """, (now, task_id))

                    db.execute("""
                        INSERT INTO task_events (
                            task_id, old_status, new_status,
                            note, created_at
                        ) VALUES (?, ?, 'paid', ?, ?)
                    """, (
                        task_id, row["status"],
                        f"Verified payout {provider}:{transaction_id}",
                        now
                    ))

            return True

    def balances(self):
        with self.connect() as db:
            rows = db.execute("""
                SELECT currency, amount FROM earnings_ledger
            """).fetchall()

        totals = {}

        for row in rows:
            currency = row["currency"]
            totals[currency] = (
                totals.get(currency, Decimal("0"))
                + Decimal(row["amount"])
            )

        return {k: str(v) for k, v in totals.items()}


# =========================================================
# REWARD QUALIFICATION
# =========================================================

def decimal_value(value):
    if value is None or isinstance(value, bool):
        return None

    try:
        value = Decimal(str(value).replace(",", "").strip())

        if not value.is_finite() or value < 0:
            return None

        return value
    except (ValueError, InvalidOperation):
        return None


def valid_https_url(value):
    try:
        parsed = urlparse(str(value))
        return parsed.scheme == "https" and bool(parsed.netloc)
    except Exception:
        return False


def qualify_reward(amount, currency, basis):
    """
    Qualifies total task/project rewards, not ambiguous salaries.
    Returns (qualifies, USD equivalent, explanation).
    """

    amount = decimal_value(amount)

    if amount is None or not currency:
        return False, None, "Reward amount/currency unavailable"

    if basis not in {"task_total", "fixed_project"}:
        return False, None, "Total task reward not established"

    rate = FX_TO_USD.get(currency.upper())

    if rate is None:
        return False, None, "No configured exchange rate"

    reward_usd = amount * rate

    if reward_usd < MINIMUM_REWARD_USD:
        return False, reward_usd, "Below minimum reward"

    return True, reward_usd, "Qualified"


# =========================================================
# FEED ADAPTERS
# =========================================================

def normalize_job(source, job):
    if source == "remoteok":
        title = job.get("position")
        company = job.get("company") or "Unknown company"
        url = job.get("url") or job.get("apply_url")
        source_id = str(job.get("id") or url or "")
    else:
        title = job.get("title")
        company = job.get("company_name") or "Unknown company"
        url = job.get("url")
        source_id = str(job.get("id") or url or "")

    if not title or not url or not valid_https_url(url):
        return None

    # Public feed salary data is not automatically a per-task reward.
    # Only explicitly normalized reward fields are accepted.
    amount = decimal_value(job.get("reward_amount"))
    currency = job.get("reward_currency")
    basis = job.get("reward_basis", "unknown")

    qualifies, reward_usd, reason = qualify_reward(
        amount, currency, basis
    )

    stable_id = hashlib.sha256(
        f"{source}:{source_id or title + str(company)}".encode()
    ).hexdigest()

    return {
        "task_id": stable_id,
        "source": source,
        "source_id": source_id,
        "title": str(title)[:300],
        "company": str(company)[:200],
        "url": url,
        "reward_amount": str(amount) if amount is not None else None,
        "reward_currency": currency.upper() if currency else None,
        "reward_usd": str(reward_usd) if reward_usd is not None else None,
        "reward_basis": basis,
        "eligibility": str(
            job.get("candidate_required_location")
            or job.get("location")
            or "Not verified"
        ),
        "status": "qualified" if qualifies else "needs_review",
        "qualification_reason": reason,
        "qualifies": qualifies,
    }


# =========================================================
# TELEGRAM ALERTS
# =========================================================

async def send_telegram(client, task):
    if not TELEGRAM_TOKEN or not TARGET_CHAT_ID:
        return False

    message = (
        "🌍 <b>MICROTASK OPPORTUNITY</b>\n"
        "────────────────────\n"
        f"📌 <b>Task:</b> {html.escape(task['title'])}\n"
        f"🏢 <b>Company:</b> {html.escape(task['company'])}\n"
        f"💰 <b>Reward:</b> {html.escape(str(task['reward_amount']))} "
        f"{html.escape(str(task['reward_currency']))}\n"
        f"💵 <b>USD equivalent:</b> ${Decimal(task['reward_usd']):.2f}\n"
        f"🌐 <b>Eligibility:</b> {html.escape(task['eligibility'])}\n"
        "⚠️ <i>Discovery is not proof of hiring, acceptance or payment.</i>\n"
        f'🔗 <a href="{html.escape(task["url"], quote=True)}">'
        "Open original listing</a>"
    )

    try:
        response = await client.post(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
            json={
                "chat_id": TARGET_CHAT_ID,
                "text": message,
                "parse_mode": "HTML",
                "disable_web_page_preview": True
            },
            timeout=10
        )

        response.raise_for_status()
        return response.json().get("ok") is True

    except (httpx.HTTPError, ValueError):
        log.exception("Telegram notification failed")
        return False


# =========================================================
# SOLVER / DELIVERY / PAYOUT ADAPTER CONTRACTS
# =========================================================

class SolverAdapter:
    """
    Implement for a specific task provider and task type.
    Do not submit fabricated answers or bypass provider restrictions.
    """

    name = "unconfigured"

    async def solve(self, task):
        raise NotImplementedError(
            "Configure a provider-approved solver adapter"
        )


class DeliveryAdapter:
    """
    Submit through an authorized provider API or submission interface.
    """

    name = "unconfigured"

    async def deliver(self, task, result):
        raise NotImplementedError(
            "Configure an authorized delivery adapter"
        )


class PayoutVerificationAdapter:
    """
    Connect this to the provider's official payout API or a verified
    transaction export. Never assume that an accepted task was paid.
    """

    name = "unconfigured"

    async def fetch_verified_payouts(self):
        raise NotImplementedError(
            "Configure an official payout verification adapter"
        )


# =========================================================
# ENGINE
# =========================================================

class ForeignMicroTaskEngine:

    def __init__(self):
        self.db = Database()

    async def fetch_feed(self, client, source, url):
        try:
            response = await client.get(
                url,
                headers={
                    "User-Agent": "ForeignMicroTaskEngine/1.0"
                },
                timeout=15
            )
            response.raise_for_status()
            data = response.json()

            if source == "remoteok":
                return [
                    x for x in data
                    if isinstance(x, dict) and x.get("position")
                ]

            if source == "remotive":
                return data.get("jobs", [])

        except (httpx.HTTPError, ValueError):
            log.exception("Feed failed: %s", source)

        return []

    async def scan_once(self):
        discovered = 0
        qualified = 0

        async with httpx.AsyncClient() as client:
            for source, url in SOURCES.items():
                jobs = await self.fetch_feed(client, source, url)
                log.info("%s returned %d listings", source, len(jobs))

                for job in jobs:
                    task = normalize_job(source, job)

                    if task is None:
                        continue

                    is_new = self.db.save_task(task)

                    if not is_new:
                        continue

                    discovered += 1

                    if not task["qualifies"]:
                        log.info(
                            "Saved for review: %s (%s)",
                            task["title"],
                            task["qualification_reason"]
                        )
                        continue

                    qualified += 1
                    sent = await send_telegram(client, task)

                    log.info(
                        "Qualified task saved: %s; Telegram sent=%s",
                        task["title"], sent
                    )

        return discovered, qualified

    async def run_forever(self):
        log.info("Foreign MicroTask Engine starting")
        log.info("Minimum reward: $%s", MINIMUM_REWARD_USD)
        log.info("Database: %s", DB_PATH)

        while True:
            try:
                discovered, qualified = await self.scan_once()

                log.info(
                    "Cycle finished: %d new listings, %d qualified",
                    discovered, qualified
                )

                log.info("Verified earnings ledger: %s", self.db.balances())

            except Exception:
                log.exception("Scan cycle failed")

            await asyncio.sleep(SCAN_INTERVAL)


if __name__ == "__main__":
    asyncio.run(ForeignMicroTaskEngine().run_forever())
