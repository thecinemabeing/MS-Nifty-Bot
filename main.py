"""
main.py
=======
Paper trading entry point using real Kite live market data.

    NIFTY_BOT_MODE=PAPER python main.py

This mode connects to Kite for live spot/option prices and historical
data, but all orders are simulated locally (no real capital at risk).
For backtesting, use backtest.py instead - it replays historical CSVs.
For live trading with real money, use NIFTY_BOT_MODE=LIVE with
NIFTY_BOT_CONFIRM_LIVE=YES.

Self-healing:
  - Kite REST calls retry with backoff.
  - WebSocket reconnects and restores queued Kite subscriptions.
  - The main loop itself is wrapped so an unexpected exception in one
    iteration is logged and the loop continues rather than crashing
    the whole process; force square-off logic runs independently in
    the position monitor thread so an entry-scan bug can't strand you
    in a naked position.
"""

from __future__ import annotations

import signal
import sys
import threading
import time as _time
from datetime import datetime, timedelta, time as dtime

import dashboard
import daily_report
import indicators as ind
from config import CONFIG, TradingMode
from kite_api import KiteAPI
from logger import get_logger, log_decision, send_telegram_alert
from margin_manager import MarginManager
from market_data import CandleStore
from option_selector import OptionUniverse
from order_manager import OrderManager
from risk_manager import RiskManager
from strategy import StrategyEngine
from telegram_control import start_control_loop, read_control, set_suggestion_bot, panic_requested, clear_panic
from telegram_suggestions import TradeSuggestionBot
from utils import (TradeSide, is_force_square_off_time, is_market_open,
                    now_ist_time,
                    )

_log = get_logger("main")

_shutdown = threading.Event()


def _handle_sigterm(signum, frame) -> None:
    _log.warning("Shutdown signal received - stopping gracefully.")
    _shutdown.set()


