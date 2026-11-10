"""
session_dashboard.py
====================
Separate dashboard for session-wise trade analytics.
Serves on a different port than main dashboard.
Reads from trade_journal.jsonl - no strategy/logic dependencies.
"""

from __future__ import annotations

import json
import threading
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from config import CONFIG
from logger import get_logger
from trade_journal import _get_log_dir

_log = get_logger("session_dashboard")

_STATE_LOCK = threading.Lock()
_SERVER: ThreadingHTTPServer | None = None


def _parse_trades() -> list[dict]:
    """Parse all completed trades from the journal, grouped by date."""
    path = _get_log_dir() / "trades.jsonl"
    if not path.exists():
        return []

    trades = []
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                t = json.loads(line)
                if t.get("event") == "EXIT" and "pnl" in t:
                    ts_str = t.get("ts") or t.get("exit_time")
                    if ts_str:
                        try:
                            dt = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
                        except Exception:
                            continue
                        t["_dt"] = dt
                        t["_date"] = dt.date().isoformat()
                        trades.append(t)
            except json.JSONDecodeError:
                continue
    return trades


def _compute_daily_stats(trades: list[dict]) -> dict[str, Any]:
    """Compute per-date stats."""
    by_date: dict[str, dict] = {}

    for t in trades:
        date = t["_date"]
        pnl = float(t.get("pnl", 0))

        if date not in by_date:
            by_date[date] = {
                "trades": 0,
                "wins": 0,
                "losses": 0,
                "total_pnl": 0.0,
                "win_pnl": 0.0,
                "loss_pnl": 0.0,
            }

        s = by_date[date]
        s["trades"] += 1
        s["total_pnl"] += pnl
        if pnl > 0:
            s["wins"] += 1
            s["win_pnl"] += pnl
        else:
            s["losses"] += 1
            s["loss_pnl"] += pnl

    return by_date


