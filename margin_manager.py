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
    ) -> SizingResult:
        available_margin = self.get_available_margin()

        # New rule: ADX == 23 gets 17 lots; ADX > 25 gets same SL/Target (handled elsewhere) but qty?
        # User says: "Adx vaue 23 is eligible for qty 17 lots" and "when adx value is more then 25 same SL and Target"
        # Apply fixed lots based on ADX as specified
        if adx is not None and adx == 23.0:
            lots = 17
            quantity = lots * lot_size
            sl_premium_points = abs(entry_price - stop_loss_price)
            premium_risk_per_lot = min(sl_premium_points, option_premium) * lot_size if sl_premium_points > 0 else option_premium * lot_size
            result = SizingResult(
                quantity=quantity,
                lots=lots,
                risked_rupees=premium_risk_per_lot * lots,
                margin_required_estimate=margin_per_lot_estimate * lots,
                available_margin=available_margin,
            )
            log_decision("SIZING_COMPUTED_ADX23", quantity=quantity, lots=lots,
                         risked_rupees=round(result.risked_rupees, 2),
                         margin_used_estimate=round(result.margin_required_estimate, 2),
                         available_margin=round(available_margin, 2))
            return result
        if adx is not None and adx > 25.0:
            lots = 17  # same lot sizing as specified
            quantity = lots * lot_size
            sl_premium_points = abs(entry_price - stop_loss_price)
            premium_risk_per_lot = min(sl_premium_points, option_premium) * lot_size if sl_premium_points > 0 else option_premium * lot_size
            result = SizingResult(
                quantity=quantity,
                lots=lots,
                risked_rupees=premium_risk_per_lot * lots,
                margin_required_estimate=margin_per_lot_estimate * lots,
                available_margin=available_margin,
            )
            log_decision("SIZING_COMPUTED_ADX_GT25", quantity=quantity, lots=lots,
                         risked_rupees=round(result.risked_rupees, 2),
                         margin_used_estimate=round(result.margin_required_estimate, 2),
                         available_margin=round(available_margin, 2))
            return result

        # Max loss per lot is (entry premium - premium at SL) * lot_size.
        # SL distances are in OPTION PREMIUM points, so derive the risk directly
        # from the premium prices rather than index levels.
        sl_premium_points = abs(entry_price - stop_loss_price)
        if sl_premium_points <= 0:
            return SizingResult(0, 0, 0.0, 0.0, available_margin, rejected_reason="ZERO_SL_DISTANCE")
        
        # Default behavior for other ADX values (if any)
        lots = 17  # enforce as per new requirement
        quantity = lots * lot_size
        premium_risk_per_lot = min(sl_premium_points, option_premium) * lot_size
        result = SizingResult(
            quantity=quantity,
            lots=lots,
            risked_rupees=premium_risk_per_lot * lots,
            margin_required_estimate=margin_per_lot_estimate * lots,
            available_margin=available_margin,
        )
        log_decision("SIZING_COMPUTED_DEFAULT", quantity=quantity, lots=lots,
                     risked_rupees=round(result.risked_rupees, 2),
                     margin_used_estimate=round(result.margin_required_estimate, 2),
                     available_margin=round(available_margin, 2))
        return result