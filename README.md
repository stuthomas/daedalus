# Daedalus — Autonomous ASX Portfolio AI

A self-contained background service that runs a team of Claude-powered agents during
ASX market hours to analyse, decide, and **autonomously trade** a paper portfolio — plus
a full-page dashboard (dark/light) served by the same app.

```
 Corporate Analyst (Haiku)   News Intelligence (Haiku)   Technical Analyst (local)
          │                          │                          │
          └───────────┬──────────────┴───────────┬──────────────┘
                       ▼                          ▼
              Market Regime (local)       Earnings Calendar (Haiku)
                       │                          │
                       └────────────┬─────────────┘
                                    ▼
                        Risk & Rebalancing (local)
                                    ▼
                    Portfolio Manager (Opus 4.8)  ──►  auto-executes trades
                                    ▼
                    Portfolio state (JSON)  +  Dashboard (served at /)
```

Everything runs on just two things: the **Anthropic API** and **free Yahoo Finance**
data. No email, no broker keys, no paid market-data feeds.

---

## What it does

- **7 agents per cycle.** Two search agents (Haiku) discover opportunities and news; a
  local Technical Analyst computes indicators from Yahoo Finance; a **Market Regime**
  module reads the ASX 200 ("is it safe to enter today?"); a local Risk agent handles
  concentration, trailing stops and take-profit; the **Portfolio Manager (Opus 4.8)**
  synthesises everything and decides trades.
- **Fully autonomous.** By default every trade the PM decides is executed automatically
  within the risk limits — the paper portfolio runs and grows on its own. (You can flip
  a switch to require dashboard approval instead.)
- **Grows sensibly.** Positions are sized as a volatility-scaled % of capital, so the book
  compounds as it grows. There are **no small increments** — every trade is at least the
  greater of `$MIN_TRADE_VALUE` or `MIN_TRADE_PCT` of the whole book, and at most
  `MAX_POSITION_PCT`. Winners are trimmed on take-profit to lock in gains and recycle cash.
- **Market Regime gate.** Entries are gated on a free-data adaptation of the SPX GEX
  "regime / safe-to-enter" idea: realised-volatility regime, trend vs mean-reversion,
  breadth and news sentiment produce a GREEN / AMBER / RED signal on the ASX 200.
- **Learning.** Realised P&L, win rate and closed trades feed back into the PM's context.
- **Dashboard included.** A responsive app-shell dashboard (charts, allocation, regime
  gauge, holdings, activity) with a dark/light toggle, served by the daemon at `/`.

---

## Quick start (local)

```bash
git clone https://github.com/stuthomas/Daedalus.git
cd Daedalus
python -m venv .venv
.venv\Scripts\activate           # macOS/Linux: source .venv/bin/activate
pip install -r requirements.txt

cp .env.example .env             # then edit .env — add your ANTHROPIC_API_KEY
python main.py
```

Open **http://localhost:8080/** for the dashboard. Trigger a cycle immediately:

```bash
curl -X POST http://localhost:8080/api/trigger        # add -H "X-API-Key: <key>" if set
```

---

## Deploy to Railway — single repo

Everything lives in **one repo** now. The daemon serves the API **and** the dashboard,
so you no longer need a separate `daedalus-dashboard` project.

1. **Push everything to `stuthomas/Daedalus`:**
   ```bash
   git add .
   git commit -m "Autonomous rebuild + dashboard"
   git push origin main
   ```
2. Railway auto-detects the `Procfile` (`web: python main.py`) and deploys. Your dashboard
   is then live at your Railway URL (e.g. `https://<app>.up.railway.app/`) — same origin as
   the API, so the dashboard needs no configuration.
3. **Keep your existing portfolio.** Add a **Volume** mounted at `/data` and set
   `PORTFOLIO_FILE=/data/portfolio.json` in Variables. Code deploys never touch the Volume,
   and the loader migrates older portfolios automatically (new fields are added on load), so
   your status, holdings and history are preserved across the upgrade.
4. Set the other Variables you want (`DAEDALUS_API_KEY`, risk knobs, etc.).
5. **Retire the old dashboard service.** The separate `daedalus-dashboard` static site is
   redundant — you can delete that Railway service and repo. (If you'd rather keep hosting
   the dashboard separately, open its **Settings** gear and set the API base URL to your
   Railway API URL + the API key.)

---

## Configuration

| Variable | Default | Description |
|---|---|---|
| `ANTHROPIC_API_KEY` | _required_ | Your Anthropic API key |
| `ANALYST_MODEL` / `NEWS_MODEL` | `claude-haiku-4-5` | Search agents (cheap) |
| `PM_MODEL` | `claude-opus-4-8` | Portfolio Manager (most capable) |
| `STARTING_CAPITAL` | `1000` | Paper starting capital (AUD) |
| `PORTFOLIO_FILE` | `portfolio.json` | State file (use `/data/...` on Railway) |
| `CASH_BUFFER_PCT` | `0.10` | Minimum cash reserve |
| `STOP_LOSS_PCT` | `0.06` | Fixed stop-loss from avg buy |
| `TRAILING_STOP_PCT` | `0.10` | Trailing stop from peak |
| `TAKE_PROFIT_PCT` | `0.25` | Gain that triggers a take-profit trim |
| `TAKE_PROFIT_TRIM_PCT` | `0.5` | Fraction sold on take-profit |
| `MAX_POSITION_PCT` | `0.25` | Max single position |
| `MAX_SECTOR_PCT` | `0.40` | Max single sector |
| `MIN_TRADE_VALUE` | `150` | Absolute minimum trade value (AUD) |
| `MIN_TRADE_PCT` | `0.08` | Minimum trade as % of book (no small increments) |
| `ASX_INDEX_SYMBOL` | `^AXJO` | Index for the regime read |
| `CYCLE_HOURS` | `10,12,14` | AEST hours to run cycles (Mon–Fri) |
| `RUN_ON_STARTUP` | `false` | Run a cycle on startup if market open |
| `AUTO_APPROVE_TRADES` | `true` | Execute trades automatically |
| `AUTO_APPROVE_MIN_CONFIDENCE` | `LOW` | Min confidence to auto-execute (`LOW` = all) |
| `DAEDALUS_API_KEY` | _(blank)_ | Protects POST endpoints |

---

## API reference

| Endpoint | Method | Description |
|---|---|---|
| `/` , `/dashboard` | GET | The dashboard (served by the daemon) |
| `/health` | GET | Health check |
| `/api/status` | GET | Summary + regime + win rate + config |
| `/api/portfolio/full` | GET | Full portfolio state (what the dashboard reads) |
| `/api/trigger` | POST | Run an agent cycle now |
| `/api/portfolio/add-funds` | POST | Add cash `{ "amount": 500 }` |
| `/api/portfolio/manual-trade` | POST | Enter a trade manually |
| `/api/portfolio/approve-trade` | POST | Approve a pending trade `{ "ticker": "BHP.AX" }` |
| `/api/portfolio/reject-trade` | POST | Reject a pending trade |

POST endpoints require `X-API-Key` if `DAEDALUS_API_KEY` is set.

---

## Market hours

Daedalus runs Monday–Friday during ASX hours (`Australia/Sydney`, auto-handles AEST/AEDT).
Default cycles: 10:00, 12:00, 14:00 AEST — open, midday, pre-close.

---

## Disclaimer

Daedalus is a **paper trading simulation** for educational and research purposes. Nothing
it produces is financial advice. All trades are simulated. Past paper performance does not
predict real-market results. Consult a licensed financial adviser before investing real money.
