
import os
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
    level=os.getenv("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger("ForeignMicroTaskEngine")

try:
    MINIMUM_REWARD_USD = Decimal(
        os.getenv("MINIMUM_REWARD_USD", "0.75")
    )
    if not MINIMUM_REWARD_USD.is_finite() or MINIMUM_REWARD_USD < 0:
        raise ValueError("Minimum reward must be a finite non-negative number")
except (InvalidOperation, ValueError):
    raise SystemExit("Invalid MINIMUM_REWARD_USD configuration")

DB_PATH = os.getenv("TASK_DB_PATH", "task_engine.sqlite3")
TELEGRAM_TOKEN = os.getenv("TELEGRAMBOTTOKEN", "").strip()
TARGET_CHAT_ID = os.getenv("TARGETCHATID", "").strip()

# Each rate represents USD per one unit of the named currency.
# Only use exchange rates from a trusted source.
try:
    raw_fx = json.loads(
        os.getenv("FX_TO_USD_JSON", '{"USD":"1"}')
    )
    if not isinstance(raw_fx, dict):
        raise ValueError("FX configuration must be a JSON object")

    FX_TO_USD = {}
    for currency, value in raw_fx.items():
        rate = Decimal(str(value))
        code = str(currency).strip().upper()

        if (
            len(code) == 3
            and code.isalpha()
            and rate.is_finite()
            and rate > 0
        ):
            FX_TO_USD[code] = rate

    if "USD" not in FX_TO_USD:
        FX_TO_USD["USD"] = Decimal("1")

except (ValueError, TypeError, InvalidOperation):
    raise SystemExit("Invalid FX_TO_USD_JSON; expected positive currency rates")

SOURCES = {
    "remoteok": "https://remoteok.com/api",
    "remotive": "https://remotive.com/api/remote-jobs",
}

HTTP_TIMEOUT = httpx.Timeout(20.0, connect=10.0)
MAX_FEED_ITEMS = 500
MAX_PENDING_NOTIFICATIONS = 100


# =========================================================
# DATABASE
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

                CREATE TABLE IF NOT EXISTS task_notifications (
                    task_id TEXT PRIMARY KEY,
                    status TEXT NOT NULL,
                    attempts INTEGER NOT NULL DEFAULT 0,
                    last_error TEXT,
                    updated_at INTEGER NOT NULL,
                    FOREIGN KEY(task_id) REFERENCES tasks(task_id)
                );

                CREATE INDEX IF NOT EXISTS idx_tasks_status
                    ON tasks(status);

                CREATE INDEX IF NOT EXISTS idx_notifications_status
                    ON task_notifications(status);
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
                task["task_id"], task["source"], task["source_id"],
                task["title"], task["company"], task["url"],
                task["reward_amount"], task["reward_currency"],
                task["reward_usd"], task["reward_basis"],
                task["eligibility"], task["status"], now, now,
            ))

            inserted = cursor.rowcount == 1

            if inserted:
                db.execute("""
                    INSERT INTO task_events (
                        task_id, old_status, new_status, note, created_at
                    ) VALUES (?, NULL, ?, ?, ?)
                """, (
                    task["task_id"], task["status"],
                    "Discovered from public feed; reward not independently verified",
                    now,
                ))

            return inserted

    def pending_notifications(self, limit=MAX_PENDING_NOTIFICATIONS):
        with self.connect() as db:
            rows = db.execute("""
                SELECT t.*
                FROM tasks AS t
                LEFT JOIN task_notifications AS n
                    ON n.task_id = t.task_id
                WHERE t.status = 'qualified'
                  AND (n.status IS NULL OR n.status != 'sent')
                ORDER BY t.created_at ASC
                LIMIT ?
            """, (limit,)).fetchall()

        return [dict(row) for row in rows]

    def record_notification(self, task_id, sent, error=None):
        now = int(time.time())
        status = "sent" if sent else "pending"

        with self.connect() as db:
            db.execute("""
                INSERT INTO task_notifications (
                    task_id, status, attempts, last_error, updated_at
                ) VALUES (?, ?, 1, ?, ?)
                ON CONFLICT(task_id) DO UPDATE SET
                    status = excluded.status,
                    attempts = task_notifications.attempts + 1,
                    last_error = excluded.last_error,
                    updated_at = excluded.updated_at
            """, (
                task_id, status, (error or "")[:1000] or None, now,
            ))

    def set_status(self, task_id, new_status, note=""):
        allowed = {
            "discovered", "qualified", "solving",
            "ready_to_submit", "submitted", "accepted",
            "payout_pending", "paid", "rejected",
            "needs_review", "failed",
        }

        if new_status not in allowed:
            raise ValueError("Invalid task status")

        now = int(time.time())

        with self.connect() as db:
            row = db.execute(
                "SELECT status FROM tasks WHERE task_id=?",
                (task_id,),
            ).fetchone()

            if not row:
                raise KeyError("Task not found")

            db.execute("""
                UPDATE tasks SET status=?, updated_at=? WHERE task_id=?
            """, (new_status, now, task_id))

            db.execute("""
                INSERT INTO task_events (
                    task_id, old_status, new_status, note, created_at
                ) VALUES (?, ?, ?, ?, ?)
            """, (
                task_id, row["status"], new_status, note[:1000], now,
            ))

    def record_submission(
        self, task_id, adapter, external_id, status, evidence=None
    ):
        with self.connect() as db:
            db.execute("""
                INSERT OR IGNORE INTO submissions (
                    task_id, adapter, external_id, status,
                    evidence, created_at
                ) VALUES (?, ?, ?, ?, ?, ?)
            """, (
                task_id, adapter, external_id, status,
                json.dumps(evidence or {}), int(time.time()),
            ))

    def credit_verified_payout(
        self, provider, transaction_id, currency, amount,
        verified, evidence, task_id=None
    ):
        # `verified` must be set only by trusted server-side verification.
        if verified is not True:
            raise ValueError("Unverified payment cannot be credited")

        amount = decimal_value(amount)
        if amount is None or amount <= 0:
            raise ValueError("Payment amount must be positive")

        currency = str(currency or "").strip().upper()
        provider = str(provider or "").strip().lower()
        transaction_id = str(transaction_id or "").strip()

        if not provider or not transaction_id or not currency:
            raise ValueError("Missing transaction identifiers")

        now = int(time.time())

        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")

            existing = db.execute("""
                SELECT 1 FROM earnings_ledger
                WHERE provider=? AND transaction_id=?
            """, (provider, transaction_id)).fetchone()

            if existing:
                return False

            # The ledger and payout record are committed together.
            db.execute("""
                INSERT INTO payouts (
                    provider, transaction_id, currency, amount,
                    task_id, evidence, received_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """, (
                provider, transaction_id, currency, str(amount),
                task_id, json.dumps(evidence or {}), now,
            ))

            db.execute("""
                INSERT INTO earnings_ledger (
                    provider, transaction_id, currency,
                    amount, task_id, credited_at
                ) VALUES (?, ?, ?, ?, ?, ?)
            """, (
                provider, transaction_id, currency,
                str(amount), task_id, now,
            ))

            if task_id:
                row = db.execute(
                    "SELECT status FROM tasks WHERE task_id=?",
                    (task_id,),
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
                        "Verified payout recorded", now,
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

        return {key: str(value) for key, value in totals.items()}


# =========================================================
# REWARD QUALIFICATION
# =========================================================

def decimal_value(value):
    if value is None or isinstance(value, bool):
        return None

    try:
        result = Decimal(str(value).replace(",", "").strip())
        if not result.is_finite() or result < 0:
            return None
        return result
    except (ValueError, TypeError, InvalidOperation):
        return None


def valid_https_url(value):
    try:
        parsed = urlparse(str(value))
        return (
            parsed.scheme == "https"
            and bool(parsed.hostname)
            and not parsed.username
            and not parsed.password
        )
    except (TypeError, ValueError):
        return False


def qualify_reward(amount, currency, basis):
    amount = decimal_value(amount)

    if amount is None or not currency:
        return False, None, "Reward amount/currency unavailable"

    if basis not in {"task_total", "fixed_project"}:
        return False, None, "Total task reward not established"

    rate = FX_TO_USD.get(str(currency).upper())
    if rate is None:
        return False, None, "No configured exchange rate"

    reward_usd = amount * rate

    if reward_usd < MINIMUM_REWARD_USD:
        return False, reward_usd, "Below minimum reward"

    return True, reward_usd, "Qualified by feed fields; verify original listing"


# =========================================================
# FEED NORMALIZATION
# =========================================================

def normalize_job(source, job):
    if not isinstance(job, dict):
        return None

    if source == "remoteok":
        title = job.get("position")
        company = job.get("company") or "Unknown company"
        url = job.get("url") or job.get("apply_url")
        source_id = str(job.get("id") or url or "")
    elif source == "remotive":
        title = job.get("title")
        company = job.get("company_name") or "Unknown company"
        url = job.get("url")
        source_id = str(job.get("id") or url or "")
    else:
        return None

    if not title or not url or not valid_https_url(url):
        return None

    title = str(title).strip()
    company = str(company).strip()

    if not title:
        return None

    amount = decimal_value(job.get("reward_amount"))
    currency_value = job.get("reward_currency")
    currency = (
        str(currency_value).strip().upper()
        if currency_value else None
    )
    basis = str(job.get("reward_basis", "unknown")).strip()

    qualifies, reward_usd, reason = qualify_reward(
        amount, currency, basis
    )

    stable_source_id = source_id or (
        title + "|" + company + "|" + str(url)
    )
    stable_id = hashlib.sha256(
        f"{source}:{stable_source_id}".encode("utf-8")
    ).hexdigest()

    return {
        "task_id": stable_id,
        "source": source,
        "source_id": stable_source_id[:500],
        "title": title[:300],
        "company": company[:200],
        "url": str(url),
        "reward_amount": str(amount) if amount is not None else None,
        "reward_currency": currency,
        "reward_usd": str(reward_usd) if reward_usd is not None else None,
        "reward_basis": basis[:100] or "unknown",
        "eligibility": str(
            job.get("candidate_required_location")
            or job.get("location")
            or "Not verified"
        )[:500],
        "status": "qualified" if qualifies else "needs_review",
        "qualification_reason": reason,
        "qualifies": qualifies,
    }


# =========================================================
# TELEGRAM
# =========================================================

async def send_telegram(client, task):
    if not TELEGRAM_TOKEN or not TARGET_CHAT_ID:
        return False, "Telegram credentials are not configured"

    reward_usd = decimal_value(task.get("reward_usd"))
    if reward_usd is None:
        return False, "Qualified task has no valid USD equivalent"

    message = (
        "🌍 <b>TASK OPPORTUNITY — CHECK BEFORE APPLYING</b>\n"
        "────────────────────\n"
        f"📌 <b>Title:</b> {html.escape(task['title'])}\n"
        f"🏢 <b>Company:</b> {html.escape(task['company'])}\n"
        f"💰 <b>Listed reward:</b> "
        f"{html.escape(str(task['reward_amount']))} "
        f"{html.escape(str(task['reward_currency']))}\n"
        f"💵 <b>Estimated USD:</b> ${reward_usd:.2f}\n"
        f"🌐 <b>Eligibility:</b> {html.escape(task['eligibility'])}\n"
        f"🧾 <b>Reward basis:</b> "
        f"{html.escape(task['reward_basis'])}\n"
        "⚠️ <i>Check eligibility, terms and reward with the provider. "
        "This is not proof of acceptance or payment.</i>\n"
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
                "disable_web_page_preview": True,
            },
            timeout=15,
        )

        response.raise_for_status()
        body = response.json()

        if body.get("ok") is True:
            return True, None

        return False, str(body.get("description", "Telegram API rejected message"))

    except (httpx.HTTPError, ValueError) as exc:
        log.warning("Telegram notification failed: %s", type(exc).__name__)
        return False, str(exc)[:500]


