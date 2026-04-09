"""
Daedalus Agents
Three Claude-powered agents that work together each market-hours cycle.

  Corporate Analyst  → claude-haiku-4-5  (web search, fundamentals)
  News Intelligence  → claude-haiku-4-5  (web search, market news)
  Portfolio Manager  → claude-sonnet-4-6 (synthesis + trade decisions)
"""

import json
import logging
import time
from datetime import datetime

import pytz
from anthropic import Anthropic

from config import Config
from portfolio import load_portfolio, save_portfolio, execute_trade, snapshot_history, total_value
from notifier import send_cycle_summary

log = logging.getLogger("daedalus.agents")
AEST = pytz.timezone("Australia/Sydney")


# ── Shared Claude caller ──────────────────────────────────────────────────────

def _call_claude(client: Anthropic, model: str, system: str, prompt: str, use_search: bool = True) -> dict:
    """
    Call Claude, optionally with web_search, and parse the JSON response.
    Retries up to 3 times if the response has no text block (can happen when
    web search consumes the full context window) or hits a rate limit.
    """
    kwargs = {
        "model": model,
        "max_tokens": 1000,
        "system": system,
        "messages": [{"role": "user", "content": prompt}],
    }
    if use_search:
        kwargs["tools"] = [{"type": "web_search_20250305", "name": "web_search"}]

    for attempt in range(3):
        try:
            response = client.messages.create(**kwargs)

            # Collect all text blocks (search results are separate block types)
            raw_text = "".join(b.text for b in response.content if b.type == "text")

            if not raw_text.strip():
                # Model returned tool-use blocks only — no synthesised JSON yet.
                # Wait and retry so the rate-limit window resets.
                log.warning(f"Empty text response on attempt {attempt + 1}, retrying in 15s…")
                time.sleep(15)
                continue

            # Strip any accidental markdown fences
            clean = raw_text.replace("```json", "").replace("```", "").strip()
            return json.loads(clean)

        except Exception as e:
            if "rate_limit" in str(e).lower() or "429" in str(e):
                wait = 20 * (attempt + 1)
                log.warning(f"Rate limited on attempt {attempt + 1}, waiting {wait}s…")
                time.sleep(wait)
            elif attempt < 2:
                raise  # Non-rate-limit errors bubble up immediately

    raise RuntimeError("All retries exhausted — no valid JSON response from Claude")


# ── Agent 1: Corporate Analyst (Haiku) ────────────────────────────────────────

def run_corporate_analyst(client: Anthropic, config: Config, portfolio: dict) -> dict:
    """
    Searches the web for current ASX prices and company news,
    then produces structured investment recommendations.
    """
    log.info("▶ Corporate Analyst (Haiku) — searching ASX fundamentals...")
    today = datetime.now(AEST).strftime("%Y-%m-%d")
    held  = ", ".join(h["ticker"] for h in portfolio.get("holdings", [])) or "none"

    system = (
        "You are an expert ASX equity research analyst. "
        "Search for real, current data. "
        "Respond ONLY with valid JSON — no markdown, no backticks, no preamble."
    )

    prompt = f"""Today is {today}. Search for current ASX market data.
Current holdings: {held}. Available cash: ${portfolio['cash']:.2f} AUD.

Search current prices + recent news for: CBA, BHP, CSL, WES, ANZ, WBC, NAB, MQG, FMG, GMG (all on ASX).

Return ONLY this JSON (use real prices from your search):
{{
  "date": "{today}",
  "market": "2-sentence ASX overview",
  "recs": [
    {{
      "t": "CBA.AX", "n": "Commonwealth Bank", "action": "BUY",
      "price": 118.50, "alloc": 25,
      "thesis": "One-sentence investment rationale",
      "sector": "Financials", "conf": "HIGH",
      "risks": ["credit risk"], "pe": 18.2, "div": "4.1%"
    }},
    {{
      "t": "BHP.AX", "n": "BHP Group", "action": "BUY",
      "price": 42.10, "alloc": 20,
      "thesis": "Resources rationale",
      "sector": "Resources", "conf": "HIGH",
      "risks": ["commodity prices"], "pe": 10.1, "div": "5.8%"
    }},
    {{
      "t": "CSL.AX", "n": "CSL Limited", "action": "HOLD",
      "price": 285.00, "alloc": 15,
      "thesis": "Healthcare rationale",
      "sector": "Healthcare", "conf": "MEDIUM",
      "risks": ["FX exposure"], "pe": 28.5, "div": "1.1%"
    }}
  ],
  "notes": "Brief overall note"
}}"""

    result = _call_claude(client, config.ANALYST_MODEL, system, prompt, use_search=True)
    recs = result.get("recs", [])
    log.info(f"  Analyst: {len(recs)} recommendations — {result.get('market','')[:100]}")
    return result


