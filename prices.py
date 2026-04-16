"""
Daedalus Prices
Fetches real-time and historical price data from Yahoo Finance.
Falls back gracefully if yfinance is unavailable or a ticker fails.
"""

import logging
import math
from datetime import datetime, timedelta

import pytz

log = logging.getLogger("daedalus.prices")
AEST = pytz.timezone("Australia/Sydney")

try:
    import yfinance as yf
    HAS_YFINANCE = True
except ImportError:
    HAS_YFINANCE = False
    log.warning("yfinance not installed — price lookups will be skipped")


def get_live_price(ticker: str) -> float | None:
    """Fetch the current/last price for an ASX ticker. Returns None on failure."""
    if not HAS_YFINANCE:
        return None
    try:
        t = yf.Ticker(ticker)
        info = t.info
        # Try multiple fields — Yahoo is inconsistent
        price = (
            info.get("currentPrice")
            or info.get("regularMarketPrice")
            or info.get("previousClose")
        )
        if price and price > 0:
            return round(float(price), 4)
        return None
    except Exception as e:
        log.debug(f"Price fetch failed for {ticker}: {e}")
        return None


def get_bulk_prices(tickers: list[str]) -> dict[str, float]:
    """Fetch live prices for multiple tickers. Returns {ticker: price} for successes."""
    if not HAS_YFINANCE or not tickers:
        return {}

    prices = {}
    try:
        data = yf.download(tickers, period="1d", progress=False, threads=True)
        if data.empty:
            return {}
        # yf.download returns MultiIndex columns for multiple tickers
        if len(tickers) == 1:
            close = data.get("Close")
            if close is not None and not close.empty:
                val = close.iloc[-1]
                if not math.isnan(val):
                    prices[tickers[0]] = round(float(val), 4)
        else:
            close = data.get("Close")
            if close is not None:
                for ticker in tickers:
                    if ticker in close.columns:
                        val = close[ticker].dropna()
                        if not val.empty:
                            prices[ticker] = round(float(val.iloc[-1]), 4)
    except Exception as e:
        log.warning(f"Bulk price fetch failed: {e}")
        # Fall back to individual lookups
        for ticker in tickers:
            p = get_live_price(ticker)
            if p:
                prices[ticker] = p

    return prices


def get_price_history(ticker: str, days: int = 60) -> list[dict]:
    """
    Fetch daily OHLCV history for a ticker.
    Returns list of {"date": str, "open": f, "high": f, "low": f, "close": f, "volume": int}.
    """
    if not HAS_YFINANCE:
        return []
    try:
        t = yf.Ticker(ticker)
        hist = t.history(period=f"{days}d")
        if hist.empty:
            return []
        result = []
        for date, row in hist.iterrows():
            result.append({
                "date": date.strftime("%Y-%m-%d"),
                "open": round(float(row["Open"]), 4),
                "high": round(float(row["High"]), 4),
                "low": round(float(row["Low"]), 4),
                "close": round(float(row["Close"]), 4),
                "volume": int(row["Volume"]),
            })
        return result
    except Exception as e:
        log.debug(f"History fetch failed for {ticker}: {e}")
        return []


def compute_technicals(history: list[dict]) -> dict:
    """
    Compute basic technical indicators from price history.
    Returns dict with RSI, SMA20, SMA50, VWAP, support/resistance, etc.
    """
    if len(history) < 5:
        return {}

    closes = [d["close"] for d in history]
    volumes = [d["volume"] for d in history]
    highs = [d["high"] for d in history]
    lows = [d["low"] for d in history]
    current = closes[-1]

    result = {"currentPrice": current}

    # Simple Moving Averages
    if len(closes) >= 20:
        sma20 = sum(closes[-20:]) / 20
        result["sma20"] = round(sma20, 4)
        result["aboveSMA20"] = current > sma20

    if len(closes) >= 50:
        sma50 = sum(closes[-50:]) / 50
        result["sma50"] = round(sma50, 4)
        result["aboveSMA50"] = current > sma50

    # RSI (14-period)
    if len(closes) >= 15:
        deltas = [closes[i] - closes[i - 1] for i in range(1, len(closes))]
        recent = deltas[-14:]
        gains = [d for d in recent if d > 0]
        losses = [-d for d in recent if d < 0]
        avg_gain = sum(gains) / 14 if gains else 0
        avg_loss = sum(losses) / 14 if losses else 0.0001
        rs = avg_gain / avg_loss
        rsi = 100 - (100 / (1 + rs))
        result["rsi14"] = round(rsi, 2)
        result["rsiSignal"] = (
            "OVERSOLD" if rsi < 30 else
            "OVERBOUGHT" if rsi > 70 else
            "NEUTRAL"
        )

    # Support/Resistance (recent 20-day high/low)
    recent_highs = highs[-20:] if len(highs) >= 20 else highs
    recent_lows = lows[-20:] if len(lows) >= 20 else lows
    result["resistance20d"] = round(max(recent_highs), 4)
    result["support20d"] = round(min(recent_lows), 4)

    # 52-week high/low
    result["high52w"] = round(max(highs), 4)
    result["low52w"] = round(min(lows), 4)
    result["pctFrom52wHigh"] = round(((current - result["high52w"]) / result["high52w"]) * 100, 2)
    result["pctFrom52wLow"] = round(((current - result["low52w"]) / result["low52w"]) * 100, 2)

    # Average volume (20-day)
    if len(volumes) >= 20:
        avg_vol = sum(volumes[-20:]) / 20
        result["avgVolume20d"] = int(avg_vol)
        result["volumeRatio"] = round(volumes[-1] / avg_vol, 2) if avg_vol > 0 else 0

    # Daily returns volatility
    if len(closes) >= 10:
        returns = [(closes[i] - closes[i - 1]) / closes[i - 1] for i in range(1, len(closes))]
        mean_ret = sum(returns) / len(returns)
        variance = sum((r - mean_ret) ** 2 for r in returns) / len(returns)
        result["volatility"] = round(math.sqrt(variance) * 100, 2)

    # Price momentum (% change over periods)
    if len(closes) >= 5:
        result["change5d"] = round(((current - closes[-5]) / closes[-5]) * 100, 2)
    if len(closes) >= 20:
        result["change20d"] = round(((current - closes[-20]) / closes[-20]) * 100, 2)

    return result


def update_all_holding_prices(portfolio: dict) -> dict[str, float]:
    """
    Fetch live prices for all holdings and update them in-place.
    Returns the price map {ticker: price} for successfully updated holdings.
    """
    tickers = [h["ticker"] for h in portfolio.get("holdings", [])]
    if not tickers:
        return {}

    prices = get_bulk_prices(tickers)
    today = datetime.now(AEST).strftime("%Y-%m-%d")

    for holding in portfolio["holdings"]:
        ticker = holding["ticker"]
        if ticker in prices:
            new_price = prices[ticker]
            holding["currentPrice"] = new_price
            holding["lastUpdated"] = today

            history = holding.setdefault("priceHistory", [])
            if not history or history[-1]["date"] != today:
                history.append({"date": today, "price": new_price})
            else:
                history[-1]["price"] = new_price
            holding["priceHistory"] = history[-30:]

    log.info(f"Updated prices for {len(prices)}/{len(tickers)} holdings via Yahoo Finance")
    return prices
