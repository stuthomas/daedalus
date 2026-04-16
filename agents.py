"""
Daedalus Agents
Six Claude-powered agents that work together each market-hours cycle.

  Corporate Analyst     → claude-haiku-4-5  (web search, free-range market discovery)
  News Intelligence     → claude-haiku-4-5  (web search, market news + sentiment)
  Technical Analyst     → local only        (Yahoo Finance price data + indicators)
  Earnings Calendar     → claude-haiku-4-5  (web search, upcoming events)
  Risk / Rebalancing    → local only        (portfolio concentration + limits)
  Portfolio Manager     → claude-sonnet-4-6 (synthesis + trade decisions, with memory)
"""

import json
import logging
import time
from datetime import datetime

import pytz
from anthropic import Anthropic

from config import Config
from portfolio import (
    load_portfolio, save_portfolio, execute_trade,
    snapshot_history, total_value, check_stop_losses,
    update_holding_prices,
)
from prices import (
    update_all_holding_prices, get_price_history,
    compute_technicals, get_bulk_prices,
)
from notifier import send_cycle_summary

log = logging.getLogger("daedalus.agents")
AEST = pytz.timezone("Australia/Sydney")


# ── Shared Claude caller ──────────────────────────────────────────────────────

def _call_claude(client: Anthropic, model: str, system: str, prompt: str,
                 use_search: bool = True) -> dict:
    """
    Call Claude, optionally with web_search, parse the JSON response.
    Handles: empty text responses, JSON parse errors, rate limits.
    The Anthropic client already retries rate limits internally with retry-after.
    Our outer loop handles the remaining failure modes.
    """
    kwargs = {
        "model": model,
        "max_tokens": 4000,
        "system": system,
        "messages": [{"role": "user", "content": prompt}],
    }
    if use_search:
        kwargs["tools"] = [{"type": "web_search_20250305", "name": "web_search"}]

    raw_text = ""
    for attempt in range(3):
        try:
            response = client.messages.create(**kwargs)
            raw_text = "".join(b.text for b in response.content if b.type == "text")

            if not raw_text.strip():
                log.warning(f"Empty text response on attempt {attempt + 1}, retrying in 30s…")
                time.sleep(30)
                continue

            # Strip markdown fences, extract outermost JSON object/array
            clean = raw_text.replace("```json", "").replace("```", "").strip()
            start = next((i for i, c in enumerate(clean) if c in "{["), None)
            end_b = clean.rfind("}")
            end_k = clean.rfind("]")
            end   = max(end_b, end_k)

            if start is not None and end > start:
                clean = clean[start:end + 1]

            if not clean:
                log.warning(f"No JSON found on attempt {attempt + 1}, retrying in 30s…")
                time.sleep(30)
                continue

            return json.loads(clean)

        except json.JSONDecodeError as e:
            log.warning(f"JSON parse error on attempt {attempt + 1}: {e}, retrying in 15s…")
            if attempt < 2:
                time.sleep(15)
            else:
                raise RuntimeError(
                    f"Could not parse JSON after 3 attempts. "
                    f"Last text snippet: {raw_text[:300]}"
                )
        except Exception as e:
            err = str(e)
            if "rate_limit" in err.lower() or "429" in err:
                # Client already exhausted its internal retries — wait a full window
                log.warning(f"Rate limited (client retries exhausted) on attempt {attempt + 1}, waiting 65s…")
                time.sleep(65)
            else:
                raise  # Non-rate-limit errors bubble up immediately

    raise RuntimeError("All retries exhausted — no valid JSON response from Claude")


# ── Agent 1: Corporate Analyst (Haiku) ───────────────────────────────────────

def run_corporate_analyst(client: Anthropic, config: Config, portfolio: dict) -> dict:
    """
    Freely searches the ASX for market opportunities — no fixed watchlist.
    Identifies which sectors and stocks are moving based on current conditions.
    Also checks performance of existing holdings.
    """
    log.info("▶ Corporate Analyst (Haiku) — discovering ASX opportunities...")
    today = datetime.now(AEST).strftime("%Y-%m-%d")
    held  = ", ".join(
        f"{h['ticker']} ({h['name']}, avg ${h['avgBuyPrice']:.2f}, "
        f"now ~${h.get('currentPrice', h['avgBuyPrice']):.2f}, "
        f"source: {h.get('source','agent')})"
        for h in portfolio.get("holdings", [])
    ) or "none"
    cash = portfolio["cash"]

    system = (
        "You are an ASX equity research analyst. Search for real, current market data. "
        "Identify opportunities based on what is ACTUALLY moving in markets today. "
        "Do not default to the same safe blue-chip stocks every time — seek genuine opportunities. "
        "You are SECTOR-AGNOSTIC: do not favour or fixate on any particular sector "
        "(e.g. data centres, gold, mining). Invest in whatever sector offers the best "
        "risk-adjusted opportunity RIGHT NOW based on current news, earnings, and price action. "
        "Respond ONLY with valid JSON — no markdown, no backticks, no preamble, no trailing text."
    )

    prompt = f"""Today is {today}. You are analysing the ASX for investment opportunities.

CURRENT PORTFOLIO:
Holdings: {held}
Available cash: ${cash:.2f} AUD

YOUR TASK:
1. Search broadly for what is genuinely moving on the ASX today — which sectors are outperforming?
2. Do NOT default to the same sectors each cycle. Scan ALL sectors including but not limited to:
   tech, healthcare, financials, consumer, industrials, energy, materials, mining, agriculture,
   biotech, retail, telecoms, infrastructure, REITs, defence, and any emerging themes.
3. Identify 3–6 specific ASX-listed stocks (use .AX suffix) that represent the BEST opportunities
   RIGHT NOW regardless of sector. Follow the opportunity, not the sector.
4. Also search current prices for any existing holdings listed above.
5. For each recommendation, check for recent insider trading activity (directors/CEO selling
   or buying shares). Flag any significant insider selling as a risk factor.
6. Prioritise stocks trading at attractive valuations relative to their recent history
   (i.e. buy low opportunities — stocks that have pulled back but have strong fundamentals).

Return ONLY this JSON with real data from your searches:
{{
  "date": "{today}",
  "market": "2-sentence overview of today's ASX conditions",
  "topSectors": ["sector1", "sector2", "sector3"],
  "recs": [
    {{
      "t": "TICKER.AX",
      "n": "Company Name",
      "action": "BUY",
      "price": 0.00,
      "alloc": 20,
      "thesis": "One sentence: why this stock, why now",
      "sector": "Sector name",
      "conf": "HIGH",
      "risks": ["risk1", "risk2"],
      "pe": 15.0,
      "div": "3.5%",
      "catalyst": "Specific near-term catalyst",
      "insiderActivity": "Summary of recent insider buying/selling, or 'none detected'"
    }}
  ],
  "holdingUpdates": {{
    "TICKER.AX": {{"price": 0.00, "view": "HOLD or SELL or ADD", "note": "brief", "insiderActivity": "any insider trades detected"}}
  }},
  "notes": "Any important market-wide observations"
}}"""

    result = _call_claude(client, config.ANALYST_MODEL, system, prompt, use_search=True)
    recs = result.get("recs", [])
    sectors = result.get("topSectors", [])
    log.info(f"  Analyst: {len(recs)} recs across sectors {sectors} — {result.get('market','')[:80]}")
    return result


