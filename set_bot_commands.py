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
    {"command": "adx", "description": "ADX Live Data"},
    {"command": "login", "description": "Kite login link"},
    {"command": "token", "description": "Save Kite request token: /token <token>"},
    {"command": "panic", "description": "Stop + square-off (use: /panic YES)"},
    {"command": "suggest", "description": "Manual suggestions [ON/OFF]"},
    {"command": "autosuggest", "description": "Auto suggestions [ON/OFF]"},
]
for scope in [{"type": "default"}, {"type": "all_private_chats"}]:
    payload = json.dumps({"commands": commands, "scope": scope}).encode("utf-8")
    req = urllib.request.Request(f"https://api.telegram.org/bot{token}/setMyCommands", data=payload, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            print(scope, resp.read().decode())
    except Exception as e:
        print(scope, e)
