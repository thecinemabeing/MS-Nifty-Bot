#!/usr/bin/env python3
import re
import subprocess

# Read from env
with open('.env') as f:
    s = f.read()

# Get token
m = re.search(r'^KITE_ACCESS_TOKEN=(.+)$', s, re.M)
token = m.group(1) if m else ''

# Update railway vars
with open('railway_vars.txt') as f:
    rv = f.read()
rv2 = re.sub(r'^KITE_ACCESS_TOKEN=.*', f'KITE_ACCESS_TOKEN={token}', rv, flags=re.M)
with open('railway_vars.txt', 'w') as f:
    f.write(rv2)

subprocess.call(['git', 'add', 'railway_vars.txt'])
subprocess.call(['git', 'commit', '-m', 'update token'])
subprocess.call(['git', 'push'])
