"""
Daedalus — Autonomous ASX Portfolio Management Daemon
"""

import os
import sys
import logging
import threading
import signal
from datetime import datetime

import pytz
from flask import Flask, jsonify, request, send_from_directory
from flask_cors import CORS
from apscheduler.schedulers.blocking import BlockingScheduler
from apscheduler.events import EVENT_JOB_ERROR, EVENT_JOB_EXECUTED

from config import Config
from agents import run_cycle
from portfolio import (
    load_portfolio, save_portfolio, add_funds, execute_trade,
    snapshot_history, add_notification, win_rate,
)

# Windows consoles default to cp1252 and choke on the Unicode banner/arrows;
# force UTF-8 on the console streams so logging never raises UnicodeEncodeError.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8")
    except Exception:
        pass

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
BASE_DIR = os.path.dirname(os.path.abspath(__file__))

app = Flask(__name__)
CORS(app, resources={r"/*": {
    "origins": "*",
    "allow_headers": ["X-API-Key", "Content-Type"],
    "methods": ["GET", "POST", "OPTIONS"],
}})
_config: Config = None


def _require_api_key():
    """Return error response if API key is required and missing/wrong."""
    if _config.API_KEY:
        key = request.headers.get("X-API-Key", "")
        if key != _config.API_KEY:
            return jsonify({"error": "Unauthorized"}), 401
    return None


def _find_pending(portfolio: dict, data: dict):
    """Locate a pending trade by explicit index or ticker."""
    pending = portfolio.get("pendingTrades", [])
    idx = data.get("index")
    if isinstance(idx, int) and 0 <= idx < len(pending):
        return pending[idx]
    ticker = str(data.get("ticker", "")).upper()
    if ticker:
        return next((t for t in pending if str(t.get("ticker", "")).upper() == ticker), None)
    return None


# ── Dashboard (served locally, same-origin) ─────────────────────────────────────

@app.route("/")
@app.route("/dashboard")
def dashboard():
    return send_from_directory(BASE_DIR, "index.html")


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
    wr = win_rate(portfolio)
    regime = portfolio.get("lastRegime") or {}

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
        "pendingTrades": len(portfolio.get("pendingTrades", [])),
        "scheduledCycles": [f"{h:02d}:00 AEST Mon–Fri" for h in _config.CYCLE_HOURS],
        "cycleHealth": portfolio.get("cycleHealth", {}),
        "stopLossPct": _config.STOP_LOSS_PCT,
        "trailingStopPct": _config.TRAILING_STOP_PCT,
        "takeProfitPct": _config.TAKE_PROFIT_PCT,
        "minTradeValue": _config.MIN_TRADE_VALUE,
        "autoApprove": _config.AUTO_APPROVE_TRADES,
        "autoApproveMinConfidence": _config.AUTO_APPROVE_MIN_CONFIDENCE,
        "winRate": wr["winRate"],
        "wins": wr["wins"],
        "losses": wr["losses"],
        "realizedPnL": wr["realizedPnL"],
        "regime": {
            "signal": regime.get("signal"),
            "posture": regime.get("posture"),
            "headline": regime.get("headline"),
        } if regime.get("available") else None,
        "riskHealthScore": (portfolio.get("lastRiskReport") or {}).get("healthScore"),
    })


@app.route("/api/portfolio")
def get_portfolio():
    portfolio = load_portfolio(_config)
    keys_to_strip = {"lastAnalysis", "lastNews", "lastPMOutput"}
    return jsonify({k: v for k, v in portfolio.items() if k not in keys_to_strip})


@app.route("/api/portfolio/full")
def get_portfolio_full():
    return jsonify(load_portfolio(_config))


@app.route("/api/trigger", methods=["POST"])
def trigger_cycle():
    err = _require_api_key()
    if err:
        return err
    threading.Thread(target=run_cycle, args=[_config], daemon=True).start()
    return jsonify({"status": "cycle triggered", "time": datetime.now(AEST).isoformat()})


