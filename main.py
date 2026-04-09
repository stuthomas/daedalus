"""
Daedalus — Autonomous ASX Portfolio Management Daemon
Runs AI agent cycles during ASX market hours (10am–4pm AEST, Mon–Fri)
Exposes a REST API so the dashboard artifact can sync portfolio state.
"""

import os
import sys
import logging
import threading
import signal
from datetime import datetime

import pytz
from flask import Flask, jsonify, request
from flask_cors import CORS
from apscheduler.schedulers.blocking import BlockingScheduler
from apscheduler.events import EVENT_JOB_ERROR, EVENT_JOB_EXECUTED

from config import Config
from agents import run_cycle
from portfolio import load_portfolio

# ── Logging ──────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler("daedalus.log", encoding="utf-8"),
    ],
)
log = logging.getLogger("daedalus")
AEST = pytz.timezone("Australia/Sydney")

# ── Flask API ─────────────────────────────────────────────────────────────────
app = Flask(__name__)
CORS(app, resources={r"/*": {"origins": "*", "allow_headers": ["X-API-Key", "Content-Type"], "methods": ["GET", "POST", "OPTIONS"]}})         # Allow the dashboard artifact to call this API
_config: Config = None


@app.route("/health")
def health():
    return jsonify({"status": "ok", "service": "daedalus"})


@app.route("/api/status")
def status():
    portfolio = load_portfolio(_config)
    invested = sum(
        h["shares"] * h.get("currentPrice", h["avgBuyPrice"])
        for h in portfolio.get("holdings", [])
    )
    total = portfolio["cash"] + invested
    pl = total - portfolio["startingCapital"]
    now = datetime.now(AEST)
    market_open = now.weekday() < 5 and 10 <= now.hour < 16

    return jsonify({
        "status": "running",
        "time": now.isoformat(),
        "marketOpen": market_open,
        "portfolioValue": round(total, 2),
        "cash": round(portfolio["cash"], 2),
        "invested": round(invested, 2),
        "pl": round(pl, 2),
        "plPct": round((pl / portfolio["startingCapital"]) * 100, 2),
        "holdings": len(portfolio.get("holdings", [])),
        "scheduledCycles": [f"{h:02d}:00 AEST Mon–Fri" for h in _config.CYCLE_HOURS],
    })


@app.route("/api/portfolio")
def get_portfolio():
    """Lightweight portfolio — omits bulky cached agent outputs."""
    portfolio = load_portfolio(_config)
    keys_to_strip = {"lastAnalysis", "lastNews", "lastPMOutput"}
    return jsonify({k: v for k, v in portfolio.items() if k not in keys_to_strip})


@app.route("/api/portfolio/full")
def get_portfolio_full():
    """Full portfolio including last agent outputs."""
    return jsonify(load_portfolio(_config))


@app.route("/api/trigger", methods=["POST"])
def trigger_cycle():
    """Manually trigger an agent cycle (requires X-API-Key header)."""
    if _config.API_KEY:
        key = request.headers.get("X-API-Key", "")
        if key != _config.API_KEY:
            return jsonify({"error": "Unauthorized"}), 401
    threading.Thread(target=run_cycle, args=[_config], daemon=True).start()
    return jsonify({"status": "cycle triggered", "time": datetime.now(AEST).isoformat()})


# ── Scheduler ─────────────────────────────────────────────────────────────────
def _job_listener(event):
    if event.exception:
        log.error(f"Agent cycle FAILED: {event.exception}")
    else:
        log.info("Agent cycle completed successfully")


def main():
    global _config
    _config = Config().validate()

    log.info("╔══════════════════════════════════════════╗")
    log.info("║         DAEDALUS — ASX Portfolio AI      ║")
    log.info("╚══════════════════════════════════════════╝")
    log.info(f"  Starting capital : ${_config.STARTING_CAPITAL:,.2f} AUD")
    log.info(f"  Analyst model    : {_config.ANALYST_MODEL}  (Haiku)")
    log.info(f"  News model       : {_config.NEWS_MODEL}  (Haiku)")
    log.info(f"  PM model         : {_config.PM_MODEL}  (Sonnet)")
    log.info(f"  Auto-approve     : {_config.AUTO_APPROVE_TRADES}")
    log.info(f"  Notify email     : {_config.NOTIFY_EMAIL or 'not configured'}")

    # Start Flask in a daemon thread — Railway/Render need a bound port
    port = int(os.getenv("PORT", 8080))
    flask_thread = threading.Thread(
        target=lambda: app.run(host="0.0.0.0", port=port, use_reloader=False),
        daemon=True,
    )
    flask_thread.start()
    log.info(f"  API server       : http://0.0.0.0:{port}")

    # Build the scheduler
    scheduler = BlockingScheduler(timezone=AEST)
    scheduler.add_listener(_job_listener, EVENT_JOB_ERROR | EVENT_JOB_EXECUTED)

    for hour in _config.CYCLE_HOURS:
        scheduler.add_job(
            run_cycle,
            trigger="cron",
            day_of_week="mon-fri",
            hour=hour,
            minute=0,
            timezone=AEST,
            args=[_config],
            id=f"cycle_{hour:02d}h",
            name=f"Agent Cycle {hour:02d}:00 AEST",
            misfire_grace_time=300,   # 5 min grace if a cycle is delayed
        )
        log.info(f"  Scheduled cycle  : {hour:02d}:00 AEST Mon–Fri")

    # Optional immediate startup cycle
    if _config.RUN_ON_STARTUP:
        now = datetime.now(AEST)
        if now.weekday() < 5 and 10 <= now.hour < 16:
            log.info("Market is open — queueing startup cycle")
            scheduler.add_job(run_cycle, args=[_config], id="startup", name="Startup Cycle")

    # Graceful shutdown on SIGTERM / SIGINT
    def _shutdown(sig, frame):
        log.info("Shutdown signal received — stopping Daedalus gracefully")
        scheduler.shutdown(wait=False)
        sys.exit(0)

    signal.signal(signal.SIGTERM, _shutdown)
    signal.signal(signal.SIGINT, _shutdown)

    log.info("Daedalus is live. Monitoring ASX market hours...")
    scheduler.start()   # Blocks the main thread


if __name__ == "__main__":
    main()