# ── Agent 2: News Intelligence (Haiku) ────────────────────────────────────────

def run_news_agent(client: Anthropic, config: Config, portfolio: dict) -> dict:
    """
    Scans live financial news for market-moving events and sentiment signals.
    Covers both agent-managed and manual holdings.
    """
    log.info("▶ News Intelligence (Haiku) — scanning ASX news...")
    today   = datetime.now(AEST).strftime("%Y-%m-%d")
    tickers = ", ".join(
        f"{h['ticker']} [{'MANUAL' if h.get('source')=='manual' else 'AGENT'}]"
        for h in portfolio.get("holdings", [])
    ) or "ASX general"

    system = (
        "You are a financial news intelligence analyst for Australian markets. "
        "Search for real, current news from today. "
        "Respond ONLY with valid JSON — no markdown, no backticks, no preamble, no trailing text."
    )

    prompt = f"""Today is {today}. Search for the latest ASX and Australian financial news.

PORTFOLIO TICKERS TO MONITOR: {tickers}
Broadly scan ALL sectors — do NOT fixate on specific industries. Cover:
- Macro: RBA decisions, ASX200 index, AUD/USD, bond yields, employment data
- Global: US/European/Asian market overnight, trade policy, tariffs, geopolitical events
- Commodities: iron ore, gold, oil, lithium, copper, agricultural commodities — whatever is moving
- Sectors: scan ALL sectors for opportunities — tech, healthcare, financials, consumer,
  industrials, energy, materials, biotech, retail, REITs, defence, telecoms, etc.
- Insider activity: flag any notable insider selling (CEO/directors) in portfolio holdings or
  recommended stocks. Large insider sales are a significant warning signal.
- Identify which sectors are OUTPERFORMING and which are UNDERPERFORMING today.

Return ONLY this JSON with real news from your searches:
{{
  "date": "{today}",
  "sentiment": "BULLISH",
  "score": 65,
  "trend": "improving",
  "news": [
    {{
      "title": "Real headline from search",
      "summary": "2-sentence factual summary",
      "impact": "POSITIVE",
      "stocks": ["TICKER.AX"],
      "urgency": "HIGH",
      "sector": "Sector name"
    }}
  ],
  "alerts": [
    {{
      "ticker": "TICKER.AX",
      "alert": "Specific alert description",
      "rec": "SELL",
      "urgent": true
    }}
  ],
  "insiderSelling": [
    {{
      "ticker": "TICKER.AX",
      "who": "CEO/CFO/Director name",
      "sharesValue": "$X.XM worth of shares",
      "detail": "Brief description of the insider sale",
      "severity": "HIGH or MEDIUM or LOW"
    }}
  ],
  "topSectors": ["best performing sectors today"],
  "worstSectors": ["worst performing sectors today"],
  "macro": "2-sentence Australian macro summary with key data points",
  "globalFactors": "1-sentence on key global factor affecting ASX today"
}}"""

    result = _call_claude(client, config.NEWS_MODEL, system, prompt, use_search=True)
    log.info(
        f"  News: {result.get('sentiment')} ({result.get('score')}/100, trend: {result.get('trend')}) — "
        f"{len(result.get('news', []))} items, {len(result.get('alerts', []))} alerts"
    )
    return result


# ── Agent 3: Technical Analyst (local — Yahoo Finance) ───────────────────────

