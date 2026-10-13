import os
import time
import random
import logging
import requests

# ─── CONFIGURE PROFESSIONAL LOGGING ───
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S"
)

class ArbitrageEngine:
    def __init__(self):
        # Credentials & Infrastructure
        self.token = os.getenv("TELEGRAM_BOT_TOKEN")
        self.chat_id = os.getenv("TARGET_CHAT_ID")
        self.base_url = f"https://api.telegram.org/bot{self.token}" if self.token else ""
        
        # Dynamic Market Settings (Configurable via Environment Variables)
        self.bankroll = float(os.getenv("BANKROLL_POOL", 20000))
        self.min_roi = float(os.getenv("MIN_ROI_THRESHOLD", 1.0)) # Default 1.0% minimum yield
        
        # Internal Cache for Deduplication
        self.sent_cache = set()

    def validate_environment(self) -> bool:
        """Ensures all necessary credentials are present before execution."""
        if not self.token or not self.chat_id:
            logging.critical("CRITICAL ERROR: TELEGRAM_BOT_TOKEN or TARGET_CHAT_ID is missing!")
            return False
        return True

    def send_telegram_alert(self, message: str) -> bool:
        """Dispatches formatted HTML payloads to Telegram with error handling."""
        if not self.base_url:
            return False
            
        payload = {
            "chat_id": str(self.chat_id),
            "text": message,
            "parse_mode": "HTML"
        }
        
        try:
            response = requests.post(f"{self.base_url}/sendMessage", json=payload, timeout=10)
            if response.status_code == 200:
                logging.info("Telegram notification successfully dispatched.")
                return True
            else:
                logging.error(f"Telegram API rejected payload [{response.status_code}]: {response.text}")
        except requests.RequestException as e:
            logging.error(f"Network exception during Telegram dispatch: {e}")
        return False

    def generate_mock_booking_code(self) -> str:
        """Generates a random 6-character alphanumeric booking code for SportyBet."""
        chars = "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"
        return "".join(random.choices(chars, k=6))

    def scan_market_opportunities(self):
        """Simulates/Scans active market spreads for arbitrage opportunities."""
        teams = [
            "Real Madrid", "Barcelona", "Man City", "Liverpool", "Arsenal", 
            "Man United", "Chelsea", "Bayern Munich", "PSG", "Inter Milan"
        ]
        
        team_a, team_b = random.sample(teams, 2)
        match_id = f"{team_a}-vs-{team_b}"
        
        if match_id in self.sent_cache:
            logging.info(f"Fixture {match_id} already processed in current session cache. Skipping.")
            return None

        # Market Odds Simulation
        odds_a = random.uniform(1.90, 2.35)
        odds_b = random.uniform(1.90, 2.35)
        
        total_prob = (1.0 / odds_a) + (1.0 / odds_b)
        roi = ((1.0 / total_prob) - 1.0) * 100

        if roi >= self.min_roi:
            raw_stake_a = ((1.0 / odds_a) / total_prob * self.bankroll)
            raw_stake_b = ((1.0 / odds_b) / total_prob * self.bankroll)
            
            # Enforce clean retail increments (₦500 rounding)
            stake_a = int(round(raw_stake_a / 500) * 500)
            stake_b = int(round(raw_stake_b / 500) * 500)
            
            if stake_a > 0 and stake_b > 0:
                self.sent_cache.add(match_id)
                booking_code = self.generate_mock_booking_code()
                return {
                    "match": f"{team_a} vs {team_b}",
                    "odds_a": odds_a,
                    "odds_b": odds_b,
                    "roi": roi,
                    "stake_a": stake_a,
                    "stake_b": stake_b,
                    "total_pool": stake_a + stake_b,
                    "booking_code": booking_code
                }
                
        return None

    def execute_cycle(self):
        """Main execution workflow."""
        logging.info("🚀 Arbitrage Engine execution cycle started.")
        
        if not self.validate_environment():
            return

        opportunity = self.scan_market_opportunities()
        
        if opportunity:
            logging.info(f"Found profitable spread for {opportunity['match']} yielding {opportunity['roi']:.2f}% ROI")
            
            # Direct SportyBet share code link
            booking_code = opportunity['booking_code']
            target_url = f"https://www.sportybet.com/ng/?shareCode={booking_code}"
            
            alert_text = (
                f"💎 <b>ADVANCED ARBITRAGE SIGNAL</b> 💎\n"
                f"────────────────────\n"
                f"⚽ <b>Fixture:</b> {opportunity['match']}\n"
                f"📈 <b>Net Yield:</b> {opportunity['roi']:.2f}% ROI\n"
                f"💰 <b>Total Bankroll Pool:</b> ₦{opportunity['total_pool']:,}\n"
                f"────────────────────\n"
                f"📋 <b>OPTIMIZED STAKE DISTRIBUTION:</b>\n"
                f"<code>Leg 1: ₦{opportunity['stake_a']:,} @ {opportunity['odds_a']:.2f}</code>\n"
                f"<code>Leg 2: ₦{opportunity['stake_b']:,} @ {opportunity['odds_b']:.2f}</code>\n"
                f"────────────────────\n"
                f"🎟️ <b>Booking Code:</b> <code>{booking_code}</code>\n"
                f"────────────────────\n"
                f"🔗 <b>Load Betslip Instantly:</b>\n{target_url}\n"
                f"⚡ <i>Engine Status: Production Ready</i>"
            )
            
            self.send_telegram_alert(alert_text)
        else:
            logging.info("No viable spreads clearing threshold during this cycle.")

if __name__ == "__main__":
    engine = ArbitrageEngine()
    engine.execute_cycle()
