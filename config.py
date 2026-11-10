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
    redirect_url: str = os.getenv("KITE_REDIRECT_URL", "")
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
    configured_capital: float = 30000.0   # 30k base capital
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
    warm_up_until: time = time(9, 15)
    # First actionable signal is the close of the 09:15-09:20 bar; earliest fill is 09:20 bar open.
    setup_scan_start: time = time(9, 20)
    # No new entries at or after 15:15 (keeps time for exit before 15:30 sq-off).
    entry_cutoff_time: time = time(15, 14)
    last_entry_time: time = time(15, 14)
    force_square_off_time: time = time(15, 30)
    market_close: time = time(15, 30)

    enable_time_based_entry_gate: bool = True

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
    adx_kite_period: int = 10   # Kite chart-style ADX period (for display/comparison only)
    atr_period: int = 14
    vwap_reset_daily: bool = True
    volume_lookback: int = 1                # compare current vs previous candle


# ============================================================
# STRATEGY THRESHOLDS
# ============================================================

@dataclass(frozen=True)
class StrategyConfig:
    # V1 band: eligible when ADX >= 23.0 and <= 32.0
    adx_min: float = 23.0
    adx_max: float = 32.0
    atr_min_points: float = 10.0
    ema_flat_slope_lookback: int = 5
    ema_flat_slope_min_points: float = 1.5

    pullback_max_distance_from_ema20_points: float = 35.0
    vwap_cross_lookback_bars: int = 10
    vwap_max_crosses_allowed: int = 10

    gap_max_pct: float = 3.0

    # confirmation candle
    strong_candle_body_to_range_min_pct: float = 0.25

    # Max trades per day
    max_trades_per_day: int = 10

    manual_event_disable: bool = False


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

    # Continuous trailing (premium points): after trail_start profit, lock
    # (highest_profit - trail_gap). Step applies on every trail_step gain.
    # Example (rule 1): +10 -> lock +8, then every +2 keep SL 2 pts below high.
    enable_trailing_after_breakeven: bool = True
    trail_start_premium_points: float = 10.0
    trail_initial_lock_premium_points: float = 8.0
    trail_gap_premium_points: float = 2.0
    trail_step_premium_points: float = 2.0
    trail_trigger_premium_points: float = 10.0   # legacy alias
    trail_lock_premium_points: float = 8.0      # legacy alias
    trail_step_index_points: float = 2.0         # legacy, unused

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

    # V1: fixed initial SL; winners managed by continuous trailing (no fixed target exit)
    use_fixed_sl_target: bool = True
    use_fixed_target_exit: bool = False
    fixed_sl_premium_points: float = 10.0        # SL = entry - 10 premium points
    fixed_target_premium_points: float = 10.0    # reference distance; target exit off


# ============================================================
# POSITION SIZING / MARGIN
# ============================================================

@dataclass(frozen=True)
class PositionSizingConfig:
    risk_per_trade_pct_of_equity: float = 5.0
    max_lots_per_trade: int = 27
    max_premium_exposure_pct_of_equity: float = 100.0  # V1: no equity premium exposure cap
    drop_lots_after_consecutive_losses: int = 0       # V1
    lots_drop_per_consecutive_loss_step: int = 1
    min_lots_after_loss_reduction: int = 1
    round_lots_down: bool = True


# ============================================================
# RISK MANAGEMENT (account level)
# ============================================================

@dataclass(frozen=True)
class RiskConfig:
    daily_loss_limit_pct_of_equity: float = 5.0
    daily_profit_target_pct_of_equity: float = 10.0
    stop_after_daily_loss_limit: bool = False
    stop_after_daily_profit_target: bool = False
    # Targeted guards (keep opportunity; remove known loss patterns)
    enable_entry_protection_guards: bool = False
    cooldown_after_sl_sec: int = 15 * 60
    same_contract_cooldown_sec: int = 15 * 60  # blocks rapid re-entry (loss #8 pattern)
    consecutive_loss_pause_count: int = 2
    consecutive_loss_pause_sec: int = 30 * 60
    max_daily_losses: int = 4
    max_daily_loss_rupees: float = 0.0
    anti_chase_enabled: bool = False
    anti_chase_max_pct_above_recent_avg: float = 5.0
    anti_chase_require_up_candle: bool = True
    option_confirm_enabled: bool = False
    option_confirm_ema_period: int = 3
    no_progress_exit_enabled: bool = False
    no_progress_min_profit_points: float = 3.0
    no_progress_minutes: int = 10
    # Opening window: cap to 1 lot until strategy proves open (loss #5/#9 pattern)
    opening_window_end: time = time(9, 30)
    opening_window_max_lots: int = 1


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
    port: int = 8767
    refresh_interval_sec: float = 2.0


# ============================================================
# BACKTEST
# ============================================================

@dataclass(frozen=True)
class BacktestConfig:
    data_dir: Path = Path.home() / ".nifty_bot" / "backtest_data"
    results_dir: Path = Path.home() / ".nifty_bot" / "backtest_results"
    starting_equity: float = 30000.0  # Paper trading capital for ₹30,000
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