def run_technical_analyst(config: Config, portfolio: dict, analyst_data: dict) -> dict:
    """
    Fetches real price data via Yahoo Finance and computes technical indicators
    for all holdings and analyst recommendations. No Claude call needed.
    """
    log.info("▶ Technical Analyst — fetching Yahoo Finance data...")

    # Gather all tickers: holdings + analyst recs
    holding_tickers = [h["ticker"] for h in portfolio.get("holdings", [])]
    rec_tickers = [r["t"] for r in (analyst_data.get("recs") or []) if r.get("t")]
    all_tickers = list(set(holding_tickers + rec_tickers))

    technicals = {}
    for ticker in all_tickers:
        history = get_price_history(ticker, days=60)
        if history:
            tech = compute_technicals(history)
            tech["ticker"] = ticker
            technicals[ticker] = tech

    # Update holding prices with real Yahoo data
    yf_prices = update_all_holding_prices(portfolio)

    # Also update analyst rec prices if Yahoo has better data
    for rec in (analyst_data.get("recs") or []):
        ticker = rec.get("t")
        if ticker and ticker in technicals:
            real_price = technicals[ticker].get("currentPrice")
            if real_price:
                rec["price"] = real_price

    # Generate buy-low signals: stocks near support or oversold
    buy_low_signals = []
    sell_high_signals = []
    for ticker, tech in technicals.items():
        rsi = tech.get("rsi14")
        pct_from_high = tech.get("pctFrom52wHigh", 0)
        pct_from_low = tech.get("pctFrom52wLow", 0)

        if rsi and rsi < 30:
            buy_low_signals.append({
                "ticker": ticker,
                "signal": "RSI_OVERSOLD",
                "rsi": rsi,
                "detail": f"RSI {rsi:.0f} — oversold, potential bounce",
            })
        elif pct_from_high < -20:
            buy_low_signals.append({
                "ticker": ticker,
                "signal": "NEAR_52W_LOW",
                "pctFromHigh": pct_from_high,
                "detail": f"{pct_from_high:.1f}% from 52-week high — deep pullback",
            })

        if rsi and rsi > 70:
            sell_high_signals.append({
                "ticker": ticker,
                "signal": "RSI_OVERBOUGHT",
                "rsi": rsi,
                "detail": f"RSI {rsi:.0f} — overbought, consider taking profits",
            })
        elif pct_from_low > 50 and ticker in holding_tickers:
            sell_high_signals.append({
                "ticker": ticker,
                "signal": "EXTENDED_RUN",
                "pctFromLow": pct_from_low,
                "detail": f"+{pct_from_low:.1f}% from 52-week low — extended move",
            })

    result = {
        "technicals": technicals,
        "buyLowSignals": buy_low_signals,
        "sellHighSignals": sell_high_signals,
        "pricesUpdated": len(yf_prices),
        "tickersAnalysed": len(technicals),
    }

    log.info(
        f"  Tech Analyst: {len(technicals)} tickers analysed, "
        f"{len(buy_low_signals)} buy-low signals, {len(sell_high_signals)} sell-high signals"
    )
    return result


# ── Agent 4: Earnings Calendar (Haiku) ───────────────────────────────────────

def run_earnings_calendar(client: Anthropic, config: Config, portfolio: dict) -> dict:
    """
    Scans for upcoming earnings dates, ex-dividend dates, and AGMs
    for holdings and watchlist stocks.
    """
    log.info("▶ Earnings Calendar (Haiku) — scanning upcoming events...")
    today = datetime.now(AEST).strftime("%Y-%m-%d")
    tickers = ", ".join(h["ticker"] for h in portfolio.get("holdings", [])) or "none"
    watchlist = ", ".join(
        (portfolio.get("lastPMOutput") or {}).get("watchlist", [])
    ) or "none"

    system = (
        "You are a financial calendar analyst for Australian markets. "
        "Search for upcoming corporate events that could affect stock prices. "
        "Respond ONLY with valid JSON — no markdown, no backticks, no preamble, no trailing text."
    )

    prompt = f"""Today is {today}. Search for upcoming corporate events for ASX stocks.

PORTFOLIO HOLDINGS: {tickers}
WATCHLIST: {watchlist}

Search for events in the next 2 weeks:
1. Earnings report dates / results announcements
2. Ex-dividend dates
3. AGMs or EGMs
4. Capital raises, share buybacks, or corporate actions
5. Trading halts or resumptions

Return ONLY this JSON:
{{
  "date": "{today}",
  "events": [
    {{
      "ticker": "TICKER.AX",
      "name": "Company Name",
      "eventType": "EARNINGS|EX_DIVIDEND|AGM|CAPITAL_RAISE|TRADING_HALT",
      "eventDate": "YYYY-MM-DD",
      "detail": "Brief description",
      "impact": "POSITIVE|NEGATIVE|NEUTRAL|UNKNOWN",
      "actionAdvice": "HOLD_THROUGH|SELL_BEFORE|BUY_BEFORE|MONITOR"
    }}
  ],
  "warnings": [
    {{
      "ticker": "TICKER.AX",
      "warning": "Specific warning about upcoming event risk"
    }}
  ]
}}"""

    result = _call_claude(client, config.NEWS_MODEL, system, prompt, use_search=True)
    events = result.get("events", [])
    log.info(f"  Earnings Calendar: {len(events)} upcoming events found")
    return result


# ── Agent 5: Risk & Rebalancing (local) ──────────────────────────────────────

