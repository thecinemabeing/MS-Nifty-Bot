"""
risk_manager.py
=================
Tracks trade outcomes, enforces daily loss/profit limits and
max-trades-per-day caps, and exposes a simple can_take_new_trade()
gate that the main scan loop checks before every entry attempt.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional

from config import CONFIG
from logger import get_logger
from utils import TradeSide

_log = get_logger("risk_manager")


@dataclass
class RiskStats:
    realized_pnl: float = 0.0
    trades_taken: int = 0
    wins: int = 0
    losses: int = 0
    consecutive_losses: int = 0


@dataclass
class _SymbolCooldown:
    until_ts: float = 0.0
    reason: str = ""
    strike: Optional[int] = None
    option_type: Optional[str] = None


class RiskManager:
    def __init__(self, starting_equity: float = 0.0) -> None:
        self.equity: float = starting_equity
        self._starting_equity: float = starting_equity
        self.stats: RiskStats = RiskStats()
        self._daily_pnl: float = 0.0
        self._first_trade_was_loss: bool = False
        self._first_trade_taken: bool = False
        self._cooldowns: dict[str, _SymbolCooldown] = {}
        self._pause_until_ts: float = 0.0
        self._pause_reason: str = ""
        self._halted_for_day: bool = False
        self._halt_reason: str = ""

    def _now_ts(self) -> float:
        return datetime.now().timestamp()

    def adx_band_allows(self, adx: float) -> tuple[bool, str]:
        """V1 band: eligible when ADX >= adx_min and <= adx_max (default 23..32)."""
        if adx is None:
            return False, "ADX_MISSING"
        adx = float(adx)
        amin = float(getattr(CONFIG.strategy, "adx_min", 23.0))
        amax = float(getattr(CONFIG.strategy, "adx_max", 32.0))
        if adx < amin:
            return False, f"ADX_BELOW_MIN({adx:.1f}<={amin:.1f})"
        if adx > amax:
            return False, f"ADX_ABOVE_MAX({adx:.1f}>{amax:.1f})"
        return True, "OK"

    def can_take_new_trade(self) -> tuple[bool, str]:
        # V1-style: no "2nd only if 1st loss" gate; only max trades/day and optional guard halts.
        if CONFIG.risk.enable_entry_protection_guards:
            if self._halted_for_day:
                return False, self._halt_reason or "DAILY_GUARD_HALT"
            now = self._now_ts()
            if now < self._pause_until_ts:
                remaining = int(self._pause_until_ts - now)
                return False, f"{self._pause_reason or 'ENTRY_PAUSED'}({remaining}s)"
            if CONFIG.risk.max_daily_losses > 0 and self.stats.losses >= CONFIG.risk.max_daily_losses:
                self._halted_for_day = True
                self._halt_reason = f"MAX_DAILY_LOSSES_HIT({self.stats.losses}>={CONFIG.risk.max_daily_losses})"
                return False, self._halt_reason
            if CONFIG.risk.max_daily_loss_rupees > 0 and self._daily_pnl <= -abs(CONFIG.risk.max_daily_loss_rupees):
                self._halted_for_day = True
                self._halt_reason = f"MAX_DAILY_LOSS_RUPEES_HIT({self._daily_pnl:.2f}<=-{abs(CONFIG.risk.max_daily_loss_rupees):.2f})"
                return False, self._halt_reason

        if self.stats.trades_taken >= CONFIG.strategy.max_trades_per_day:
            return False, f"MAX_TRADES_REACHED({self.stats.trades_taken}>={CONFIG.strategy.max_trades_per_day})"

        if CONFIG.risk.stop_after_daily_loss_limit:
            loss_limit = self._starting_equity * CONFIG.risk.daily_loss_limit_pct_of_equity / 100.0
            if self._daily_pnl <= -loss_limit:
                return False, "DAILY_LOSS_LIMIT_HIT"

        if CONFIG.risk.stop_after_daily_profit_target:
            profit_target = self._starting_equity * CONFIG.risk.daily_profit_target_pct_of_equity / 100.0
            if self._daily_pnl >= profit_target:
                return False, "DAILY_PROFIT_TARGET_HIT"

        return True, "OK"

    def can_enter_symbol(self, tradingsymbol: str, strike: Optional[int] = None, option_type: Optional[str] = None) -> tuple[bool, str]:
        if not CONFIG.risk.enable_entry_protection_guards:
            return True, "OK"
        key = f"{tradingsymbol}|{strike or ''}|{option_type or ''}"
        cooldown = self._cooldowns.get(key) or self._cooldowns.get(tradingsymbol)
        if not cooldown:
            return True, "OK"
        now = self._now_ts()
        if now < cooldown.until_ts:
            remaining = int(cooldown.until_ts - now)
            return False, f"{cooldown.reason}({remaining}s)"
        return True, "OK"

    def record_trade_result(self, pnl: float, tradingsymbol: str = "", reason: str = "", strike: Optional[int] = None, option_type: Optional[str] = None) -> None:
        trade_num = self.stats.trades_taken + 1
        self.stats.trades_taken += 1
        self._daily_pnl += pnl
        self.stats.realized_pnl += pnl
        was_sl_loss = pnl < 0 and ("SL" in (reason or "").upper() or "STOP_LOSS" in (reason or "").upper())

        if trade_num == 1:
            self._first_trade_was_loss = pnl < 0
        if pnl > 0:
            self.stats.wins += 1
            self.stats.consecutive_losses = 0
        elif pnl < 0:
            self.stats.losses += 1
            self.stats.consecutive_losses += 1
        else:
            self.stats.consecutive_losses = 0

        # Targeted 15-min same-contract cooldown: blocks rapid re-entry after SL/fast loss (#8 pattern).
        sec = int(getattr(CONFIG.risk, "same_contract_cooldown_sec", 0) or 0)
        is_loss_exit = pnl < 0 and ("SL" in (reason or "").upper() or "STOP_LOSS" in (reason or "").upper() or "FORCE" not in (reason or "").upper() or sec > 0)
        if CONFIG.risk.enable_entry_protection_guards:
            sec = sec or int(CONFIG.risk.cooldown_after_sl_sec or 0)
            is_loss_exit = pnl < 0 and ("SL" in (reason or "").upper() or "STOP_LOSS" in (reason or "").upper())
        if sec > 0 and tradingsymbol and (pnl < 0):
            now = self._now_ts()
            cooldown = _SymbolCooldown(until_ts=now + sec, reason="SAME_CONTRACT_COOLDOWN", strike=strike, option_type=option_type)
            self._cooldowns[tradingsymbol] = cooldown
            self._cooldowns[f"{tradingsymbol}|{strike or ''}|{option_type or ''}"] = cooldown
            _log.info("SAME_CONTRACT_COOLDOWN | symbol=%s | seconds=%d | reason=%s pnl=%.2f", tradingsymbol, sec, reason, pnl)

        if CONFIG.risk.enable_entry_protection_guards:
            now = self._now_ts()
            if pnl < 0 and CONFIG.risk.consecutive_loss_pause_count > 0 and self.stats.consecutive_losses >= CONFIG.risk.consecutive_loss_pause_count:
                self._pause_until_ts = now + CONFIG.risk.consecutive_loss_pause_sec
                self._pause_reason = f"CONSECUTIVE_LOSS_PAUSE({self.stats.consecutive_losses})"
                _log.warning("ENTRY_GUARD_PAUSE | consecutive_losses=%d | seconds=%d | daily_pnl=%.2f", self.stats.consecutive_losses, CONFIG.risk.consecutive_loss_pause_sec, self._daily_pnl)
            if CONFIG.risk.max_daily_losses > 0 and self.stats.losses >= CONFIG.risk.max_daily_losses:
                self._halted_for_day = True
                self._halt_reason = f"MAX_DAILY_LOSSES_HIT({self.stats.losses}>={CONFIG.risk.max_daily_losses})"
                _log.warning("ENTRY_GUARD_HALT | reason=%s | daily_pnl=%.2f", self._halt_reason, self._daily_pnl)
            if CONFIG.risk.max_daily_loss_rupees > 0 and self._daily_pnl <= -abs(CONFIG.risk.max_daily_loss_rupees):
                self._halted_for_day = True
                self._halt_reason = f"MAX_DAILY_LOSS_RUPEES_HIT({self._daily_pnl:.2f}<=-{abs(CONFIG.risk.max_daily_loss_rupees):.2f})"
                _log.warning("ENTRY_GUARD_HALT | reason=%s", self._halt_reason)

        _log.info(
            "Trade result recorded: pnl=%.2f daily_pnl=%.2f consecutive_losses=%d losses=%d trades_taken=%d reason=%s symbol=%s first_trade_was_loss=%s",
            pnl, self._daily_pnl, self.stats.consecutive_losses, self.stats.losses, self.stats.trades_taken, reason, tradingsymbol, self._first_trade_was_loss,
        )

    def win_rate(self) -> float:
        if self.stats.trades_taken == 0:
            return 0.0
        return self.stats.wins / self.stats.trades_taken * 100.0

    def reset_daily_counters(self) -> None:
        self._daily_pnl = 0.0
        self.stats.trades_taken = 0
        self.stats.wins = 0
        self.stats.losses = 0
        self.stats.consecutive_losses = 0
        self._first_trade_was_loss = False
        self._first_trade_taken = False
        self._cooldowns.clear()
        self._pause_until_ts = 0.0
        self._pause_reason = ""
        self._halted_for_day = False
        self._halt_reason = ""
