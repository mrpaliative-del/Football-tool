# Football-tool — Read-only 1X2 Odds Scanner

This package compares SportyBet Nigeria 1X2 prices with The Odds API and sends Telegram alerts for **theoretical** arbitrage candidates. It does not place bets.

## Important limitations

- The Odds API endpoint and response shape are documented, but available competitions/bookmakers depend on your API plan and quota. Default competition is `soccer_epl`.
- SportyBet's public website endpoint used as a default is undocumented and may change. Its current schema, market mapping, access permissions, and continued availability have **not** been verified in this package.
- The SportyBet parser is best-effort. If it reports zero parsed events, no valid cross-feed comparison can be made. Do not treat that as evidence there are no opportunities.
- A theoretical arbitrage is not a guaranteed profit. Confirm all three live prices, exact fixture and market, rules, voids, stake limits, fees, currency, and account restrictions manually.
- GitHub-hosted runners are ephemeral. The SQLite deduplication database is recreated on each run, so duplicate alerts may recur. This workflow does not persist state between runs.
- Respect SportyBet's terms, applicable law, API-provider terms, rate limits, and access controls. Do not use private browser cookies/session tokens to bypass access restrictions.

## GitHub setup

1. Open your repository on GitHub.
2. Add/replace `bot.py`, `requirements.txt`, `.gitignore`, `.github/workflows/sports-value-engine.yml`, and this `README.md`.
3. In **Settings → Secrets and variables → Actions → Secrets**, create:
   - `ODDS_API_KEY` — your The Odds API key.
   - `TELEGRAMBOTTOKEN` — your Telegram bot token from BotFather.
   - `TARGETCHATID` — the Telegram chat/group ID that should receive alerts.
   - `SPORTYBET_API_KEY` — optional; only use if an authorized provider actually issued an API key and documented the auth method. Do not use a browser session cookie/token here.
4. Under **Settings → Secrets and variables → Actions → Variables**, optional settings are listed below.
5. Open **Actions → Football-tool Odds Scanner → Run workflow** to test manually. Review the logs before relying on alerts.

## GitHub Actions variables (optional)

| Variable | Default | Purpose |
|---|---|---|
| `ODDS_SPORT_KEY` | `soccer_epl` | The Odds API sport key; must be available on your plan |
| `ODDS_REGIONS` | `uk,eu` | Odds API regions |
| `SPORTYBET_FEED_URL` | public website endpoint | Only use a feed endpoint you are authorized to access |
| `SPORTYBET_AUTH_MODE` | `bearer` | `bearer` or `x-api-key`, only if documented by your provider |
| `SPORTYBET_SPORT_ID` | `sr:sport:1` | Best-effort website parameter; may need changing |
| `SPORTYBET_MARKET_ID` | `1` | Best-effort website parameter; may need changing |
| `SPORTYBET_PAGE_SIZE` | `100` | Page size |
| `SPORTYBET_TIMELINE_HOURS` | `48` | Feed look-ahead parameter |
| `BANKROLL_NGN` | `20000` | Hypothetical stake pool used for stake sizing; not connected to a betting account |
| `MIN_ARBITRAGE_ROI` | `0.5` | Minimum theoretical ROI percentage |
| `MAX_ARBITRAGE_ROI` | `30` | Upper filter to suppress suspicious bad data |
| `MATCH_TIME_TOLERANCE_MINUTES` | `180` | Kickoff matching tolerance |

## Local run

Python 3.11+:

```bash
python -m pip install -r requirements.txt
export ODDS_API_KEY="your_key_here"
export TELEGRAMBOTTOKEN="your_bot_token"
export TARGETCHATID="your_chat_id"
python bot.py
```

Do not commit real keys. In PowerShell, use `$env:ODDS_API_KEY="..."` etc.

## Troubleshooting

- **Missing ODDS_API_KEY:** create the secret with the exact name above.
- **401 from The Odds API:** check the secret, account status, and key.
- **429 from The Odds API:** quota/rate limit reached; check response headers/account plan.
- **SportyBet HTTP 401/403:** endpoint needs different authorized access or blocks the request. Do not paste browser cookies into code.
- **SportyBet parsed 0 events:** inspect a redacted JSON sample and update the parser to the actual schema. Never share `Authorization`, `Cookie`, `Set-Cookie`, or tokens.
- **Telegram silent:** check `TELEGRAMBOTTOKEN`, `TARGETCHATID`, bot membership/permissions, and Actions logs. The bot attempts a startup message and scan summary.
- **No matches:** confirm sport key, regions, kickoff tolerance, team-name matching, and feed coverage.

## What this package has and has not been tested for

The Python source is syntax-checked when this archive is generated. No live API calls, SportyBet schema validation, Telegram delivery, or GitHub Actions deployment are claimed. Run a manual workflow and inspect logs before relying on it.
