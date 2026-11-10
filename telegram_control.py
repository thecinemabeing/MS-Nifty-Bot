#!/usr/bin/env python3
import json
import socket
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from config import CONFIG
from logger import get_logger

_log = get_logger("telegram_control")

CONTROL_FILE = Path.home() / ".nifty_bot" / "bot_control.json"
PANIC_FILE = Path.home() / ".nifty_bot" / "panic.flag"
CONTROL_FILE.parent.mkdir(parents=True, exist_ok=True)

_suggestion_bot = None
_auto_suggest_enabled = True
_manual_suggest_enabled = True


def set_suggestion_bot(bot):
    global _suggestion_bot
    _suggestion_bot = bot


def _telegram_api(method, payload=None, timeout=35):
    token = CONFIG.alerting.telegram_bot_token
    if not token:
        return None, "NO_TOKEN"
    url = f"https://api.telegram.org/bot{token}/{method}"
    try:
        data = json.dumps(payload).encode("utf-8") if payload is not None else None
        headers = {"Content-Type": "application/json"} if data is not None else {}
        req = urllib.request.Request(url, data=data, headers=headers)
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode()), None
    except urllib.error.HTTPError as e:
        body = ""
        try:
            body = e.read().decode("utf-8", errors="replace")
        except Exception:
            pass
        desc = body
        try:
            desc = json.loads(body).get("description") or body
        except Exception:
            pass
        return None, f"HTTP_{e.code}:{desc}"
    except (TimeoutError, socket.timeout):
        return None, "TIMEOUT"
    except Exception as e:
        return None, str(e)


def _get_updates(offset=None):
    params = {"timeout": 30}
    if offset is not None:
        params["offset"] = offset
    token = CONFIG.alerting.telegram_bot_token
    if not token:
        return {"ok": False}
    url = f"https://api.telegram.org/bot{token}/getUpdates?{urllib.parse.urlencode(params)}"
    try:
        req = urllib.request.Request(url)
        with urllib.request.urlopen(req, timeout=40) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        body = ""
        try:
            body = e.read().decode("utf-8", errors="replace")
        except Exception:
            pass
        desc = body
        try:
            desc = json.loads(body).get("description") or body
        except Exception:
            pass
        _log.warning("getUpdates failed: HTTP %s %s", e.code, desc)
        return {"ok": False, "error_code": e.code, "description": desc}
    except (TimeoutError, socket.timeout):
        return {"ok": False, "timeout": True}
    except Exception as e:
        _log.warning("getUpdates failed: %s", e)
        return {"ok": False}


def _send_message(text, chat_id=None):
    token = CONFIG.alerting.telegram_bot_token
    chat_id = chat_id or CONFIG.alerting.telegram_chat_id
    if not token or not chat_id:
        return False
    # Telegram message limit is 4096 chars
    if text and len(text) > 4096:
        text = text[:4090] + "\n..."
    payload = {"chat_id": chat_id, "text": text}
    data, err = _telegram_api("sendMessage", payload, timeout=15)
    if err:
        _log.warning("sendMessage failed: %s", err)
        return False
    return bool(data and data.get("ok"))


def write_control(state):
    try:
        CONTROL_FILE.write_text(json.dumps({"state": state, "updated_at": time.time()}))
    except Exception:
        pass


def read_control():
    if not CONTROL_FILE.exists():
        return {"state": "RUNNING"}
    try:
        return json.loads(CONTROL_FILE.read_text())
    except Exception:
        return {"state": "RUNNING"}


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


def request_panic():
    try:
        PANIC_FILE.write_text(json.dumps({"ts": time.time()}))
    except Exception:
        pass


def clear_panic():
    try:
        if PANIC_FILE.exists():
            PANIC_FILE.unlink()
    except Exception:
        pass


def panic_requested():
    return PANIC_FILE.exists()


RESTART_FILE = Path.home() / ".nifty_bot" / "restart_requested"


def request_restart():
    """Signal the running bot to shut down gracefully. A supervisor
    (launchd KeepAlive) restarts main.py automatically, so this is a
    one-command bot restart from Telegram."""
    try:
        RESTART_FILE.write_text("1")
    except Exception:
        pass
    _send_message("🔄 Restart requested - shutting down bot now (supervisor will relaunch).")
    try:
        import os as _os, signal as _signal
        time.sleep(1.0)
        _os.kill(_os.getpid(), _signal.SIGTERM)
    except Exception:
        _log.exception("request_restart: failed to signal self")


def restart_requested():
    return RESTART_FILE.exists()


def clear_restart():
    try:
        if RESTART_FILE.exists():
            RESTART_FILE.unlink()
    except Exception:
        pass


def _token_status():
    try:
        tp = CONFIG.kite.tokens_path
        if not tp.exists():
            return "missing"
        data = json.loads(tp.read_text())
        if data.get("access_token"):
            return "saved"
        return "empty"
    except Exception:
        return "?"


