# V1 (saved 2026-11-07)

Paper/live code snapshot (buy-only options, PAPER simulated orders).

## Core rules
- **Signal**: NIFTY 5m/15m closed candles. EMA20/50 direction (LONG→buy ATM CE, SHORT→buy ATM PE), EMA20 not flat, pullback <= 35 pts from EMA20, VWAP crosses <= 10, body/range >= 25%, ATR >= 10.
- **ADX eligibility**: 23.0–32.0 (`adx_min=23`, `adx_max=32`) checked in strategy + `risk.adx_band_allows`.
- **Trades/day**: max 10. No "2nd only if 1st loss". One open position at a time.
- **Entry/exit**: fixed initial SL **10 premium pts**; `use_fixed_target_exit=False`; continuous trailing **start 10 / initial lock 8 / gap 2** premium pts (`enable_trailing_after_breakeven=True`). Force square-off **15:30 IST**.
- **Session**: time gate on, `setup_scan_start=09:20`, no entries when completed bar time >= 15:15 or now > 15:14 (avoids 09:15-bar look-ahead and late entries). Uses completed/closed bars only.
- **Sizing**: margin-based `(available_margin - ₹2000 buffer) * 90%`, rounded down to lot; no 40% equity premium cap; no loss-step lot drop. **Opening window < 09:30: max 1 lot**.
- **Targeted safety**: 15-min **same-contract cooldown after any loss** (`same_contract_cooldown_sec=900`, `can_enter_symbol`/`record_trade_result`). Full anti-chase/option-confirm/pause/halt guards off.
- **Execution**: buy-only (BUY CE/PE, SELL to close), `profit=ltp-entry`, trailing SL moves up only, MFE/MAE logged, entry ADX carried to exit/Telegram.
- **Mode**: PAPER default; LIVE blocked by config safety (`NIFTY_BOT_CONFIRM_LIVE=YES` + deployment prerequisites).

## 1-week replay reference (2026-10-31–2026-11-06, simulated 5m, buy-only)
Targeted-fixes replay: net **+₹25,014.95** (₹20k→₹45,014.94), 16 trades, 9W/7L, `TRAIL 9 +30,421.71 / SL 7 -5,406.76`. See `/tmp/week_replay_targeted_fixes.json`. Earlier V1-style replay (pre targeted fixes): `/tmp/week_replay_V1_2026-10-31_2026-11-06.json`. *Simulated; not live fills/profit guarantee.*
