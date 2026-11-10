#!/usr/bin/env python3
"""Minimal Kite redirect callback server for daily login.

Run on the whitelisted/static-IP server behind HTTPS (nginx/caddy).
Register KITE_REDIRECT_URL in the Kite app to point to this endpoint, e.g.:
  https://your-domain/kite/callback
"""
from __future__ import annotations

import json
import os
from pathlib import Path

from flask import Flask, request
from kiteconnect import KiteConnect

from config import CONFIG
from logger import get_logger, send_telegram_alert

_log = get_logger("kite_auth_server")
app = Flask(__name__)


@app.get("/kite/callback")
def kite_callback():
    rt = request.args.get("request_token")
    status = request.args.get("status")
    if not rt or status != "success":
        _log.warning("Kite callback failed: status=%s request_token_present=%s", status, bool(rt))
        return "Kite login failed or cancelled.", 400
    try:
        kite = KiteConnect(api_key=CONFIG.kite.api_key)
        data = kite.generate_session(rt, api_secret=CONFIG.kite.api_secret)
        token = data.get("access_token")
        if not token:
            return "Missing access_token in Kite response.", 500
        p = CONFIG.kite.tokens_path
        p.parent.mkdir(parents=True, exist_ok=True)
        payload = {"access_token": token, "user_id": data.get("user_id"), "login_time": data.get("login_time")}
        p.write_text(json.dumps(payload, default=str))
        os.chmod(p, 0o600)
        send_telegram_alert("KITE_LOGIN", f"Access token saved for {data.get('user_id')}. Restart/reload bot if needed.")
        _log.info("Kite token saved for user %s", data.get("user_id"))
        return "Kite login successful. Token saved. You can close this tab."
    except Exception as exc:
        _log.exception("Kite callback error")
        return f"Kite callback error: {exc}", 500


@app.get("/health")
def health():
    return {"ok": True, "mode": CONFIG.mode.value}


if __name__ == "__main__":
    port = int(os.getenv("KITE_AUTH_PORT", "8088"))
    app.run(host="0.0.0.0", port=port)