# =========================================================
# PROVIDER ADAPTER CONTRACTS
# =========================================================

class SolverAdapter:
    name = "unconfigured"

    async def solve(self, task):
        raise NotImplementedError(
            "Implement a provider-approved solver adapter"
        )


class DeliveryAdapter:
    name = "unconfigured"

    async def deliver(self, task, result):
        raise NotImplementedError(
            "Implement an authorized task submission adapter"
        )


class PayoutVerificationAdapter:
    name = "unconfigured"

    async def fetch_verified_payouts(self):
        raise NotImplementedError(
            "Implement official payout verification"
        )


# =========================================================
# ENGINE — ONE SCAN PER INVOCATION
# =========================================================

class ForeignMicroTaskEngine:
    def __init__(self):
        self.db = Database()

    async def fetch_feed(self, client, source, url):
        try:
            response = await client.get(
                url,
                headers={
                    "User-Agent": "ForeignMicroTaskEngine/1.0",
                    "Accept": "application/json",
                },
            )
            response.raise_for_status()
            data = response.json()

            if source == "remoteok":
                if not isinstance(data, list):
                    log.warning("Unexpected RemoteOK response format")
                    return []
                jobs = [
                    item for item in data
                    if isinstance(item, dict) and item.get("position")
                ]

            elif source == "remotive":
                if not isinstance(data, dict):
                    log.warning("Unexpected Remotive response format")
                    return []
                jobs = data.get("jobs", [])
                if not isinstance(jobs, list):
                    log.warning("Remotive jobs field is not a list")
                    return []

            else:
                return []

            return jobs[:MAX_FEED_ITEMS]

        except (httpx.HTTPError, ValueError) as exc:
            log.warning(
                "Feed request failed for %s: %s",
                source, type(exc).__name__,
            )
            return []

    async def deliver_pending_notifications(self, client):
        pending = self.db.pending_notifications()
        sent_count = 0

        for task in pending:
            sent, error = await send_telegram(client, task)
            self.db.record_notification(task["task_id"], sent, error)

            if sent:
                sent_count += 1
                log.info("Telegram alert sent for task %s", task["task_id"])
            else:
                log.warning(
                    "Notification remains pending for %s",
                    task["task_id"],
                )

        return sent_count, len(pending)

    async def scan_once(self):
        new_listings = 0
        qualified_new = 0

        async with httpx.AsyncClient(timeout=HTTP_TIMEOUT, follow_redirects=True) as client:
            for source, url in SOURCES.items():
                jobs = await self.fetch_feed(client, source, url)
                log.info("%s returned %d listings", source, len(jobs))

                for job in jobs:
                    task = normalize_job(source, job)
                    if task is None:
                        continue

                    if not self.db.save_task(task):
                        continue

                    new_listings += 1

                    if task["qualifies"]:
                        qualified_new += 1
                        log.info(
                            "Reward fields meet configured threshold: %s",
                            task["title"],
                        )
                    else:
                        log.info(
                            "Saved for review: %s — %s",
                            task["title"],
                            task["qualification_reason"],
                        )

            sent, attempted = await self.deliver_pending_notifications(client)

        log.info(
            "Scan complete: new=%d, newly_qualified=%d, "
            "notifications_sent=%d/%d, ledger=%s",
            new_listings, qualified_new, sent, attempted,
            self.db.balances(),
        )

    async def run_once(self):
        log.info("Global task engine starting")
        log.info("Minimum reward threshold: USD %s", MINIMUM_REWARD_USD)
        log.info("Database path: %s", DB_PATH)
        await self.scan_once()


if __name__ == "__main__":
    try:
        asyncio.run(ForeignMicroTaskEngine().run_once())
    except KeyboardInterrupt:
        log.info("Engine stopped")
    except Exception:
        log.exception("Engine failed")
        raise
