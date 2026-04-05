"""
Daedalus Portfolio
Manages portfolio state as a JSON file.

Railway/Render persistence note:
  - Railway: add a Volume mounted at /data and set PORTFOLIO_FILE=/data/portfolio.json
  - Render:  add a Disk mounted at /data and set PORTFOLIO_FILE=/data/portfolio.json
  Both platforms will then persist the file across deploys and restarts.
"""

import json
import logging
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
        "version": "1.0",
    }


# ── Load / Save ───────────────────────────────────────────────────────────────

def load_portfolio(config) -> dict:
    path = config.PORTFOLIO_FILE
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
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


# ── Trade Execution ───────────────────────────────────────────────────────────

def execute_trade(portfolio: dict, trade: dict) -> dict:
    """
    Execute a BUY or SELL trade against the portfolio.
    Returns {"success": bool, "error": str | None}.
    Modifies `portfolio` in place on success.
    """
    action = trade.get("action", "").upper()
    ticker = trade["ticker"]
    shares = int(trade["shares"])
    price  = float(trade["price"])
    total  = round(shares * price, 4)
    today  = datetime.now(AEST).strftime("%Y-%m-%d")

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
        else:
            portfolio["holdings"].append({
                "ticker": ticker,
                "name": trade.get("name", ticker),
                "shares": shares,
                "avgBuyPrice": price,
                "currentPrice": price,
                "purchaseDate": today,
            })

    elif action == "SELL":
        existing = next((h for h in portfolio["holdings"] if h["ticker"] == ticker), None)
        if not existing:
            return {"success": False, "error": f"No holding found for {ticker}"}

        selling = min(shares, existing["shares"])
        portfolio["cash"] = round(portfolio["cash"] + selling * price, 4)

        if existing["shares"] - selling <= 0:
            portfolio["holdings"] = [h for h in portfolio["holdings"] if h["ticker"] != ticker]
        else:
            existing["shares"] -= selling
            existing["currentPrice"] = price

    else:
        return {"success": False, "error": f"Unknown action: {action}"}

    # ── Record transaction ──────────────────────────────────────────────────
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
        "agent": "Portfolio Manager (Auto-approved)" if trade.get("autoApproved") else "Portfolio Manager",
    }
    portfolio.setdefault("transactions", []).insert(0, tx)

    # ── Agent log entry ─────────────────────────────────────────────────────
    portfolio.setdefault("logs", []).insert(0, {
        "ts": datetime.now(AEST).isoformat(),
        "agent": "Portfolio Manager",
        "type": "EXECUTED",
        "title": f"{action} {shares}× {ticker} @ ${price:.2f}",
        "content": trade.get("reason", ""),
    })

    return {"success": True, "error": None}


# ── History Snapshot ──────────────────────────────────────────────────────────

def snapshot_history(portfolio: dict) -> None:
    """Add or update today's value entry in portfolio history."""
    today  = datetime.now(AEST).strftime("%Y-%m-%d")
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
