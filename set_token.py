import sys
rt = sys.argv[1].strip()
from kiteconnect import KiteConnect
from config import CONFIG
import re
k = KiteConnect(api_key=CONFIG.kite.api_key)
# extract if url
if 'request_token' in rt and '=' in rt:
    try:
        from urllib.parse import urlparse, parse_qs
        p = urlparse(rt)
        q = parse_qs(p.query)
        if q.get('request_token'):
            rt = q['request_token'][0]
    except:
        pass
data = k.generate_session(rt, api_secret=CONFIG.kite.api_secret)
tok = data['access_token']
with open('.env') as f:
    s = f.read()
s2 = re.sub(r'^KITE_ACCESS_TOKEN=.*', f'KITE_ACCESS_TOKEN={tok}', s, flags=re.M)
with open('.env','w') as f:
    f.write(s2)
print('done')
