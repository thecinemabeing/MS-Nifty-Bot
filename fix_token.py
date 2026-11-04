from kiteconnect import KiteConnect
from config import CONFIG

api_key = CONFIG.kite.api_key
api_secret = CONFIG.kite.api_secret
kite = KiteConnect(api_key=api_key)
rt = input("Paste request_token: ").strip()
try:
    data = kite.generate_session(rt, api_secret=api_secret)
    print(f"\nKITE_ACCESS_TOKEN={data['access_token']}\n")
    # update env
    with open('.env','r') as f:
        s=f.read()
    import re
    s2 = re.sub(r'^KITE_ACCESS_TOKEN=.*', f'KITE_ACCESS_TOKEN={data["access_token"]}', s, flags=re.M)
    with open('.env','w') as f:
        f.write(s2)
    print("Updated .env")
except Exception as e:
    print(e)