def run_risk_rebalancer(config: Config, portfolio: dict, tech_data: dict) -> dict:
    """
    Analyses portfolio concentration, checks trailing stops, take-profit levels,
    and generates rebalancing recommendations. No Claude call needed.
    """
    log.info("▶ Risk & Rebalancing — checking portfolio health...")

    holdings = portfolio.get("holdings", [])
    current_val = total_value(portfolio)
    cash_pct = (portfolio["cash"] / current_val * 100) if current_val > 0 else 100

    # ── Concentration analysis ───────────────────────────────────────────
    sector_exposure = {}
    position_sizes = []
    for h in holdings:
        val = h["shares"] * h.get("currentPrice", h["avgBuyPrice"])
        pct = (val / current_val * 100) if current_val > 0 else 0
        position_sizes.append({
            "ticker": h["ticker"],
            "name": h["name"],
            "value": round(val, 2),
            "pctOfPortfolio": round(pct, 2),
        })
        # Sector tracking (best-effort from last analysis)
        sector = h.get("sector", "Unknown")
        sector_exposure[sector] = sector_exposure.get(sector, 0) + pct

    concentration_warnings = []
    for pos in position_sizes:
        if pos["pctOfPortfolio"] > config.MAX_POSITION_PCT * 100:
            concentration_warnings.append({
                "ticker": pos["ticker"],
                "warning": f"Position is {pos['pctOfPortfolio']:.1f}% of portfolio "
                           f"(max {config.MAX_POSITION_PCT*100:.0f}%)",
                "action": "REDUCE",
            })

    for sector, pct in sector_exposure.items():
        if pct > config.MAX_SECTOR_PCT * 100:
            concentration_warnings.append({
                "sector": sector,
                "warning": f"Sector is {pct:.1f}% of portfolio "
                           f"(max {config.MAX_SECTOR_PCT*100:.0f}%)",
                "action": "DIVERSIFY",
            })

    # ── Trailing stop-loss checks ────────────────────────────────────────
    trailing_stop_alerts = []
    for h in holdings:
        cur = h.get("currentPrice", h["avgBuyPrice"])
        # Track the highest price seen
        peak = h.get("peakPrice", h["avgBuyPrice"])
        if cur > peak:
            h["peakPrice"] = cur
            peak = cur

        if peak > 0 and config.TRAILING_STOP_PCT > 0:
            drop_from_peak = (peak - cur) / peak
            trailing_stop_level = round(peak * (1 - config.TRAILING_STOP_PCT), 4)
            if drop_from_peak >= config.TRAILING_STOP_PCT:
                trailing_stop_alerts.append({
                    "ticker": h["ticker"],
                    "peakPrice": round(peak, 4),
                    "currentPrice": round(cur, 4),
                    "dropFromPeak": round(drop_from_peak * 100, 2),
                    "trailingStopLevel": trailing_stop_level,
                    "action": "SELL — trailing stop triggered",
                })

    # ── Take-profit checks ───────────────────────────────────────────────
    take_profit_alerts = []
    for h in holdings:
        cur = h.get("currentPrice", h["avgBuyPrice"])
        gain_pct = ((cur - h["avgBuyPrice"]) / h["avgBuyPrice"]) if h["avgBuyPrice"] > 0 else 0
        if gain_pct >= config.TAKE_PROFIT_PCT:
            take_profit_alerts.append({
                "ticker": h["ticker"],
                "avgBuyPrice": h["avgBuyPrice"],
                "currentPrice": round(cur, 4),
                "gainPct": round(gain_pct * 100, 2),
                "threshold": round(config.TAKE_PROFIT_PCT * 100, 0),
                "action": f"Consider taking profits — up {gain_pct*100:.1f}%",
            })

    # ── Pending trade expiry ─────────────────────────────────────────────
    expired_trades = []
    still_pending = []
    for trade in portfolio.get("pendingTrades", []):
        created = trade.get("createdAt")
        if created:
            age_hours = (datetime.now(AEST) - datetime.fromisoformat(created)).total_seconds() / 3600
            if age_hours > config.PENDING_TRADE_EXPIRY_HOURS:
                trade["expiredReason"] = f"Expired after {age_hours:.0f}h (max {config.PENDING_TRADE_EXPIRY_HOURS}h)"
                expired_trades.append(trade)
                continue
        still_pending.append(trade)

    portfolio["pendingTrades"] = still_pending

    result = {
        "portfolioValue": round(current_val, 2),
        "cashPct": round(cash_pct, 2),
        "positionSizes": position_sizes,
        "sectorExposure": sector_exposure,
        "concentrationWarnings": concentration_warnings,
        "trailingStopAlerts": trailing_stop_alerts,
        "takeProfitAlerts": take_profit_alerts,
        "expiredTrades": expired_trades,
        "healthScore": _calculate_health_score(
            cash_pct, concentration_warnings, trailing_stop_alerts, take_profit_alerts
        ),
    }

    log.info(
        f"  Risk: health={result['healthScore']}/100, "
        f"{len(concentration_warnings)} concentration warnings, "
        f"{len(trailing_stop_alerts)} trailing-stop alerts, "
        f"{len(take_profit_alerts)} take-profit alerts, "
        f"{len(expired_trades)} expired pending trades"
    )
    return result


def _calculate_health_score(cash_pct, conc_warnings, trailing_alerts, tp_alerts) -> int:
    """Simple portfolio health score 0-100."""
    score = 100
    # Penalise low cash
    if cash_pct < 5:
        score -= 20
    elif cash_pct < 10:
        score -= 10
    # Penalise concentration
    score -= len(conc_warnings) * 15
    # Penalise trailing stop breaches
    score -= len(trailing_alerts) * 20
    # Mild penalty for take-profit (it's a good problem to have)
    score -= len(tp_alerts) * 5
    return max(0, min(100, score))


# ── Position Sizing Helper ───────────────────────────────────────────────────

def calculate_position_size(
    cash_available: float,
    price: float,
    confidence: str,
    volatility: float | None,
    min_shares: int,
    max_position_value: float,
) -> int:
    """
    Calculate shares to buy based on confidence, volatility, and constraints.
    Higher confidence + lower volatility = larger position.
    """
    if price <= 0 or cash_available <= 0:
        return 0

    # Base allocation by confidence
    confidence_multiplier = {"HIGH": 1.0, "MEDIUM": 0.6, "LOW": 0.3}.get(confidence, 0.4)

    # Volatility adjustment: higher vol = smaller position
    vol_multiplier = 1.0
    if volatility and volatility > 0:
        if volatility > 5:
            vol_multiplier = 0.5
        elif volatility > 3:
            vol_multiplier = 0.7
        elif volatility > 1.5:
            vol_multiplier = 0.85

    # Target value for this position
    target_value = cash_available * 0.30 * confidence_multiplier * vol_multiplier
    target_value = min(target_value, max_position_value)

    shares = int(target_value / price)
    if shares < min_shares:
        # Check if we can even afford min_shares
        if min_shares * price <= cash_available:
            shares = min_shares
        else:
            return 0  # Can't afford minimum

    return shares


