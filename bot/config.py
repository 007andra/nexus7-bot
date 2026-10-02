import os


def _pct(value: str, default: float, name: str = "") -> float:
    """Normalize percentage-like values used by bounded percentage controls."""
    try:
        v = float(value)
    except (TypeError, ValueError):
        return default
    if v <= 0:
        return default
    if v > 1.0:
        v = v / 100.0
    if v > 1.0:
        v = 1.0
    return v


class Config:
    # API / authentication
    API_KEY:        str = os.environ.get("KUCOIN_API_KEY",        "")
    API_SECRET:     str = os.environ.get("KUCOIN_API_SECRET",     "")
    API_PASSPHRASE: str = os.environ.get("KUCOIN_API_PASSPHRASE", "")
    BOT_API_SECRET: str = os.environ.get("BOT_API_SECRET", "")

    # KuCoin Futures symbols
    SYMBOLS: list = [
        "BTCUSDT", "ETHUSDT", "SOLUSDT", "XRPUSDT",
        "ADAUSDT", "DOGEUSDT", "LINKUSDT", "AVAXUSDT",
        "DOTUSDT", "LTCUSDT", "NEARUSDT", "ATOMUSDT",
    ]

    # PRE-LIVE hardened risk defaults.
    # These defaults are intentionally conservative compared with the former
    # validation profile (50x / near-full-margin). Railway may override them,
    # but the safe fallback no longer exposes almost the entire account.
    LEVERAGE:       int   = int(os.environ.get("LEVERAGE", "10"))
    MAX_RISK_PCT:   float = float(os.environ.get("MAX_RISK_PCT", "0.01"))
    MAX_MARGIN_PCT: float = float(os.environ.get("MAX_MARGIN_PCT", "0.10"))
    # F-003: MAX_RISK_PCT is the real per-trade loss budget at the stop
    # (equity x pct, modeled costs included). MAX_OPEN_RISK_PCT caps the sum
    # of risk reserved by open BGX positions plus the new entry. Values above
    # bot.risk_budget.RISK_PCT_HARD_CEILING are refused at entry time.
    MAX_OPEN_RISK_PCT: float = float(os.environ.get("MAX_OPEN_RISK_PCT", "0.02"))
    MAX_DRAWDOWN:   float = _pct(os.environ.get("MAX_DRAWDOWN", "0.10"), 0.10)
    DRAWDOWN_MODE:  str   = os.environ.get("DRAWDOWN_MODE", "ADVISORY").strip().upper()
    if DRAWDOWN_MODE not in {"ADVISORY", "HARD_GATE"}:
        raise ValueError("DRAWDOWN_MODE must be ADVISORY or HARD_GATE")
    INITIAL_CAP:    float = float(os.environ.get("INITIAL_CAP", "0"))
    MAX_POSITIONS:  int   = int(os.environ.get("MAX_POSITIONS", "2"))
    MIN_CONFIDENCE: float = float(os.environ.get("MIN_CONFIDENCE", "0.75"))
    MIN_RR_RATIO:   float = float(os.environ.get("MIN_RR_RATIO", "2.0"))

    # Trailing Stop
    TRAILING_TRIGGER:     float = float(os.environ.get("TRAILING_TRIGGER", "0.50"))
    TRAILING_LOCK:        float = float(os.environ.get("TRAILING_LOCK", "0.25"))
    TRAILING_LOCK_R_MULT: float = float(os.environ.get("TRAILING_LOCK_R_MULT", "1.0"))

    # Correlation
    MAX_CORRELATION: float = float(os.environ.get("MAX_CORRELATION", "0.70"))

    # Entry quality. 60 is the canonical fallback shared with the NEXUS gate.
    # Production may override it, but config drift must never be created merely
    # because an environment variable is absent in a new environment.
    MIN_ENTRY_SCORE: int   = int(os.environ.get("MIN_ENTRY_SCORE", "60"))
    POST_TARGET_SCORE: int = int(os.environ.get("POST_TARGET_SCORE", "72"))
    POST_TARGET_RISK: float = float(os.environ.get("POST_TARGET_RISK", "0.005"))
    MIN_VOLUME_MULT: float = float(os.environ.get("MIN_VOLUME_MULT", "0.5"))
    FEE_MULTIPLIER:  float = float(os.environ.get("FEE_MULTIPLIER", "2.0"))

    # Daily controls. A 3% daily stop is an independent circuit breaker;
    # absolute overrides remain disabled by default so percentages govern.
    DAILY_TARGET_PCT:    float = _pct(os.environ.get("DAILY_TARGET_PCT", "0.05"), 0.05)
    DAILY_STOP_LOSS_PCT: float = _pct(os.environ.get("DAILY_STOP_LOSS_PCT", "0.03"), 0.03)
    DAILY_TARGET:        float = float(os.environ.get("DAILY_TARGET", "0"))
    DAILY_STOP_LOSS:     float = float(os.environ.get("DAILY_STOP_LOSS", "0"))

    REPORT_INTERVAL_H: int = int(os.environ.get("REPORT_INTERVAL_H", "24"))

    # Timeframes
    TF_TREND: str = "240"
    TF_CONF:  str = "60"
    TF_ENTRY: str = "15"

    # SL / TP
    SL_ATR_MULT: float = float(os.environ.get("SL_ATR_MULT", "1.5"))
    TP_ATR_MULT: float = float(os.environ.get("TP_ATR_MULT", "3.0"))

    COOLDOWN_SECONDS: int = int(os.environ.get("COOLDOWN_SECONDS", "900"))

    LOG_LEVEL: str = os.environ.get("LOG_LEVEL", "INFO")
    PORT:      int = int(os.environ.get("PORT", "8000"))

    TELEGRAM_TOKEN: str = os.environ.get("TELEGRAM_TOKEN", "")
    TELEGRAM_CHAT:  str = os.environ.get("TELEGRAM_CHAT", "")

    ALLOWED_ORIGINS: list = [
        o.strip()
        for o in os.environ.get("ALLOWED_ORIGINS", "").split(",")
        if o.strip()
    ] or ["http://localhost:3000", "http://localhost:8000"]


cfg = Config()
