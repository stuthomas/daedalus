# Daedalus — ASX Portfolio AI Daemon

Autonomous background service that runs three Claude-powered agents during ASX market hours to monitor, analyse, and manage a paper trading portfolio.

```
  Corporate Analyst (Haiku)     News Intelligence (Haiku)
         │                               │
         └──────────────┬────────────────┘
                        ▼
              Portfolio Manager (Sonnet)
                        │
              ┌─────────┴──────────┐
              │                    │
         Email Report       Portfolio State
           (Gmail)           (JSON + API)
```

---

## Features

- **Three AI agents** running in sequence each cycle
- **Haiku** for the two search-heavy agents (cost-efficient)
- **Sonnet** for the Portfolio Manager (better reasoning)
- **ASX market hours only** — 10am, 12pm, 2pm AEST Mon–Fri by default
- **REST API** — dashboard artifact can sync portfolio state in real time
- **Email reports** — styled HTML after every cycle (optional)
- **Manual or auto-approve** trades — you stay in control
- Deploys to **Railway** or **Render** in under 10 minutes

---

## Estimated Cost

| Component | Cost (AUD/month) |
|---|---|
| Railway Hobby or Render Starter | ~$8–11 |
| Anthropic API (Haiku × 2 + Sonnet × 1, 3 cycles/day, trading days) | ~$3–5 |
| **Total** | **~$11–16 / month** |

---

## Quick Start (local)

### 1. Clone and install

```bash
git clone <your-repo>
cd daedalus
python -m venv .venv
source .venv/bin/activate      # Windows: .venv\Scripts\activate
pip install -r requirements.txt
```

### 2. Configure

```bash
cp .env.example .env
# Edit .env — add your ANTHROPIC_API_KEY at minimum
```

### 3. Run

```bash
python main.py
```

Daedalus will start the API server on port 8080 and wait for the next scheduled market-hours cycle. To trigger one immediately for testing:

```bash
curl -X POST http://localhost:8080/api/trigger
# If DAEDALUS_API_KEY is set:
curl -X POST http://localhost:8080/api/trigger -H "X-API-Key: your_key"
```

---

## Deploy to Railway

### 1. Push to GitHub

```bash
git init
git add .
git commit -m "init daedalus"
gh repo create daedalus --private --push --source=.
```

### 2. Create Railway project

