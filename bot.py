
name: Sports Value Engine

on:
  push:
    branches:
      - main
  schedule:
    - cron: "*/10 * * * *"
  workflow_dispatch:

permissions:
  contents: read

concurrency:
  group: sports-value-engine-${{ github.ref }}
  cancel-in-progress: false

jobs:
  run-sports-engine:
    name: Run Multi-Match Arbitrage Scanner
    runs-on: ubuntu-24.04
    timeout-minutes: 15

    env:
      PYTHONUNBUFFERED: "1"
      TELEGRAMBOTTOKEN: ${{ secrets.TELEGRAMBOTTOKEN }}
      TARGETCHATID: ${{ secrets.TARGETCHATID }}
      BANKROLLPOOL: "20000"
      MIN_ARBITRAGE_ROI: "5"
      MAX_ARBITRAGE_ROI: "50"
      RUN_DURATION_SECONDS: "480"
      POLL_INTERVAL_SECONDS: "15"
      PAGE_WAIT_SECONDS: "5"
      LOG_LEVEL: "INFO"
      BOOKMAKER_A_URL: "https://www.sportybet.com/ng/sport/football"
      BOOKMAKER_B_URL: "https://www.bet9ja.com/sport/soccer"
      ARBITRAGE_DB_PATH: "arbitrage_signals.sqlite3"

    steps:
      - name: Checkout Code
        uses: actions/checkout@v7

      - name: Set up Python
        uses: actions/setup-python@v7
        with:
          python-version: "3.11"
          cache: pip
          cache-dependency-path: requirements.txt

      - name: Check Required Files
        run: |
          test -f requirements.txt || {
            echo "::error::requirements.txt is missing from the repository root"
            exit 1
          }

          if [ -f multi_match_arbitrage.py ]; then
            echo "Scanner file found: multi_match_arbitrage.py"
          elif [ -f bot.py ]; then
            echo "Scanner file found: bot.py"
          else
            echo "::error::Neither multi_match_arbitrage.py nor bot.py exists in the repository root"
            exit 1
          fi

      - name: Install Dependencies
        run: |
          python -m pip install --upgrade pip
          python -m pip install -r requirements.txt
          python -m playwright install --with-deps chromium

      - name: Validate Telegram Configuration
        run: |
          python - <<'PY'
          import os
          import sys

          missing = [
              name for name in ("TELEGRAMBOTTOKEN", "TARGETCHATID")
              if not os.environ.get(name, "").strip()
          ]

          if missing:
              print("Missing GitHub Secrets: " + ", ".join(missing))
              sys.exit(1)

          print("Required Telegram secrets are configured.")
          PY

      - name: Run Multi-Match Arbitrage Scanner
        run: |
          if [ -f multi_match_arbitrage.py ]; then
            python -u multi_match_arbitrage.py
          else
            python -u bot.py
          fi
