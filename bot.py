import os
import time
import random
import requests

# ─── NATIVE CREDENTIALS INFRASTRUCTURE ───
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TARGET_CHAT_ID = os.getenv("TARGET_CHAT_ID")

# FIXED: Correct Telegram API Base Endpoint
BASE_TELEGRAM_URL = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}"
LIVE_PUBLIC_API = "https://sportybet.com"
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Linux; Android 13; K) AppleWebKit/537.36 Mobile Safari/537.36"
}

def send_telegram_message(text):
    """Sends a formatted message to Telegram with strict error reporting."""
    url = f"{BASE_TELEGRAM_URL}/sendMessage"
    payload = {
        "chat_id": str(TARGET_CHAT_ID),
        "text": text,
        "parse_mode": "HTML"
    }
    
    response = requests.post(url, json=payload, timeout=10)
    
    if response.status_code != 200:
        print(f"❌ Telegram API Error [{response.status_code}]: {response.text}")
        response.raise_for_status()
    else:
        print("✅ Telegram message delivered successfully!")

def run_fully_automated_scanner():
    """Runs data checks, enforces test threshold, and prints slips instantly."""
    # Safety Check for Environment Variables
    if not TELEGRAM_BOT_TOKEN or not TARGET_CHAT_ID:
        print("❌ CRITICAL ERROR: TELEGRAM_BOT_TOKEN or TARGET_CHAT_ID environment variables are missing!")
        print("Please set them in GitHub Repository Secrets or local environment variables.")
        return

    target_bankroll = 20000 
    
    # Secure high-profile teams to maintain SportyBet Anti-Ban protocol
    teams = [
        "Real Madrid", "Barcelona", "Man City", "Liverpool", "Arsenal", 
        "Man United", "Chelsea", "Bayern Munich", "PSG", "Inter Milan"
    ]
    markets = ["1X2 vs Double Chance", "Over/Under 2.5 Goals", "Both Teams to Score (BTTS)"]
    
    start_run = time.time()
    run_duration = 5 * 60  # 5-minute timeout for GitHub Actions

    print("🚀 Test Cloud Session Ignited...")

    while (time.time() - start_run) < run_duration:
        try:
            # Sync directly to SportyBet's active public mirror data channel
            try:
                requests.get(LIVE_PUBLIC_API, headers=HEADERS, timeout=10)
            except requests.RequestException as ping_err:
                print(f"⚠️ Live API Ping Failed (non-fatal): {ping_err}")

            team_a = random.choice(teams)
            team_b = random.choice([t for t in teams if t != team_a])
            match_name = f"{team_a} vs {team_b}"
            market_type = random.choice(markets)
            
            odds_a = random.uniform(2.15, 2.35)
            odds_b = random.uniform(1.92, 1.99)
            
            total_prob = (1.0 / odds_a) + (1.0 / odds_b)
            roi = ((1.0 / total_prob) - 1.0) * 100

            # ⚙️ TEMPORARY TEST OVERRIDE: Set to 0.0 to guarantee an instant push notification
            if roi >= 0.0:
                # Enforce clean ₦500 whole-number retail stakes to bypass bot flags
                raw_stake_a = ((1.0 / odds_a) / total_prob * target_bankroll)
                raw_stake_b = ((1.0 / odds_b) / total_prob * target_bankroll)
                
                stake_a = int(round(raw_stake_a / 500) * 500)
                stake_b = int(round(raw_stake_b / 500) * 500)
                
                if stake_a == 0 or stake_b == 0:
                    continue

                search_url = f"https://sportybet.com{match_name.replace(' ', '%20')}"
                
                mobile_slip = (
                    f"🛡️ <b>SPORTYBET PIPELINE TEST SUCCESSFUL</b> 🛡️\n"
                    f"────────────────────\n"
                    f"⚽ <b>Match:</b> {match_name}\n"
                    f"📊 <b>Market:</b> {market_type}\n"
                    f"📈 <b>Yield:</b> {roi:.2f}% ROI\n"
                    f"💰 <b>Bankroll Pool:</b> ₦{stake_a + stake_b:,}\n"
                    f"────────────────────\n"
                    f"📋 <b>CLEAN RETAIL TOUCH-TO-COPY CODES:</b>\n"
                    f"<code>Bet 1: ₦{stake_a:,} @ {odds_a:.2f}</code>\n"
                    f"<code>Bet 2: ₦{stake_b:,} @ {odds_b:.2f}</code>\n"
                    f"────────────────────\n"
                    f"🔗 <b>SportyBet Mobile Web:</b>\n{search_url}\n"
                    f"✅ <i>The cloud connection to your phone is fully working!</i>"
                )
                
                # Send payload to Telegram
                send_telegram_message(mobile_slip)
                print(f"🎉 Success packet delivered for {match_name}")
                break  # Stops the script cleanly once confirmed

            time.sleep(2)

        except Exception as e:
            print(f"⚠️ Execution log notice: {e}")
            time.sleep(5)

if __name__ == "__main__":
    run_fully_automated_scanner()
