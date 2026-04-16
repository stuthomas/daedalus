"""
Daedalus Portfolio
Manages portfolio state as a JSON file on the Railway volume.
"""

import json
import logging
import math
import os
from datetime import datetime

import pytz

log = logging.getLogger("daedalus.portfolio")
AEST = pytz.timezone("Australia/Sydney")


# ── Initial state ─────────────────────────────────────────────────────────────

def _blank_portfolio(starting_capital: float) -> dict:
    today = datetime.now(AEST).strftime("%Y-%m-%d")
    return {
        "cash": starting_capital,
        "startingCapital": starting_capital,
        "currency": "AUD",
        "holdings": [],
        "transactions": [],
        "history": [
            {"date": today, "value": starting_capital, "cash": starting_capital, "invested": 0}
        ],
        "logs": [],
        "lastAnalysis": None,
        "lastNews": None,
        "lastPMOutput": None,
        "startDate": today,
        # Trade memory: last N PM decisions for context injection
        "tradeMemory": [],
        # Sentiment history: last N scores for trend analysis
        "sentimentHistory": [],
        # Cycle health tracking
        "cycleHealth": {
            "lastSuccess": None,
            "lastFailure": None,
            "successStreak": 0,
            "totalCycles": 0,
            "totalErrors": 0,
        },
        "version": "2.0",
    }


# ── Load / Save ───────────────────────────────────────────────────────────────

def load_portfolio(config) -> dict:
    path = config.PORTFOLIO_FILE
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        # Migrate old portfolios missing new fields
        data.setdefault("tradeMemory", [])
        data.setdefault("sentimentHistory", [])
        data.setdefault("cycleHealth", {
            "lastSuccess": None, "lastFailure": None,
            "successStreak": 0, "totalCycles": 0, "totalErrors": 0,
        })
        log.debug(f"Portfolio loaded from {path}")
        return data

    log.info(f"No portfolio file at {path} — creating fresh ${config.STARTING_CAPITAL:.2f} AUD portfolio")
    portfolio = _blank_portfolio(config.STARTING_CAPITAL)
    save_portfolio(portfolio, config)
    return portfolio


