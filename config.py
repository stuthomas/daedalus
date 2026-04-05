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

    # Haiku 4.5 for the two search-heavy agents (cheaper, fast)
    ANALYST_MODEL: str = os.getenv("ANALYST_MODEL", "claude-haiku-4-5-20251001")
    NEWS_MODEL: str    = os.getenv("NEWS_MODEL",    "claude-haiku-4-5-20251001")

    # Sonnet 4.6 for the Portfolio Manager (needs better reasoning)
    PM_MODEL: str = os.getenv("PM_MODEL", "claude-sonnet-4-6")

    # ── Portfolio ──────────────────────────────────────────────────────────
    STARTING_CAPITAL: float = float(os.getenv("STARTING_CAPITAL", "1000"))
    PORTFOLIO_FILE: str     = os.getenv("PORTFOLIO_FILE", "portfolio.json")
    CASH_BUFFER_PCT: float  = float(os.getenv("CASH_BUFFER_PCT", "0.10"))

    # ── Schedule ───────────────────────────────────────────────────────────
    # Hours in AEST (24h), comma-separated.  Default: 10am, 12pm, 2pm.
    CYCLE_HOURS: list[int] = [
        int(h.strip())
        for h in os.getenv("CYCLE_HOURS", "10,12,14").split(",")
    ]
    # Run a cycle immediately on startup if the market is open
    RUN_ON_STARTUP: bool = os.getenv("RUN_ON_STARTUP", "false").lower() == "true"

    # ── Trade Execution ────────────────────────────────────────────────────
    # false (default) = email recommendations, never touch the portfolio
    # true            = auto-execute trades where confidence >= AUTO_APPROVE_MIN_CONFIDENCE
    AUTO_APPROVE_TRADES: bool = (
        os.getenv("AUTO_APPROVE_TRADES", "false").lower() == "true"
    )
    AUTO_APPROVE_MIN_CONFIDENCE: str = os.getenv(
        "AUTO_APPROVE_MIN_CONFIDENCE", "HIGH"
    )

    # ── API Security ───────────────────────────────────────────────────────
    # Set this to protect the /api/trigger endpoint.
    # Leave blank to allow unauthenticated triggers (not recommended in prod).
    API_KEY: str = os.getenv("DAEDALUS_API_KEY", "")

    # ── Email Notifications ────────────────────────────────────────────────
    # Uses Gmail SMTP with an App Password (not your Gmail password).
    # Instructions: https://support.google.com/accounts/answer/185833
    NOTIFY_EMAIL: str = os.getenv("NOTIFY_EMAIL", "")   # recipient
    SMTP_USER: str    = os.getenv("SMTP_USER", "")       # Gmail address
    SMTP_PASS: str    = os.getenv("SMTP_PASS", "")       # Gmail App Password

    def validate(self) -> "Config":
        if not self.ANTHROPIC_API_KEY:
            raise ValueError(
                "ANTHROPIC_API_KEY is required. "
                "Set it in your .env file or as an environment variable."
            )
        if not self.CYCLE_HOURS:
            raise ValueError("CYCLE_HOURS must contain at least one hour.")
        return self
