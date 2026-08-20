"""查交易所资金流水, 回答'亏损从哪来' (密钥从 .env 读取, 不硬编码)"""
import time, hashlib, hmac, urllib.parse, requests, os
from collections import defaultdict
from pathlib import Path
from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent.parent / ".env")
KEY = os.environ.get("BINANCE_TESTNET_API_KEY", "")
SEC = os.environ.get("BINANCE_TESTNET_API_SECRET", "")
BASE = 'https://testnet.binancefuture.com'
if not KEY or not SEC:
    raise SystemExit("❌ 请先在项目根目录 .env 配置 BINANCE_TESTNET_API_KEY / BINANCE_TESTNET_API_SECRET")


def signed(path, **params):
    params['timestamp'] = int(time.time() * 1000)
    params['recvWindow'] = 10000
    q = urllib.parse.urlencode(params)
    sig = hmac.new(SEC.encode(), q.encode(), hashlib.sha256).hexdigest()
    r = requests.get(f'{BASE}{path}?{q}&signature={sig}', headers={'X-MBX-APIKEY': KEY}, timeout=20)
    if r.status_code != 200:
        print('API错误:', r.status_code, r.text[:200])
        return []
    return r.json()


all_inc = []
start = None
for _ in range(30):
    params = {'limit': 1000}
    if start:
        params['startTime'] = start
    batch = signed('/fapi/v1/income', **params)
    if not batch:
        break
    all_inc.extend(batch)
    if len(batch) < 1000:
        break
    start = int(batch[-1]['time']) + 1

print(f'资金流水共 {len(all_inc)} 条')
sums = defaultdict(float)
for inc in all_inc:
    sums[inc.get('incomeType', '?')] += float(inc.get('income', 0))

print()
print('=== 资金流水汇总 ===')
for t, v in sorted(sums.items(), key=lambda x: -abs(x[1])):
    print(f'  {t:<18} {v:+.2f} USDT')

print()
print('=== 入金记录 ===')
for inc in all_inc:
    if inc.get('incomeType') == 'DEPOSIT':
        ts = time.strftime('%m-%d %H:%M', time.localtime(inc['time'] / 1000))
        print(f'  {ts}: +{float(inc["income"])} USDT')

print()
print('=== 最近12条流水 ===')
for inc in all_inc[-12:]:
    ts = time.strftime('%m-%d %H:%M', time.localtime(inc['time'] / 1000))
    print(f'  {ts} {inc["incomeType"]:<16} {float(inc["income"]):+.4f} {inc.get("asset","")}')