def _generate_html(stats: dict[str, Any]) -> str:
    """Generate complete HTML page with session-wise report (server-side rendered)."""
    # Compute totals
    total_trades = 0
    total_wins = 0
    total_losses = 0
    total_pnl = 0.0

    rows = []
    for date in sorted(stats.keys(), reverse=True):
        s = stats[date]
        date_total_pnl = s["total_pnl"]
        date_total_trades = s["trades"]
        date_wins = s["wins"]
        date_losses = s["losses"]

        total_trades += date_total_trades
        total_wins += date_wins
        total_losses += date_losses
        total_pnl += date_total_pnl

        pnl_color = "#37f26b" if date_total_pnl >= 0 else "#ff4444"
        win_rate = (s["wins"] / s["trades"] * 100) if s["trades"] > 0 else 0
        rows.append(f"""
            <tr style="cursor:pointer;" onclick="openDayModal('{date}', {s["trades"]}, {s["wins"]}, {s["losses"]}, {win_rate:.1f}, {s["win_pnl"]:.2f}, {s["loss_pnl"]:.2f}, {date_total_pnl:.2f})">
                <td style="color:#37f26b;text-decoration:underline;">{date}</td>
                <td>{s["trades"]}</td>
                <td>{s["wins"]}</td>
                <td>{s["losses"]}</td>
                <td>{win_rate:.1f}%</td>
                <td style="color:#37f26b;">₹{s["win_pnl"]:,.2f}</td>
                <td style="color:#ff4444;">₹{s["loss_pnl"]:,.2f}</td>
                <td style="color:{pnl_color};font-weight:bold;">₹{s["total_pnl"]:,.2f}</td>
            </tr>""")

    if not rows:
        table_rows = '<tr><td colspan="8" style="text-align:center;padding:40px;color:#777;">No trades recorded yet</td></tr>'
    else:
        table_rows = "".join(rows)

    # Summary values
    win_rate_pct = (total_wins / total_trades * 100) if total_trades > 0 else 0
    total_pnl_color = "#37f26b" if total_pnl >= 0 else "#ff4444"

    html = f"""<!DOCTYPE html>
<html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>NIFTY Session Analytics</title>
<style>
body{{background:#0b0f0b;color:#37f26b;font-family:'Courier New',monospace;padding:20px;margin:0;}}
h1{{color:#37f26b;border-bottom:1px solid #37f26b;padding-bottom:8px;font-size:1.4rem;}}
table{{width:100%;border-collapse:collapse;margin-top:10px;font-size:13px;}}
th,td{{padding:6px 8px;border-bottom:1px solid #123;text-align:right;}}
th{{color:#7fdc9a;background:#0d120d;position:sticky;top:0;}}
td:first-child,th:first-child{{text-align:left;}}
tr:hover{{background:#122;}}
.refresh-btn{{background:#37f26b;color:#0b0f0b;border:none;padding:10px 20px;font-family:inherit;font-weight:bold;margin:10px 0;cursor:pointer;}}
.refresh-btn:active{{background:#2ad95b;}}
.summary{{background:#0d120d;padding:12px;border:1px solid #123;margin-bottom:16px;border-radius:4px;}}
.summary-grid{{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:10px;}}
.summary-item{{text-align:center;}}
.summary-label{{color:#7fdc9a;font-size:11px;}}
.summary-value{{font-size:18px;font-weight:bold;}}
.positive{{color:#37f26b;}}
.negative{{color:#ff4444;}}
@media (max-width:600px){{table,th,td{{font-size:11px;padding:4px 6px;}}}}
#modal table{{font-size:12px;}}
#modal th, #modal td{{padding:5px 7px;}}
#modal th{{background:#0d120d;}}
</style></head><body>
<h1>📊 NIFTY Daily Analytics</h1>
<div class="summary">
    <div class="summary-grid">
        <div class="summary-item"><div class="summary-label">Total Trades</div><div class="summary-value">{total_trades}</div></div>
        <div class="summary-item"><div class="summary-label">Wins</div><div class="summary-value positive">{total_wins}</div></div>
        <div class="summary-item"><div class="summary-label">Losses</div><div class="summary-value negative">{total_losses}</div></div>
        <div class="summary-item"><div class="summary-label">Win Rate</div><div class="summary-value">{win_rate_pct:.1f}%</div></div>
        <div class="summary-item"><div class="summary-label">Net P&L</div><div class="summary-value" style="color:{total_pnl_color};">₹{total_pnl:,.2f}</div></div>
    </div>
</div>
<button class="refresh-btn" onclick="location.reload()">🔄 Refresh</button>
<table>
<thead>
<tr>
    <th>Date</th><th>Trades</th><th>Wins</th><th>Losses</th><th>Win%</th>
    <th>Win P&L</th><th>Loss P&L</th><th>Net P&L</th>
</tr>
</thead>
<tbody>{table_rows}</tbody>
</table>
<div id="modal" style="display:none;position:fixed;top:0;left:0;width:100%;height:100%;background:rgba(0,0,0,0.85);z-index:1000;overflow:auto;">
    <div style="background:#0b0f0b;color:#37f26b;margin:40px auto;padding:20px;border:1px solid #37f26b;max-width:98%;border-radius:6px;">
        <div style="display:flex;justify-content:space-between;align-items:center;">
            <h2 id="modal-title" style="margin:0;"></h2>
            <button onclick="closeModal()" style="background:#ff4444;color:#fff;border:none;padding:8px 16px;font-family:inherit;font-weight:bold;cursor:pointer;">✕ Close</button>
        </div>
        <div id="modal-summary" class="summary" style="margin:12px 0;"></div>
        <div id="modal-table" style="max-height:70vh;overflow:auto;"></div>
    </div>
</div>
<script>
async function openDayModal(date, trades, wins, losses, winRate, winPnl, lossPnl, netPnl) {{
    const res = await fetch('/api/trades/' + date);
    const dayTrades = await res.json();
    const pnlColor = netPnl >= 0 ? '#37f26b' : '#ff4444';
    document.getElementById('modal-title').innerHTML = '📊 ' + date;
    document.getElementById('modal-summary').innerHTML =
        '<div class="summary-grid">' +
        '<div class="summary-item"><div class="summary-label">Total Trades</div><div class="summary-value">' + trades + '</div></div>' +
        '<div class="summary-item"><div class="summary-label">Wins</div><div class="summary-value positive">' + wins + '</div></div>' +
        '<div class="summary-item"><div class="summary-label">Losses</div><div class="summary-value negative">' + losses + '</div></div>' +
        '<div class="summary-item"><div class="summary-label">Win Rate</div><div class="summary-value">' + winRate + '%</div></div>' +
        '<div class="summary-item"><div class="summary-label">Net P&L</div><div class="summary-value" style="color:' + pnlColor + ';">₹' + Number(netPnl).toLocaleString('en-IN', {{maximumFractionDigits:2}}) + '</div></div>' +
        '</div>';
    let html = '<table style="width:100%;border-collapse:collapse;font-size:12px;"><thead><tr>' +
        '<th style="text-align:left;padding:6px 8px;">Entry Time</th>' +
        '<th style="text-align:left;padding:6px 8px;">Exit Time</th>' +
        '<th style="text-align:left;padding:6px 8px;">Symbol</th>' +
        '<th style="text-align:right;padding:6px 8px;">Strike</th>' +
        '<th style="text-align:right;padding:6px 8px;">Type</th>' +
        '<th style="text-align:right;padding:6px 8px;">Entry ₹</th>' +
        '<th style="text-align:right;padding:6px 8px;">Exit ₹</th>' +
        '<th style="text-align:right;padding:6px 8px;">Pts</th>' +
        '<th style="text-align:right;padding:6px 8px;">Qty</th>' +
        '<th style="text-align:right;padding:6px 8px;">P&L ₹</th>' +
        '<th style="text-align:left;padding:6px 8px;">Reason</th>' +
        '</tr></thead><tbody>';
    for (const t of dayTrades) {{
        const pnl = parseFloat(t.pnl || 0);
        const entry = parseFloat(t.entry_price || 0);
        const exit = parseFloat(t.exit_price || 0);
        const pts = (exit - entry).toFixed(2);
        const c = pnl >= 0 ? '#37f26b' : '#ff4444';
        let et = (t.entry_time || '').split('T')[1] || t.entry_time || '-';
        et = et.split('.')[0];
        let xt = (t.exit_time || '').split('T')[1] || t.exit_time || '-';
        xt = xt.split('.')[0];
        html += '<tr>' +
            '<td style="text-align:left;padding:5px 8px;">' + et + '</td>' +
            '<td style="text-align:left;padding:5px 8px;">' + xt + '</td>' +
            '<td style="text-align:left;padding:5px 8px;">' + (t.symbol || '-') + '</td>' +
            '<td style="text-align:right;padding:5px 8px;">' + (t.strike || '-') + '</td>' +
            '<td style="text-align:right;padding:5px 8px;">' + (t.option_type || '-') + '</td>' +
            '<td style="text-align:right;padding:5px 8px;">' + entry.toFixed(2) + '</td>' +
            '<td style="text-align:right;padding:5px 8px;">' + exit.toFixed(2) + '</td>' +
            '<td style="text-align:right;padding:5px 8px;color:' + c + ';">' + (pts >= 0 ? '+' : '') + pts + '</td>' +
            '<td style="text-align:right;padding:5px 8px;">' + (t.quantity || '-') + '</td>' +
            '<td style="text-align:right;padding:5px 8px;color:' + c + ';font-weight:bold;">' + (pnl >= 0 ? '₹' : '₹-') + Math.abs(pnl).toLocaleString('en-IN', {{maximumFractionDigits:2}}) + '</td>' +
            '<td style="text-align:left;padding:5px 8px;">' + (t.reason || '-') + '</td>' +
            '</tr>';
    }}
    html += '</tbody></table>';
    document.getElementById('modal-table').innerHTML = html;
    document.getElementById('modal').style.display = 'block';
}}

function closeModal() {{
    document.getElementById('modal').style.display = 'none';
}}

document.getElementById('modal').addEventListener('click', function(e) {{
    if (e.target === this) closeModal();
}});
// Auto-refresh every 30 seconds
setInterval(function() {{ location.reload(); }}, 30000);
</script>
</body></html>"""
    return html


