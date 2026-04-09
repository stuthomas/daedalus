"""
Daedalus Agents
Three Claude-powered agents that work together each market-hours cycle.

  Corporate Analyst  → claude-haiku-4-5  (web search, free-range market discovery)
  News Intelligence  → claude-haiku-4-5  (web search, market news + sentiment)
  Portfolio Manager  → claude-sonnet-4-6 (synthesis + trade decisions, with memory)
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
        "Respond ONLY with valid JSON — no markdown, no backticks, no preamble, no trailing text."
    )

    prompt = f"""Today is {today}. You are analysing the ASX for investment opportunities.

CURRENT PORTFOLIO:
Holdings: {held}
Available cash: ${cash:.2f} AUD

YOUR TASK:
1. Search for what is genuinely moving on the ASX today — which sectors are outperforming?
2. Think about current macro themes (e.g. AI/data centres, defence/weapons contractors, 
   commodity cycles, energy transition, RBA rate decisions, China demand, US tariffs impact on ASX).
3. Identify 3–6 specific ASX-listed stocks (use .AX suffix) that represent real opportunities 
   RIGHT NOW based on current news, price movements, and fundamentals.
4. Also search current prices for any existing holdings listed above.
5. Include a MIX of sectors — don't cluster all recs in one sector.

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
      "catalyst": "Specific near-term catalyst"
    }}
  ],
  "holdingUpdates": {{
    "TICKER.AX": {{"price": 0.00, "view": "HOLD or SELL or ADD", "note": "brief"}}
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
Also broadly scan: RBA interest rate decisions, ASX200 index, AUD/USD, iron ore prices,
oil prices, tech sector (AI/data centres), defence contractors, banking sector,
Chinese economic data affecting ASX, US market overnight performance.

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
  "macro": "2-sentence Australian macro summary with key data points",
  "globalFactors": "1-sentence on key global factor affecting ASX today"
}}"""

    result = _call_claude(client, config.NEWS_MODEL, system, prompt, use_search=True)
    log.info(
        f"  News: {result.get('sentiment')} ({result.get('score')}/100, trend: {result.get('trend')}) — "
        f"{len(result.get('news', []))} items, {len(result.get('alerts', []))} alerts"
    )
    return result


# ── Agent 3: Portfolio Manager (Sonnet) ───────────────────────────────────────

def run_portfolio_manager(
    client: Anthropic,
    config: Config,
    portfolio: dict,
    analyst_data: dict,
    news_data: dict,
) -> dict:
    """
    Synthesises analyst + news data and generates specific trade recommendations.
    Has access to trade memory (past decisions) and sentiment trend.
    No web search — uses the rich context already gathered.
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

TRADING RULES:
  - Keep ≥ ${cash_buffer:.2f} cash buffer at all times
  - Maximum 2–3 trades per cycle (don't over-trade)
  - Only BUY stocks with HIGH or MEDIUM analyst confidence
  - SELL if: analyst rates AVOID, news alert says SELL, or you see a pattern of consistent losses
  - The system auto-enforces stop-losses — you don't need to recommend stop-loss SELLs
  - Consider sentiment trend: declining trend = be more conservative
  - Consider manual holdings: monitor and flag concerns, but let the investor decide
  - Calculate exact share counts (integer) from available cash and analyst prices

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
      0. Check stop-losses (auto-execute before agents run)
      1. Corporate Analyst  (Haiku + web search)
      2. News Intelligence  (Haiku + web search)
      3. Portfolio Manager  (Sonnet, no search, with trade memory)
      4. Execute / queue trades
      5. Update trade memory + sentiment history
      6. Persist state + snapshot history
      7. Send email notification
    """
    now = datetime.now(AEST)
    log.info(f"{'═'*60}")
    log.info(f"  DAEDALUS CYCLE — {now.strftime('%A %Y-%m-%d %H:%M AEST')}")
    log.info(f"{'═'*60}")

    client    = Anthropic(api_key=config.ANTHROPIC_API_KEY)
    portfolio = load_portfolio(config)

    errors         = []
    analyst_data   = None
    news_data      = None
    pm_data        = None
    stop_loss_sells = []

    # Update cycle health counter
    portfolio.setdefault("cycleHealth", {
        "lastSuccess": None, "lastFailure": None,
        "successStreak": 0, "totalCycles": 0, "totalErrors": 0,
    })
    portfolio["cycleHealth"]["totalCycles"] = portfolio["cycleHealth"].get("totalCycles", 0) + 1

    # ── Step 0: Stop-loss enforcement ─────────────────────────────────────
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
        # Update holding prices from analyst data
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
    # Wait 65s for the rate-limit window to fully reset between Haiku agents
    log.info("Pausing 65s between agents to respect rate limits…")
    time.sleep(65)

    try:
        news_data = run_news_agent(client, config, portfolio)
        portfolio["lastNews"] = news_data

        # Append to sentiment history
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

    # ── Step 3: Portfolio Manager ─────────────────────────────────────────
    if analyst_data or news_data:
        try:
            pm_data = run_portfolio_manager(
                client, config, portfolio,
                analyst_data or {},
                news_data or {},
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

    # ── Step 4: Handle trades ─────────────────────────────────────────────
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
            result = execute_trade(portfolio, trade, source="agent")
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
            pending_trades.append(trade)
            log.info(
                f"  PENDING APPROVAL: {trade['action']} {trade['shares']}× "
                f"{trade['ticker']} @ ${trade['price']:.2f} "
                f"[confidence={confidence}]"
            )

    portfolio["pendingTrades"] = pending_trades

    # ── Step 5: Update trade memory ───────────────────────────────────────
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

    # ── Step 6: Cycle health + snapshot + persist ─────────────────────────
    cycle_errors = [e for e in errors if not e.startswith("STOP-LOSS")]
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

    # ── Step 7: Email notification ────────────────────────────────────────
    if config.NOTIFY_EMAIL:
        try:
            all_executed = executed_trades + stop_loss_sells
            send_cycle_summary(
                config, portfolio,
                analyst_data, news_data, pm_data,
                all_executed, pending_trades, errors,
            )
        except Exception as exc:
            log.error(f"Notification failed: {exc}", exc_info=True)
