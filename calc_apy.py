import httpx
from datetime import datetime
from zoneinfo import ZoneInfo

r = httpx.get('https://www.deribit.com/api/v2/public/get_instruments?currency=BTC&kind=option&expired=false')
insts = r.json().get('result', [])
inst_map = {x['instrument_name']: x for x in insts}

r_book = httpx.get('https://www.deribit.com/api/v2/public/get_book_summary_by_currency?currency=BTC&kind=option')
items = r_book.json().get('result', [])
spot = items[0].get('underlying_price', 81000)

from collections import defaultdict
by_exp = defaultdict(list)
for it in items:
    name = it['instrument_name']
    meta = inst_map.get(name, {})
    exp_ts = meta.get('expiration_timestamp', 0)
    exp_dt = datetime.fromtimestamp(exp_ts / 1000.0, tz=ZoneInfo('Asia/Bangkok'))
    strike = meta.get('strike', 0)
    otype = meta.get('option_type', '')
    by_exp[(exp_ts, exp_dt)].append({
        'name': name,
        'strike': strike,
        'type': otype,
        'mark_btc': it.get('mark_price', 0.0),
        'mark_usd': it.get('mark_price', 0.0) * spot,
        'oi': it.get('open_interest', 0.0),
    })

print(f"Current BTC Spot: ${spot:,.2f}")
print("=" * 70)
sorted_exps = sorted(by_exp.keys(), key=lambda x: x[0])

# Let's inspect: 
# 1. Tomorrow (Daily)
# 2. Day after tomorrow (Daily)
# 3. This Friday (Weekly)
# 4. Next Friday (Weekly)
for exp_ts, exp_dt in sorted_exps[:6]:
    sub = by_exp[(exp_ts, exp_dt)]
    now_dt = datetime.now(ZoneInfo('Asia/Bangkok'))
    days_left = (exp_dt - now_dt).total_seconds() / 86400.0
    calls = sorted([x for x in sub if x['type'] == 'call'], key=lambda x: x['oi'], reverse=True)
    puts = sorted([x for x in sub if x['type'] == 'put'], key=lambda x: x['oi'], reverse=True)
    print(f"\n[Expiry] {exp_dt.strftime('%Y-%m-%d %H:%M BKK')} ({days_left:.1f} days left)")
    if calls:
        c1 = calls[0]
        c2 = calls[1] if len(calls) > 1 else None
        print(f"  Call 1 (C1): Strike={c1['strike']:,.0f} ({(c1['strike']-spot)/spot*100:+.2f}%), OI={c1['oi']:.1f}, Mark=${c1['mark_usd']:.1f} ({c1['mark_usd']/spot*100:.3f}%)")
        if c2:
            print(f"  Call 2 (C2): Strike={c2['strike']:,.0f} ({(c2['strike']-spot)/spot*100:+.2f}%), OI={c2['oi']:.1f}, Mark=${c2['mark_usd']:.1f} ({c2['mark_usd']/spot*100:.3f}%)")
    if puts:
        p1 = puts[0]
        p2 = puts[1] if len(puts) > 1 else None
        print(f"  Put 1  (P1): Strike={p1['strike']:,.0f} ({(p1['strike']-spot)/spot*100:+.2f}%), OI={p1['oi']:.1f}, Mark=${p1['mark_usd']:.1f} ({p1['mark_usd']/spot*100:.3f}%)")
        if p2:
            print(f"  Put 2  (P2): Strike={p2['strike']:,.0f} ({(p2['strike']-spot)/spot*100:+.2f}%), OI={p2['oi']:.1f}, Mark=${p2['mark_usd']:.1f} ({p2['mark_usd']/spot*100:.3f}%)")
