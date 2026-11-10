#!/usr/bin/env python3
import json
import urllib.request
from config import CONFIG

token = CONFIG.alerting.telegram_bot_token
commands = [
    {"command": "start", "description": "Trading ON"},
    {"command": "stop", "description": "Trading OFF"},
    {"command": "status", "description": "Status"},
    {"command": "pnl", "description": "PnL"},
    {"command": "suggest", "description": "Manual [ON/OFF]"},
    {"command": "autosuggest", "description": "Auto [ON/OFF]"},
]
url = f"https://api.telegram.org/bot{token}/setMyCommands"
data = {"commands": commands}
req = urllib.request.Request(url, data=json.dumps(data).encode(), headers={"Content-Type": "application/json"}, method="POST")
try:
    with urllib.request.urlopen(req) as r:
        print(r.read())
except Exception as e:
    print(e)