def _set_my_commands():
    """Register bot commands with Telegram so the slash-menu pops up
    automatically when the user types '/' in the chat bar.
    Scoped to the allowed chat so the menu is correct even if the bot
    is added to other chats."""
    allowed = str(CONFIG.alerting.telegram_chat_id)
    commands = [
        {"command": "start", "description": "Trading ON"},
        {"command": "stop", "description": "Trading OFF"},
        {"command": "status", "description": "Status"},
        {"command": "pnl", "description": "PnL"},
        {"command": "adx", "description": "ADX Live"},
        {"command": "login", "description": "Kite login link"},
        {"command": "token", "description": "Save daily Kite token"},
        {"command": "restart", "description": "Restart bot"},
        {"command": "panic", "description": "Stop + square-off"},
        {"command": "autosuggest", "description": "Auto [ON/OFF]"},
    ]
    payload = {"commands": commands}
    if allowed:
        payload["scope"] = {"type": "chat", "chat_id": allowed}
    res, err = _telegram_api("setMyCommands", payload, timeout=15)
    if err:
        _log.warning("setMyCommands failed: %s", err)
    else:
        _log.info("setMyCommands registered: %d commands%s", len(commands), f" scope=chat {allowed}" if allowed else "")
    return res, err


def register_commands():
    """Public helper - re-register the slash menu on demand
    (e.g. after adding a new command, without restarting the bot)."""
    res, err = _set_my_commands()
    if isinstance(res, dict):
        return res.get("ok", False), err
    return bool(res), err


