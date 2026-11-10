"""Zerodha KiteConnect API wrapper with paper trading and live broker access."""

from __future__ import annotations

import json
import threading
from datetime import datetime, date
from pathlib import Path
from typing import Callable, Optional

import requests
from kiteconnect import KiteConnect
from kiteconnect import KiteTicker

from config import CONFIG
from logger import get_logger
from utils import retry_with_backoff

_log = get_logger("kite_api")


class KiteAuthError(RuntimeError):
    pass


class KiteAPI:
    def __init__(self) -> None:
        self.kite = KiteConnect(
            api_key=CONFIG.kite.api_key,
        )
        self._ticker: Optional[KiteTicker] = None
        self._subscribed_tokens: set[int] = set()
        self._token_exchange: dict[str, int] = {}
        self._token_symbol: dict[str, str] = {}
        self._last_volume: dict[str, int] = {}
        self._instrument_by_symbol: dict[str, dict] = {}
        self._instruments_cache: Optional[list[dict]] = None
        self._tick_callback: Optional[Callable] = None
        self._ws_lock = threading.Lock()
        self._ws_connected = threading.Event()
        self._ws_stop = threading.Event()
        self._ws_thread: Optional[threading.Thread] = None
        self._paper_mode = CONFIG.mode.value == "PAPER"
        self._paper_orders: dict[str, dict] = {}
        self._paper_positions: dict[str, int] = {}
        self._paper_equity: float = 0.0
        self._paper_initial_equity: float = 0.0
        self._paper_fill_ratio: float = 1.0

    def _simulate_fill(self, order: dict) -> tuple[int, float]:
        order_type = order.get("order_type", "LIMIT")
        requested = int(order.get("quantity", 0) or 0)
        price = float(order.get("price", 0.0) or 0.0)
        trigger = float(order.get("trigger_price", 0.0) or 0.0)
        if requested <= 0:
            return 0, 0.0
        if order_type == "MARKET":
            return requested, price if price > 0 else 0.0
        if order_type in ("SL-M", "SL"):
            return requested, trigger if trigger > 0 else price

        fill_ratio = getattr(self, "_paper_fill_ratio", 1.0)
        if fill_ratio >= 1.0:
            return requested, price
        fill_qty = max(1, int(requested * fill_ratio))
        return fill_qty, price

    def _apply_fill_to_position(self, symbol: str, transaction_type: str, qty: int) -> None:
        current = self._paper_positions.get(symbol, 0)
        if transaction_type == "BUY":
            self._paper_positions[symbol] = current + qty
        else:
            self._paper_positions[symbol] = current - qty

    def _cancel_opposite_protective_orders(self, symbol: str, filled_txn: str) -> None:
        opposite = "BUY" if filled_txn == "SELL" else "SELL"
        for order_id, order in list(self._paper_orders.items()):
            if order.get("tradingsymbol") != symbol:
                continue
            if order.get("transaction_type") == opposite and order.get("order_type") == "SL" and order.get("status") == "TRIGGER PENDING":
                order["status"] = "CANCELLED"

    def _paper_margin_snapshot(self) -> dict:
        return {
            "paper_initial_equity": self._paper_initial_equity,
            "paper_current_equity": self._paper_equity,
            "paper_available_margin": self._paper_equity,
        }

    def credit_paper_pnl(self, pnl: float) -> None:
        self._paper_equity += pnl

    def set_paper_fill_ratio(self, ratio: float) -> None:
        self._paper_fill_ratio = max(0.0, min(1.0, float(ratio)))

    def process_paper_orders(self, symbol: str, ltp: float) -> None:
        if not self._paper_mode:
            return
        for order_id, order in list(self._paper_orders.items()):
            if order.get("tradingsymbol") != symbol:
                continue
            status = order.get("status", "")
            if status in ("CANCELLED", "REJECTED"):
                continue
            order_type = order.get("order_type", "")
            if order_type not in ("LIMIT", "MARKET", "SL", "SL-M"):
                continue
            txn = order.get("transaction_type", "BUY")
            filled_qty = order.get("filled_quantity", 0)
            if filled_qty > 0 and status == "COMPLETE":
                current_qty = self._paper_positions.get(symbol, 0)
                if txn == "SELL" and current_qty >= 0:
                    self._cancel_opposite_protective_orders(symbol, "SELL")
                elif txn == "BUY" and current_qty <= 0:
                    self._cancel_opposite_protective_orders(symbol, "BUY")

        for order_id, order in list(self._paper_orders.items()):
            if order.get("tradingsymbol") != symbol:
                continue
            status = order.get("status", "")
            if status in ("COMPLETE", "FILLED", "CANCELLED", "REJECTED"):
                continue
            order_type = order.get("order_type", "")
            trigger = float(order.get("trigger_price", 0.0) or 0.0)
            requested = int(order.get("quantity", 0) or 0)
            remaining = int(order.get("remaining_quantity", requested) or requested)
            txn = order.get("transaction_type", "BUY")

            if order_type == "SL" and status == "TRIGGER PENDING":
                if (txn == "SELL" and ltp <= trigger) or (txn == "BUY" and ltp >= trigger):
                    fill_qty, fill_price = self._simulate_fill(order)
                    fill_qty = min(fill_qty, remaining)
                    if fill_qty > 0:
                        order["filled_quantity"] = int(order.get("filled_quantity", 0)) + fill_qty
                        order["remaining_quantity"] = remaining - fill_qty
                        order["average_price"] = fill_price
                        order["status"] = "COMPLETE" if order["remaining_quantity"] == 0 else "PARTIAL"
                        self._apply_fill_to_position(symbol, txn, fill_qty)
                        self._cancel_opposite_protective_orders(symbol, txn)
            elif order_type == "SL-M" and status == "TRIGGER PENDING":
                if (txn == "SELL" and ltp <= trigger) or (txn == "BUY" and ltp >= trigger):
                    fill_qty, fill_price = self._simulate_fill(order)
                    fill_qty = min(fill_qty, remaining)
                    if fill_qty > 0:
                        order["filled_quantity"] = int(order.get("filled_quantity", 0)) + fill_qty
                        order["remaining_quantity"] = remaining - fill_qty
                        order["average_price"] = fill_price
                        order["status"] = "COMPLETE" if order["remaining_quantity"] == 0 else "PARTIAL"
                        self._apply_fill_to_position(symbol, txn, fill_qty)
                        self._cancel_opposite_protective_orders(symbol, txn)
            elif order_type == "LIMIT" and status in ("OPEN", "PARTIAL"):
                current_qty = self._paper_positions.get(symbol, 0)
                if txn == "SELL" and current_qty > 0 and ltp <= float(order.get("price", 0.0) or 0.0):
                    fill_qty, fill_price = self._simulate_fill(order)
                    fill_qty = min(fill_qty, remaining, current_qty)
                    if fill_qty > 0:
                        order["filled_quantity"] = int(order.get("filled_quantity", 0)) + fill_qty
                        order["remaining_quantity"] = remaining - fill_qty
                        order["average_price"] = fill_price
                        order["status"] = "COMPLETE" if order["remaining_quantity"] == 0 else "PARTIAL"
                        self._apply_fill_to_position(symbol, txn, fill_qty)
                        self._cancel_opposite_protective_orders(symbol, txn)
                elif txn == "BUY" and current_qty < 0 and ltp >= float(order.get("price", 0.0) or 0.0):
                    fill_qty, fill_price = self._simulate_fill(order)
                    fill_qty = min(fill_qty, remaining, abs(current_qty))
                    if fill_qty > 0:
                        order["filled_quantity"] = int(order.get("filled_quantity", 0)) + fill_qty
                        order["remaining_quantity"] = remaining - fill_qty
                        order["average_price"] = fill_price
                        order["status"] = "COMPLETE" if order["remaining_quantity"] == 0 else "PARTIAL"
                        self._apply_fill_to_position(symbol, txn, fill_qty)
                        self._cancel_opposite_protective_orders(symbol, txn)
            elif order_type == "MARKET" and status in ("OPEN", "PARTIAL"):
                fill_qty, fill_price = self._simulate_fill(order)
                fill_qty = min(fill_qty, remaining)
                if fill_qty > 0:
                    order["filled_quantity"] = int(order.get("filled_quantity", 0)) + fill_qty
                    order["remaining_quantity"] = remaining - fill_qty
                    order["average_price"] = fill_price
                    order["status"] = "COMPLETE" if order["remaining_quantity"] == 0 else "PARTIAL"
                    self._apply_fill_to_position(symbol, txn, fill_qty)
                    self._cancel_opposite_protective_orders(symbol, txn)

    # ------------------------------------------------------------
    # AUTH
    # ------------------------------------------------------------

    def login(self) -> None:
        """
        Login to Zerodha Kite. If access token is cached or provided via env, use it.
        Otherwise expect to be logged in externally (Kite requires request_token flow).
        """
        _log.info("MODE=%s | DATA=Real Kite Live | ORDERS=%s", CONFIG.mode.value, "SIMULATED (Paper)" if self._paper_mode else "REAL (Broker)")
        
        # Try cached token first
        cached = self._load_cached_token()
        if cached and cached.get("access_token"):
            try:
                self.kite.set_access_token(cached["access_token"])
                profile = self.kite.profile()
                _log.info("Using Cached Token")
                _log.info("Authentication Successful")
                return
            except Exception:
                _log.info("Cached Kite token expired")

        # Try token from env/config
        if CONFIG.kite.access_token:
            try:
                self.kite.set_access_token(CONFIG.kite.access_token)
                profile = self.kite.profile()
                _log.info("Authentication Successful")
                return
            except Exception as exc:
                raise KiteAuthError(f"Kite login failed with access token: {exc}") from exc

        # If request_token provided, generate session
        if CONFIG.kite.request_token and CONFIG.kite.api_secret:
            try:
                data = self.kite.generate_session(CONFIG.kite.request_token, api_secret=CONFIG.kite.api_secret)
                access_token = data.get("access_token")
                if not access_token:
                    raise KiteAuthError("Kite login response missing access_token")
                self.kite.set_access_token(access_token)
                self._save_cached_token(access_token)
                _log.info("Authentication Successful")
                return
            except Exception as exc:
                raise KiteAuthError(f"Kite login failed: {exc}") from exc

        raise KiteAuthError(
            "Missing Kite credentials. Set KITE_API_KEY, KITE_API_SECRET and either KITE_ACCESS_TOKEN or complete login flow with request_token"
        )

    def _tokens_path(self) -> Path:
        return CONFIG.kite.tokens_path

    def _load_cached_token(self) -> Optional[dict]:
        path = self._tokens_path()
        if not path.exists():
            return None
        try:
            data = json.loads(path.read_text())
            if data.get("login_date") != date.today().isoformat():
                return None
            if not data.get("access_token"):
                return None
            return data
        except (OSError, json.JSONDecodeError):
            return None

    def _save_cached_token(self, access_token: str) -> None:
        path = self._tokens_path()
        data = {
            "access_token": access_token,
            "login_date": date.today().isoformat(),
            "generated_at": datetime.now().isoformat(),
        }
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, indent=2))
        path.chmod(0o600)
        _log.info("Token Saved")


    # ------------------------------------------------------------
    # DATA
    # ------------------------------------------------------------

    @retry_with_backoff()
    def instruments(self, exchange: Optional[str] = None) -> list[dict]:
        """Fetch instruments. For Kite, returns list of dicts in compatible format."""
        if self._instruments_cache and exchange is None:
            return self._instruments_cache
        try:
            if exchange:
                raw = self.kite.instruments(exchange)
            else:
                raw = self.kite.instruments()
            # Normalize to common format
            result = []
            for inst in raw:
                result.append({
                    'instrument_token': inst.get('instrument_token'),
                    'exchange_token': inst.get('exchange_token'),
                    'tradingsymbol': inst.get('tradingsymbol'),
                    'name': inst.get('name'),
                    'last_price': inst.get('last_price', 0),
                    'expiry': inst.get('expiry'),
                    'strike': inst.get('strike', 0),
                    'tick_size': inst.get('tick_size', 0.05),
                    'lot_size': inst.get('lot_size', 1),
                    'instrument_type': inst.get('instrument_type'),
                    'segment': inst.get('segment'),
                    'exchange': inst.get('exchange')
                })
            if exchange is None:
                self._instruments_cache = result
            return result
        except Exception:
            _log.exception("Failed to fetch instruments")
            raise

    @retry_with_backoff()
    def quote(self, tokens: list[str]) -> dict:
        try:
            return self.kite.quote(tokens)
        except Exception:
            _log.exception("Failed to fetch quotes")
            raise

    @retry_with_backoff()
    def ltp(self, tokens: list[str]) -> dict:
        try:
            return self.kite.ltp(tokens)
        except Exception:
            _log.exception("Failed to fetch ltp")
            raise

    @retry_with_backoff()
    def historical_data(self, instrument_token, from_date, to_date, interval, continuous=False, oi=False):
        try:
            return self.kite.historical_data(instrument_token, from_date, to_date, interval, continuous=continuous, oi=oi)
        except Exception:
            _log.exception("Failed to fetch historical data")
            raise

    def profile(self) -> dict:
        try:
            return self.kite.profile()
        except Exception:
            raise


    # ------------------------------------------------------------
    # ORDERS (Paper/Live)
    # ------------------------------------------------------------

    def place_order(self, **kwargs) -> str:
        tradingsymbol = kwargs.get('tradingsymbol') or kwargs.get('symbol') or 'UNKNOWN'
        exchange = kwargs.get('exchange', 'NFO')
        transaction_type = (kwargs.get('transaction_type') or kwargs.get('side') or 'BUY').upper()
        quantity = int(kwargs.get('quantity') or kwargs.get('qty') or 0)
        product = kwargs.get('product', 'MIS')
        order_type = kwargs.get('order_type', 'MARKET')
        price = float(kwargs.get('price', 0.0) or 0.0)
        trigger_price = float(kwargs.get('trigger_price', 0.0) or 0.0)
        validity = kwargs.get('validity', 'DAY')
        variety = kwargs.get('variety', 'regular')
        disclosed_quantity = kwargs.get('disclosed_quantity')
        tag = kwargs.get('tag')

        if self._paper_mode:
            order_id = f"PAPER_{abs(hash(str(kwargs) + str(datetime.now())))}"
            requested_qty = quantity
            order = {
                'order_id': order_id,
                'tradingsymbol': tradingsymbol,
                'exchange': exchange,
                'transaction_type': transaction_type,
                'quantity': requested_qty,
                'filled_quantity': 0,
                'remaining_quantity': requested_qty,
                'average_price': 0.0,
                'product': product,
                'order_type': order_type,
                'price': price,
                'trigger_price': trigger_price,
                'status': 'TRIGGER PENDING' if order_type in ['SL', 'SL-M'] else 'OPEN',
                'timestamp': datetime.now().isoformat(),
                'variety': variety,
            }
            self._paper_orders[order_id] = order

            if order_type == 'SL':
                order['status'] = 'TRIGGER PENDING'
            elif order_type == 'SL-M':
                order['status'] = 'TRIGGER PENDING'
            elif order_type == 'MARKET':
                fill_qty, fill_price = self._simulate_fill(order)
                if fill_qty > 0:
                    order['filled_quantity'] = fill_qty
                    order['remaining_quantity'] = requested_qty - fill_qty
                    order['average_price'] = fill_price
                    order['status'] = 'COMPLETE' if fill_qty >= requested_qty else 'PARTIAL'
                    self._apply_fill_to_position(tradingsymbol, transaction_type, fill_qty)
            else:
                fill_qty, fill_price = self._simulate_fill(order)
                if fill_qty > 0:
                    order['filled_quantity'] = fill_qty
                    order['remaining_quantity'] = requested_qty - fill_qty
                    order['average_price'] = fill_price
                    order['status'] = 'COMPLETE' if fill_qty >= requested_qty else 'PARTIAL'
                    self._apply_fill_to_position(tradingsymbol, transaction_type, fill_qty)
            _log.info("[PAPER] Order placed: %s %s %s qty=%s @%s type=%s status=%s", order_id, transaction_type, tradingsymbol, requested_qty, price or trigger_price, order_type, order['status'])
            return order_id

        try:
            if hasattr(self.kite, 'placeOrderFullResponse'):
                payload = {
                    'variety': variety,
                    'exchange': exchange,
                    'tradingsymbol': tradingsymbol,
                    'transactiontype': transaction_type,
                    'quantity': str(quantity),
                    'producttype': product,
                    'ordertype': order_type,
                    'price': str(price),
                    'triggerprice': str(trigger_price),
                    'duration': validity,
                }
                response = self.kite.placeOrderFullResponse(payload)
            else:
                response = self.kite.place_order(
                    variety=variety,
                    exchange=exchange,
                    tradingsymbol=tradingsymbol,
                    transaction_type=transaction_type,
                    quantity=quantity,
                    product=product,
                    order_type=order_type,
                    price=price,
                    trigger_price=trigger_price,
                    validity=validity,
                    disclosed_quantity=disclosed_quantity,
                    tag=tag,
                )
            if isinstance(response, dict):
                data = response.get('data') or {}
                order_id = data.get('orderid') or data.get('order_id')
                if order_id is not None:
                    return str(order_id)
            return str(response)
        except Exception:
            _log.exception("Failed to place order")
            raise


    def modify_order(self, variety: str, order_id: str, **kwargs) -> str:
        if self._paper_mode:
            if order_id in self._paper_orders:
                order = self._paper_orders[order_id]
                for key, value in kwargs.items():
                    if key in ('price', 'trigger_price'):
                        order[key] = float(value) if value is not None else order.get(key, 0.0)
                    else:
                        order[key] = value
                if order.get('status') in ('TRIGGER PENDING', 'OPEN') and 'trigger_price' in kwargs:
                    order['trigger_price'] = float(kwargs['trigger_price'])
            return order_id
        try:
            if hasattr(self.kite, 'modifyOrder'):
                return str(self.kite.modifyOrder(variety=variety, order_id=order_id, **kwargs))
            return str(self.kite.modify_order(variety=variety, order_id=order_id, **kwargs))
        except Exception:
            _log.exception("Failed to modify order")
            raise

    def cancel_order(self, variety: str = 'regular', order_id: str | None = None) -> str:
        if order_id is None:
            order_id = variety
            variety = 'regular'
        if self._paper_mode:
            if order_id in self._paper_orders:
                self._paper_orders[order_id]['status'] = 'CANCELLED'
            return str(order_id)
        try:
            if hasattr(self.kite, 'cancelOrder'):
                return str(self.kite.cancelOrder(str(order_id), variety or 'regular'))
            return str(self.kite.cancel_order(variety=variety, order_id=order_id))
        except Exception:
            _log.exception("Failed to cancel order")
            raise

    def orders(self) -> list:
        if self._paper_mode:
            return list(self._paper_orders.values())
        try:
            return self.kite.orders()
        except Exception:
            _log.exception("Failed to fetch orders")
            raise

    def order_history(self, order_id: str) -> list[dict]:
        if self._paper_mode:
            order = self._paper_orders.get(order_id)
            if not order:
                return []
            return [dict(order)]
        try:
            return [order for order in self.kite.orders() if str(order.get("order_id")) == str(order_id)]
        except Exception:
            _log.exception("Failed to fetch order history for %s", order_id)
            return []

    def positions(self) -> dict:
        if self._paper_mode:
            net = []
            for symbol, qty in self._paper_positions.items():
                if qty != 0:
                    net.append({"tradingsymbol": symbol, "quantity": qty})
            return {'net': net}
        try:
            return self.kite.positions()
        except Exception:
            _log.exception("Failed to fetch positions")
            raise

    def margins(self) -> dict:
        if self._paper_mode:
            return {
                'equity': {'available': {'live_balance': self._paper_equity}},
                'paper_equity': self._paper_equity,
                'paper_initial_equity': self._paper_initial_equity,
            }
        try:
            return self.kite.margins()
        except Exception:
            _log.exception("Failed to fetch margins")
            raise


    # ------------------------------------------------------------
    # WEBSOCKET (real KiteTicker - live ticks drive the scan loop)
    # ------------------------------------------------------------

    def start_ticker(self, tokens, on_tick: Callable) -> None:
        if not tokens:
            _log.warning("start_ticker called with no tokens")
            return
        self._tick_callback = on_tick
        self._subscribed_tokens.update(tokens)
        if self._ticker is not None:
            try:
                self._ticker.subscribe(tokens)
                self._ticker.set_mode(self._ticker.MODE_LTP, tokens)
                _log.info("Ticker already running - subscribed %d tokens", len(tokens))
                return
            except Exception:
                _log.exception("Failed to reuse existing ticker")
        try:
            self._ticker = KiteTicker(
                CONFIG.kite.api_key,
                CONFIG.kite.access_token,
                debug=False,
                reconnect=True,
                reconnect_max_tries=50,
                reconnect_max_delay=60,
                connect_timeout=30,
            )
            self._ticker.on_open = self._on_ws_open
            self._ticker.on_connect = self._on_ws_connect
            self._ticker.on_ticks = self._on_ws_ticks
            self._ticker.on_error = self._on_ws_error
            self._ticker.on_close = self._on_ws_close
            self._ticker.on_reconnect = self._on_ws_reconnect
            self._ticker.on_noreconnect = self._on_ws_noreconnect
            self._ws_thread = threading.Thread(
                target=self._ticker.connect, name="kite-ticker", daemon=True
            )
            self._ws_thread.start()
            _log.info("KiteTicker thread started for %d tokens", len(tokens))
        except Exception:
            _log.exception("Failed to start KiteTicker")
            self._ticker = None

    def _on_ws_open(self, ws):
        _log.info("WebSocket opened")

    def _on_ws_connect(self, ws, response):
        try:
            tokens = list(self._subscribed_tokens)
            if tokens:
                ws.subscribe(tokens)
                ws.set_mode(ws.MODE_LTP, tokens)
                _log.info("WebSocket connected - subscribed %d tokens", len(tokens))
            self._ws_connected.set()
        except Exception:
            _log.exception("on_connect handler failed")

    def _on_ws_ticks(self, ws, ticks):
        try:
            if self._tick_callback:
                self._tick_callback(ticks)
        except Exception:
            _log.exception("tick callback raised")

    def _on_ws_error(self, ws, code, reason):
        _log.warning("WebSocket error: code=%s reason=%s", code, reason)

    def _on_ws_close(self, ws, code, reason):
        self._ws_connected.clear()
        _log.warning("WebSocket closed: code=%s reason=%s", code, reason)

    def _on_ws_reconnect(self, ws, code, reason):
        _log.info("WebSocket reconnecting: code=%s reason=%s", code, reason)

    def _on_ws_noreconnect(self, code, reason):
        _log.error("WebSocket gave up reconnecting: code=%s reason=%s", code, reason)

    def wait_until_connected(self, timeout: int = 20) -> bool:
        return self._ws_connected.wait(timeout=timeout)

    def add_subscription(self, token) -> None:
        self._subscribed_tokens.add(token)
        if self._ticker is not None:
            try:
                self._ticker.subscribe([token])
                self._ticker.set_mode(self._ticker.MODE_LTP, [token])
            except Exception:
                _log.exception("add_subscription failed for token %s", token)

    def remove_subscription(self, token) -> None:
        self._subscribed_tokens.discard(token)
        if self._ticker is not None:
            try:
                self._ticker.unsubscribe([token])
            except Exception:
                _log.exception("remove_subscription failed for token %s", token)

    def set_paper_equity(self, equity: float) -> None:
        self._paper_equity = equity
        self._paper_initial_equity = equity

