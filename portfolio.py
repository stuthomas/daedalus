"""
Daedalus Portfolio
Manages portfolio state as a JSON file on the deployment volume.
"""

import json
import logging
import math
import os
from datetime import datetime

import pytz

log = logging.getLogger("daedalus.portfolio")
AEST = pytz.timezone("Australia/Sydney")

# Cap on retained closed-trade records (dashboard history). Kept as a module
# constant so execute_trade needn't take the Config object.
_MAX_CLOSED_TRADES = 200
_MAX_NOTIFICATIONS = 60


# ── Initial state ─────────────────────────────────────────────────────────────

def _blank_portfolio(starting_capital: float) -> dict:
    today = datetime.now(AEST).strftime("%Y-%m-%d")
    return {
        "cash": starting_capital,
        "startingCapital": starting_capital,
        "currency": "AUD",
        "holdings": [],
        "transactions": [],
        "pendingTrades": [],
        "history": [
            {"date": today, "value": starting_capital, "cash": starting_capital, "invested": 0}
        ],
        "logs": [],
        "notifications": [],
        "lastAnalysis": None,
        "lastNews": None,
        "lastPMOutput": None,
        "lastRegime": None,
        "startDate": today,
        # Trade memory: last N PM decisions for context injection
        "tradeMemory": [],
        # Sentiment history: last N scores for trend analysis
        "sentimentHistory": [],
        # Realised-P&L / win-rate tracking (learning signal + dashboard)
        "closedTrades": [],
        "realizedPnL": 0.0,
        "wins": 0,
        "losses": 0,
        # Cycle health tracking
        "cycleHealth": {
            "lastSuccess": None,
            "lastFailure": None,
            "successStreak": 0,
            "totalCycles": 0,
            "totalErrors": 0,
        },
        "version": "3.0",
    }


# ── Load / Save ───────────────────────────────────────────────────────────────

def load_portfolio(config) -> dict:
    path = config.PORTFOLIO_FILE
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        # Migrate older portfolios missing newer fields
        data.setdefault("tradeMemory", [])
        data.setdefault("sentimentHistory", [])
        data.setdefault("pendingTrades", [])
        data.setdefault("notifications", [])
        data.setdefault("closedTrades", [])
        data.setdefault("realizedPnL", 0.0)
        data.setdefault("wins", 0)
        data.setdefault("losses", 0)
        data.setdefault("lastRegime", None)
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


# ── Notifications ─────────────────────────────────────────────────────────────

def add_notification(portfolio: dict, kind: str, title: str, detail: str = "") -> None:
    """
    Append an in-app notification (replaces email). kind is a semantic tag the
    dashboard styles: TRADE | ALERT | REGIME | CYCLE | INFO.
    """
    notes = portfolio.setdefault("notifications", [])
    notes.insert(0, {
        "ts": datetime.now(AEST).isoformat(),
        "kind": kind,
        "title": title,
        "detail": detail,
    })
    portfolio["notifications"] = notes[:_MAX_NOTIFICATIONS]


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
    add_notification(portfolio, "INFO", f"Funds added: ${amount:.2f} AUD",
                     f"Cash balance is now ${portfolio['cash']:.2f} AUD")

    log.info(f"Funds added: ${amount:.2f} AUD → cash now ${portfolio['cash']:.2f}")
    return {"success": True, "error": None, "newCash": portfolio["cash"]}


# ── Realised P&L ──────────────────────────────────────────────────────────────

def _record_closed_trade(portfolio: dict, holding: dict, shares_sold: int,
                         sell_price: float, reason: str, source: str) -> float:
    """Record realised P&L for a (partial) SELL and update win/loss stats."""
    avg = holding.get("avgBuyPrice", sell_price)
    realized = round((sell_price - avg) * shares_sold, 2)
    realized_pct = round(((sell_price - avg) / avg) * 100, 2) if avg > 0 else 0.0

    portfolio["realizedPnL"] = round(portfolio.get("realizedPnL", 0.0) + realized, 2)
    if realized > 0:
        portfolio["wins"] = portfolio.get("wins", 0) + 1
    elif realized < 0:
        portfolio["losses"] = portfolio.get("losses", 0) + 1

    closed = portfolio.setdefault("closedTrades", [])
    closed.insert(0, {
        "ts": datetime.now(AEST).isoformat(),
        "ticker": holding["ticker"],
        "name": holding.get("name", holding["ticker"]),
        "shares": shares_sold,
        "avgBuyPrice": round(avg, 4),
        "sellPrice": round(sell_price, 4),
        "realizedPnL": realized,
        "realizedPct": realized_pct,
        "reason": reason,
        "source": source,
    })
    portfolio["closedTrades"] = closed[:_MAX_CLOSED_TRADES]
    return realized


# ── Trade Execution ───────────────────────────────────────────────────────────

def execute_trade(portfolio: dict, trade: dict, source: str = "agent",
                  min_value: float = 0.0) -> dict:
    """
    Execute a BUY or SELL trade against the portfolio.
    source: "agent" | "manual"
    min_value: minimum AUD value per agent BUY trade (0 = no minimum).
    Returns {"success": bool, "error": str | None}.
    Modifies `portfolio` in place on success.
    """
    action = trade.get("action", "").upper()
    ticker = trade["ticker"]
    shares = int(trade["shares"])
    price  = float(trade["price"])
    total  = round(shares * price, 4)
    today  = datetime.now(AEST).strftime("%Y-%m-%d")

    # Enforce minimum trade *value* for agent BUY orders (manual trades exempt).
    if action == "BUY" and source == "agent" and min_value > 0 and total < min_value:
        return {
            "success": False,
            "error": f"Below minimum trade value: ${total:.2f} < ${min_value:.2f} minimum",
        }

    if action == "BUY":
        if shares <= 0:
            return {"success": False, "error": "Share count must be positive"}
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

        # Record realised P&L before mutating/removing the holding.
        _record_closed_trade(portfolio, existing, selling, price,
                             trade.get("reason", ""), existing.get("source", source))

        # Reflect the actual number of shares sold in the transaction record.
        shares = selling
        total = proceeds

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
                add_notification(portfolio, "ALERT", f"Stop-loss: sold {holding['ticker']}",
                                 trade["reason"])
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


def win_rate(portfolio: dict) -> dict:
    """Return {wins, losses, total, winRate} from closed-trade stats."""
    wins = portfolio.get("wins", 0)
    losses = portfolio.get("losses", 0)
    total = wins + losses
    return {
        "wins": wins,
        "losses": losses,
        "total": total,
        "winRate": round((wins / total) * 100, 1) if total else None,
        "realizedPnL": round(portfolio.get("realizedPnL", 0.0), 2),
    }