def start_control_loop():
    _log.info("Telegram command listener started. Commands: /start /stop /status /pnl /adx /login /token /panic /restart /autosuggest")
    write_control("RUNNING")
    allowed = str(CONFIG.alerting.telegram_chat_id)
    _set_my_commands()
    # Drop stale pending updates on startup (prevents replay storms / stale backlog) and clear webhook if set.
    _telegram_api("deleteWebhook", {"drop_pending_updates": True}, timeout=15)
    _send_message(
        "🤖 Bot Control Ready\n"
        "/start - Trading ON\n/stop - Trading OFF\n/status - Status\n/pnl - PnL\n/adx - ADX Live\n"
        "/login - Kite login link\n/token <request_token> - Save daily Kite token\n/panic YES - Stop + square-off\n"
        "/restart - Restart bot (supervisor relaunches)\n/autosuggest - Auto [ON/OFF]"
    )
    offset = None
    conflict_backoff = 5
    while True:
        try:
            updates = _get_updates(offset)
            if updates.get("error_code") == 409:
                _log.warning("Telegram getUpdates 409 conflict (another poller/webhook). Backing off %ss; ensure only one bot instance runs.", conflict_backoff)
                time.sleep(conflict_backoff)
                conflict_backoff = min(conflict_backoff * 2, 120)
                continue
            conflict_backoff = 5
            if updates.get("timeout"):
                continue
            if updates.get("ok") and updates.get("result"):
                for upd in updates["result"]:
                    offset = upd["update_id"] + 1
                    msg = upd.get("message") or upd.get("edited_message")
                    if not msg:
                        continue
                    chat_id = str(msg.get("chat", {}).get("id"))
                    if chat_id != allowed:
                        continue
                    raw = (msg.get("text") or "").strip()
                    text = raw.lower()

                    if text in ["/start", "/start_trading", "/resume", "start"]:
                        clear_panic()
                        write_control("RUNNING")
                        _send_message("✅ Trading ENABLED")
                    elif text in ["/stop", "/stop_trading", "/pause", "stop"]:
                        write_control("STOPPED")
                        _send_message("⛔ Trading DISABLED - no new trades")
                    elif text in ["/status", "status"]:
                        st = read_control().get("state")
                        try:
                            import dashboard
                            s = dashboard.get_state()
                            _send_message(
                                f"📊 {st} | {s.get('mode','-')}\n"
                                f"Pos: {s.get('position') or 'none'} | Trades: {s.get('trades_today') or 0}\n"
                                f"PnL: ₹{(s.get('daily_pnl') or 0):.2f} | NIFTY: {s.get('nifty_spot','-')}\n"
                                f"Token: {_token_status()} | Panic: {'YES' if panic_requested() else 'no'}"
                            )
                        except Exception:
                            _send_message(f"📊 Trading Status: {st}")
                    elif text in ["/pnl", "pnl", "/report"]:
                        try:
                            import dashboard
                            s = dashboard.get_state()
                            _send_message(
                                f"📈 PnL: ₹{(s.get('daily_pnl') or 0):.2f}\n"
                                f"Trades: {s.get('trades_today') or 0} | Win%: {(s.get('win_rate') or 0):.1f}\n"
                                f"Margin: ₹{(s.get('available_margin') or 0):,.0f} | {s.get('mode','-')}"
                            )
                        except Exception:
                            _send_message("📈 Check dashboard/logs for PnL")
                    elif text in ["/adx", "/adxlive", "adx"]:
                        try:
                            import dashboard
                            s = dashboard.get_state()
                            adx = s.get('adx'); atr = s.get('atr'); spot = s.get('nifty_spot')
                            adx_s = f"{adx:.2f}" if adx is not None else "N/A (warming up)"
                            atr_s = f"{atr:.2f}" if atr is not None else "N/A"
                            spot_s = f"{spot:.2f}" if spot is not None else "N/A"
                            band = "IN (23-32)" if (adx is not None and 23 <= adx <= 32) else ("LOW <23" if adx is not None and adx < 23 else ("HIGH >32" if adx is not None and adx > 32 else "N/A"))
                            _send_message(
                                f"📊 ADX Live\nNIFTY: {spot_s} | Trend: {s.get('trend') or '-'}\n"
                                f"ADX(14): {adx_s} [{band}]\nATR(14): {atr_s}\n"
                                f"EMA20: {s.get('ema20','-')} | EMA50: {s.get('ema50','-')}\n"
                                f"VWAP: {s.get('vwap','-')}\nS1: {s.get('s1','-')} | R1: {s.get('r1','-')}"
                            )
                        except Exception as e:
                            _log.warning("adx cmd failed: %s", e)
                            _send_message("ADX unavailable")
                    elif text in ["/login", "login"]:
                        try:
                            from kiteconnect import KiteConnect
                            kite = KiteConnect(api_key=CONFIG.kite.api_key)
                            url = kite.login_url()
                            redir = getattr(CONFIG.kite, 'redirect_url', '') or '(KITE_REDIRECT_URL not set)'
                            _send_message(
                                f"🔐 Kite Login\n{url}\n\nRedirect: {redir}\n"
                                "After login copy request_token from redirect URL and send:\n"
                                "/token <request_token>\n"
                                "Token is used once to generate access token (saved on this machine). "
                                "Keep secrets in env; do not share token publicly."
                            )
                        except Exception as e:
                            _send_message(f"Login error: {e}")
                    elif text.startswith("/token ") or text.startswith("/token@"):
                        try:
                            parts = raw.split(None, 1)
                            req_token = parts[1].strip() if len(parts) > 1 else ""
                            # accept if user pasted full redirect URL
                            if "request_token=" in req_token:
                                from urllib.parse import urlparse, parse_qs
                                q = parse_qs(urlparse(req_token).query)
                                req_token = (q.get("request_token") or [""])[0]
                            if not req_token:
                                _send_message("Usage: /token <request_token> (or paste redirect URL)")
                            elif not CONFIG.kite.api_secret:
                                _send_message("KITE_API_SECRET missing in env.")
                            else:
                                from kiteconnect import KiteConnect
                                kite = KiteConnect(api_key=CONFIG.kite.api_key)
                                data = kite.generate_session(req_token, api_secret=CONFIG.kite.api_secret)
                                access = data.get("access_token")
                                if not access:
                                    _send_message("Login failed: no access_token returned.")
                                else:
                                    tp = CONFIG.kite.tokens_path
                                    tp.parent.mkdir(parents=True, exist_ok=True)
                                    import os
                                    tp.write_text(json.dumps({"access_token": access, "user_id": data.get("user_id"), "login_time": str(data.get("login_time",""))}))
                                    os.chmod(tp, 0o600)
                                    # also update .env KITE_ACCESS_TOKEN for current project convenience
                                    try:
                                        from pathlib import Path as _P
                                        ep = _P(__file__).resolve().parent / ".env"
                                        if ep.exists():
                                            import re as _re
                                            t = ep.read_text()
                                            if "KITE_ACCESS_TOKEN=" in t:
                                                t = _re.sub(r'^KITE_ACCESS_TOKEN=.*$', f'KITE_ACCESS_TOKEN={access}', t, flags=_re.M)
                                            else:
                                                t += f"\nKITE_ACCESS_TOKEN={access}\n"
                                            ep.write_text(t)
                                    except Exception:
                                        pass
                                    _send_message(f"✅ Kite token saved for {data.get('user_id')}. Restart bot if it was using an old token.")
                                    _log.info("Kite token saved via Telegram for %s", data.get("user_id"))
                        except Exception as e:
                            _log.exception("token command failed")
                            _send_message(f"Token error: {e}")
                    elif text == "/panic yes":
                        write_control("STOPPED")
                        request_panic()
                        _send_message("🚨 PANIC: trading STOPPED, square-off requested. Verify positions/orders.")
                    elif text.startswith("/panic"):
                        _send_message("⚠️ Confirm with: /panic YES")
                    elif text in ["/autosuggest", "/autosuggestion", "/auto_suggestion", "/auto"]:
                        cur = read_suggest_control().get("state")
                        if cur == "ON":
                            write_suggest_control("OFF")
                            _send_message("Auto suggestions: OFF")
                        else:
                            write_suggest_control("ON")
                            _send_message("Auto suggestions: ON")
                    elif text in ["/restart", "/restart_bot"]:
                        if restart_requested():
                            _send_message("🔄 Restart already requested - waiting for supervisor to relaunch.")
                        else:
                            request_restart()
            time.sleep(2)
        except Exception as e:
            _log.warning("control loop error: %s", e)
            time.sleep(5)