class TradingBot:
    def __init__(self) -> None:
        self.kite = KiteAPI()
        self.candles = CandleStore()
        self.strategy = StrategyEngine()
        self.risk = RiskManager(starting_equity=CONFIG.backtest.starting_equity
                                 if CONFIG.mode != TradingMode.LIVE else 0.0)
        self.option_universe = OptionUniverse(self.kite)
        self.margin_mgr = MarginManager(
            self.kite,
            paper_equity_override=None if CONFIG.mode == TradingMode.LIVE
            else CONFIG.backtest.starting_equity,
        )
        if CONFIG.mode == TradingMode.PAPER:
            self.kite.set_paper_equity(CONFIG.backtest.starting_equity)
        self.order_mgr = OrderManager(self.kite, get_ltp=self._get_option_ltp)
        self.order_mgr.on_exit(self._on_position_exit)

        self.spot_token: int | None = None
        self._latest_option_ltp: dict[str, float] = {}
        self._active_symbol: str | None = None
        self._active_option_token: int | None = None
        self._last_daily_report_date: str | None = None
        self._last_session_save: float = 0.0
        self._margin_blocked: bool = False

    # ------------------------------------------------------------
    # STARTUP
    # ------------------------------------------------------------

    def start(self) -> None:
        _log.info("Starting NIFTY bot in %s mode.", CONFIG.mode.value)
        if CONFIG.mode == TradingMode.PAPER:
            _log.info("PAPER TRADING: Using REAL Kite market data | Orders are SIMULATED (no real capital at risk)")
        elif CONFIG.mode == TradingMode.LIVE:
            _log.info("LIVE TRADING: Using REAL Kite market data | Orders are REAL (real capital at risk)")

        try:
            self.kite.login()
        except Exception:
            _log.exception("Kite login failed - aborting")
            return

        if CONFIG.mode == TradingMode.LIVE:
            margin = self.margin_mgr.get_available_margin()
            self.risk.equity = margin
            self.risk._starting_equity = margin
            _log.info("Live equity initialized from broker margin: Rs.%.2f", margin)

        try:
            profile = {}
            profile = self.kite.profile()
            account_name = profile.get("name") or profile.get("clientcode") or "unknown"
            _log.info("Pre-flight: user=%s margin=%.2f", account_name, margin if CONFIG.mode == TradingMode.LIVE else 0.0)
        except Exception:
            _log.warning("Pre-flight profile check failed - continuing")

        send_telegram_alert("BOT_START", f"Mode={CONFIG.mode.value} user={profile.get('name') or profile.get('clientcode') or 'unknown'}")

        # Clear any pending restart flag so the supervisor relaunch is clean.
        try:
            from telegram_control import clear_restart
            clear_restart()
        except Exception:
            pass

        self._load_session_state()

        # Rebuild today's risk counters from the trade journal so the
        # dashboard shows the full day's P&L after a mid-day restart.
        try:
            from datetime import datetime as _dt
            self.risk.rebuild_today_from_journal(_dt.now().date())
        except Exception:
            _log.exception("rebuild_today_from_journal failed")

        self.spot_token = self.option_universe.get_spot_instrument_token()
        self._warm_up_history()

        # Push rebuilt counters to the dashboard immediately (the scan
        # loop only updates state while the market is open).
        try:
            self._update_dashboard_indicators(
                self.candles.get_closed_candles(self.spot_token, CONFIG.session.primary_timeframe_min)
                or []
            )
        except Exception:
            _log.exception("initial dashboard update failed")

        tokens = [self.spot_token]
        self.kite.start_ticker(tokens, on_tick=self._on_ticks)
        if not self.kite.wait_until_connected(timeout=20):
            _log.error("WebSocket failed to connect within timeout - retrying in background.")

        gap_thread = threading.Thread(target=self._ws_gap_recovery_loop, daemon=True)
        gap_thread.start()

        dashboard.start_dashboard()
        import session_dashboard
        session_dashboard.start_session_dashboard()

        signal.signal(signal.SIGINT, _handle_sigterm)
        signal.signal(signal.SIGTERM, _handle_sigterm)

        monitor_thread = threading.Thread(target=self._position_monitor_loop, daemon=True)
        monitor_thread.start()

        session_thread = threading.Thread(target=self._session_persistence_loop, daemon=True)
        session_thread.start()

        margin_thread = threading.Thread(target=self._margin_monitor_loop, daemon=True)
        margin_thread.start()

        ctrl_thread = threading.Thread(target=start_control_loop, daemon=True)
        ctrl_thread.start()
        _log.info("Telegram control enabled - use /start, /stop, /status from your bot")

        self.suggestion_bot = TradeSuggestionBot(self.kite, self.candles, self.strategy)
        self.suggestion_bot.spot_token = None
        set_suggestion_bot(self.suggestion_bot)

        self._main_scan_loop()

    def _warm_up_history(self) -> None:
        now = datetime.now()
        # Load several days so ADX/ATR/EMA50 are available early in the session.
        from_dt = now - timedelta(days=7)
        try:
            bars_5m = self.kite.historical_data(self.spot_token, from_dt, now, "5minute")
            bars_15m = self.kite.historical_data(self.spot_token, from_dt, now, "15minute")
            self.candles.load_historical(self.spot_token, CONFIG.session.primary_timeframe_min, bars_5m)
            self.candles.load_historical(self.spot_token, CONFIG.session.higher_timeframe_min, bars_15m)
            _log.info("Warm-up loaded %d 5m / %d 15m bars (7d lookback)",
                      len(bars_5m or []), len(bars_15m or []))
        except Exception:
            _log.exception("Historical warm-up fetch failed - continuing, will build from live ticks.")
        try:
            daily = self.kite.historical_data(self.spot_token, now - timedelta(days=30), now, "day")
            if daily and len(daily) >= 2:
                # previous completed day (last may be today)
                prev = daily[-2] if str(daily[-1].get('date',''))[:10] == now.strftime('%Y-%m-%d') else daily[-1]
                pdh, pdl, pdc = float(prev['high']), float(prev['low']), float(prev['close'])
                pivot = (pdh + pdl + pdc) / 3.0
                ranges = [float(b['high']) - float(b['low']) for b in daily[:-1] if b.get('high') and b.get('low')]
                adr = sum(ranges[-14:]) / len(ranges[-14:]) if ranges else None
                self._market_levels = {
                    'pdh': round(pdh,2), 'pdl': round(pdl,2), 'pdc': round(pdc,2),
                    'pivot': round(pivot,2),
                    's1': round(2*pivot - pdh, 2), 'r1': round(2*pivot - pdl, 2),
                    'adr_14': round(adr, 2) if adr else None,
                }
                _log.info("Market levels: %s", self._market_levels)
        except Exception:
            _log.exception("Failed to load daily market levels")

    # ------------------------------------------------------------
    # TICK HANDLING
    # ------------------------------------------------------------

    def _on_ticks(self, ticks: list[dict]) -> None:
        self._tick_count = getattr(self, "_tick_count", 0) + len(ticks)
        for t in ticks:
            token = t.get("instrument_token")
            price = t.get("last_price")
            vol = t.get("last_traded_quantity", 0)
            if token == self.spot_token and price:
                self.candles.on_tick(token, price, vol)
            tsym = t.get("tradingsymbol")
            if tsym and price:
                self._latest_option_ltp[tsym] = price

    def _get_option_ltp(self, tradingsymbol: str) -> float | None:
        if tradingsymbol in self._latest_option_ltp:
            return self._latest_option_ltp[tradingsymbol]
        # fall back to a direct quote call if we don't have a live tick yet
        try:
            q = self.kite.ltp([f"{CONFIG.instrument.option_exchange}:{tradingsymbol}"])
            key = f"{CONFIG.instrument.option_exchange}:{tradingsymbol}"
            return q.get(key, {}).get("last_price")
        except Exception:
            _log.exception("Failed to fetch LTP for %s", tradingsymbol)
            return None

    # ------------------------------------------------------------
    # ACTIVE OPTION WEBSOCKET SUBSCRIPTION (tick-level LTP)
    # ------------------------------------------------------------

    def _subscribe_active_option(self, token: int) -> None:
        if self._active_option_token is not None and self._active_option_token != token:
            try:
                self.kite.remove_subscription(self._active_option_token)
            except Exception:
                _log.exception("Failed to unsubscribe previous option token %s", self._active_option_token)
        if self._active_option_token != token:
            try:
                self.kite.add_subscription(token)
                _log.info("Subscribed option token %s for tick-level LTP", token)
            except Exception:
                _log.exception("Failed to subscribe option token %s", token)
            self._active_option_token = token

    def _unsubscribe_active_option(self) -> None:
        if self._active_option_token is not None:
            try:
                self.kite.remove_subscription(self._active_option_token)
                _log.info("Unsubscribed option token %s", self._active_option_token)
            except Exception:
                _log.exception("Failed to unsubscribe option token %s", self._active_option_token)
            self._active_option_token = None

    # ------------------------------------------------------------
    # MAIN SCAN LOOP (entries)
    # ------------------------------------------------------------

    def _main_scan_loop(self) -> None:
        last_processed_bar_ts = None
        while not _shutdown.is_set():
            try:
                self._scan_iteration_guard(last_processed_bar_ts)
            except Exception:
                _log.exception("Unhandled error in scan loop iteration - continuing.")
            _time.sleep(CONFIG.session.scan_loop_interval_sec)

        self._shutdown_sequence()

    def _scan_iteration_guard(self, last_processed_bar_ts) -> None:
        ctrl = read_control()
        if panic_requested():
            if is_market_open():
                try:
                    self.order_mgr.force_square_off_all()
                except Exception:
                    _log.exception("Panic square-off failed")
            clear_panic()
            write_control("STOPPED") if False else None
            try:
                from telegram_control import write_control as _wc
                _wc("STOPPED")
                from logger import send_telegram_alert
                send_telegram_alert("PANIC_EXECUTED", "Panic processed: stopped and square-off attempted.")
            except Exception:
                pass
            return
        if ctrl.get("state") == "STOPPED":
            return

        if CONFIG.killswitch.killswitch_path.exists():
            _log.warning("Killswitch active at %s - blocking new entries", CONFIG.killswitch.killswitch_path)
            if is_market_open():
                self.order_mgr.force_square_off_all()
            return

        today_str = datetime.now().date().isoformat()
        if not is_market_open():
            if self._last_daily_report_date != today_str:
                try:
                    daily_report.save_daily_report()
                    daily_report.send_daily_report_telegram()
                    self._last_daily_report_date = today_str
                    _log.info("Daily report saved and sent via Telegram for %s", today_str)
                except Exception:
                    _log.exception("Failed to process daily report for %s", today_str)
            return

        closed_5m = self.candles.get_closed_candles(self.spot_token, CONFIG.session.primary_timeframe_min)
        if not closed_5m:
            return
        # update dashboard even during warm-up (strategy still requires 45 bars)
        self._update_dashboard_indicators(closed_5m)
        if len(closed_5m) < 45:
            return
        latest_bar_ts = closed_5m[-1].ts
        if latest_bar_ts == last_processed_bar_ts:
            return  # already evaluated this closed candle

        if hasattr(self, 'suggestion_bot'):
            self.suggestion_bot.spot_token = self.spot_token
            try:
                self.suggestion_bot.analyze_and_suggest(self.spot_token)
            except Exception:
                pass

        if is_force_square_off_time():
            return  # position_monitor_loop handles the actual square-off

        if self.risk.stats.trades_taken >= CONFIG.strategy.max_trades_per_day:
            return  # daily trade cap reached

        if self.order_mgr.open_position_count() > 0:
            return  # one position at a time (sequential entries)

        if not self._check_margin_safety():
            return

        allowed, reason = self.risk.can_take_new_trade()
        if not allowed:
            if reason == "DAILY_LOSS_LIMIT_HIT":
                send_telegram_alert("DAILY_LOSS_LIMIT_HIT", f"daily_pnl={self.risk._daily_pnl:.2f}")
            return

        closed_15m = self.candles.get_closed_candles(self.spot_token, CONFIG.session.higher_timeframe_min)
        if len(closed_15m) < 20:
            return
        vwap_series = [self.candles.get_current_vwap(self.spot_token, CONFIG.session.primary_timeframe_min)] * 1
        # build an aligned vwap series matching closed_5m length using stored candles
        vwap_series = self._vwap_series_for(closed_5m)

        signal = self.strategy.evaluate(closed_5m, closed_15m, vwap_series, symbol="NIFTY")
        if signal.side in (TradeSide.LONG, TradeSide.SHORT):
            now_t = now_ist_time()
            start_t = getattr(CONFIG.session, 'setup_scan_start', dtime(9, 20))
            cutoff_t = getattr(CONFIG.session, 'entry_cutoff_time', getattr(CONFIG.session, 'last_entry_time', dtime(15, 14)))
            last_t = min(getattr(CONFIG.session, 'last_entry_time', dtime(15, 14)), cutoff_t)
            # Guard: completed-bar only (closed_5m[-1] is closed), no entries before 09:20 context or at/after 15:15.
            bar_t = closed_5m[-1].ts.time() if hasattr(closed_5m[-1].ts, 'time') else None
            if CONFIG.session.enable_time_based_entry_gate:
                if now_t < start_t or now_t > last_t:
                    return
                if bar_t is not None and bar_t >= dtime(15, 15):
                    return
            allowed, adx_reason = self.risk.adx_band_allows(signal.adx)
            if not allowed:
                _log.info("Entry blocked by ADX band: %s", adx_reason)
                return
            self._execute_signal(signal)

    def _vwap_series_for(self, closed_5m) -> list[float]:
        # Recomputed cheaply from closed candles each pass; fine at 5-min cadence.
        vals, cum_pv, cum_vol, day = [], 0.0, 0.0, None
        for c in closed_5m:
            if day != c.ts.date():
                day, cum_pv, cum_vol = c.ts.date(), 0.0, 0.0
            v = max(c.volume, 1)
            cum_pv += c.close * v
            cum_vol += v
            vals.append(cum_pv / cum_vol)
        return vals

    def _option_entry_filters(self, contract, premium: float) -> tuple[bool, str]:
        """Anti-chase/option confirmation for a bought option. Returns (ok, reason)."""
        if not (CONFIG.risk.anti_chase_enabled or CONFIG.risk.option_confirm_enabled):
            return True, "OK"
        try:
            from_date = datetime.combine(datetime.now().date() - timedelta(days=2), dtime(9, 15))
            to_date = datetime.now()
            bars = self.kite.historical_data(contract.instrument_token, from_date, to_date, "5minute")
            oc = [Candle(ts=(b["date"].replace(tzinfo=None) if hasattr(b["date"], "tzinfo") and b["date"].tzinfo else b["date"]),
                         open=float(b["open"]), high=float(b["high"]), low=float(b["low"]), close=float(b["close"]),
                         volume=int(b.get("volume", 0))) for b in (bars or [])]
            if not oc:
                return True, "OPTION_CANDLES_UNAVAILABLE"
            c = oc[-1]
            ep = premium if premium > 0 else c.close
            if CONFIG.risk.anti_chase_enabled:
                closes = [x.close for x in oc[-5:] if x.close > 0]
                if closes:
                    avg = sum(closes) / len(closes)
                    if avg > 0 and ep > avg * (1 + CONFIG.risk.anti_chase_max_pct_above_recent_avg / 100.0):
                        return False, f"ANTI_CHASE(premium={ep:.2f}>avg={avg:.2f})"
                if CONFIG.risk.anti_chase_require_up_candle and not (c.close > c.open):
                    return False, f"ANTI_CHASE_WEAK_CANDLE(o={c.open:.2f} c={c.close:.2f})"
            if CONFIG.risk.option_confirm_enabled:
                closes = [x.close for x in oc[-max(2, CONFIG.risk.option_confirm_ema_period):] if x.close > 0]
                if closes:
                    k = 2 / (len(closes) + 1)
                    e = closes[0]
                    for v in closes[1:]:
                        e = v * k + e * (1 - k)
                    if ep < e:
                        return False, f"OPTION_CONFIRM_FAIL(premium={ep:.2f}<ema={e:.2f})"
            return True, "OK"
        except Exception:
            _log.debug("Option candle filters unavailable for %s; proceeding", getattr(contract, "tradingsymbol", "?"))
            return True, "OPTION_FILTERS_UNAVAILABLE"

    def _execute_signal(self, signal) -> None:
        if not self._check_margin_safety():
            _log.warning("Entry blocked due to low margin")
            return
        allowed, reason = self.risk.can_take_new_trade()
        if not allowed:
            _log.info("Entry blocked by risk guard: %s", reason)
            return

        try:
            spot_price = signal.entry_price
            contract = self.option_universe.resolve_atm_contract(spot_price, signal.side)
        except Exception:
            _log.exception("Failed to resolve ATM contract for signal")
            return

        ok, sym_reason = self.risk.can_enter_symbol(contract.tradingsymbol, strike=contract.strike, option_type=contract.option_type)
        if not ok:
            _log.info("ENTRY_SYMBOL_GUARD_SKIP | %s | %s", contract.tradingsymbol, sym_reason)
            return

        premium = self._get_option_ltp(contract.tradingsymbol) or 0.0
        if premium <= 0:
            _log.warning("Entry skipped: option LTP unavailable for %s", contract.tradingsymbol)
            return
        fok, freason = self._option_entry_filters(contract, premium)
        if not fok:
            _log.info("ENTRY_OPTION_FILTER_SKIP | %s | %s", contract.tradingsymbol, freason)
            return
        equity = self.risk.equity

        # Buy-only options (CE for bullish, PE for bearish): all distances are
        # OPTION PREMIUM points on the bought option. Loss = premium falls by SL distance.
        sl_distance = abs(signal.entry_price - signal.stop_loss)
        target_distance = abs(signal.target - signal.entry_price)
        premium_sl = max(premium - sl_distance, 0.05)
        premium_target = max(premium + target_distance, 0.05)

        sizing = self.margin_mgr.size_position(
            equity=equity, entry_price=premium, stop_loss_price=premium_sl,
            option_premium=premium, lot_size=contract.lot_size,
            margin_per_lot_estimate=self._estimate_margin_per_lot(contract, premium),
            adx=signal.adx, consecutive_losses=self.risk.stats.consecutive_losses,
        )
        if sizing.quantity <= 0:
            return

        self._active_symbol = contract.tradingsymbol
        self.order_mgr.enter_position(
            contract=contract, side=signal.side, quantity=sizing.quantity,
            entry_price=premium, stop_loss=premium_sl, target=premium_target,
            initial_sl=sl_distance, entry_adx=signal.adx,
        )
        self._subscribe_active_option(contract.instrument_token)
        trade_num = self.risk.stats.trades_taken + 1
        send_telegram_alert(
            "POSITION_OPEN",
            f"TRADE: {trade_num} | {contract.tradingsymbol} | Qty: {sizing.quantity} | Side: {signal.side.value} | Entry: ₹{premium:.2f} | SL: ₹{premium_sl:.2f} | Target: ₹{premium_target:.2f} | ADX: {signal.adx:.1f} | Lots: {sizing.lots}",
        )

    def _estimate_margin_per_lot(self, contract, premium: float) -> float:
        # Buying options is fully paid (no leverage on the long premium side);
        # margin required ~= premium * lot_size for a long option buy.
        return max(premium, 0.05) * contract.lot_size

    # ------------------------------------------------------------
    # POSITION MONITOR LOOP (exits - runs independently of entry scan)
    # ------------------------------------------------------------

    def _position_monitor_loop(self) -> None:
        while not _shutdown.is_set():
            try:
                self.order_mgr.monitor_positions()

                if is_force_square_off_time():
                    self.order_mgr.force_square_off_all()
            except Exception:
                _log.exception("Unhandled error in position monitor loop - continuing.")
            _time.sleep(CONFIG.session.position_monitor_interval_sec)

    def _on_position_exit(self, symbol: str, result) -> None:
        self._unsubscribe_active_option()
        pos = self.order_mgr.get_position(symbol) if hasattr(self.order_mgr, 'get_position') else None
        strike = getattr(getattr(pos, 'contract', None), 'strike', None)
        opt_type = getattr(getattr(pos, 'contract', None), 'option_type', None)
        self.risk.record_trade_result(result.pnl, tradingsymbol=symbol, reason=getattr(result, 'reason', ''), strike=strike, option_type=opt_type)
        if CONFIG.mode != TradingMode.LIVE:
            self.risk.equity += result.pnl
            self.margin_mgr.update_paper_equity(self.risk.equity)
        # Send detailed trade summary to telegram
        total_trades = self.risk.stats.trades_taken
        win_rate = self.risk.win_rate()
        daily_pnl = self.risk.stats.realized_pnl
        mfe = getattr(getattr(self.order_mgr, '_last_exit_metrics', {}), 'get', lambda *_: None)('mfe') if False else ''
        msg = f"{symbol} | PnL: ₹{result.pnl:.2f} | Reason: {getattr(result,'reason','')} | Trades Today: {total_trades} | Daily PnL: ₹{daily_pnl:.2f} | WinRate: {win_rate:.1f}%"
        send_telegram_alert("POSITION_CLOSE", msg)
        dashboard.update_state(
            position=None, current_pnl=0, daily_pnl=round(self.risk.stats.realized_pnl, 2),
            trades_today=self.risk.stats.trades_taken, win_rate=round(self.risk.win_rate(), 1),
        )

    # ------------------------------------------------------------
    # KILL-SWITCH
    # ------------------------------------------------------------

    def _check_killswitch(self) -> bool:
        if CONFIG.killswitch.killswitch_path.exists():
            _log.warning("Killswitch active at %s", CONFIG.killswitch.killswitch_path)
            if is_market_open():
                self.order_mgr.force_square_off_all()
            return True
        return False

    # ------------------------------------------------------------
    # WEBSOCKET GAP RECOVERY
    # ------------------------------------------------------------

    def _ws_gap_recovery_loop(self) -> None:
        while not _shutdown.is_set():
            try:
                self._check_ws_gap()
            except Exception:
                _log.exception("Unhandled error in WS gap recovery loop - continuing.")
            _time.sleep(CONFIG.ws_recovery.gap_sync_interval_sec)

    def _check_ws_gap(self) -> None:
        if not self.spot_token:
            return
        closed = self.candles.get_closed_candles(self.spot_token, CONFIG.session.primary_timeframe_min)
        if not closed:
            return
        latest = closed[-1].ts
        missing_min = (datetime.now() - latest).total_seconds() / 60.0
        if missing_min >= CONFIG.ws_recovery.gap_candle_missing_min:
            _log.warning("WS gap detected: last candle is %.1f min old - syncing history", missing_min)
            try:
                self._warm_up_history()
            except Exception:
                _log.exception("WS gap recovery warm-up failed")

    # ------------------------------------------------------------
    # SESSION PERSISTENCE
    # ------------------------------------------------------------

    def _session_persistence_loop(self) -> None:
        while not _shutdown.is_set():
            try:
                now = _time.time()
                if now - self._last_session_save >= CONFIG.killswitch.session_save_interval_sec:
                    self._save_session_state()
                    self._last_session_save = now
            except Exception:
                _log.exception("Unhandled error in session persistence loop - continuing.")
            _time.sleep(5.0)

    def _save_session_state(self) -> None:
        state = self.order_mgr.export_session_state()
        if not state:
            return
        path = CONFIG.killswitch.session_state_path
        try:
            import json
            path.write_text(json.dumps(state, default=str), encoding="utf-8")
            _log.debug("Session state saved (%d positions)", len(state.get("positions", [])))
        except Exception:
            _log.exception("Failed to save session state")

    def _load_session_state(self) -> None:
        path = CONFIG.killswitch.session_state_path
        if not path.exists():
            return
        try:
            import json
            data = json.loads(path.read_text(encoding="utf-8"))
            self.order_mgr.import_session_state(data)
            _log.info("Session state restored (%d positions)", len(data.get("positions", [])))
            for pos in self.order_mgr.get_open_positions():
                try:
                    self.kite.add_subscription(pos.contract.instrument_token)
                    self._active_option_token = pos.contract.instrument_token
                    _log.info("Resubscribed restored option token %s", self._active_option_token)
                except Exception:
                    _log.exception("Failed to resubscribe restored option token")
        except Exception:
            _log.exception("Failed to load session state")

    # ------------------------------------------------------------
    # MARGIN SAFETY
    # ------------------------------------------------------------

    def _margin_monitor_loop(self) -> None:
        while not _shutdown.is_set():
            try:
                self._check_margin_safety()
            except Exception:
                _log.exception("Unhandled error in margin monitor loop - continuing.")
            _time.sleep(CONFIG.margin_safety.margin_check_interval_sec)

    def _check_margin_safety(self) -> bool:
        available = self.margin_mgr.get_available_margin()
        equity = self.risk.equity if self.risk.equity > 0 else available
        min_abs = CONFIG.margin_safety.min_available_margin
        min_pct = CONFIG.margin_safety.min_available_margin_pct
        threshold = max(min_abs, equity * min_pct / 100.0) if min_pct > 0 else min_abs

        if available < threshold:
            if not self._margin_blocked:
                self._margin_blocked = True
                _log.warning("LOW_MARGIN_ENTRY_BLOCK | available=%.2f threshold=%.2f", available, threshold)
                log_structured(
                    "LOW_MARGIN_ENTRY_BLOCK",
                    symbol="-",
                    side="-",
                    position_quantity=0,
                    exit_reason="LOW_MARGIN",
                    strategy_exit_price=0.0,
                    limit_price=0.0,
                    buffer_pct=0.0,
                    limit_order_id="-",
                    filled_quantity=0,
                    remaining_quantity=0,
                    timeout_seconds=0,
                    fallback_used=False,
                    final_exit_status="BLOCKED",
                    available_margin=round(available, 2),
                    used_margin=round(equity - available, 2),
                    margin_threshold=round(threshold, 2),
                )
                send_telegram_alert("LOW_MARGIN_ENTRY_BLOCK", f"available={available:.2f} threshold={threshold:.2f}")
            return False

        if self._margin_blocked:
            self._margin_blocked = False
            _log.info("MARGIN_RECOVERED | available=%.2f threshold=%.2f", available, threshold)
            log_structured(
                "MARGIN_RECOVERED",
                symbol="-",
                side="-",
                position_quantity=0,
                exit_reason="MARGIN_RECOVERED",
                strategy_exit_price=0.0,
                limit_price=0.0,
                buffer_pct=0.0,
                limit_order_id="-",
                filled_quantity=0,
                remaining_quantity=0,
                timeout_seconds=0,
                fallback_used=False,
                final_exit_status="RECOVERED",
                available_margin=round(available, 2),
                used_margin=round(equity - available, 2),
                margin_threshold=round(threshold, 2),
            )
            send_telegram_alert("MARGIN_RECOVERED", f"available={available:.2f} threshold={threshold:.2f}")

        return True

    # ------------------------------------------------------------
    # DASHBOARD FEED
    # ------------------------------------------------------------

    def _get_spot_ltp(self) -> float | None:
        try:
            q = self.kite.ltp([f"{CONFIG.instrument.exchange}:{'NIFTY 50'}"])
            key = f"{CONFIG.instrument.exchange}:NIFTY 50"
            return (q.get(key) or {}).get("last_price")
        except Exception:
            return None

    def _update_dashboard_indicators(self, closed_5m) -> None:
        closes = [c.close for c in closed_5m]
        ema20 = ind.latest_ema(closes, CONFIG.indicators.ema_fast)
        ema50 = ind.latest_ema(closes, CONFIG.indicators.ema_slow)
        adx_val = ind.adx(closed_5m, CONFIG.indicators.adx_period)
        adx_kite_val = ind.adx(closed_5m, getattr(CONFIG.indicators, "adx_kite_period", 10))
        atr_val = ind.atr(closed_5m, CONFIG.indicators.atr_period)
        vwap = self.candles.get_current_vwap(self.spot_token, CONFIG.session.primary_timeframe_min)
        positions = self.order_mgr.get_open_positions()
        pos = positions[0] if positions else None
        levels = getattr(self, "_market_levels", {}) or {}
        live_spot = self._get_spot_ltp()

        dashboard.update_state(
            nifty_spot=round(live_spot, 2) if live_spot else (closes[-1] if closes else None),
            trend=("UP" if ema20 and ema50 and ema20 > ema50 else "DOWN" if ema20 and ema50 else None),
            ema20=round(ema20, 2) if ema20 else None,
            ema50=round(ema50, 2) if ema50 else None,
            vwap=round(vwap, 2) if vwap else None,
            adx=round(adx_val, 2) if adx_val else None,
            adx_kite=round(adx_kite_val, 2) if adx_kite_val else None,
            atr=round(atr_val, 2) if atr_val else None,
            adr_14=levels.get("adr_14"),
            pdh=levels.get("pdh"), pdl=levels.get("pdl"), pdc=levels.get("pdc"),
            pivot=levels.get("pivot"), s1=levels.get("s1"), r1=levels.get("r1"),
            position=pos.contract.tradingsymbol if pos else None,
            entry_price=pos.entry_price if pos else None,
            atm_strike=pos.contract.strike if pos else None,
            option_premium=self._get_option_ltp(pos.contract.tradingsymbol) if pos else None,
            available_margin=round(self.margin_mgr.get_available_margin(), 2),
            trades_today=self.risk.stats.trades_taken,
            daily_pnl=round(self.risk.stats.realized_pnl, 2),
            win_rate=round(self.risk.win_rate(), 1),
        )

    # ------------------------------------------------------------
    # SHUTDOWN
    # ------------------------------------------------------------

    def _shutdown_sequence(self) -> None:
        _log.info("Shutting down: squaring off any open positions and closing connections.")
        try:
            self.order_mgr.force_square_off_all()
        except Exception:
            _log.exception("Error during shutdown square-off")
        try:
            self.kite.stop_ticker()
        except Exception:
            pass
        try:
            daily_report.save_daily_report()
            report = daily_report.build_daily_report()
            today_str = datetime.now().date().isoformat()
            summary = report.get(today_str)
            if summary:
                _log.info("Day complete: PnL=%.2f Trades=%d WinRate=%.1f%%",
                          summary["net_pnl"], summary["trades_count"], summary["win_rate_pct"])
            _log.info("Final daily report saved on shutdown")
        except Exception:
            _log.exception("Failed to save daily report on shutdown")
        try:
            self._save_session_state()
        except Exception:
            _log.exception("Failed to save session state on shutdown")
        log_decision("BOT_SHUTDOWN", trades_today=self.risk.stats.trades_taken,
                     daily_pnl=round(self.risk.stats.realized_pnl, 2))
        send_telegram_alert("BOT_SHUTDOWN", f"Trades={self.risk.stats.trades_taken} PnL={self.risk.stats.realized_pnl:.2f}")

        # If a restart was requested, exit non-zero so the launchd
        # supervisor (KeepAlive SuccessfulExit=false) relaunches main.py.
        try:
            from telegram_control import restart_requested, clear_restart
            if restart_requested():
                _log.info("Restart requested via Telegram - exiting for supervisor relaunch.")
                clear_restart()
                sys.exit(1)
        except Exception:
            _log.exception("restart check failed on shutdown")
        sys.exit(0)


if __name__ == "__main__":
    bot = TradingBot()
    bot.start()