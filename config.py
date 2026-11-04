"""
config.py
=========
Single source of truth for every configurable parameter in the bot.

Nothing in strategy.py / risk_manager.py / order_manager.py etc. should
ever hardcode a threshold - it must be read from here. This is what
lets you tune the bot from paper -> backtest -> live without touching
logic code.

Usage:
    from config import CONFIG
    if adx > CONFIG.strategy.adx_min: ...
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from datetime import time

from dotenv import load_dotenv

load_dotenv()


# ============================================================
# MODE
# ============================================================

class TradingMode(str, Enum):
    BACKTEST = "BACKTEST"
    PAPER = "PAPER"
    LIVE = "LIVE"


# ============================================================
# BROKER / API
# ============================================================

@dataclass(frozen=True)
class KiteConfig:
    api_key: str = os.getenv("KITE_API_KEY", os.getenv("ANGELONE_API_KEY", ""))
    api_secret: str = os.getenv("KITE_API_SECRET", os.getenv("ANGELONE_TOTP_SECRET", ""))
    access_token: str = os.getenv("KITE_ACCESS_TOKEN", "")
    request_token: str = os.getenv("KITE_REQUEST_TOKEN", "")
    user_id: str = os.getenv("KITE_USER_ID", os.getenv("ANGELONE_CLIENT_CODE", ""))
    password: str = os.getenv("KITE_PASSWORD", os.getenv("ANGELONE_PASSWORD", ""))
    tokens_path: Path = Path.home() / ".nifty_bot" / "kite_tokens.json"

    request_timeout_sec: float = 8.0
    max_retries: int = 4
    retry_backoff_base_sec: float = 1.5
    retry_backoff_max_sec: float = 20.0

    # WebSocket
    ws_reconnect_max_tries: int = 300
    ws_reconnect_backoff_sec: float = 3.0


@dataclass(frozen=True)
class AngelOneConfig:
    api_key: str = os.getenv("ANGELONE_API_KEY", os.getenv("KITE_API_KEY", ""))
    client_code: str = os.getenv("ANGELONE_CLIENT_CODE", os.getenv("KITE_USER_ID", ""))
    password: str = os.getenv("ANGELONE_PASSWORD", os.getenv("KITE_PASSWORD", ""))
    totp_secret: str = os.getenv("ANGELONE_TOTP_SECRET", os.getenv("KITE_API_SECRET", ""))
    access_token: str = os.getenv("ANGELONE_ACCESS_TOKEN", "")
    refresh_token: str = os.getenv("ANGELONE_REFRESH_TOKEN", "")
    feed_token: str = os.getenv("ANGELONE_FEED_TOKEN", "")
    tokens_path: Path = Path.home() / ".nifty_bot" / "angelone_tokens.json"
    scrip_master_url: str = "https://margincalculator.angelone.in/OpenAPI_File/files/OpenAPIScripMaster.json"
    variety_regular: str = "NORMAL"

    request_timeout_sec: float = 8.0
    max_retries: int = 4
    retry_backoff_base_sec: float = 1.5
    retry_backoff_max_sec: float = 20.0


# ============================================================
# CAPITAL / ACCOUNT
# ============================================================

@dataclass(frozen=True)
class CapitalConfig:
    configured_capital: float = 20000.0   # 20k base capital
    product_type: str = "MIS"               # intraday only
    max_exposure_pct_of_margin: float = 0.90  # never use more than 90% of
                                               # available margin on one trade
    min_margin_buffer_rupees: float = 2000.0  # always keep this much untouched
    margin_leverage_multiplier: float = 1.0   # Zerodha intraday leverage factor;


# ============================================================
# INSTRUMENT / UNIVERSE
# ============================================================

@dataclass(frozen=True)
class InstrumentConfig:
    underlying_symbol: str = "NIFTY 50"
    underlying_tradingsymbol_prefix: str = "NIFTY"
    exchange: str = "NSE"
    option_exchange: str = "NFO"
    strike_step: int = 50                   # NIFTY strikes are in steps of 50
    expiry_mode: str = "NEAREST_WEEKLY"      # always nearest weekly expiry
    option_type_ce: str = "CE"
    option_type_pe: str = "PE"


# ============================================================
# TIMEFRAME / TRADING WINDOW
# ============================================================

@dataclass(frozen=True)
class SessionConfig:
    market_open: time = time(9, 15)
    warm_up_until: time = time(9, 15)        # no warm-up delay
    setup_scan_start: time = time(9, 15)     # start scanning immediately
    last_entry_time: time = time(15, 15)     # allow entries until 15:15
    force_square_off_time: time = time(15, 15)
    market_close: time = time(15, 30)

    # No session gating: the bot may enter at any time inside the trading window.
    enable_time_based_entry_gate: bool = False

    primary_timeframe_min: int = 5
    higher_timeframe_min: int = 15

    # loop cadence
    scan_loop_interval_sec: float = 5.0
    position_monitor_interval_sec: float = 1.0


# ============================================================
# INDICATORS
# ============================================================

@dataclass(frozen=True)
class IndicatorConfig:
    ema_fast: int = 20
    ema_slow: int = 50
    htf_ema_fast: int = 20
    htf_ema_slow: int = 50
    adx_period: int = 14
    atr_period: int = 14
    vwap_reset_daily: bool = True
    volume_lookback: int = 1                # compare current vs previous candle


# ============================================================
# STRATEGY THRESHOLDS
# ============================================================

@dataclass(frozen=True)
class StrategyConfig:
    # ADX eligibility: every trade independently needs ADX inside this band.
    # There is no comparison against the previous trade, so ADX may fall
    # between trades and return to the same band. Example: trade 1 at 23,
    # ADX drops to 21 (blocked), returns to 23 -> trade 2 is eligible.
    adx_min: float = 23.0                    # trend must be at least this strong
    atr_min_points: float = 10.0             # minimum NIFTY-points ATR to trade;
                                                   # below this = too quiet, skip
    adx_max: float = 32.0                    # no entries once ADX is above this
    ema_flat_slope_lookback: int = 5        # bars used to judge "EMA is flat"
    ema_flat_slope_min_points: float = 1.5  # min EMA20 movement over lookback
                                                   # bars to NOT be considered flat

    pullback_max_distance_from_ema20_points: float = 35.0
    vwap_cross_lookback_bars: int = 10
    vwap_max_crosses_allowed: int = 10      # more than this in lookback = choppy, skip

    gap_max_pct: float = 3.0                # skip day if opening gap > this %

    # confirmation candle
    strong_candle_body_to_range_min_pct: float = 0.25  # body >=25% of candle range
                                                           # to count as "strong"

    # Only 2 trades per day allowed
    max_trades_per_day: int = 2

    manual_event_disable: bool = False      # flip True on RBI policy / budget days etc.


# ============================================================
# ENTRY / STOP / TARGET (in NIFTY INDEX POINTS - converted to
# option premium terms by option_selector / order_manager using
# the option's own ATR-scaled delta-equivalent move where needed)
# ============================================================

@dataclass(frozen=True)
class TradeManagementConfig:
    """
    All distances below are OPTION PREMIUM POINTS, not NIFTY index points.

    The bot buys NIFTY options, so every order it places is at an option
    premium. Example with a 100.00 premium long CE:
        entry            100.00
        stop-loss         93.00   (-7 premium points)
        trail triggers   107.00   (+7 premium points) -> SL moves to 105.00
        target           110.00   (+10 premium points)
    """
    target_index_points: float = 10.0
    stop_loss_index_points_cap: float = 3.0   # SL = min(swing_sl, this)

    # Single-step trailing: once profit reaches trail_trigger_premium_points,
    # the SL is moved to lock trail_lock_premium_points of profit.
    # Measured on the OPTION PREMIUM, since that is what we trade.
    enable_trailing_after_breakeven: bool = True
    trail_trigger_premium_points: float = 7.0
    trail_lock_premium_points: float = 5.0
    trail_step_index_points: float = 2.0       # legacy, unused

    sl_order_type: str = "SL-M"                # stop-market for guaranteed fill in live
    sl_limit_offset_points: float = 2.0         # limit price = trigger +/- this
    sl_escalation_watchdog_sec: float = 5.0     # if SL-limit unfilled in this
                                                   # window, fire market exit

    exit_limit_buffer_pct: float = 0.02         # 2% aggressive buffer for LIMIT exit orders

    profit_trail_pct: float = 0.7               # exit when profit reaches 70% of target distance

    trail_start_points: float = 2.5             # start breakeven SL-M at this profit
    trail_lock_points: float = 2.0              # lock 2.0 profit at this level
    trail_step_points: float = 2.0              # trail SL by 2 points per move (2-point gap)
    immediate_trail_lock_points: float = 0.0    # 0 = keep initial strategy SL until trailing engages (avoids premature exit before trailing starts)
    use_sl_m_after_trail: bool = True           # use SL-M instead of SL-Limit after trail starts
    sl_m_slippage_points: float = 0.5           # expected slippage on SL-M fills

    # Fixed SL/Target mode (overrides swing-based 1:2 RR)
    #
    # Trade path, in OPTION PREMIUM points from entry (long CE example):
    #   entry 100.00 -> SL sits at 93.00   (7 points of risk)
    #   +7     107.00 -> trailing engages, SL moves to 105.00 (locks 5)
    #   +10    110.00 -> target reached, position closed
    use_fixed_sl_target: bool = True
    fixed_sl_premium_points: float = 10.0        # SL = entry -/+ 10 premium points
    fixed_target_premium_points: float = 10.0   # target = entry +/+ 10 premium points


# ============================================================
# POSITION SIZING / MARGIN
# ============================================================

@dataclass(frozen=True)
class PositionSizingConfig:
    risk_per_trade_pct_of_equity: float = 5.0   # % of account equity risked per trade
    max_lots_per_trade: int = 27                 # hard ceiling regardless of margin (~2L at ~112 premium)
    round_lots_down: bool = True                  # never round up quantity


# ============================================================
# RISK MANAGEMENT (account level)
# ============================================================

@dataclass(frozen=True)
class RiskConfig:
    daily_loss_limit_pct_of_equity: float = 5.0  # Allow more room for ₹20k
    daily_profit_target_pct_of_equity: float = 10.0
    stop_after_daily_loss_limit: bool = False
    stop_after_daily_profit_target: bool = False


# ============================================================
# ORDER MANAGEMENT
# ============================================================

@dataclass(frozen=True)
class OrderConfig:
    entry_order_type: str = "MARKET"
    fill_confirmation_poll_interval_sec: float = 0.5
    fill_confirmation_timeout_sec: float = 10.0
    max_order_retries: int = 3
    reject_retry_backoff_sec: float = 2.0
    exit_limit_timeout_sec: float = 3.0
    entry_fill_timeout_sec: float = 300.0


# ============================================================
# LOGGING
# ============================================================

@dataclass(frozen=True)
class LoggingConfig:
    log_dir: Path = Path.home() / ".nifty_bot" / "logs"
    log_level: str = "INFO"
    log_to_console: bool = True
    log_to_file: bool = True
    log_rotate_max_bytes: int = 10_000_000
    log_rotate_backups: int = 10
    decision_log_filename: str = "decisions.jsonl"   # structured, one JSON per line


# ============================================================
# DASHBOARD
# ============================================================

@dataclass(frozen=True)
class DashboardConfig:
    host: str = "127.0.0.1"
    port: int = 8765
    refresh_interval_sec: float = 2.0


# ============================================================
# BACKTEST
# ============================================================

@dataclass(frozen=True)
class BacktestConfig:
    data_dir: Path = Path.home() / ".nifty_bot" / "backtest_data"
    results_dir: Path = Path.home() / ".nifty_bot" / "backtest_results"
    starting_equity: float = 20000.0  # Paper trading capital for ₹20,000
    commission_per_lot: float = 40.0        # round-trip brokerage + charges estimate
    slippage_points: float = 0.5            # applied to entries/exits in backtest

    # Options-specific backtest
    backtest_months: int = 8                # number of months to backtest
    atm_delta_approx: float = 0.5           # delta for translating index points to premium
    option_brokerage_per_order: float = 20.0  # Zerodha: ₹20 or 0.1% whichever is lower
    option_gst_pct: float = 0.18            # 18% GST on brokerage
    option_stamp_duty_per_lakh: float = 0.01  # approx stamp duty


# ============================================================
# MARGIN SAFETY
# ============================================================

@dataclass(frozen=True)
class MarginSafetyConfig:
    margin_check_interval_sec: float = 10.0
    min_available_margin: float = 0.0
    min_available_margin_pct: float = 0.0
    enable_market_exit_fallback: bool = True


# ============================================================
# ALERTING
# ============================================================

@dataclass(frozen=True)
class AlertingConfig:
    telegram_bot_token: str = os.getenv("TELEGRAM_BOT_TOKEN", "")
    telegram_chat_id: str = os.getenv("TELEGRAM_CHAT_ID", "")
    enabled: bool = False

    def __post_init__(self) -> None:
        if self.telegram_bot_token and self.telegram_chat_id:
            object.__setattr__(self, "enabled", True)


# ============================================================
# KILL-SWITCH / SESSION PERSISTENCE
# ============================================================

@dataclass(frozen=True)
class KillSwitchConfig:
    killswitch_path: Path = Path("/tmp/nifty_bot_killswitch")
    session_state_path: Path = Path.home() / ".nifty_bot" / "session_state.json"
    session_save_interval_sec: float = 30.0


# ============================================================
# WEBSOCKET GAP RECOVERY
# ============================================================

@dataclass(frozen=True)
class WSRecoveryConfig:
    gap_sync_interval_sec: float = 60.0
    gap_candle_missing_min: int = 5
    warm_up_history_timeout_sec: float = 30.0


# ============================================================
# ROOT CONFIG
# ============================================================

@dataclass(frozen=True)
class RootConfig:
    mode: TradingMode = TradingMode(os.getenv("NIFTY_BOT_MODE", "PAPER"))

    kite: KiteConfig = field(default_factory=KiteConfig)
    angelone: AngelOneConfig = field(default_factory=AngelOneConfig)
    capital: CapitalConfig = field(default_factory=CapitalConfig)
    instrument: InstrumentConfig = field(default_factory=InstrumentConfig)
    session: SessionConfig = field(default_factory=SessionConfig)
    indicators: IndicatorConfig = field(default_factory=IndicatorConfig)
    strategy: StrategyConfig = field(default_factory=StrategyConfig)
    trade_mgmt: TradeManagementConfig = field(default_factory=TradeManagementConfig)
    sizing: PositionSizingConfig = field(default_factory=PositionSizingConfig)
    risk: RiskConfig = field(default_factory=RiskConfig)
    order: OrderConfig = field(default_factory=OrderConfig)
    logging: LoggingConfig = field(default_factory=LoggingConfig)
    dashboard: DashboardConfig = field(default_factory=DashboardConfig)
    backtest: BacktestConfig = field(default_factory=BacktestConfig)
    alerting: AlertingConfig = field(default_factory=AlertingConfig)
    killswitch: KillSwitchConfig = field(default_factory=KillSwitchConfig)
    ws_recovery: WSRecoveryConfig = field(default_factory=WSRecoveryConfig)
    margin_safety: MarginSafetyConfig = field(default_factory=MarginSafetyConfig)

    def __post_init__(self) -> None:
        # ensure runtime dirs exist regardless of mode
        self.logging.log_dir.mkdir(parents=True, exist_ok=True)
        self.kite.tokens_path.parent.mkdir(parents=True, exist_ok=True)
        if self.mode == TradingMode.BACKTEST:
            self.backtest.data_dir.mkdir(parents=True, exist_ok=True)
            self.backtest.results_dir.mkdir(parents=True, exist_ok=True)

        # hard safety check: LIVE mode requires explicit env confirmation,
        # so a stray env var or default can never silently flip you live.
        if self.mode == TradingMode.LIVE and os.getenv("NIFTY_BOT_CONFIRM_LIVE") != "YES":
            raise RuntimeError(
                "Refusing to start in LIVE mode: set env var "
                "NIFTY_BOT_CONFIRM_LIVE=YES to confirm you intend to trade "
                "with real capital. (mode=PAPER or BACKTEST need no confirmation.)"
            )


CONFIG = RootConfig()


if __name__ == "__main__":
    # quick sanity dump when run directly
    import json
    from dataclasses import asdict

    def _default(o):
        if isinstance(o, Path):
            return str(o)
        if isinstance(o, time):
            return o.isoformat()
        return str(o)

    print(f"Mode: {CONFIG.mode.value}")
    print(json.dumps(asdict(CONFIG), indent=2, default=_default))