def _fmt_time(iso_str: str) -> str:
    """Format an ISO timestamp as HH:MM:SS only."""
    if not iso_str:
        return "-"
    try:
        dt = datetime.fromisoformat(str(iso_str).replace("Z", "+00:00"))
        return dt.strftime("%H:%M:%S")
    except Exception:
        s = str(iso_str)
        if "T" in s:
            return s.split("T", 1)[1].split(".", 1)[0]
        return s


def _fmt(v, default="-"):
    if v is None or v == "":
        return default
    return str(v)


def _generate_day_html(date: str, trades: list[dict]) -> str:
    """Detail page for one trading day: every trade with entry/exit/points/pnl."""
    total_pnl = sum(float(t.get("pnl", 0)) for t in trades)
    wins = sum(1 for t in trades if float(t.get("pnl", 0)) > 0)
    losses = len(trades) - wins
    pnl_color = "#37f26b" if total_pnl >= 0 else "#ff4444"
    has_mfe = any(t.get("mfe_pts") is not None or t.get("mae_pts") is not None for t in trades)
    mfe_th = '<th>MFE</th><th>MAE</th>' if has_mfe else ''

    rows = []
    for t in sorted(trades, key=lambda x: x.get("entry_time", "")):
        pnl = float(t.get("pnl", 0))
        entry = float(t.get("entry_price", 0))
        exit_p = float(t.get("exit_price", 0))
        points = round(exit_p - entry, 2)
        pnl_color_local = "#37f26b" if pnl >= 0 else "#ff4444"
        mfe = t.get("mfe_pts")
        mae = t.get("mae_pts")
        has_mfe = mfe is not None or mae is not None
        mfe_cell = f'<td style="text-align:right;">{mfe}</td>' if has_mfe else ''
        mae_cell = f'<td style="text-align:right;">{mae}</td>' if has_mfe else ''
        rows.append(f"""
        <tr>
            <td>{_fmt_time(t.get("entry_time", ""))}</td>
            <td>{_fmt_time(t.get("exit_time", ""))}</td>
            <td>{_fmt(t.get("symbol"))}</td>
            <td>{_fmt(t.get("strike"))}</td>
            <td>{_fmt(t.get("option_type"))}</td>
            <td style="text-align:right;">{entry:.2f}</td>
            <td style="text-align:right;">{exit_p:.2f}</td>
            <td style="text-align:right;color:{pnl_color_local};">{points:+.2f}</td>
            <td style="text-align:right;">{t.get("quantity", "-")}</td>
            <td style="text-align:right;color:{pnl_color_local};font-weight:bold;">₹{pnl:,.2f}</td>
            <td>{_fmt(t.get("reason"))}</td>
            {mfe_cell}{mae_cell}
        </tr>""")

    if not rows:
        colspan = 11 + (2 if has_mfe else 0)
        table_rows = f'<tr><td colspan="{colspan}" style="text-align:center;padding:40px;color:#777;">No trades on this day</td></tr>'
    else:
        table_rows = "".join(rows)

    html = f"""<!DOCTYPE html>
<html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>NIFTY Trades - {date}</title>
<style>
body{{background:#0b0f0b;color:#37f26b;font-family:'Courier New',monospace;padding:20px;margin:0;}}
h1{{color:#37f26b;border-bottom:1px solid #37f26b;padding-bottom:8px;font-size:1.4rem;}}
a{{color:#37f26b;}}
table{{width:100%;border-collapse:collapse;margin-top:10px;font-size:12px;}}
th,td{{padding:6px 8px;border-bottom:1px solid #123;text-align:right;}}
th{{color:#7fdc9a;background:#0d120d;position:sticky;top:0;}}
td:first-child,th:first-child{{text-align:left;}}
tr:hover{{background:#122;}}
.summary{{background:#0d120d;padding:12px;border:1px solid #123;margin-bottom:16px;border-radius:4px;}}
.summary-grid{{display:grid;grid-template-columns:repeat(auto-fit,minmax(120px,1fr));gap:10px;}}
.summary-item{{text-align:center;}}
.summary-label{{color:#7fdc9a;font-size:11px;}}
.summary-value{{font-size:18px;font-weight:bold;}}
.positive{{color:#37f26b;}}
.negative{{color:#ff4444;}}
.back-btn{{background:#37f26b;color:#0b0f0b;border:none;padding:10px 20px;font-family:inherit;font-weight:bold;margin:10px 0;cursor:pointer;}}
@media (max-width:600px){{table,th,td{{font-size:10px;padding:3px 5px;}}}}
</style></head><body>
<h1>📊 {date}</h1>
<div class="summary">
    <div class="summary-grid">
        <div class="summary-item"><div class="summary-label">Total Trades</div><div class="summary-value">{len(trades)}</div></div>
        <div class="summary-item"><div class="summary-label">Wins</div><div class="summary-value positive">{wins}</div></div>
        <div class="summary-item"><div class="summary-label">Losses</div><div class="summary-value negative">{losses}</div></div>
        <div class="summary-item"><div class="summary-label">Win Rate</div><div class="summary-value">{(wins/len(trades)*100) if len(trades) else 0:.1f}%</div></div>
        <div class="summary-item"><div class="summary-label">Net P&L</div><div class="summary-value" style="color:{pnl_color};">₹{total_pnl:,.2f}</div></div>
    </div>
</div>
<button class="back-btn" onclick="history.back()">← Back to Daily Analytics</button>
<table>
<thead>
<tr>
    <th>Entry Time</th><th>Exit Time</th><th>Symbol</th><th>Strike</th><th>Type</th>
    <th>Entry ₹</th><th>Exit ₹</th><th>Pts</th><th>Qty</th>
    <th>P&L ₹</th><th>Reason</th>{mfe_th}
</tr>
</thead>
<tbody>{table_rows}</tbody>
</table>
<script>
setInterval(function() {{ location.reload(); }}, 30000);
</script>
</body></html>"""
    return html


