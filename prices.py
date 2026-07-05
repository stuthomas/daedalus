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


def _stdev_pct(closes: list[float]) -> float | None:
    """Standard deviation of daily returns, as a percentage."""
    if len(closes) < 3:
        return None
    returns = [(closes[i] - closes[i - 1]) / closes[i - 1] for i in range(1, len(closes))]
    mean = sum(returns) / len(returns)
    variance = sum((r - mean) ** 2 for r in returns) / len(returns)
    return round(math.sqrt(variance) * 100, 3)


def compute_regime(history: list[dict], breadth: float | None = None,
                   sentiment_score: float | None = None) -> dict:
    """
    Self-contained ASX market-regime read — the free-data adaptation of the SPX
    GEX 'which regime am I in / is it safe to enter today' concept.

    In dealer-GEX terms: positive gamma = mean-reverting = calmer = good to enter;
    negative gamma = trending/volatile = dangerous. With no free ASX options
    chain we approximate that posture from index price action, realised
    volatility, breadth, and news sentiment.

    Args:
        history: index OHLCV history (e.g. ^AXJO), newest last.
        breadth: fraction (0-1) of tracked names above their SMA20, if known.
        sentiment_score: news sentiment 0-100, if known.
    """
    if len(history) < 20:
        return {
            "available": False,
            "signal": "AMBER",
            "headline": "Insufficient index data for a regime read",
            "posture": "UNKNOWN",
        }

    tech = compute_technicals(history)
    closes = [d["close"] for d in history]
    current = closes[-1]

    # Realised volatility: short (10d) vs long (30d) window of daily returns.
    vol_short = _stdev_pct(closes[-11:]) or 0.0   # last 10 returns
    vol_long = _stdev_pct(closes[-31:]) or vol_short

    if vol_short < 0.8:
        vol_regime = "calm"
    elif vol_short < 1.3:
        vol_regime = "normal"
    elif vol_short < 2.0:
        vol_regime = "elevated"
    else:
        vol_regime = "high"

    if vol_long > 0 and vol_short > vol_long * 1.15:
        vol_trend = "rising"
    elif vol_long > 0 and vol_short < vol_long * 0.87:
        vol_trend = "falling"
    else:
        vol_trend = "stable"

    sma20 = tech.get("sma20")
    sma50 = tech.get("sma50")

    # SMA20 slope over the last 5 sessions (momentum direction).
    slope_pct = 0.0
    if len(closes) >= 25:
        sma20_now = sum(closes[-20:]) / 20
        sma20_prev = sum(closes[-25:-5]) / 20
        if sma20_prev:
            slope_pct = round(((sma20_now - sma20_prev) / sma20_prev) * 100, 2)

    above20 = sma20 is not None and current > sma20
    above50 = sma50 is not None and current > sma50

    # Trend / regime classification.
    if vol_regime == "high":
        trend = "VOLATILE"
    elif above20 and above50 and slope_pct > 0.2:
        trend = "TRENDING_UP"
    elif (not above20) and (sma50 is None or not above50) and slope_pct < -0.2:
        trend = "TRENDING_DOWN"
    else:
        trend = "RANGE"

    # GEX-analog posture.
    if trend == "RANGE" and vol_regime in ("calm", "normal"):
        posture = "MEAN-REVERTING"      # ~ positive gamma — favourable to enter
    elif trend == "TRENDING_UP" and vol_regime in ("calm", "normal"):
        posture = "TRENDING"            # constructive, but chase-risk
    else:
        posture = "VOLATILE"            # ~ negative gamma — dangerous

    # Safe-to-enter traffic light.
    if (vol_regime == "high" or trend == "TRENDING_DOWN"
            or (sentiment_score is not None and sentiment_score < 35)):
        signal = "RED"
    elif (vol_regime in ("calm", "normal") and trend in ("RANGE", "TRENDING_UP")
          and (sentiment_score is None or sentiment_score >= 55)
          and (breadth is None or breadth >= 0.5)):
        signal = "GREEN"
    else:
        signal = "AMBER"

    advice = {
        "GREEN": "Conditions favour entries — calm, mean-reverting/constructive tape.",
        "AMBER": "Mixed conditions — be selective, size down, favour pullbacks.",
        "RED": "Elevated risk — trending-down or volatile tape. Prefer to wait / hold cash.",
    }[signal]

    posture_note = {
        "MEAN-REVERTING": "Calm, range-bound tape — pullback entries tend to revert (best regime).",
        "TRENDING": "Constructive uptrend — momentum favourable but avoid chasing extensions.",
        "VOLATILE": "Trending/volatile tape — moves accelerate, mean-reversion unreliable.",
    }[posture]

    headline = f"{posture} · vol {vol_regime} ({vol_trend}) · {signal}"

    return {
        "available": True,
        "asOf": history[-1]["date"],
        "price": round(current, 2),
        "signal": signal,
        "posture": posture,
        "postureNote": posture_note,
        "advice": advice,
        "trend": trend,
        "volShort": vol_short,
        "volLong": vol_long,
        "volRegime": vol_regime,
        "volTrend": vol_trend,
        "slopePct": slope_pct,
        "breadthPct": round(breadth * 100, 1) if breadth is not None else None,
        "sentimentScore": sentiment_score,
        "rsi14": tech.get("rsi14"),
        "sma20": sma20,
        "sma50": sma50,
        "support": tech.get("support20d"),
        "resistance": tech.get("resistance20d"),
        "change5d": tech.get("change5d"),
        "change20d": tech.get("change20d"),
        "pctFrom52wHigh": tech.get("pctFrom52wHigh"),
        "headline": headline,
    }


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
