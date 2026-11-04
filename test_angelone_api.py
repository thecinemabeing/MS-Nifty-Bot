from __future__ import annotations

import unittest

from angelone_api import AngelOneAPI


class TestAngelOneMappings(unittest.TestCase):
    def test_normalizes_option_from_scrip_master(self):
        instrument = AngelOneAPI._normalize_instrument({
            "token": "123456",
            "symbol": "NIFTY29OCT2626000CE",
            "name": "NIFTY",
            "expiry": "29OCT2026",
            "strike": "2600000.000000",
            "lotsize": "65",
            "instrumenttype": "OPTIDX",
            "exch_seg": "NFO",
            "tick_size": "0.05",
        })

        self.assertEqual(instrument["instrument_token"], 123456)
        self.assertEqual(instrument["tradingsymbol"], "NIFTY29OCT2626000CE")
        self.assertEqual(instrument["strike"], 26000)
        self.assertEqual(instrument["instrument_type"], "CE")
        self.assertEqual(instrument["segment"], "NFO-OPT")
        self.assertEqual(instrument["expiry"].isoformat(), "2026-10-29")
        self.assertEqual(instrument["lot_size"], 65)

    def test_normalizes_nifty_index(self):
        instrument = AngelOneAPI._normalize_instrument({
            "token": "99926000",
            "symbol": "Nifty 50",
            "name": "NIFTY",
            "instrumenttype": "AMXIDX",
            "exch_seg": "NSE",
        })

        self.assertEqual(instrument["tradingsymbol"], "NIFTY 50")
        self.assertEqual(instrument["segment"], "INDICES")

    def test_maps_stop_market_order_to_angel_one_payload(self):
        api = AngelOneAPI.__new__(AngelOneAPI)
        api._instrument_by_symbol = {
            "NFO:NIFTY29OCT2626000CE": {"instrument_token": 123456}
        }

        payload = api._angel_order_payload({
            "tradingsymbol": "NIFTY29OCT2626000CE",
            "exchange": "NFO",
            "transaction_type": "SELL",
            "quantity": 65,
            "product": "MIS",
            "order_type": "SL-M",
            "trigger_price": 120.5,
        })

        self.assertEqual(payload["variety"], "NORMAL")
        self.assertEqual(payload["ordertype"], "STOPLOSS_MARKET")
        self.assertEqual(payload["producttype"], "INTRADAY")
        self.assertEqual(payload["symboltoken"], "123456")
        self.assertEqual(payload["transactiontype"], "SELL")
        self.assertEqual(payload["quantity"], "65")
        self.assertEqual(payload["triggerprice"], "120.5")

    def test_normalizes_order_book_status_and_fields(self):
        order = AngelOneAPI._normalize_order({
            "orderid": "abc123",
            "orderstatus": "trigger pending",
            "tradingsymbol": "NIFTY29OCT2626000CE",
            "quantity": "65",
            "filledshares": "0",
            "averageprice": "0",
            "transactiontype": "SELL",
            "ordertype": "STOPLOSS_LIMIT",
            "triggerprice": "100.5",
        })

        self.assertEqual(order["order_id"], "abc123")
        self.assertEqual(order["status"], "TRIGGER PENDING")
        self.assertEqual(order["order_type"], "SL")
        self.assertEqual(order["transaction_type"], "SELL")
        self.assertEqual(order["quantity"], 65)
        self.assertEqual(order["trigger_price"], 100.5)


if __name__ == "__main__":
    unittest.main()