@app.route("/api/portfolio/add-funds", methods=["POST"])
def api_add_funds():
    """Add cash to the portfolio. Body: {"amount": 500.00}"""
    err = _require_api_key()
    if err:
        return err

    data = request.get_json(silent=True) or {}
    try:
        amount = float(data.get("amount", 0))
    except (TypeError, ValueError):
        return jsonify({"error": "Invalid amount"}), 400

    if amount <= 0:
        return jsonify({"error": "Amount must be positive"}), 400

    portfolio = load_portfolio(_config)
    result = add_funds(portfolio, amount)
    if result["success"]:
        snapshot_history(portfolio)
        save_portfolio(portfolio, _config)
        return jsonify({
            "success": True,
            "added": amount,
            "newCash": result["newCash"],
            "newStartingCapital": portfolio["startingCapital"],
        })
    return jsonify({"error": result["error"]}), 400


@app.route("/api/portfolio/manual-trade", methods=["POST"])
def api_manual_trade():
    """
    Manually enter a trade. Agents will monitor it like any other holding.
    Body: {"ticker": "BHP.AX", "name": "BHP Group", "action": "BUY",
           "shares": 10, "price": 42.50}
    """
    err = _require_api_key()
    if err:
        return err

    data = request.get_json(silent=True) or {}
    required = ["ticker", "action", "shares", "price"]
    missing = [f for f in required if not data.get(f)]
    if missing:
        return jsonify({"error": f"Missing fields: {missing}"}), 400

    try:
        trade = {
            "ticker": str(data["ticker"]).upper(),
            "name": str(data.get("name", data["ticker"])),
            "action": str(data["action"]).upper(),
            "shares": int(data["shares"]),
            "price": float(data["price"]),
            "total": int(data["shares"]) * float(data["price"]),
            "confidence": "MANUAL",
            "reason": str(data.get("reason", "Manual trade entered by investor")),
        }
    except (TypeError, ValueError) as e:
        return jsonify({"error": f"Invalid trade data: {e}"}), 400

    portfolio = load_portfolio(_config)
    result = execute_trade(portfolio, trade, source="manual")

    if result["success"]:
        snapshot_history(portfolio)
        save_portfolio(portfolio, _config)
        return jsonify({
            "success": True,
            "trade": trade,
            "newCash": round(portfolio["cash"], 2),
        })
    return jsonify({"error": result["error"]}), 400


@app.route("/api/portfolio/approve-trade", methods=["POST"])
def api_approve_trade():
    """Approve a pending trade. Body: {"ticker": "BHP.AX"} or {"index": 0}"""
    err = _require_api_key()
    if err:
        return err

    data = request.get_json(silent=True) or {}
    portfolio = load_portfolio(_config)
    trade = _find_pending(portfolio, data)
    if not trade:
        return jsonify({"error": "Pending trade not found"}), 404

    # Investor explicitly approved — don't re-gate on the minimum trade value.
    result = execute_trade(portfolio, trade, source="agent", min_value=0.0)
    if not result["success"]:
        return jsonify({"error": result["error"]}), 400

    portfolio["pendingTrades"] = [t for t in portfolio.get("pendingTrades", []) if t is not trade]
    add_notification(portfolio, "TRADE",
                     f"Approved: {trade['action']} {trade['shares']}× {trade['ticker']}",
                     trade.get("reason", ""))
    snapshot_history(portfolio)
    save_portfolio(portfolio, _config)
    return jsonify({"success": True, "trade": trade, "newCash": round(portfolio["cash"], 2)})