# ── Agent 6: Portfolio Manager (Sonnet) ──────────────────────────────────────

def run_portfolio_manager(
    client: Anthropic,
    config: Config,
    portfolio: dict,
    analyst_data: dict,
    news_data: dict,
    tech_data: dict | None = None,
    earnings_data: dict | None = None,
    risk_data: dict | None = None,
) -> dict:
    """
    Synthesises all agent data and generates specific trade recommendations.
    Has access to trade memory (past decisions), technicals, risk analysis,
    and sentiment trend. No web search — uses the rich context already gathered.
    """
    log.info("▶ Portfolio Manager (Sonnet) — generating trade decisions...")

    cash_buffer  = round(portfolio["cash"] * config.CASH_BUFFER_PCT, 2)
    current_val  = total_value(portfolio)
    pl_abs       = current_val - portfolio["startingCapital"]
    pl_pct       = (pl_abs / portfolio["startingCapital"]) * 100

    holdings_summary = []
    for h in portfolio.get("holdings", []):
        cur = h.get("currentPrice", h["avgBuyPrice"])
        drop_from_buy = ((cur - h["avgBuyPrice"]) / h["avgBuyPrice"]) * 100
        stop_loss_pct = config.STOP_LOSS_PCT * 100
        holdings_summary.append({
            "ticker": h["ticker"],
            "name": h["name"],
            "shares": h["shares"],
            "avgBuyPrice": h["avgBuyPrice"],
            "currentPrice": cur,
            "value": round(h["shares"] * cur, 2),
            "unrealisedPL_pct": round(drop_from_buy, 2),
            "source": h.get("source", "agent"),  # "manual" holdings flagged
            "volatility": h.get("volatility"),
            "stopLossAt": round(h["avgBuyPrice"] * (1 - config.STOP_LOSS_PCT), 2),
            "distanceToStopLoss_pct": round(drop_from_buy + stop_loss_pct, 2),
        })

    # Trade memory — last N decisions for context
    trade_memory = portfolio.get("tradeMemory", [])[-config.TRADE_MEMORY_SIZE:]

    # Sentiment trend from history
    sent_history = portfolio.get("sentimentHistory", [])
    if len(sent_history) >= 2:
        recent_avg = sum(s["score"] for s in sent_history[-3:]) / min(3, len(sent_history))
        older_avg  = sum(s["score"] for s in sent_history[:-3]) / max(1, len(sent_history) - 3)
        sent_trend = "improving" if recent_avg > older_avg + 5 else \
                     "declining" if recent_avg < older_avg - 5 else "stable"
    else:
        sent_trend = "insufficient data"

    analyst_summary = [
        {
            "t": r["t"], "n": r["n"], "action": r["action"],
            "price": r["price"], "conf": r["conf"],
            "alloc": r.get("alloc", 0), "thesis": r.get("thesis", ""),
            "catalyst": r.get("catalyst", ""),
        }
        for r in (analyst_data.get("recs") or [])[:6]
    ]

    system = (
        "You are a disciplined, risk-aware portfolio manager for an Australian retail investor. "
        "Make evidence-based decisions. Prioritise capital preservation. "
        "Learn from your trade history — don't repeat failed trades. "
        "CORE PRINCIPLE: Buy low, sell high. Favour stocks that have pulled back from highs "
        "but have strong fundamentals (value opportunities). Sell holdings that have run up "
        "significantly and may be overvalued. Do not chase stocks at their peaks. "
        "You are SECTOR-AGNOSTIC — follow the best opportunity regardless of sector. "
        "If insider selling is detected (CEO/directors selling large share parcels), treat "
        "this as a strong sell signal and recommend selling that position. "
        "Respond ONLY with valid JSON — no markdown, no backticks, no preamble, no trailing text."
    )

    prompt = f"""Manage this ASX paper trading portfolio. Today's full context:

PORTFOLIO STATE:
  Cash available   : ${portfolio['cash']:.2f} AUD (minimum buffer: ${cash_buffer:.2f})
  Total value      : ${current_val:.2f} AUD
  P&L              : {'+' if pl_abs >= 0 else ''}{pl_pct:.2f}% (${'+' if pl_abs >= 0 else ''}{pl_abs:.2f})
  Stop-loss level  : {config.STOP_LOSS_PCT*100:.0f}% drop from avg buy (auto-enforced by system)
  Holdings         : {json.dumps(holdings_summary, indent=2)}

NOTE: Holdings marked source="manual" were entered by the investor directly.
Monitor them the same as agent holdings. The stop-loss system auto-sells if they
drop {config.STOP_LOSS_PCT*100:.0f}% from avg buy price.

ANALYST FINDINGS (today):
  Top sectors: {analyst_data.get('topSectors', [])}
  Market: {analyst_data.get('market', '')}
  Recommendations: {json.dumps(analyst_summary, indent=2)}

NEWS INTELLIGENCE (today):
  Sentiment  : {news_data.get('sentiment','NEUTRAL')} ({news_data.get('score',50)}/100)
  Trend      : {news_data.get('trend', sent_trend)} (7-cycle trend: {sent_trend})
  Macro      : {news_data.get('macro','')}
  Global     : {news_data.get('globalFactors', '')}
  Alerts     : {json.dumps(news_data.get('alerts', []))}

TRADE MEMORY (your last {len(trade_memory)} decisions — learn from these):
{json.dumps(trade_memory, indent=2) if trade_memory else "No trade history yet — this is your first cycle."}

INSIDER SELLING ALERTS (from news agent):
  {json.dumps(news_data.get('insiderSelling', []))}

SECTOR PERFORMANCE:
  Top sectors today: {news_data.get('topSectors', [])}
  Worst sectors today: {news_data.get('worstSectors', [])}

TECHNICAL ANALYSIS (Yahoo Finance — real price data):
  Buy-low signals (oversold/pullback): {json.dumps((tech_data or {}).get('buyLowSignals', []))}
  Sell-high signals (overbought/extended): {json.dumps((tech_data or {}).get('sellHighSignals', []))}
  Key technicals for holdings/recs:
{json.dumps({t: {k: v for k, v in d.items() if k in ('rsi14','rsiSignal','sma20','aboveSMA20','pctFrom52wHigh','pctFrom52wLow','change5d','change20d','volatility')} for t, d in (tech_data or {}).get('technicals', {}).items()}, indent=2)}

EARNINGS CALENDAR (upcoming events):
  {json.dumps((earnings_data or {}).get('events', [])[:8])}
  Warnings: {json.dumps((earnings_data or {}).get('warnings', []))}

RISK & REBALANCING:
  Portfolio health score: {(risk_data or {}).get('healthScore', 'N/A')}/100
  Concentration warnings: {json.dumps((risk_data or {}).get('concentrationWarnings', []))}
  Trailing stop alerts: {json.dumps((risk_data or {}).get('trailingStopAlerts', []))}
  Take-profit alerts: {json.dumps((risk_data or {}).get('takeProfitAlerts', []))}
  Expired pending trades: {json.dumps((risk_data or {}).get('expiredTrades', []))}

TRADING RULES:
  - Keep ≥ ${cash_buffer:.2f} cash buffer at all times
  - Maximum 2–3 trades per cycle (don't over-trade)
  - MINIMUM TRADE SIZE: {config.MIN_TRADE_SHARES} shares per trade. Do NOT buy fewer than {config.MIN_TRADE_SHARES} shares
    of any stock. If you can't afford {config.MIN_TRADE_SHARES} shares, skip that stock.
  - Only BUY stocks with HIGH or MEDIUM analyst confidence
  - BUY LOW, SELL HIGH: Favour stocks trading below their recent highs with strong fundamentals.
    Avoid chasing stocks at peak prices. Sell holdings that have appreciated significantly.
  - INSIDER SELLING RULE: If CEO or senior executives are selling large portions of shares
    in a company we hold, SELL that position. Insiders know more than we do.
  - SELL if: analyst rates AVOID, news alert says SELL, insider selling detected,
    or you see a pattern of consistent losses
  - The system auto-enforces fixed stop-losses — you don't need to recommend those
  - TRAILING STOP: If a trailing stop alert is triggered (stock dropped {config.TRAILING_STOP_PCT*100:.0f}% from
    its peak), recommend SELL to lock in remaining gains
  - TAKE PROFIT: If a take-profit alert fires (stock up {config.TAKE_PROFIT_PCT*100:.0f}%+ from buy),
    consider selling at least half the position to lock in gains
  - TECHNICAL SIGNALS: Use RSI and price data — favour OVERSOLD stocks for buys,
    sell OVERBOUGHT stocks. Don't buy stocks near 52-week highs unless catalyst is exceptional
  - EARNINGS RISK: If earnings are within 3 days, be cautious — consider waiting or reducing position
  - Consider sentiment trend: declining trend = be more conservative
  - Be SECTOR-AGNOSTIC: invest in the best opportunities regardless of sector
  - CONCENTRATION: If any position exceeds {config.MAX_POSITION_PCT*100:.0f}% of portfolio, reduce it
  - Consider manual holdings: monitor and flag concerns, but let the investor decide
  - Calculate exact share counts (integer ≥ {config.MIN_TRADE_SHARES}) from available cash and analyst prices

Return ONLY this JSON:
{{
  "decision": "TRADE",
  "rationale": "One concise sentence explaining the overall decision",
  "trades": [
    {{
      "ticker": "TICKER.AX",
      "name": "Company Name",
      "action": "BUY",
      "shares": 5,
      "price": 100.00,
      "total": 500.00,
      "confidence": "HIGH",
      "reason": "Specific reason citing analyst and news context"
    }}
  ],
  "manualHoldingComments": [
    {{"ticker": "TICKER.AX", "comment": "Brief note on manual holding performance"}}
  ],
  "strategy": "One sentence on current portfolio strategy",
  "watchlist": ["Tickers or themes to monitor before next cycle"],
  "riskNote": "Any risk or concern worth flagging"
}}"""

    result = _call_claude(client, config.PM_MODEL, system, prompt, use_search=False)
    trades = result.get("trades", [])
    log.info(
        f"  PM: decision={result.get('decision')} — {len(trades)} trade(s) — "
        f"{result.get('rationale','')[:100]}"
    )
    return result