1. Go to [railway.com](https://railway.com) → **New Project** → **Deploy from GitHub repo**
2. Select your `daedalus` repository
3. Railway auto-detects the `Procfile` and starts a web service

### 3. Add environment variables

In Railway → your service → **Variables**, add:

```
ANTHROPIC_API_KEY    = sk-ant-...
DAEDALUS_API_KEY     = (generate a random string, e.g. openssl rand -hex 32)
NOTIFY_EMAIL         = you@email.com.au    (optional)
SMTP_USER            = you@gmail.com       (optional)
SMTP_PASS            = your-app-password   (optional)
```

Leave all other vars at defaults unless you want to customise the schedule.

### 4. Add a Volume (for portfolio persistence)

Railway filesystem resets on each deploy unless you use a Volume.

1. Railway → your project → **+ New** → **Volume**
2. Mount path: `/data`
3. In Variables, add: `PORTFOLIO_FILE=/data/portfolio.json`

Your portfolio now survives redeploys. ✓

### 5. Get your public URL

Railway → your service → **Settings** → **Networking** → Generate Domain.

Your Daedalus API will be at: `https://your-app.up.railway.app`

---

## Deploy to Render

### 1. Push to GitHub (same as Railway step 1)

### 2. Create Render web service

1. Go to [render.com](https://render.com) → **New** → **Web Service**
2. Connect your GitHub repo
3. Settings:
   - **Runtime**: Python 3
   - **Build command**: `pip install -r requirements.txt`
   - **Start command**: `python main.py`

### 3. Add environment variables

In Render → your service → **Environment**, add the same vars as above.

### 4. Add a Disk (for portfolio persistence)

1. Render → your service → **Disks** → **Add Disk**
2. Mount path: `/data`, Size: 1 GB (free tier)
3. In Environment, add: `PORTFOLIO_FILE=/data/portfolio.json`

### 5. Deploy

Click **Deploy** — Render will build and start Daedalus.

Your public URL: `https://your-app.onrender.com`

---

## API Reference

| Endpoint | Method | Description |
|---|---|---|
| `/health` | GET | Health check — returns `{"status":"ok"}` |
| `/api/status` | GET | Portfolio summary + next cycle times |
| `/api/portfolio` | GET | Full portfolio (no cached agent outputs) |
| `/api/portfolio/full` | GET | Full portfolio including last agent outputs |
| `/api/trigger` | POST | Manually trigger an agent cycle |

**Trigger requires `X-API-Key` header** if `DAEDALUS_API_KEY` is set.

---

## Connect the Dashboard Artifact

The Daedalus dashboard artifact (apex-portfolio.jsx) can be updated to sync with this daemon.

In the artifact, paste your Daedalus URL into the **Connect to Daemon** field. The dashboard will:
- Pull the live portfolio state every 60 seconds
- Display agent activity from the daemon's log
- Allow you to approve/reject pending trades (which it then POSTs back via `/api/trigger`)

---

## Configuration Reference

| Variable | Default | Description |
|---|---|---|
| `ANTHROPIC_API_KEY` | required | Your Anthropic API key |
| `ANALYST_MODEL` | `claude-haiku-4-5-20251001` | Model for Corporate Analyst |
| `NEWS_MODEL` | `claude-haiku-4-5-20251001` | Model for News Intelligence |
| `PM_MODEL` | `claude-sonnet-4-6` | Model for Portfolio Manager |
| `STARTING_CAPITAL` | `1000` | Paper trading starting capital (AUD) |
| `PORTFOLIO_FILE` | `portfolio.json` | Path to portfolio state file |
| `CASH_BUFFER_PCT` | `0.10` | Minimum cash reserve (10%) |
| `CYCLE_HOURS` | `10,12,14` | AEST hours to run cycles (Mon–Fri) |
| `RUN_ON_STARTUP` | `false` | Run a cycle on startup if market is open |
| `DAEDALUS_API_KEY` | _(blank)_ | Protects `/api/trigger` endpoint |
| `AUTO_APPROVE_TRADES` | `false` | Auto-execute HIGH confidence trades |
| `AUTO_APPROVE_MIN_CONFIDENCE` | `HIGH` | Minimum confidence for auto-execution |
| `NOTIFY_EMAIL` | _(blank)_ | Email address for cycle reports |
| `SMTP_USER` | _(blank)_ | Gmail address (sender) |
| `SMTP_PASS` | _(blank)_ | Gmail App Password |

---

## Email Notifications Setup

1. Enable **2-Step Verification** on your Gmail account
2. Go to **Google Account → Security → App Passwords**
3. Create a new App Password (name it "Daedalus")
4. Copy the 16-character password into `SMTP_PASS`

You'll receive a styled HTML email after every agent cycle showing trades, holdings, P&L, analyst recommendations, and market news.

---

## ASX Market Hours

Daedalus only runs during ASX trading hours: **Monday–Friday, 10:00am–4:00pm AEST**.

The daemon uses `Australia/Sydney` timezone, which automatically handles AEST (UTC+10) / AEDT (UTC+11) daylight saving transitions.

**Default cycle schedule**: 10:00am, 12:00pm, 2:00pm AEST — chosen to capture the open, midday, and pre-close windows.

To change: `CYCLE_HOURS=10,13,15` (or any combination of hours between 10 and 15).

---

## Roadmap: Going Live with Real Money

When your paper trading trial is complete and you're ready to trade with real money, the next step is integrating a broker API. Options for Australian investors:

- **Interactive Brokers (IBKR)** — has a full Python API (`ib_insync`). Supports ASX. Best option for automation.
- **SelfWealth** — no public API yet, but being developed
- **CommSec** — no public API (as of 2026). Would require browser automation via Playwright, which is fragile and against their ToS.

IBKR is strongly recommended for automated trading. The Portfolio Manager's trade output maps cleanly to IBKR order objects.

---

## Disclaimer

Daedalus is a paper trading simulation for educational and research purposes. Nothing it produces constitutes financial advice. All trades are simulated. Past paper trading performance does not predict real-market results. Always consult a licensed financial advisor before investing real money.