@app.route("/api/portfolio/reject-trade", methods=["POST"])
def api_reject_trade():
    """Reject/discard a pending trade. Body: {"ticker": "BHP.AX"} or {"index": 0}"""
    err = _require_api_key()
    if err:
        return err

    data = request.get_json(silent=True) or {}
    portfolio = load_portfolio(_config)
    trade = _find_pending(portfolio, data)
    if not trade:
        return jsonify({"error": "Pending trade not found"}), 404

    portfolio["pendingTrades"] = [t for t in portfolio.get("pendingTrades", []) if t is not trade]
    add_notification(portfolio, "INFO",
                     f"Rejected: {trade['action']} {trade['shares']}× {trade['ticker']}", "")
    save_portfolio(portfolio, _config)
    return jsonify({"success": True})


@app.route("/api/config", methods=["GET"])
def get_config():
    """Return editable configuration values."""
    return jsonify({
        "stopLossPct": _config.STOP_LOSS_PCT,
        "trailingStopPct": _config.TRAILING_STOP_PCT,
        "takeProfitPct": _config.TAKE_PROFIT_PCT,
        "takeProfitTrimPct": _config.TAKE_PROFIT_TRIM_PCT,
        "maxPositionPct": _config.MAX_POSITION_PCT,
        "maxSectorPct": _config.MAX_SECTOR_PCT,
        "cashBufferPct": _config.CASH_BUFFER_PCT,
        "autoApproveTrades": _config.AUTO_APPROVE_TRADES,
        "autoApproveMinConfidence": _config.AUTO_APPROVE_MIN_CONFIDENCE,
        "cycleHours": _config.CYCLE_HOURS,
        "tradeMemorySize": _config.TRADE_MEMORY_SIZE,
        "minTradeValue": _config.MIN_TRADE_VALUE,
        "asxIndexSymbol": _config.ASX_INDEX_SYMBOL,
        "pendingTradeExpiryHours": _config.PENDING_TRADE_EXPIRY_HOURS,
    })


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
    log.info(f"  PM model         : {_config.PM_MODEL}  (Opus)")
    log.info(f"  Auto-approve     : {_config.AUTO_APPROVE_TRADES} (≥ {_config.AUTO_APPROVE_MIN_CONFIDENCE})")
    log.info(f"  Stop-loss        : {_config.STOP_LOSS_PCT*100:.0f}%")
    log.info(f"  Trailing stop    : {_config.TRAILING_STOP_PCT*100:.0f}%")
    log.info(f"  Take-profit trim : {_config.TAKE_PROFIT_PCT*100:.0f}% → sell {_config.TAKE_PROFIT_TRIM_PCT*100:.0f}%")
    log.info(f"  Min trade value  : ${_config.MIN_TRADE_VALUE:.0f}")
    log.info(f"  Max position     : {_config.MAX_POSITION_PCT*100:.0f}%")
    log.info(f"  Regime index     : {_config.ASX_INDEX_SYMBOL}")

    port = int(os.getenv("PORT", 8080))
    flask_thread = threading.Thread(
        target=lambda: app.run(host="0.0.0.0", port=port, use_reloader=False),
        daemon=True,
    )
    flask_thread.start()
    log.info(f"  API + dashboard  : http://0.0.0.0:{port}/")

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
            misfire_grace_time=300,
        )
        log.info(f"  Scheduled cycle  : {hour:02d}:00 AEST Mon–Fri")

    if _config.RUN_ON_STARTUP:
        now = datetime.now(AEST)
        if now.weekday() < 5 and 10 <= now.hour < 16:
            log.info("Market is open — queueing startup cycle")
            scheduler.add_job(run_cycle, args=[_config], id="startup", name="Startup Cycle")

    def _shutdown(sig, frame):
        log.info("Shutdown signal received — stopping Daedalus gracefully")
        scheduler.shutdown(wait=False)
        sys.exit(0)

    signal.signal(signal.SIGTERM, _shutdown)
    signal.signal(signal.SIGINT, _shutdown)

    log.info("Daedalus is live. Monitoring ASX market hours...")
    scheduler.start()


if __name__ == "__main__":
    main()
