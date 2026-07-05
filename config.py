"""
Daedalus Configuration
All values are loaded from environment variables (see .env.example).
"""

import os
from dotenv import load_dotenv

load_dotenv()

# Confidence ranking used for auto-approve thresholds (LOW < MEDIUM < HIGH).
_CONF_RANK = {"LOW": 1, "MEDIUM": 2, "HIGH": 3}


def conf_rank(confidence: str) -> int:
    """Return the numeric rank of a confidence label (unknown → 0)."""
    return _CONF_RANK.get(str(confidence).upper(), 0)


class Config:
    # ── Anthropic ──────────────────────────────────────────────────────────
    ANTHROPIC_API_KEY: str = os.getenv("ANTHROPIC_API_KEY", "")

    ANALYST_MODEL: str = os.getenv("ANALYST_MODEL", "claude-haiku-4-5")
    NEWS_MODEL: str    = os.getenv("NEWS_MODEL",    "claude-haiku-4-5")
    # Portfolio Manager is the reasoning-critical agent → most capable model.
    PM_MODEL: str      = os.getenv("PM_MODEL",      "claude-opus-4-8")

    # ── Portfolio ──────────────────────────────────────────────────────────
    STARTING_CAPITAL: float = float(os.getenv("STARTING_CAPITAL", "1000"))
    PORTFOLIO_FILE: str     = os.getenv("PORTFOLIO_FILE", "portfolio.json")
    CASH_BUFFER_PCT: float  = float(os.getenv("CASH_BUFFER_PCT", "0.10"))

    # ── Risk Management ────────────────────────────────────────────────────
    # Auto-sell any position that drops this % from its average buy price.
    # Applies to both agent-managed and manually-entered positions.
    # Set to 0.0 to disable. Change via STOP_LOSS_PCT env var.
    STOP_LOSS_PCT: float = float(os.getenv("STOP_LOSS_PCT", "0.06"))  # 6%

    # Trailing stop-loss: sell if price drops this % from its PEAK (not buy price)
    TRAILING_STOP_PCT: float = float(os.getenv("TRAILING_STOP_PCT", "0.10"))  # 10%

    # Take-profit threshold: trim positions up this % from buy price
    TAKE_PROFIT_PCT: float = float(os.getenv("TAKE_PROFIT_PCT", "0.25"))  # 25%

    # When take-profit fires (and regime isn't strongly trending), sell this
    # fraction of the position to lock in gains and free cash to compound.
    TAKE_PROFIT_TRIM_PCT: float = float(os.getenv("TAKE_PROFIT_TRIM_PCT", "0.5"))  # 50%

    # Maximum single position as % of total portfolio value
    MAX_POSITION_PCT: float = float(os.getenv("MAX_POSITION_PCT", "0.25"))  # 25%

    # Maximum single sector as % of total portfolio value
    MAX_SECTOR_PCT: float = float(os.getenv("MAX_SECTOR_PCT", "0.40"))  # 40%

    # Minimum trade size — enforced as the GREATER of an absolute dollar floor
    # and a percentage of the whole portfolio, so the agent never buys in small
    # increments (no trivial nibbles) and the floor scales as the book grows.
    # Manual trades are exempt. Sizing itself is a volatility-scaled % of capital.
    MIN_TRADE_VALUE: float = float(os.getenv("MIN_TRADE_VALUE", "150"))
    MIN_TRADE_PCT: float   = float(os.getenv("MIN_TRADE_PCT", "0.08"))  # 8% of book

    # Pending trades expire after this many hours
    PENDING_TRADE_EXPIRY_HOURS: float = float(os.getenv("PENDING_TRADE_EXPIRY_HOURS", "8"))

    # Past PM decisions injected into PM context for trade memory
    TRADE_MEMORY_SIZE: int = int(os.getenv("TRADE_MEMORY_SIZE", "10"))

    # Past sentiment scores tracked for trend analysis
    SENTIMENT_HISTORY_SIZE: int = int(os.getenv("SENTIMENT_HISTORY_SIZE", "7"))

    # Closed trades / notifications retained for the dashboard
    CLOSED_TRADES_SIZE: int = int(os.getenv("CLOSED_TRADES_SIZE", "100"))
    NOTIFICATIONS_SIZE: int = int(os.getenv("NOTIFICATIONS_SIZE", "50"))

    # ── Market Regime ──────────────────────────────────────────────────────
    # Index used for the ASX market-regime read (Yahoo Finance symbol).
    ASX_INDEX_SYMBOL: str = os.getenv("ASX_INDEX_SYMBOL", "^AXJO")

    # ── Schedule ───────────────────────────────────────────────────────────
    CYCLE_HOURS: list[int] = [
        int(h.strip())
        for h in os.getenv("CYCLE_HOURS", "10,12,14").split(",")
    ]
    RUN_ON_STARTUP: bool = os.getenv("RUN_ON_STARTUP", "false").lower() == "true"

    # ── Trade Execution ────────────────────────────────────────────────────
    # Auto-execute trades at or above AUTO_APPROVE_MIN_CONFIDENCE. Default LOW =
    # execute EVERY trade the Portfolio Manager decides (no approvals) — this is
    # a paper portfolio and the goal is to let it run and grow. Raise to MEDIUM/
    # HIGH to route weaker trades to the dashboard's approve/reject panel instead.
    AUTO_APPROVE_TRADES: bool = (
        os.getenv("AUTO_APPROVE_TRADES", "true").lower() == "true"
    )
    AUTO_APPROVE_MIN_CONFIDENCE: str = os.getenv(
        "AUTO_APPROVE_MIN_CONFIDENCE", "LOW"
    ).upper()

    # ── API Security ───────────────────────────────────────────────────────
    API_KEY: str = os.getenv("DAEDALUS_API_KEY", "")

    def validate(self) -> "Config":
        if not self.ANTHROPIC_API_KEY:
            raise ValueError("ANTHROPIC_API_KEY is required.")
        if not self.CYCLE_HOURS:
            raise ValueError("CYCLE_HOURS must contain at least one hour.")
        if conf_rank(self.AUTO_APPROVE_MIN_CONFIDENCE) == 0:
            raise ValueError("AUTO_APPROVE_MIN_CONFIDENCE must be LOW, MEDIUM, or HIGH.")
        return self
