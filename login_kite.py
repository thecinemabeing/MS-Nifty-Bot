#!/usr/bin/env python3
from kiteconnect import KiteConnect
from config import CONFIG
import webbrowser
import os

api_key = CONFIG.kite.api_key
api_secret = CONFIG.kite.api_secret
kite = KiteConnect(api_key=api_key)
print("Login URL:")
print(kite.login_url())
