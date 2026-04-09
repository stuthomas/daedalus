"""
Daedalus Configuration
All values are loaded from environment variables (see .env.example).
"""

import os
from dotenv import load_dotenv

load_dotenv()


class Config:
    # ── Anthropic ──────────────────────────────────────────────────────────
    ANTHROPIC_API_KEY: str = os.getenv("ANTHROPIC_API_KEY", "")

    ANALYST_MODEL: str = os.getenv("ANALYST_MODEL", "claude-haiku-4-5-20251001")
    NEWS_MODEL: str    = os.getenv("NEWS_MODEL",    "claude-haiku-4-5-20251001")
    PM_MODEL: str      = os.getenv("PM_MODEL",      "claude-sonnet-4-6")

    # ── Portfolio ──────────────────────────────────────────────────────────
    STARTING_CAPITAL: float = float(os.getenv("STARTING_CAPITAL", "1000"))
    PORTFOLIO_FILE: str     = os.getenv("PORTFOLIO_FILE", "portfolio.json")
    CASH_BUFFER_PCT: float  = float(os.getenv("CASH_BUFFER_PCT", "0.10"))

    # ── Risk Management ────────────────────────────────────────────────────
    # Auto-sell any position that drops this % from its average buy price.
    # Applies to both agent-managed and manually-entered positions.
    # Set to 0.0 to disable. Change via STOP_LOSS_PCT env var.
    STOP_LOSS_PCT: float = float(os.getenv("STOP_LOSS_PCT", "0.06"))  # 6%

    # Past PM decisions injected into PM context for trade memory
    TRADE_MEMORY_SIZE: int = int(os.getenv("TRADE_MEMORY_SIZE", "10"))

    # Past sentiment scores tracked for trend analysis
    SENTIMENT_HISTORY_SIZE: int = int(os.getenv("SENTIMENT_HISTORY_SIZE", "7"))

    # ── Schedule ───────────────────────────────────────────────────────────
    CYCLE_HOURS: list[int] = [
        int(h.strip())
        for h in os.getenv("CYCLE_HOURS", "10,12,14").split(",")
    ]
    RUN_ON_STARTUP: bool = os.getenv("RUN_ON_STARTUP", "false").lower() == "true"

    # ── Trade Execution ────────────────────────────────────────────────────
    AUTO_APPROVE_TRADES: bool = (
        os.getenv("AUTO_APPROVE_TRADES", "false").lower() == "true"
    )
    AUTO_APPROVE_MIN_CONFIDENCE: str = os.getenv(
        "AUTO_APPROVE_MIN_CONFIDENCE", "HIGH"
    )

    # ── API Security ───────────────────────────────────────────────────────
    API_KEY: str = os.getenv("DAEDALUS_API_KEY", "")

    # ── Email Notifications ────────────────────────────────────────────────
    NOTIFY_EMAIL: str = os.getenv("NOTIFY_EMAIL", "")
    SMTP_USER: str    = os.getenv("SMTP_USER", "")
    SMTP_PASS: str    = os.getenv("SMTP_PASS", "")

    def validate(self) -> "Config":
        if not self.ANTHROPIC_API_KEY:
            raise ValueError("ANTHROPIC_API_KEY is required.")
        if not self.CYCLE_HOURS:
            raise ValueError("CYCLE_HOURS must contain at least one hour.")
        return self
