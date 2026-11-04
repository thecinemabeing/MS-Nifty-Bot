#!/usr/bin/env python3
import json
import time
import urllib.request
from pathlib import Path

from config import CONFIG
from logger import get_logger

_log = get_logger("telegram_control")

CONTROL_FILE = Path.home() / ".nifty_bot" / "bot_control.json"
CONTROL_FILE.parent.mkdir(parents=True, exist_ok=True)


# Global reference for suggestion bot
_suggestion_bot = None


def set_suggestion_bot(bot):
    global _suggestion_bot
    _suggestion_bot = bot


def _get_updates(offset=None):
    token = CONFIG.alerting.telegram_bot_token
    if not token:
        return {"ok": False}
    url = f"https://api.telegram.org/bot{token}/getUpdates"
    if offset:
        url += f"?offset={offset}&timeout=30"
    else:
        url += "?timeout=30"
    try:
        req = urllib.request.Request(url)
        with urllib.request.urlopen(req, timeout=35) as resp:
            return json.loads(resp.read().decode())
    except Exception as e:
        _log.warning("getUpdates failed: %s", e)
        return {"ok": False}


def _send_message(text):
    token = CONFIG.alerting.telegram_bot_token
    chat_id = CONFIG.alerting.telegram_chat_id
    if not token or not chat_id:
        return
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = json.dumps({"chat_id": chat_id, "text": text}).encode("utf-8")
    req = urllib.request.Request(url, data=payload, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            pass
    except Exception as e:
        _log.warning("sendMessage failed: %s", e)


def write_control(state):
    data = {"state": state, "updated_at": time.time()}
    try:
        CONTROL_FILE.write_text(json.dumps(data))
    except Exception:
        pass


def read_control():
    if not CONTROL_FILE.exists():
        return {"state": "RUNNING"}
    try:
        return json.loads(CONTROL_FILE.read_text())
    except Exception:
        return {"state": "RUNNING"}


SUGGEST_CONTROL_FILE = Path.home() / ".nifty_bot" / "suggest_control.json"


def read_suggest_control():
    global _auto_suggest_enabled
    return {"state": "ON" if _auto_suggest_enabled else "OFF"}


def write_suggest_control(state):
    global _auto_suggest_enabled
    _auto_suggest_enabled = (state == "ON")


def read_manual_suggest_control():
    global _manual_suggest_enabled
    return {"state": "ON" if _manual_suggest_enabled else "OFF"}


def write_manual_suggest_control(state):
    global _manual_suggest_enabled
    _manual_suggest_enabled = (state == "ON")


def start_control_loop():
    _log.info("Telegram command listener started. Commands: /start /stop /status /pnl /suggest /autosuggest")
    write_control("RUNNING")
    _send_message("🤖 Bot Control Ready\n/start - Trading ON\n/stop - Trading OFF\n/status - Status\n/pnl - PnL\n/suggest - Manual [ON/OFF]\n/autosuggest - Auto [ON/OFF]")
    offset = None
    allowed = str(CONFIG.alerting.telegram_chat_id)
    while True:
        try:
            updates = _get_updates(offset)
            if updates.get("ok") and updates.get("result"):
                for upd in updates["result"]:
                    offset = upd["update_id"] + 1
                    msg = upd.get("message") or upd.get("edited_message")
                    if not msg:
                        continue
                    chat_id = str(msg.get("chat", {}).get("id"))
                    if chat_id != allowed:
                        continue
                    text = (msg.get("text") or "").strip().lower()
                    if text in ["/start", "/start_trading", "/resume", "start"]:
                        write_control("RUNNING")
                        _send_message("✅ Trading ENABLED - Bot will take trades")
                    elif text in ["/stop", "/stop_trading", "/pause", "stop"]:
                        write_control("STOPPED")
                        _send_message("⛔ Trading DISABLED - No new trades will be taken")
                    elif text in ["/status", "status"]:
                        st = read_control().get("state")
                        _send_message(f"📊 Trading Status: {st}")
                    elif text in ["/pnl", "pnl", "/report"]:
                        _send_message("📈 Check dashboard/logs for PnL")
                    elif text in ["/autosuggest", "/autosuggestion", "/auto_suggestion"]:
                        cur = read_suggest_control().get("state")
                        if cur == "ON":
                            write_suggest_control("OFF")
                            _send_message("Auto: OFF")
                        else:
                            write_suggest_control("ON")
                            _send_message("Auto: ON")
                    elif text in ["/suggest", "/sug"]:
                        ms = read_manual_suggest_control()
                        if ms.get("state") == "OFF":
                            write_manual_suggest_control("ON")
                            _send_message("Manual: ON")
                        else:
                            write_manual_suggest_control("OFF")
                            _send_message("Manual: OFF")
            time.sleep(2)
        except Exception as e:
            _log.warning("control loop error: %s", e)
            time.sleep(5)