class _Handler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        pass

    def do_GET(self):
        if self.path.startswith("/api/stats"):
            trades = _parse_trades()
            stats = _compute_daily_stats(trades)
            self._send_json(stats)
        elif self.path.startswith("/api/trades/"):
            date = self.path.split("/api/trades/", 1)[1].split("?")[0]
            trades = _parse_trades()
            day_trades = [t for t in trades if t["_date"] == date]
            self._send_json(day_trades)
        elif self.path.startswith("/date/"):
            date = self.path.split("/date/", 1)[1].split("?")[0]
            trades = _parse_trades()
            day_trades = [t for t in trades if t["_date"] == date]
            html = _generate_day_html(date, day_trades)
            self._send_html(html)
        else:
            trades = _parse_trades()
            stats = _compute_daily_stats(trades)
            html = _generate_html(stats)
            self._send_html(html)

    def _send_json(self, payload):
        body = json.dumps(payload, default=str).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_html(self, html):
        body = html.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class _Server(ThreadingHTTPServer):
    allow_reuse_address = True
    daemon_threads = True


def start_session_dashboard() -> ThreadingHTTPServer:
    global _SERVER
    host = CONFIG.dashboard.host
    start_port = CONFIG.dashboard.port + 1  # different port from main dashboard
    bound_port = start_port
    for port in range(start_port, start_port + 20):
        try:
            _SERVER = _Server((host, port), _Handler)
            bound_port = port
            if port != start_port:
                _log.warning("Session dashboard port %d was busy, using fallback port %d", start_port, port)
            break
        except OSError:
            continue
    else:
        raise OSError(f"Could not bind session dashboard on {host}:{start_port}-{start_port + 19}")
    thread = threading.Thread(target=_SERVER.serve_forever, daemon=True)
    thread.start()
    _log.info("Session dashboard running at http://%s:%d", host, bound_port)
    return _SERVER


def stop_session_dashboard():
    global _SERVER
    if _SERVER:
        _SERVER.shutdown()
        _SERVER = None


def get_session_dashboard_url() -> str:
    host = CONFIG.dashboard.host
    port = CONFIG.dashboard.port + 1
    if host in ("127.0.0.1", "localhost"):
        return f"http://localhost:{port}"
    return f"http://{host}:{port}"


if __name__ == "__main__":
    start_session_dashboard()
    import time
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        stop_session_dashboard()