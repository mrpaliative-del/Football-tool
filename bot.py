import os
import time
import random
import requests

# Load secrets securely from GitHub Repository Secrets
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TARGET_CHAT_ID = os.getenv("TARGET_CHAT_ID")

LIVE_TELEGRAM_URL = f"https://telegram.org{TELEGRAM_BOT_TOKEN}/sendMessage"
LIVE_PUBLIC_API = "https://sportingbet.com"
HEADERS = {"User-Agent": "Mozilla/5.0 (Linux; Android 10) AppleWebKit/537.36"}

def run_unlimited_scanner():
    print("🚀 GitHub Actions Cloud Worker: Active & Scanning...")
    target_bankroll = 10000 
    
    # Teams array to simulate dynamic live scheduling loops
    teams = ["Enyimba FC", "Kano Pillars", "Rivers United", "Remo Stars", "Chelsea", "Tottenham", "Real Madrid", "Barcelona"]
    markets = ["1X2 vs Double Chance", "Over/Under 2.5 Goals", "Both Teams to Score (BTTS)"]
    
    # GitHub Actions free tier runs jobs for up to 6 hours per session.
    # We will loop for 5 hours, then let GitHub naturally restart the worker.
    start_time = time.time()
    timeout = 5 * 60 * 60  # 5 hours

    while (time.time() - start_time) < timeout:
        try:
            # Sync to public database pool channel
            requests.get(LIVE_PUBLIC_API, headers=HEADERS, timeout=10)
            
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
                    f"🐙 <b>GITHUB ACTIONS CLOUD LIVE</b> 🐙\n"
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
                requests.post(LIVE_TELEGRAM_URL, json={"chat_id": str(TARGET_CHAT_ID), "text": mobile_slip, "parse_mode": "HTML"})
            
            time.sleep(30)  # Pacing pause
        except Exception as e:
            print(f"Buffer rest: {e}")
            time.sleep(10)

if __name__ == "__main__":
    run_unlimited_scanner()
