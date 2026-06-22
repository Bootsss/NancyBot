# Capitol Gains 🏛️📈

A production-quality Discord bot that automatically tracks congressional stock trades, analyses activity patterns, scores opportunities, and posts rich alerts — functioning as a stock research assistant, not a trade-copying tool.

---

## Table of Contents

1. [Features](#features)
2. [Architecture Overview](#architecture-overview)
3. [Prerequisites](#prerequisites)
4. [Installation](#installation)
5. [Discord Setup](#discord-setup)
6. [Environment Variables](#environment-variables)
7. [Running Locally](#running-locally)
8. [Discord Commands](#discord-commands)
9. [Automated Schedule](#automated-schedule)
10. [Scoring System](#scoring-system)
11. [Alert Types](#alert-types)
12. [Deployment](#deployment)
13. [Testing](#testing)
14. [Troubleshooting](#troubleshooting)
15. [Future Extensions](#future-extensions)
16. [Disclaimer](#disclaimer)

---

## Features

- **Automatic trade collection** — pulls congressional stock disclosures daily via the Quiver Quantitative API (falls back to public data without an API key)
- **Ticker resolution** — maps company names to ticker symbols using yfinance search, legal-suffix stripping, and manual overrides
- **Six signal analysis engine** — buy clusters, net activity, sector surges, repeat buying, frequency acceleration, large transactions
- **0–100 composite scoring** — normalised score with full point breakdown per ticker
- **Five watchlists** — Top Picks, Most Bought, Hottest Sectors, Fastest Rising, Most Improved
- **Rich Discord embeds** — colour-coded by sector, with market data, trade history, and signal breakdowns
- **Backtesting** — 30/90/180-day return tracking with S&P 500 comparison and win-rate statistics
- **Automated reports** — daily digest, weekly sector trends, monthly analytics summary
- **Extensible architecture** — plug in insider buying, earnings data, analyst upgrades, or any new signal without touching existing code

---

## Architecture Overview

```
capitol-gains/
├── bot.py                  # Entry point — Discord bot lifecycle
├── config.py               # Settings, logging, scoring weights, constants
├── database.py             # SQLAlchemy engines, session factories
├── models.py               # ORM models: Trade, Company, Score, Alert, Backtest
├── scheduler.py            # APScheduler jobs (daily/weekly/monthly)
├── services/
│   ├── trade_collector.py  # BaseCollector + CongressCollector (plugin architecture)
│   ├── ticker_mapper.py    # Company name → ticker resolution with caching
│   ├── market_data.py      # yfinance enrichment: price, PE, 52w return, volume
│   ├── analysis_engine.py  # Six signal generators
│   ├── scoring_engine.py   # 0–100 composite score + watchlists
│   └── alert_service.py    # Alert generation, dedup, Discord embed posting
├── commands/
│   ├── top.py              # !top [N]
│   ├── stock.py            # !stock TICKER  /  !score TICKER
│   ├── sector.py           # !sector [name]
│   ├── recent.py           # !recent  /  !alerts  /  !watchlist  /  !stats
│   └── help.py             # !help [command]
├── data/
│   └── ticker_overrides.json   # Manual company → ticker mappings
├── logs/                   # Auto-rotating log files
└── tests/                  # pytest suite (95+ test cases)
```

### Data flow

```
Quiver API / Public CSV
        ↓
  CongressCollector          (daily 07:00 UTC)
        ↓
     Trade rows
        ↓
   TickerMapper              (daily 07:05 UTC)
        ↓
   Company rows
        ↓
  AnalysisEngine             (daily 07:10 UTC)
   6 signals per ticker
        ↓
  ScoringEngine  →  Score rows  →  Watchlists
        ↓
  AlertService               (daily 07:20 UTC)
   Dedup → Alert rows
        ↓
  Discord embeds             (daily 07:25 UTC)
```

---

## Prerequisites

| Requirement | Version |
|-------------|---------|
| Python | 3.12+ |
| pip | latest |
| Discord Bot Token | — |
| Quiver Quantitative API Key | optional (free tier available) |
| SQLite **or** PostgreSQL | SQLite works for dev |

---

## Installation

### 1. Clone the repository

```bash
git clone https://github.com/your-org/capitol-gains.git
cd capitol-gains
```

### 2. Create a virtual environment

```bash
python -m venv .venv
source .venv/bin/activate        # Linux/macOS
.venv\Scripts\activate           # Windows
```

### 3. Install dependencies

```bash
pip install -r requirements.txt
```

### 4. Configure environment variables

```bash
cp .env.example .env
# Edit .env with your values — see Environment Variables section below
```

### 5. Initialise the database

The database is created automatically on first run. No manual migration step needed for SQLite.

For PostgreSQL, create the database first:

```sql
CREATE DATABASE capitol_gains;
```

Then set `DATABASE_URL` in `.env` and run the bot — tables are created via `Base.metadata.create_all()` on startup.

---

## Discord Setup

### 1. Create a Discord application

1. Go to [discord.com/developers/applications](https://discord.com/developers/applications)
2. Click **New Application** → give it a name (e.g. `Capitol Gains`)
3. Go to the **Bot** tab → click **Add Bot**
4. Under **Privileged Gateway Intents**, enable **Message Content Intent**
5. Copy the **Token** → paste into `.env` as `DISCORD_TOKEN`

### 2. Set bot permissions

On the **OAuth2 → URL Generator** tab, select:
- Scopes: `bot`
- Bot permissions:
  - `Read Messages / View Channels`
  - `Send Messages`
  - `Embed Links`
  - `Read Message History`
  - `Add Reactions`

### 3. Invite the bot to your server

Copy the generated URL and open it in your browser to invite the bot.

### 4. Get the channel ID

1. In Discord, enable **Developer Mode** (User Settings → Advanced)
2. Right-click the channel where you want alerts posted → **Copy ID**
3. Paste into `.env` as `DISCORD_CHANNEL_ID`
4. Optionally set a separate `DISCORD_REPORT_CHANNEL_ID` for daily/weekly reports

---

## Environment Variables

Copy `.env.example` to `.env` and fill in each value:

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `DISCORD_TOKEN` | ✅ | — | Bot token from Discord Developer Portal |
| `DISCORD_CHANNEL_ID` | ✅ | — | Channel ID for alert embeds |
| `DISCORD_REPORT_CHANNEL_ID` | ➖ | same as above | Channel ID for daily/weekly/monthly reports |
| `DATABASE_URL` | ➖ | `sqlite:///data/capitol_gains.db` | SQLAlchemy connection URL |
| `LOG_LEVEL` | ➖ | `INFO` | `DEBUG` / `INFO` / `WARNING` / `ERROR` |
| `QUIVER_API_KEY` | ➖ | — | Quiver Quantitative API key (recommended) |
| `ALPHA_VANTAGE_KEY` | ➖ | — | Alpha Vantage key (future use) |
| `DAILY_JOB_HOUR` | ➖ | `7` | UTC hour for daily jobs (0–23) |
| `WEEKLY_JOB_DAY` | ➖ | `0` | Day for weekly jobs (0=Monday) |
| `ALERT_SCORE_THRESHOLD` | ➖ | `70` | Minimum score to trigger HIGH_SCORE alert |
| `BUY_CLUSTER_MIN_POLITICIANS` | ➖ | `3` | Minimum unique buyers to trigger BUY_CLUSTER |
| `BUY_CLUSTER_WINDOW_DAYS` | ➖ | `30` | Rolling window (days) for cluster detection |

### PostgreSQL example

```
DATABASE_URL=postgresql://user:password@localhost:5432/capitol_gains
```

For async connections (used by the Discord bot), `asyncpg` is required:

```bash
pip install asyncpg psycopg2-binary
```

---

## Running Locally

### Start the bot

```bash
python bot.py
```

On startup you should see:

```
2024-03-15 07:00:00 | INFO     | Logging configured — level=INFO
2024-03-15 07:00:00 | INFO     | Database connection OK: sqlite:///data/capitol_gains.db
2024-03-15 07:00:01 | INFO     | Loaded cog: commands.help
2024-03-15 07:00:01 | INFO     | Loaded cog: commands.top
...
2024-03-15 07:00:02 | INFO     | Logged in as CapitolGains#1234 (id=...) on 1 guild(s)
2024-03-15 07:00:02 | INFO     | Capitol Gains is ready. Prefix: !
```

### Run the daily pipeline immediately (first-run seed)

The bot doesn't run jobs until the scheduled time. To seed data immediately, add this to `bot.py` temporarily or run from a Python shell:

```python
from config import configure_logging
configure_logging()

from database import init_db
init_db()

from services.trade_collector import run_all_collectors
run_all_collectors()

from services.ticker_mapper import get_ticker_mapper
get_ticker_mapper().enrich_unresolved_trades()

from services.scoring_engine import get_scoring_engine
get_scoring_engine().score_all()

from services.alert_service import get_alert_service
get_alert_service().generate_alerts()
```

Or trigger via the scheduler method in `bot.py`'s `on_ready`:

```python
# Temporary — remove after first run
self._scheduler.trigger_daily_now()
```

### Manual override for an unresolved ticker

Edit `data/ticker_overrides.json`:

```json
{
  "raytheon technologies": "RTX",
  "lockheed martin": "LMT",
  "general dynamics": "GD"
}
```

Keys are case-insensitive. The mapper reloads this file on the next run.

---

## Discord Commands

All commands use the `!` prefix.

| Command | Description | Example |
|---------|-------------|---------|
| `!top [N]` | Top N stocks by score (default 10, max 25) | `!top 5` |
| `!stock TICKER` | Full analysis: score, signals, trades, market data | `!stock AAPL` |
| `!score TICKER` | Detailed point-by-point score breakdown | `!score NVDA` |
| `!sector [name]` | Sector overview or detail for one sector | `!sector Tech` |
| `!recent [days]` | Most recent congressional trades | `!recent 14` |
| `!alerts [days]` | Recent generated alerts | `!alerts 30` |
| `!watchlist` | All five ranked watchlists | `!watchlist` |
| `!stats` | Database statistics and backtest results | `!stats` |
| `!help [command]` | Command reference or detail for one command | `!help stock` |

### Sector name shortcuts

`!sector` accepts partial names: `tech`, `health`, `pharma`, `oil`, `telecom`, `reit`, `industrial`, `financial`, `comms`, `utility`, `staples`, `discretionary`.

---

## Automated Schedule

All times are UTC. Configured via environment variables.

### Daily (default 07:00 UTC)

| Time | Job | Description |
|------|-----|-------------|
| H:00 | `collect_trades` | Pull new congressional disclosures |
| H:05 | `resolve_tickers` | Map unresolved company names → tickers |
| H:10 | `run_scoring` | Score all active tickers |
| H:20 | `generate_alerts` | Evaluate thresholds, create Alert rows |
| H:25 | `post_alerts` | Post pending alert embeds to Discord |
| H:30 | `daily_report` | Post digest to report channel |

### Weekly (default Monday 08:00 UTC)

| Time | Job | Description |
|------|-----|-------------|
| 08:00 | `update_market_data` | Refresh Company financial metrics |
| 08:45 | `weekly_report` | Post sector trends + watchlist summary |

### Monthly (1st of month)

| Time | Job | Description |
|------|-----|-------------|
| 09:00 | `run_backtests` | Calculate 30/90/180-day returns on alerts |
| 09:30 | `monthly_report` | Post full analytics summary |

---

## Scoring System

Each ticker is scored 0–100 daily based on six signals:

| Signal | Component | Points |
|--------|-----------|--------|
| A — Buy Cluster | Per unique buyer in 30-day window | 5 pts each |
| B — Net Buy Activity | Per net purchase (buys − sells) | 3 pts each |
| C — Sector Surge | Sector concentration ≥1.5× expected | +10 pts flat |
| D — Repeat Buying | Any politician buys same stock 2+ times | +5 pts flat |
| E — Frequency Rise | Purchase rate doubling in recent half | ×1.25 multiplier |
| F — Large Transaction | ≥50% of buys are $100K+ | +5 pts flat |

Raw score is capped at 100, then normalised to 0–100.

Score history is retained indefinitely for trend analysis and backtesting.

---

## Alert Types

| Type | Trigger | Icon |
|------|---------|------|
| `BUY_CLUSTER` | 3+ politicians buy same stock within 30 days | 🔔 |
| `SECTOR_SURGE` | Sector receives 1.5× its expected share of buys | 📊 |
| `HIGH_SCORE` | Score crosses `ALERT_SCORE_THRESHOLD` (default 70) | ⭐ |
| `REPEAT_BUYING` | Same politician buys same stock 2+ times in window | 🔁 |
| `UNUSUAL_ACTIVITY` | 3+ signals firing simultaneously | 🚨 |

Alerts are deduplicated: the same ticker + alert type will not re-alert within 7 days.

---

## Deployment

### Railway

1. Create a new project on [railway.app](https://railway.app)
2. Connect your GitHub repository
3. Add environment variables in the Railway dashboard
4. Add a PostgreSQL plugin and copy the `DATABASE_URL` into your variables
5. Set the start command: `python bot.py`

Railway handles restarts and process supervision automatically.

### Render

1. Create a new **Background Worker** service on [render.com](https://render.com)
2. Connect your repository
3. Build command: `pip install -r requirements.txt`
4. Start command: `python bot.py`
5. Add environment variables in the Render dashboard
6. Add a Render PostgreSQL database and set `DATABASE_URL`

### VPS (Ubuntu/Debian)

```bash
# Install Python 3.12
sudo apt update && sudo apt install python3.12 python3.12-venv

# Clone and set up
git clone https://github.com/your-org/capitol-gains.git /opt/capitol-gains
cd /opt/capitol-gains
python3.12 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env && nano .env

# Create systemd service
sudo nano /etc/systemd/system/capitol-gains.service
```

```ini
[Unit]
Description=Capitol Gains Discord Bot
After=network.target

[Service]
Type=simple
User=ubuntu
WorkingDirectory=/opt/capitol-gains
EnvironmentFile=/opt/capitol-gains/.env
ExecStart=/opt/capitol-gains/.venv/bin/python bot.py
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable capitol-gains
sudo systemctl start capitol-gains
sudo journalctl -u capitol-gains -f   # tail logs
```

### GitHub Actions (scheduled data collection only)

For serverless environments where you only want scheduled collection (not the interactive bot), use GitHub Actions:

```yaml
# .github/workflows/collect.yml
name: Daily Trade Collection
on:
  schedule:
    - cron: '0 7 * * *'   # 07:00 UTC daily
jobs:
  collect:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with: { python-version: '3.12' }
      - run: pip install -r requirements.txt
      - run: python -c "
          from config import configure_logging; configure_logging();
          from database import init_db; init_db();
          from services.trade_collector import run_all_collectors; run_all_collectors();
          from services.scoring_engine import get_scoring_engine; get_scoring_engine().score_all();
          from services.alert_service import get_alert_service; get_alert_service().generate_alerts();
        "
        env:
          DATABASE_URL: ${{ secrets.DATABASE_URL }}
          DISCORD_TOKEN: ${{ secrets.DISCORD_TOKEN }}
          DISCORD_CHANNEL_ID: ${{ secrets.DISCORD_CHANNEL_ID }}
          QUIVER_API_KEY: ${{ secrets.QUIVER_API_KEY }}
```

---

## Testing

### Run all tests

```bash
pytest tests/ -v
```

### Run a specific test file

```bash
pytest tests/test_scoring.py -v
pytest tests/test_alerts.py -v
```

### Run with coverage

```bash
pip install pytest-cov
pytest tests/ --cov=. --cov-report=term-missing
```

### Test structure

| File | What it tests |
|------|---------------|
| `test_database.py` | ORM models, unique constraints, JSON serialisation |
| `test_scoring.py` | Signal logic, score computation, normalisation |
| `test_collector.py` | Amount parsing, date parsing, row mapping, deduplication |
| `test_alerts.py` | Alert type evaluation, deduplication window, field storage |
| `test_ticker_mapper.py` | Name normalisation, suffix stripping, override loading, resolution flow |

All tests use an in-memory SQLite database — no external services required.

---

## Troubleshooting

### Bot doesn't respond to commands

- Confirm **Message Content Intent** is enabled in the Discord Developer Portal
- Verify the bot has **Send Messages** and **Embed Links** permissions in the channel
- Check `DISCORD_TOKEN` is correct in `.env`

### "Channel not found" warning on startup

- Enable **Developer Mode** in Discord settings
- Right-click the target channel → **Copy ID**
- Paste into `DISCORD_CHANNEL_ID` in `.env` (must be an integer)

### No trades collected

- Without a `QUIVER_API_KEY`, the bot falls back to a limited public endpoint
- Get a free API key at [quiverquant.com](https://www.quiverquant.com/)
- Check `logs/errors.log` for specific HTTP errors

### Tickers not resolving

- Add manual overrides to `data/ticker_overrides.json`:
  ```json
  { "some company name": "TICK" }
  ```
- yfinance search may be rate-limited; the mapper retries automatically
- Run `!stats` to see how many tickers are unresolved

### Database errors on startup

- For SQLite: ensure the `data/` directory is writable
- For PostgreSQL: verify `DATABASE_URL` credentials and that the database exists
- Set `LOG_LEVEL=DEBUG` in `.env` for full SQLAlchemy query logging

### Scores all zero

- Scores require trades in the database first — run the collector manually (see Running Locally)
- The daily scoring job runs at `DAILY_JOB_HOUR` UTC; use `trigger_daily_now()` to run immediately

### APScheduler job missed

- Scheduler uses `misfire_grace_time=3600` — jobs up to 1 hour late will still run
- If the bot was offline longer, restart it — jobs with `coalesce=True` run once on next tick

---

## Future Extensions

The plugin architecture is designed for easy expansion:

### Adding a new data collector

```python
# services/trade_collector.py
class InsiderCollector(BaseCollector):
    def _fetch_raw(self) -> list[dict]:
        # fetch from SEC Form 4 / OpenInsider API
        ...

    def _parse_records(self, raw) -> list[Trade]:
        # map to Trade objects
        ...

# Register it:
COLLECTOR_REGISTRY["insider"] = InsiderCollector
```

### Adding a new signal

```python
# services/analysis_engine.py
def signal_g_earnings_growth(self, ticker: str, ...) -> SignalResult:
    # query earnings data
    ...

# Add to analyse_ticker():
result.signals.append(self.signal_g_earnings_growth(ticker, ...))
```

### Adding scoring weight for new signal

```python
# config.py — ScoringWeights dataclass
earnings_growth_bonus: float = 8.0

# services/scoring_engine.py — _compute_raw_score()
earnings = signals.get("EARNINGS_GROWTH")
if earnings and earnings.active:
    raw += w.earnings_growth_bonus
    breakdown["earnings_growth_points"] = w.earnings_growth_bonus
```

### Planned future signals

| Signal | Data Source |
|--------|-------------|
| Insider buying (Form 4) | SEC EDGAR / OpenInsider |
| Earnings growth | yfinance / Alpha Vantage |
| Analyst upgrades | Seeking Alpha / Quiver Quant |
| News sentiment | NewsAPI / FinBERT |
| Technical momentum | yfinance price data (RSI, MACD) |
| ETF inflows | ETF.com / VettaFi |
| Institutional 13F filings | SEC EDGAR |
| Hedge fund positioning | Whale Wisdom / WhaleWisdom |

---

## Disclaimer

Capitol Gains is a **research and educational tool** that aggregates publicly available congressional disclosure data. It is **not** financial advice.

- Congressional trading data is one signal among many — it is not a guarantee of future performance
- Disclosures are filed with up to 45-day delays under the STOCK Act
- Always conduct your own due diligence before making any investment decision
- Past congressional trading patterns do not guarantee future results

The authors of this project are not registered investment advisors. Use this tool at your own risk.