def save_portfolio(portfolio: dict, config) -> None:
    path = config.PORTFOLIO_FILE
    os.makedirs(os.path.dirname(path) if os.path.dirname(path) else ".", exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(portfolio, f, indent=2, ensure_ascii=False)
    log.debug(f"Portfolio saved to {path}")


# ── Add Funds ─────────────────────────────────────────────────────────────────

def add_funds(portfolio: dict, amount: float) -> dict:
    """
    Add cash to the portfolio (e.g. you deposited more money).
    Records a DEPOSIT transaction and updates history.
    """
    if amount <= 0:
        return {"success": False, "error": "Amount must be positive"}

    today = datetime.now(AEST).strftime("%Y-%m-%d")
    portfolio["cash"] = round(portfolio["cash"] + amount, 4)
    # Increase starting capital baseline so P&L is calculated correctly
    portfolio["startingCapital"] = round(portfolio["startingCapital"] + amount, 4)

    tx = {
        "id": int(datetime.now().timestamp() * 1000),
        "date": today,
        "type": "DEPOSIT",
        "ticker": "CASH",
        "name": "Cash Deposit",
        "shares": 0,
        "price": 1.0,
        "total": round(amount, 2),
        "confidence": "",
        "reason": f"Manual deposit of ${amount:.2f} AUD",
        "agent": "Manual",
        "source": "manual",
    }
    portfolio.setdefault("transactions", []).insert(0, tx)
    portfolio.setdefault("logs", []).insert(0, {
        "ts": datetime.now(AEST).isoformat(),
        "agent": "System",
        "type": "DEPOSIT",
        "title": f"Funds added: ${amount:.2f} AUD",
        "content": f"Cash balance is now ${portfolio['cash']:.2f} AUD",
    })

    log.info(f"Funds added: ${amount:.2f} AUD → cash now ${portfolio['cash']:.2f}")
    return {"success": True, "error": None, "newCash": portfolio["cash"]}


# ── Trade Execution ───────────────────────────────────────────────────────────

def execute_trade(portfolio: dict, trade: dict, source: str = "agent",
                  min_shares: int = 0) -> dict:
    """
    Execute a BUY or SELL trade against the portfolio.
    source: "agent" | "manual"
    min_shares: minimum shares per BUY trade (0 = no minimum, set from config.MIN_TRADE_SHARES)
    Returns {"success": bool, "error": str | None}.
    Modifies `portfolio` in place on success.
    """
    action = trade.get("action", "").upper()
    ticker = trade["ticker"]
    shares = int(trade["shares"])
    price  = float(trade["price"])
    total  = round(shares * price, 4)
    today  = datetime.now(AEST).strftime("%Y-%m-%d")

    # Enforce minimum trade size for agent BUY orders (manual trades exempt)
    if action == "BUY" and source == "agent" and min_shares > 0 and shares < min_shares:
        return {
            "success": False,
            "error": f"Below minimum trade size: {shares} shares < {min_shares} minimum",
        }

    if action == "BUY":
        if total > portfolio["cash"] + 0.01:
            return {
                "success": False,
                "error": f"Insufficient cash: need ${total:.2f}, have ${portfolio['cash']:.2f}",
            }
        portfolio["cash"] = round(portfolio["cash"] - total, 4)

        existing = next((h for h in portfolio["holdings"] if h["ticker"] == ticker), None)
        if existing:
            new_shares = existing["shares"] + shares
            existing["avgBuyPrice"] = round(
                (existing["shares"] * existing["avgBuyPrice"] + total) / new_shares, 4
            )
            existing["shares"] = new_shares
            existing["currentPrice"] = price
            existing["lastUpdated"] = today
        else:
            portfolio["holdings"].append({
                "ticker": ticker,
                "name": trade.get("name", ticker),
                "shares": shares,
                "avgBuyPrice": price,
                "currentPrice": price,
                "purchaseDate": today,
                "lastUpdated": today,
                "source": source,        # "agent" or "manual"
                "priceHistory": [{"date": today, "price": price}],
            })

    elif action == "SELL":
        existing = next((h for h in portfolio["holdings"] if h["ticker"] == ticker), None)
        if not existing:
            return {"success": False, "error": f"No holding found for {ticker}"}

        selling = min(shares, existing["shares"])
        proceeds = round(selling * price, 4)
        portfolio["cash"] = round(portfolio["cash"] + proceeds, 4)

        if existing["shares"] - selling <= 0:
            portfolio["holdings"] = [h for h in portfolio["holdings"] if h["ticker"] != ticker]
        else:
            existing["shares"] -= selling
            existing["currentPrice"] = price
            existing["lastUpdated"] = today

    else:
        return {"success": False, "error": f"Unknown action: {action}"}

    # ── Record transaction ──────────────────────────────────────────────────
    agent_label = trade.get("agent", "Portfolio Manager")
    if source == "manual":
        agent_label = "Manual"
    elif trade.get("autoApproved"):
        agent_label = "Portfolio Manager (Auto-approved)"

    tx = {
        "id": int(datetime.now().timestamp() * 1000),
        "date": today,
        "type": action,
        "ticker": ticker,
        "name": trade.get("name", ticker),
        "shares": shares,
        "price": price,
        "total": round(total, 2),
        "confidence": trade.get("confidence", ""),
        "reason": trade.get("reason", ""),
        "agent": agent_label,
        "source": source,
        "stopLoss": trade.get("stopLoss", False),
    }
    portfolio.setdefault("transactions", []).insert(0, tx)

    log_type = "STOP_LOSS" if trade.get("stopLoss") else "EXECUTED"
    portfolio.setdefault("logs", []).insert(0, {
        "ts": datetime.now(AEST).isoformat(),
        "agent": agent_label,
        "type": log_type,
        "title": f"{action} {shares}× {ticker} @ ${price:.2f}",
        "content": trade.get("reason", ""),
    })

    return {"success": True, "error": None}


# ── Stop-Loss Checker ─────────────────────────────────────────────────────────

def check_stop_losses(portfolio: dict, config) -> list[dict]:
    """
    Check all holdings against the stop-loss threshold.
    Returns list of stop-loss trades that were executed.
    Does NOT save the portfolio — caller must call save_portfolio().
    """
    if config.STOP_LOSS_PCT <= 0:
        return []

    triggered = []
    for holding in list(portfolio["holdings"]):
        current = holding.get("currentPrice", holding["avgBuyPrice"])
        drop_pct = (holding["avgBuyPrice"] - current) / holding["avgBuyPrice"]

        if drop_pct >= config.STOP_LOSS_PCT:
            trade = {
                "ticker": holding["ticker"],
                "name": holding["name"],
                "action": "SELL",
                "shares": holding["shares"],
                "price": current,
                "total": round(holding["shares"] * current, 2),
                "confidence": "HIGH",
                "reason": (
                    f"Stop-loss triggered: position dropped {drop_pct*100:.1f}% "
                    f"from avg buy ${holding['avgBuyPrice']:.2f} to ${current:.2f} "
                    f"(threshold: {config.STOP_LOSS_PCT*100:.0f}%)"
                ),
                "stopLoss": True,
            }
            result = execute_trade(portfolio, trade, source=holding.get("source", "agent"))
            if result["success"]:
                triggered.append(trade)
                log.warning(
                    f"STOP-LOSS: Sold {holding['shares']}× {holding['ticker']} "
                    f"@ ${current:.2f} (dropped {drop_pct*100:.1f}%)"
                )
            else:
                log.error(f"Stop-loss sell failed for {holding['ticker']}: {result['error']}")

    return triggered


# ── Price Update ──────────────────────────────────────────────────────────────

def update_holding_prices(portfolio: dict, analyst_data: dict) -> None:
    """
    Update currentPrice for any holding mentioned in analyst recommendations.
    Also appends to priceHistory for volatility calculation.
    """
    recs = analyst_data.get("recs") or []
    price_map = {r["t"]: r["price"] for r in recs if r.get("price")}
    today = datetime.now(AEST).strftime("%Y-%m-%d")

    for holding in portfolio["holdings"]:
        ticker = holding["ticker"]
        if ticker in price_map:
            new_price = price_map[ticker]
            holding["currentPrice"] = new_price
            holding["lastUpdated"] = today

            # Maintain rolling price history (last 30 data points)
            history = holding.setdefault("priceHistory", [])
            if not history or history[-1]["date"] != today:
                history.append({"date": today, "price": new_price})
            else:
                history[-1]["price"] = new_price  # update today's entry
            holding["priceHistory"] = history[-30:]

            # Calculate simple volatility (std dev of daily returns)
            if len(history) >= 3:
                prices = [p["price"] for p in history]
                returns = [(prices[i] - prices[i-1]) / prices[i-1] for i in range(1, len(prices))]
                mean = sum(returns) / len(returns)
                variance = sum((r - mean) ** 2 for r in returns) / len(returns)
                holding["volatility"] = round(math.sqrt(variance) * 100, 2)  # % per period


# ── History Snapshot ──────────────────────────────────────────────────────────

def snapshot_history(portfolio: dict) -> None:
    """Add or update today's value entry in portfolio history."""
    today    = datetime.now(AEST).strftime("%Y-%m-%d")
    invested = sum(
        h["shares"] * h.get("currentPrice", h["avgBuyPrice"])
        for h in portfolio.get("holdings", [])
    )
    total = round(portfolio["cash"] + invested, 2)
    entry = {
        "date": today,
        "value": total,
        "cash": round(portfolio["cash"], 2),
        "invested": round(invested, 2),
    }
    history = portfolio.setdefault("history", [])
    if history and history[-1]["date"] == today:
        history[-1] = entry
    else:
        history.append(entry)


# ── Helpers ───────────────────────────────────────────────────────────────────

def total_value(portfolio: dict) -> float:
    invested = sum(
        h["shares"] * h.get("currentPrice", h["avgBuyPrice"])
        for h in portfolio.get("holdings", [])
    )
    return round(portfolio["cash"] + invested, 2)
