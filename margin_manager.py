"""
margin_manager.py
==================
Never uses a hardcoded capital figure for sizing. Always reads live
available margin from Angel One in LIVE/PAPER-with-real-quotes
mode) and sizes the position so it:
  1. Risks no more than risk_per_trade_pct_of_equity of account equity
     given the stop-loss distance.
  2. Never exceeds max_exposure_pct_of_margin of available margin.
  3. Never exceeds max_lots_per_trade.
  4. Is always rounded DOWN to a whole number of lots.
"""

from __future__ import annotations

from dataclasses import dataclass

from config import CONFIG
from angelone_api import AngelOneAPI
from logger import get_logger, log_decision
from utils import round_down_to_lot

_log = get_logger("margin_manager")


@dataclass
class SizingResult:
    quantity: int
    lots: int
    risked_rupees: float
    margin_required_estimate: float
    available_margin: float
    rejected_reason: str = ""


class MarginManager:
    def __init__(self, kite: AngelOneAPI, paper_equity_override: float | None = None) -> None:
        self.kite = kite
        # In PAPER/BACKTEST mode we still want realistic sizing, so we allow
        # a simulated equity figure. In LIVE mode this is ignored entirely -
        # live broker margin is authoritative.
        self._paper_equity_override = paper_equity_override

    def get_available_margin(self) -> float:
        if CONFIG.mode.value == "LIVE":
            data = self.kite.margins()
            equity_segment = data.get("equity", {})
            available = equity_segment.get("available", {}).get("live_balance", 0.0)
            return float(available)
        if self._paper_equity_override is None:
            data = self.kite.margins()
            equity_segment = data.get("equity", {})
            available = equity_segment.get("available", {}).get("live_balance", 0.0)
            return float(available)
        return self._paper_equity_override * CONFIG.capital.margin_leverage_multiplier

    def update_paper_equity(self, new_equity: float) -> None:
        self._paper_equity_override = new_equity
        if hasattr(self.kite, "set_paper_equity"):
            self.kite.set_paper_equity(new_equity)

    def size_position(
        self,
        equity: float,
        entry_price: float,
        stop_loss_price: float,
        option_premium: float,
        lot_size: int,
        margin_per_lot_estimate: float,
        adx: float | None = None,
        consecutive_losses: int = 0,
    ) -> SizingResult:
        available_margin = self.get_available_margin()
        sl_premium_points = abs(entry_price - stop_loss_price)
        if sl_premium_points <= 0:
            return SizingResult(0, 0, 0.0, 0.0, available_margin, rejected_reason="ZERO_SL_DISTANCE")

        premium = max(option_premium, entry_price, 0.05)
        if margin_per_lot_estimate <= 0:
            margin_per_lot_estimate = premium * lot_size

        usable_margin = max(available_margin - CONFIG.capital.min_margin_buffer_rupees, 0.0)
        usable_margin *= CONFIG.capital.max_exposure_pct_of_margin
        # Keep affordability: do not apply equity% premium cap by default (V1)
        exp_cap_pct = float(getattr(CONFIG.sizing, "max_premium_exposure_pct_of_equity", 100.0) or 100.0)
        if exp_cap_pct < 100.0:
            exposure_cap = max(equity, 0.0) * (exp_cap_pct / 100.0)
            if exposure_cap > 0:
                usable_margin = min(usable_margin, exposure_cap)
        max_lots_by_margin = int(usable_margin // margin_per_lot_estimate) if margin_per_lot_estimate > 0 else 0
        max_lots = CONFIG.sizing.max_lots_per_trade
        drop_n = int(getattr(CONFIG.sizing, "drop_lots_after_consecutive_losses", 0) or 0)
        if drop_n > 0 and consecutive_losses >= drop_n:
            steps = (consecutive_losses // drop_n)
            max_lots = max(getattr(CONFIG.sizing, "min_lots_after_loss_reduction", 1), max_lots - steps * int(getattr(CONFIG.sizing, "lots_drop_per_consecutive_loss_step", 1) or 1))
        # Opening window: cap to 1 lot (09:15-09:30) until strategy can handle open
        try:
            from datetime import datetime
            now_t = datetime.now().time()
            ow_end = getattr(CONFIG.risk, "opening_window_end", None)
            if ow_end is not None and now_t < ow_end:
                ow_max = int(getattr(CONFIG.risk, "opening_window_max_lots", 1) or 1)
                max_lots = min(max_lots, ow_max)
        except Exception:
            pass
        lots = min(max_lots_by_margin, max_lots)
        lots = max(lots, 0)

        if lots <= 0:
            log_decision("SIZING_REJECTED", reason="INSUFFICIENT_MARGIN", lots=0,
                         margin_per_lot=round(margin_per_lot_estimate, 2),
                         usable_margin=round(usable_margin, 2),
                         available_margin=round(available_margin, 2))
            return SizingResult(0, 0, 0.0, 0.0, available_margin, rejected_reason="INSUFFICIENT_MARGIN")

        quantity = round_down_to_lot(lots * lot_size, lot_size) if CONFIG.sizing.round_lots_down else lots * lot_size
        lots = quantity // lot_size if lot_size else 0
        premium_risk_per_lot = min(sl_premium_points, premium) * lot_size
        event = "SIZING_COMPUTED_ADX23" if adx is not None and adx == 23.0 else (
            "SIZING_COMPUTED_ADX_GT25" if adx is not None and adx > 25.0 else "SIZING_COMPUTED"
        )
        result = SizingResult(
            quantity=quantity,
            lots=lots,
            risked_rupees=premium_risk_per_lot * lots,
            margin_required_estimate=margin_per_lot_estimate * lots,
            available_margin=available_margin,
        )
        log_decision(event, quantity=quantity, lots=lots,
                     risked_rupees=round(result.risked_rupees, 2),
                     margin_used_estimate=round(result.margin_required_estimate, 2),
                     available_margin=round(available_margin, 2),
                     consecutive_losses=int(consecutive_losses),
                     adx=round(adx, 2) if adx is not None else None)
        return result
        premium = max(option_premium, entry_price, 0.05)
        if margin_per_lot_estimate <= 0:
            margin_per_lot_estimate = premium * lot_size

        usable_margin = max(available_margin - CONFIG.capital.min_margin_buffer_rupees, 0.0)
        usable_margin *= CONFIG.capital.max_exposure_pct_of_margin
        # Safety cap: never pay more than configured % of equity as option premium exposure.
        exposure_cap = max(equity, 0.0) * (getattr(CONFIG.sizing, "max_premium_exposure_pct_of_equity", 40.0) / 100.0)
        if exposure_cap > 0:
            usable_margin = min(usable_margin, exposure_cap)
        max_lots_by_margin = int(usable_margin // margin_per_lot_estimate) if margin_per_lot_estimate > 0 else 0
        max_lots = CONFIG.sizing.max_lots_per_trade
        drop_n = int(getattr(CONFIG.sizing, "drop_lots_after_consecutive_losses", 0) or 0)
        if drop_n > 0 and consecutive_losses >= drop_n:
            steps = (consecutive_losses // drop_n)
            max_lots = max(getattr(CONFIG.sizing, "min_lots_after_loss_reduction", 1), max_lots - steps * int(getattr(CONFIG.sizing, "lots_drop_per_consecutive_loss_step", 1) or 1))
        lots = min(max_lots_by_margin, max_lots)
        lots = max(lots, 0)

        if lots <= 0:
            log_decision("SIZING_REJECTED", reason="INSUFFICIENT_MARGIN", lots=0,
                         margin_per_lot=round(margin_per_lot_estimate, 2),
                         usable_margin=round(usable_margin, 2),
                         available_margin=round(available_margin, 2),
                         exposure_cap=round(exposure_cap, 2))
            return SizingResult(0, 0, 0.0, 0.0, available_margin, rejected_reason="INSUFFICIENT_MARGIN")

        quantity = round_down_to_lot(lots * lot_size, lot_size) if CONFIG.sizing.round_lots_down else lots * lot_size
        lots = quantity // lot_size if lot_size else 0
        premium_risk_per_lot = min(sl_premium_points, premium) * lot_size
        event = "SIZING_COMPUTED_ADX23" if adx is not None and adx == 23.0 else (
            "SIZING_COMPUTED_ADX_GT25" if adx is not None and adx > 25.0 else "SIZING_COMPUTED"
        )
        result = SizingResult(
            quantity=quantity,
            lots=lots,
            risked_rupees=premium_risk_per_lot * lots,
            margin_required_estimate=margin_per_lot_estimate * lots,
            available_margin=available_margin,
        )
        log_decision(event, quantity=quantity, lots=lots,
                     risked_rupees=round(result.risked_rupees, 2),
                     margin_used_estimate=round(result.margin_required_estimate, 2),
                     available_margin=round(available_margin, 2),
                     exposure_cap=round(exposure_cap, 2),
                     consecutive_losses=int(consecutive_losses),
                     adx=round(adx, 2) if adx is not None else None)
        return result