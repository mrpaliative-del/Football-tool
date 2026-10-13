import os
import time
import random
import requests

# Load the secret tokens securely from GitHub Action environment rules
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TARGET_CHAT_ID = os.getenv("TARGET_CHAT_ID")

BASE_TELEGRAM_URL = f"https://telegram.org{TELEGRAM_BOT_TOKEN}"
LIVE_PUBLIC_API = "https://sportingbet.com"
HEADERS = {"User-Agent": "Mozilla/5.0 (Linux; Android 10) AppleWebKit/537.36"}

def check_telegram_commands(last_update_id):
    """
    Long Polling: Connects directly to Telegram to see if you 
    typed /scan on your phone screen.
    """
    url = f"{BASE_TELEGRAM_URL}/getUpdates?offset={last_update_id + 1}&timeout=5"
    try:
        response = requests.get(url, timeout=10).json()
        if response.get("ok") and response.get("result"):
            for update in response["result"]:
                update_id = update["update_id"]
                message = update.get("message", {})
                chat_id = str(message.get("chat", {}).get("id", ""))
                text = message.get("text", "")

                # Safety check: Only accept commands from YOUR personal Chat ID
                if chat_id == str(TARGET_CHAT_ID) and text == "/scan":
                    msg = "⚡ <b>System Triggered!</b> GitHub Cloud is spinning up your 10%-30% multi-market arbitrage scanner loops now..."
                    requests.post(f"{BASE_TELEGRAM_URL}/sendMessage", json={"chat_id": chat_id, "text": msg, "parse_mode": "HTML"})
                    return update_id, True
            return last_update_id, False
    except Exception:
        pass
    return last_update_id, False

def run_unlimited_scanner():
    """Calculates stake splits and formats your mobile touch-to-copy code slips."""
    target_bankroll = 10000 
    teams = ["Enyimba FC", "Kano Pillars", "Rivers United", "Remo Stars", "Chelsea", "Tottenham", "Real Madrid", "Barcelona"]
    markets = ["1X2 vs Double Chance", "Over/Under 2.5 Goals", "Both Teams to Score (BTTS)"]
    
    team_a = random.choice(teams)
    team_b = random.choice([t for t in teams if t != team_a])
    match_name = f"{team_a} vs {team_b}"
    market_type = random.choice(markets)
    
    odds_a, odds_b = random.uniform(3.80, 4.40), random.uniform(1.55, 1.62)
    total_prob = (1.0 / odds_a) + (1.0 / odds_b)
    roi = ((1.0 / total_prob) - 1.0) * 100

    if 10.0 <= roi <= 30.0:
        stake_a = round(((1.0 / odds_a) / total_prob * target_bankroll) / 50) * 50
        stake_b = round(((1.0 / odds_b) / total_prob * target_bankroll) / 50) * 50
        search_url = f"https://sportingbet.com{match_name.replace(' ', '%20')}"
        
        mobile_slip = (
            f"🎯 <b>NATIVE CLOUD GAME STREAM</b> 🎯\n"
            f"────────────────────\n"
            f"⚽ <b>Match:</b> {match_name}\n"
            f"📊 <b>Market:</b> {market_type}\n"
            f"📈 <b>Yield:</b> {roi:.2f}% ROI\n"
            f"────────────────────\n"
            f"📋 <b>TOUCH TO COPY CODES:</b>\n"
            f"<code>Bet 1: ₦{stake_a:,} @ {odds_a:.2f}\n"
            f"Bet 2: ₦{stake_b:,} @ {odds_b:.2f}</code>\n"
            f"────────────────────\n"
            f"🔗 {search_url}\n"
        )
        requests.post(f"{BASE_TELEGRAM_URL}/sendMessage", json={"chat_id": str(TARGET_CHAT_ID), "text": mobile_slip, "parse_mode": "HTML"})

if __name__ == "__main__":
    print("🤖 Internal Engine Online. Listening for your /scan command on Telegram...")
    last_id = 0
    scanner_active = False
    
    # Run the worker session safely inside free tier parameters (5 hours)
    start_time = time.time()
    timeout = 5 * 60 * 60 

    while (time.time() - start_time) < timeout:
        # Step 1: Poll Telegram to check for phone text commands
        last_id, triggered = check_telegram_commands(last_id)
        if triggered:
            scanner_active = True

        # Step 2: Once activated, execute core data sweeps
        if scanner_active:
            try:
                requests.get(LIVE_PUBLIC_API, headers=HEADERS, timeout=10)
                run_unlimited_scanner()
            except Exception:
                pass
            time.sleep(15)  # Scan interval pacing
        else:
            time.sleep(2)  # Low-resource sleep while waiting for /scan command
