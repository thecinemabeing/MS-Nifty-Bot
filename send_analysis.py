#!/usr/bin/env python3
import json
import urllib.request
from datetime import datetime
from config import CONFIG

# Generate analysis for ₹20k paper trade
analysis = f"""📊 NIFTY Bot Analysis - ₹20,000 Paper Trade

Date: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}

💰 Capital Settings:
• Starting Equity: ₹{CONFIG.backtest.starting_equity:,.0f}
• Risk per trade: {CONFIG.sizing.risk_per_trade_pct_of_equity}%
• Max lots per trade: {CONFIG.sizing.max_lots_per_trade}
• ADX >=23 eligible for 17 lots

📈 Strategy Rules:
• Max trades/day: 2
• Trade 2 only if Trade 1 lost
• SL: {CONFIG.trade_mgmt.fixed_sl_premium_points} pts | Target: {CONFIG.trade_mgmt.fixed_target_premium_points} pts
• ADX range: 23-32

🎯 Expected with ₹20k:
• With 17 lots on NIFTY options, margin requirement varies
• Risk per trade ~₹1000 (5% of 20k)
• Can take max 2 trades/day as per rules

Mode: PAPER (Live data, simulated orders)
"""

try:
    url = f"https://api.telegram.org/bot{CONFIG.alerting.telegram_bot_token}/sendMessage"
    payload = json.dumps({"chat_id": CONFIG.alerting.telegram_chat_id, "text": analysis}).encode("utf-8")
    req = urllib.request.Request(url, data=payload, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=10) as resp:
        print("Sent:", resp.status)
except Exception as e:
    print(e)
