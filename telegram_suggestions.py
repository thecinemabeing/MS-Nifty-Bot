#!/usr/bin/env python3
import json
import time
import urllib.request
from datetime import datetime
from pathlib import Path

from config import CONFIG
from logger import get_logger
from indicators import ema_series, adx, atr
from telegram_control import read_suggest_control

_log = get_logger("telegram_suggestions")

SUGGESTION_FILE = Path.home() / ".nifty_bot" / "last_suggestion.json"


def _send_message(text):
    token = CONFIG.alerting.telegram_bot_token
    chat_id = CONFIG.alerting.telegram_chat_id
    if not token or not chat_id:
        return False
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = json.dumps({"chat_id": chat_id, "text": text, "parse_mode": "HTML"}).encode("utf-8")
    req = urllib.request.Request(url, data=payload, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status == 200
    except Exception as e:
        _log.warning("send failed: %s", e)
        return False


class TradeSuggestionBot:
    def __init__(self, kite=None, candles=None, strategy=None):
        self.kite = kite
        self.candles = candles
        self.strategy = strategy
        self.last_suggest_ts = 0
        self.suggest_interval = 300  # 5 min

    def analyze_and_suggest(self, spot_token):
        try:
            sg = read_suggest_control()
            if sg.get("state") == "OFF":
                return
            ms = None
            try:
                from telegram_control import read_manual_suggest_control
                ms = read_manual_suggest_control()
                if ms.get("state") == "OFF":
                    return
            except Exception:
                pass
            now = time.time()
            if now - self.last_suggest_ts < self.suggest_interval:
                return
            self.last_suggest_ts = now

            closed_5m = self.candles.get_closed_candles(spot_token, CONFIG.session.primary_timeframe_min)
            if not closed_5m or len(closed_5m) < 50:
                return

            closes = [c.close for c in closed_5m]
            ema20 = ema_series(closes, CONFIG.indicators.ema_fast)
            ema50 = ema_series(closes, CONFIG.indicators.ema_slow)
            if not ema20 or not ema50:
                return
            ema20_val = ema20[-1]
            ema50_val = ema50[-1]
            adx_val = adx(closed_5m, CONFIG.indicators.adx_period)
            atr_val = atr(closed_5m, CONFIG.indicators.atr_period)
            price = closes[-1]

            trend = "BULLISH" if ema20_val > ema50_val else "BEARISH" if ema20_val < ema50_val else "NEUTRAL"

            msg = []
            msg.append(f"<b>🔍 TRADE SUGGESTION - {datetime.now().strftime('%H:%M:%S')}</b>")
            msg.append(f"NIFTY: {price:.2f} | Trend: {trend}")
            msg.append(f"EMA20: {ema20_val:.2f} | EMA50: {ema50_val:.2f}")
            msg.append(f"ADX: {adx_val:.1f} if adx_val else 'N/A' | ATR: {atr_val:.1f} if atr_val else 'N/A'")
            msg.append("")

            if adx_val and adx_val >= 23 and adx_val <= 32:
                if trend == "BULLISH" and price >= ema20_val - 35:
                    msg.append("<b>✅ SETUP MATCHING - LONG (CE)</b>")
                    msg.append(f"Entry Zone: ~{price:.2f}")
                    msg.append(f"SL: {price - 10:.2f} | Target: {price + 10:.2f}")
                    msg.append(f"Qty Suggestion: 17 lots (ADX {adx_val:.1f})")
                    msg.append("Type /auto to enable auto-trading or /stop to disable")
                elif trend == "BEARISH" and price <= ema20_val + 35:
                    msg.append("<b>✅ SETUP MATCHING - SHORT (PE)</b>")
                    msg.append(f"Entry Zone: ~{price:.2f}")
                    msg.append(f"SL: {price + 10:.2f} | Target: {price - 10:.2f}")
                    msg.append(f"Qty Suggestion: 17 lots (ADX {adx_val:.1f})")
                    msg.append("Type /auto to enable auto-trading or /stop to disable")
                else:
                    msg.append("⏳ Setup forming - Waiting for pullback near EMA20")
            elif adx_val and adx_val < 23:
                msg.append(f"⚠️ ADX {adx_val:.1f} < 23 - Weak trend, no trade")
            elif adx_val and adx_val > 32:
                msg.append(f"⚠️ ADX {adx_val:.1f} > 32 - Overextended, no trade")
            else:
                msg.append("⏳ Analyzing...")

            msg.append("")
            msg.append("<i>Auto-trade uses same rules | Max 2 trades/day</i>")
            _send_message("\n".join(msg))
        except Exception as e:
            _log.warning("suggestion error: %s", e)