# ── Agent 2: News Intelligence (Haiku) ────────────────────────────────────────

def run_news_agent(client: Anthropic, config: Config, portfolio: dict) -> dict:
    """
    Searches live financial news for market-moving events,
    corporate announcements, and sentiment signals.
    """
    log.info("▶ News Intelligence (Haiku) — scanning ASX news...")
    today   = datetime.now(AEST).strftime("%Y-%m-%d")
    tickers = ", ".join(h["ticker"] for h in portfolio.get("holdings", [])) or "ASX general"

    system = (
        "You are a financial news intelligence analyst for Australian markets. "
        "Search for real, current news. "
        "Respond ONLY with valid JSON — no markdown, no backticks, no preamble."
    )

    prompt = f"""Today is {today}. Search for the latest ASX and Australian financial news.
Monitor these portfolio tickers: {tickers}.
Also search for: RBA news, ASX200, Australian economy, iron ore price, banking sector.

Return ONLY this JSON:
{{
  "date": "{today}",
  "sentiment": "BULLISH",
  "score": 65,
  "news": [
    {{
      "title": "Real headline from search",
      "summary": "2-sentence summary",
      "impact": "POSITIVE",
      "stocks": ["CBA.AX"],
      "urgency": "MEDIUM"
    }},
    {{
      "title": "Second real headline",
      "summary": "2-sentence summary",
      "impact": "NEUTRAL",
      "stocks": [],
      "urgency": "LOW"
    }},
    {{
      "title": "Third real headline",
      "summary": "2-sentence summary",
      "impact": "POSITIVE",
      "stocks": ["BHP.AX"],
      "urgency": "MEDIUM"
    }}
  ],
  "alerts": [
    {{"ticker": "CBA.AX", "alert": "Alert description", "rec": "HOLD"}}
  ],
  "macro": "2-sentence Australian macro economic summary"
}}"""

    result = _call_claude(client, config.NEWS_MODEL, system, prompt, use_search=True)
    log.info(
        f"  News: {result.get('sentiment')} ({result.get('score')}/100) — "
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
    No web search — uses the rich context already gathered by the other two agents.
    """
    log.info("▶ Portfolio Manager (Sonnet) — generating trade decisions...")

    cash_buffer  = round(portfolio["cash"] * config.CASH_BUFFER_PCT, 2)
    current_val  = total_value(portfolio)
    pl_abs       = current_val - portfolio["startingCapital"]
    pl_pct       = (pl_abs / portfolio["startingCapital"]) * 100

    holdings_summary = [
        {
            "ticker": h["ticker"],
            "shares": h["shares"],
            "avgBuyPrice": h["avgBuyPrice"],
            "currentPrice": h.get("currentPrice", h["avgBuyPrice"]),
            "unrealisedPL": round(
                (h.get("currentPrice", h["avgBuyPrice"]) - h["avgBuyPrice"]) * h["shares"], 2
            ),
        }
        for h in portfolio.get("holdings", [])
    ]

    analyst_summary = [
        {"t": r["t"], "n": r["n"], "action": r["action"], "price": r["price"], "conf": r["conf"], "alloc": r.get("alloc", 0)}
        for r in (analyst_data.get("recs") or [])[:4]
    ]

    system = (
        "You are a disciplined, conservative portfolio manager for an Australian retail investor. "
        "Make evidence-based trade decisions. Prioritise capital preservation. "
        "Respond ONLY with valid JSON — no markdown, no backticks, no preamble."
    )

    prompt = f"""Manage this ASX paper trading portfolio. Today's context:

PORTFOLIO STATE:
  Cash available : ${portfolio['cash']:.2f} AUD (minimum buffer: ${cash_buffer:.2f})
  Total value    : ${current_val:.2f} AUD
  P&L            : {'+' if pl_abs >= 0 else ''}{pl_pct:.2f}% (${'+' if pl_abs >= 0 else ''}{pl_abs:.2f})
  Holdings       : {json.dumps(holdings_summary)}

ANALYST RECOMMENDATIONS:
{json.dumps(analyst_summary, indent=2)}

NEWS INTELLIGENCE:
  Sentiment : {news_data.get('sentiment','NEUTRAL')} ({news_data.get('score',50)}/100)
  Macro     : {news_data.get('macro','')}
  Alerts    : {json.dumps(news_data.get('alerts', []))}

TRADING RULES:
  - Keep ≥ ${cash_buffer:.2f} cash at all times
  - Maximum 2–3 trades per cycle
  - Only BUY stocks with HIGH or MEDIUM analyst confidence
  - SELL if: analyst rates AVOID, or news alert says SELL, or unrealised loss > 15%
  - Calculate exact share counts from available cash and real prices

Return ONLY this JSON:
{{
  "decision": "TRADE",
  "rationale": "One concise sentence explaining the overall decision",
  "trades": [
    {{
      "ticker": "CBA.AX",
      "name": "Commonwealth Bank",
      "action": "BUY",
      "shares": 3,
      "price": 118.50,
      "total": 355.50,
      "confidence": "HIGH",
      "reason": "Strong analyst conviction, positive news sentiment, prudent allocation"
    }}
  ],
  "strategy": "One sentence on current portfolio strategy",
  "watchlist": ["What to monitor before next cycle"]
}}"""

    result = _call_claude(client, config.PM_MODEL, system, prompt, use_search=False)
    trades = result.get("trades", [])
    log.info(f"  PM: decision={result.get('decision')} — {len(trades)} trade(s) — {result.get('rationale','')[:100]}")
    return result


# ── Full Cycle Orchestrator ───────────────────────────────────────────────────

def run_cycle(config: Config) -> None:
    """
    Run a complete agent cycle:
      1. Corporate Analyst  (Haiku + web search)
      2. News Intelligence  (Haiku + web search)
      3. Portfolio Manager  (Sonnet, no search)
      4. Execute or queue trades
      5. Persist state
      6. Send email notification
    """
    now = datetime.now(AEST)
    log.info(f"{'═'*60}")
    log.info(f"  DAEDALUS CYCLE — {now.strftime('%A %Y-%m-%d %H:%M AEST')}")
    log.info(f"{'═'*60}")

    client    = Anthropic(api_key=config.ANTHROPIC_API_KEY)
    portfolio = load_portfolio(config)

    errors        = []
    analyst_data  = None
    news_data     = None
    pm_data       = None

    # ── Step 1: Corporate Analyst ─────────────────────────────────────────
    try:
        analyst_data = run_corporate_analyst(client, config, portfolio)
        portfolio["lastAnalysis"] = analyst_data
        portfolio.setdefault("logs", []).insert(0, {
            "ts": now.isoformat(),
            "agent": "Corporate Analyst",
            "type": "ANALYSIS",
            "title": f"Analysed {len(analyst_data.get('recs', []))} ASX stocks",
            "content": analyst_data.get("market", ""),
        })
    except Exception as exc:
        log.error(f"Corporate Analyst failed: {exc}", exc_info=True)
        errors.append(f"Analyst: {exc}")

    # ── Step 2: News Intelligence ─────────────────────────────────────────
    # Brief pause to avoid hitting the 50k tokens/min rate limit on Haiku
    # since the Analyst's web search results are token-heavy.
    log.info("Pausing 20s between agents to respect rate limits…")
    time.sleep(20)

    try:
        news_data = run_news_agent(client, config, portfolio)
        portfolio["lastNews"] = news_data
        portfolio.setdefault("logs", []).insert(0, {
            "ts": now.isoformat(),
            "agent": "News Intelligence",
            "type": "NEWS",
            "title": f"Sentiment: {news_data.get('sentiment')} ({news_data.get('score')}/100)",
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
                "title": f"{len(pm_data.get('trades', []))} trade(s) recommended",
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
            result = execute_trade(portfolio, trade)
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

    # Add pending trades to portfolio state for dashboard to display
    portfolio["pendingTrades"] = pending_trades

    # ── Step 5: Snapshot history + persist ────────────────────────────────
    snapshot_history(portfolio)
    save_portfolio(portfolio, config)

    val = total_value(portfolio)
    pl  = val - portfolio["startingCapital"]
    log.info(
        f"Cycle complete — Portfolio: ${val:.2f} AUD "
        f"({'+'if pl>=0 else ''}{(pl/portfolio['startingCapital'])*100:.2f}%)"
    )
    if errors:
        log.warning(f"Cycle completed with {len(errors)} error(s): {'; '.join(errors)}")

    # ── Step 6: Email notification ────────────────────────────────────────
    if config.NOTIFY_EMAIL:
        try:
            send_cycle_summary(
                config, portfolio,
                analyst_data, news_data, pm_data,
                executed_trades, pending_trades, errors,
            )
        except Exception as exc:
            log.error(f"Notification failed: {exc}", exc_info=True)