# ── Full Cycle Orchestrator ───────────────────────────────────────────────────

def run_cycle(config: Config) -> None:
    """
    Run a complete agent cycle:
      0. Fetch real prices via Yahoo Finance + check stop-losses
      1. Corporate Analyst  (Haiku + web search)
      2. News Intelligence  (Haiku + web search)
      3. Technical Analyst  (local — Yahoo Finance indicators)
      4. Earnings Calendar  (Haiku + web search)
      5. Risk & Rebalancing (local — concentration, trailing stops, take-profit)
      6. Portfolio Manager  (Sonnet, no search, full synthesis)
      7. Execute / queue trades
      8. Update trade memory + sentiment history
      9. Persist state + snapshot history
     10. Send email notification
    """
    now = datetime.now(AEST)
    log.info(f"{'═'*60}")
    log.info(f"  DAEDALUS CYCLE — {now.strftime('%A %Y-%m-%d %H:%M AEST')}")
    log.info(f"{'═'*60}")

    client    = Anthropic(api_key=config.ANTHROPIC_API_KEY)
    portfolio = load_portfolio(config)

    errors           = []
    analyst_data     = None
    news_data        = None
    tech_data        = None
    earnings_data    = None
    risk_data        = None
    pm_data          = None
    stop_loss_sells  = []
    trailing_sells   = []

    # Update cycle health counter
    portfolio.setdefault("cycleHealth", {
        "lastSuccess": None, "lastFailure": None,
        "successStreak": 0, "totalCycles": 0, "totalErrors": 0,
    })
    portfolio["cycleHealth"]["totalCycles"] = portfolio["cycleHealth"].get("totalCycles", 0) + 1

    # ── Step 0: Fetch real prices + stop-loss enforcement ────────────────
    if portfolio.get("holdings"):
        log.info("Fetching live prices from Yahoo Finance…")
        try:
            update_all_holding_prices(portfolio)
        except Exception as exc:
            log.warning(f"Yahoo Finance price update failed: {exc}")

    if config.STOP_LOSS_PCT > 0 and portfolio.get("holdings"):
        log.info(f"Checking stop-losses (threshold: {config.STOP_LOSS_PCT*100:.0f}%)…")
        stop_loss_sells = check_stop_losses(portfolio, config)
        if stop_loss_sells:
            log.warning(f"  Stop-loss triggered {len(stop_loss_sells)} sell(s)")
            for sl in stop_loss_sells:
                errors.append(f"STOP-LOSS: Sold {sl['ticker']}")

    # ── Step 1: Corporate Analyst ─────────────────────────────────────────
    try:
        analyst_data = run_corporate_analyst(client, config, portfolio)
        update_holding_prices(portfolio, analyst_data)
        portfolio["lastAnalysis"] = analyst_data
        portfolio.setdefault("logs", []).insert(0, {
            "ts": now.isoformat(),
            "agent": "Corporate Analyst",
            "type": "ANALYSIS",
            "title": f"Discovered {len(analyst_data.get('recs', []))} opportunities in {analyst_data.get('topSectors', [])}",
            "content": analyst_data.get("market", ""),
        })
    except Exception as exc:
        log.error(f"Corporate Analyst failed: {exc}", exc_info=True)
        errors.append(f"Analyst: {exc}")

    # ── Step 2: News Intelligence ─────────────────────────────────────────
    log.info("Pausing 65s between agents to respect rate limits…")
    time.sleep(65)

    try:
        news_data = run_news_agent(client, config, portfolio)
        portfolio["lastNews"] = news_data

        sent_entry = {
            "ts": now.isoformat(),
            "sentiment": news_data.get("sentiment", "NEUTRAL"),
            "score": news_data.get("score", 50),
            "trend": news_data.get("trend", "stable"),
        }
        hist = portfolio.setdefault("sentimentHistory", [])
        hist.append(sent_entry)
        portfolio["sentimentHistory"] = hist[-config.SENTIMENT_HISTORY_SIZE:]

        portfolio.setdefault("logs", []).insert(0, {
            "ts": now.isoformat(),
            "agent": "News Intelligence",
            "type": "NEWS",
            "title": f"Sentiment: {news_data.get('sentiment')} ({news_data.get('score')}/100) · Trend: {news_data.get('trend','')}",
            "content": news_data.get("macro", ""),
        })
    except Exception as exc:
        log.error(f"News Agent failed: {exc}", exc_info=True)
        errors.append(f"News: {exc}")

    # ── Step 3: Technical Analyst (local — no rate limit concern) ─────────
    try:
        tech_data = run_technical_analyst(config, portfolio, analyst_data or {})
        portfolio["lastTechnicals"] = tech_data
        portfolio.setdefault("logs", []).insert(0, {
            "ts": now.isoformat(),
            "agent": "Technical Analyst",
            "type": "TECHNICAL",
            "title": f"Analysed {tech_data.get('tickersAnalysed', 0)} tickers — "
                     f"{len(tech_data.get('buyLowSignals', []))} buy-low, "
                     f"{len(tech_data.get('sellHighSignals', []))} sell-high signals",
            "content": "",
        })
    except Exception as exc:
        log.warning(f"Technical Analyst failed (non-critical): {exc}")
        errors.append(f"Technical: {exc}")

    # ── Step 4: Earnings Calendar ─────────────────────────────────────────
    log.info("Pausing 65s between agents to respect rate limits…")
    time.sleep(65)

    try:
        earnings_data = run_earnings_calendar(client, config, portfolio)
        portfolio["lastEarnings"] = earnings_data
        events = earnings_data.get("events", [])
        if events:
            portfolio.setdefault("logs", []).insert(0, {
                "ts": now.isoformat(),
                "agent": "Earnings Calendar",
                "type": "CALENDAR",
                "title": f"{len(events)} upcoming corporate events",
                "content": ", ".join(f"{e['ticker']} {e['eventType']}" for e in events[:5]),
            })
    except Exception as exc:
        log.warning(f"Earnings Calendar failed (non-critical): {exc}")
        errors.append(f"Earnings: {exc}")

    # ── Step 5: Risk & Rebalancing (local) ────────────────────────────────
    try:
        risk_data = run_risk_rebalancer(config, portfolio, tech_data or {})
        portfolio["lastRiskReport"] = risk_data

        # Auto-execute trailing stop sells
        for alert in risk_data.get("trailingStopAlerts", []):
            ticker = alert["ticker"]
            holding = next((h for h in portfolio["holdings"] if h["ticker"] == ticker), None)
            if holding:
                trade = {
                    "ticker": ticker,
                    "name": holding["name"],
                    "action": "SELL",
                    "shares": holding["shares"],
                    "price": alert["currentPrice"],
                    "total": round(holding["shares"] * alert["currentPrice"], 2),
                    "confidence": "HIGH",
                    "reason": f"Trailing stop: dropped {alert['dropFromPeak']:.1f}% "
                              f"from peak ${alert['peakPrice']:.2f}",
                    "stopLoss": True,
                }
                result = execute_trade(portfolio, trade, source=holding.get("source", "agent"))
                if result["success"]:
                    trailing_sells.append(trade)
                    log.warning(f"  TRAILING-STOP: Sold {ticker} ({alert['dropFromPeak']:.1f}% from peak)")
                    errors.append(f"TRAILING-STOP: Sold {ticker}")

        portfolio.setdefault("logs", []).insert(0, {
            "ts": now.isoformat(),
            "agent": "Risk & Rebalancing",
            "type": "RISK",
            "title": f"Health: {risk_data.get('healthScore', 'N/A')}/100 — "
                     f"{len(risk_data.get('concentrationWarnings', []))} warnings",
            "content": "",
        })
    except Exception as exc:
        log.warning(f"Risk & Rebalancing failed (non-critical): {exc}")
        errors.append(f"Risk: {exc}")

    # ── Step 6: Portfolio Manager ─────────────────────────────────────────
    if analyst_data or news_data:
        try:
            pm_data = run_portfolio_manager(
                client, config, portfolio,
                analyst_data or {},
                news_data or {},
                tech_data=tech_data,
                earnings_data=earnings_data,
                risk_data=risk_data,
            )
            portfolio["lastPMOutput"] = pm_data
            portfolio.setdefault("logs", []).insert(0, {
                "ts": now.isoformat(),
                "agent": "Portfolio Manager",
                "type": "TRADE",
                "title": f"{len(pm_data.get('trades', []))} trade(s) recommended — {pm_data.get('decision','')}",
                "content": pm_data.get("rationale", ""),
            })
        except Exception as exc:
            log.error(f"Portfolio Manager failed: {exc}", exc_info=True)
            errors.append(f"PM: {exc}")

    # ── Step 7: Handle trades ─────────────────────────────────────────────
    trades          = (pm_data or {}).get("trades", [])
    executed_trades = []
    pending_trades  = []

    for trade in trades:
        confidence = trade.get("confidence", "")
        should_auto = (
            config.AUTO_APPROVE_TRADES
            and confidence == config.AUTO_APPROVE_MIN_CONFIDENCE
        )

        if should_auto:
            trade["autoApproved"] = True
            result = execute_trade(portfolio, trade, source="agent",
                                   min_shares=config.MIN_TRADE_SHARES)
            if result["success"]:
                executed_trades.append(trade)
                log.info(
                    f"  AUTO-EXECUTED: {trade['action']} {trade['shares']}× "
                    f"{trade['ticker']} @ ${trade['price']:.2f} "
                    f"(total: ${trade['total']:.2f})"
                )
            else:
                log.warning(f"  Trade rejected: {result['error']}")
                errors.append(f"Trade {trade['ticker']}: {result['error']}")
        else:
            trade["createdAt"] = now.isoformat()  # For pending trade expiry
            pending_trades.append(trade)
            log.info(
                f"  PENDING APPROVAL: {trade['action']} {trade['shares']}× "
                f"{trade['ticker']} @ ${trade['price']:.2f} "
                f"[confidence={confidence}]"
            )

    # Merge with existing unexpired pending trades
    existing_pending = portfolio.get("pendingTrades", [])
    portfolio["pendingTrades"] = existing_pending + pending_trades

    # ── Step 8: Update trade memory ───────────────────────────────────────
    if pm_data:
        memory_entry = {
            "ts": now.isoformat(),
            "decision": pm_data.get("decision"),
            "rationale": pm_data.get("rationale", ""),
            "trades": [
                {
                    "ticker": t["ticker"],
                    "action": t["action"],
                    "shares": t.get("shares"),
                    "price": t.get("price"),
                    "executed": t in executed_trades,
                }
                for t in trades
            ],
            "strategy": pm_data.get("strategy", ""),
            "sentiment": (news_data or {}).get("sentiment", ""),
            "sentimentScore": (news_data or {}).get("score", 50),
        }
        mem = portfolio.setdefault("tradeMemory", [])
        mem.append(memory_entry)
        portfolio["tradeMemory"] = mem[-config.TRADE_MEMORY_SIZE:]

    # ── Step 9: Cycle health + snapshot + persist ─────────────────────────
    cycle_errors = [e for e in errors if not e.startswith(("STOP-LOSS", "TRAILING-STOP"))]
    if cycle_errors:
        portfolio["cycleHealth"]["lastFailure"] = now.isoformat()
        portfolio["cycleHealth"]["successStreak"] = 0
        portfolio["cycleHealth"]["totalErrors"] = portfolio["cycleHealth"].get("totalErrors", 0) + len(cycle_errors)
    else:
        portfolio["cycleHealth"]["lastSuccess"] = now.isoformat()
        portfolio["cycleHealth"]["successStreak"] = portfolio["cycleHealth"].get("successStreak", 0) + 1

    snapshot_history(portfolio)
    save_portfolio(portfolio, config)

    val = total_value(portfolio)
    pl  = val - portfolio["startingCapital"]
    log.info(
        f"Cycle complete — Portfolio: ${val:.2f} AUD "
        f"({'+'if pl>=0 else ''}{(pl/portfolio['startingCapital'])*100:.2f}%) "
        f"| Streak: {portfolio['cycleHealth']['successStreak']} ✓"
    )
    if errors:
        log.warning(f"Cycle completed with {len(errors)} note(s): {'; '.join(errors)}")

    # ── Step 10: Email notification ───────────────────────────────────────
    if config.NOTIFY_EMAIL:
        try:
            all_executed = executed_trades + stop_loss_sells + trailing_sells
            send_cycle_summary(
                config, portfolio,
                analyst_data, news_data, pm_data,
                all_executed, pending_trades, errors,
            )
        except Exception as exc:
            log.error(f"Notification failed: {exc}", exc_info=True)
