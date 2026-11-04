#!/usr/bin/env python3
import json
import urllib.request
from datetime import datetime
from config import CONFIG

summary = f"""📊 TRADE SUMMARY - ₹20,000 Paper

Time: {datetime.now().strftime('%H:%M:%S')}
Capital: ₹{CONFIG.backtest.starting_equity:,.0f}
Status: Monitoring...

Will send on each trade:
• Trade amount (qty x premium)
• Entry/SL/Target
• PnL on exit
• Daily PnL, Trades, Win Rate
"""
try:
    url = f"https://api.telegram.org/bot{CONFIG.alerting.telegram_bot_token}/sendMessage"
    payload = json.dumps({"chat_id": CONFIG.alerting.telegram_chat_id, "text": summary}).encode("utf-8")
    req = urllib.request.Request(url, data=payload, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=10) as resp:
        print("Sent:", resp.status)
except Exception as e:
    print(e)
