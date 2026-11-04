#!/usr/bin/env python3
from kiteconnect import KiteConnect
from config import CONFIG
from kite_api import KiteAPI
import os

api_key = CONFIG.kite.api_key
api_secret = CONFIG.kite.api_secret
request_token = os.getenv('KITE_REQUEST_TOKEN') or CONFIG.kite.request_token

if not request_token:
    print("Set KITE_REQUEST_TOKEN in .env first")
    exit(1)

kite = KiteConnect(api_key=api_key)
data = kite.generate_session(request_token, api_secret=api_secret)
access_token = data['access_token']
print(f"KITE_ACCESS_TOKEN={access_token}")
# Save to .env
with open('.env', 'a') as f:
    f.write(f"\nKITE_ACCESS_TOKEN={access_token}\n")
print("Saved to .env")
