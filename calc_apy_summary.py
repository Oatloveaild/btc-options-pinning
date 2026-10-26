# Exact calculations for Portfolio 1 to 4

# Days in year = 365, Weeks in year = 52
# Initial capital = $10,000 ($5,000 BTC + $5,000 USD)

# 1. Daily C1 / P1:
# Typically C1 is near ATM (+0.5% to +1.5%), P1 is near ATM (-1.0% to -2.5%)
# Average premium collected per day:
# Call premium: ~0.15% - 0.25% of BTC value ($7.50 - $12.50 per day)
# Put premium: ~0.10% - 0.20% of USD value ($5.00 - $10.00 per day)
# Combined daily gross premium on $10,000: ~0.12% - 0.18% per day (~$12 - $18/day)

# 2. Daily C2 / P2:
# C2/P2 are further OTM (+3% to +6% OTM Call, -3% to -6% OTM Put)
# Lower premium, but higher win-rate (>88-92%)
# Combined daily gross premium on $10,000: ~0.04% - 0.08% per day (~$4 - $8/day)

# 3. Weekly C1 / P1 (7-day):
# Weekly options have square root of time theta:
# Combined weekly premium on $10,000: ~0.80% - 1.40% per week (~$80 - $140/week)

# 4. Weekly C2 / P2 (7-day):
# Further OTM weekly strikes (+8% to +15% Call, -8% to -15% Put):
# Combined weekly premium on $10,000: ~0.35% - 0.65% per week (~$35 - $65/week)

results = {}

configs = [
    ("Port 1: Daily C1/P1", 365, 0.0012, 0.0016, 0.72, -0.0035),
    ("Port 2: Daily C2/P2", 365, 0.0005, 0.0008, 0.88, -0.0020),
    ("Port 3: Weekly C1/P1", 52, 0.0090, 0.0130, 0.68, -0.0150),
    ("Port 4: Weekly C2/P2", 52, 0.0040, 0.0065, 0.86, -0.0090),
]

for name, n_cycles, r_min, r_max, win_rate, breach_loss in configs:
    # Gross yield (before slippage/breach)
    r_avg = (r_min + r_max) / 2.0
    simple_gross = r_avg * n_cycles * 100.0
    compound_gross = ((1.0 + r_avg) ** n_cycles - 1.0) * 100.0
    
    # Net yield (accounting for breach drag and Deribit trading fees ~0.03% underlying per leg)
    # Fee per trade: ~0.03% of notional on Deribit
    fee_per_cycle = 0.0004 if n_cycles == 365 else 0.0006
    net_per_cycle = r_avg * win_rate + (r_avg + breach_loss) * (1 - win_rate) - fee_per_cycle
    simple_net = net_per_cycle * n_cycles * 100.0
    compound_net = ((1.0 + net_per_cycle) ** n_cycles - 1.0) * 100.0 if net_per_cycle > 0 else 0
    
    results[name] = {
        "n_cycles": n_cycles,
        "premium_per_cycle": f"{r_avg*100:.2f}%",
        "dollar_per_cycle": f"${r_avg * 10000:.1f}",
        "simple_gross_apy": f"{simple_gross:.1f}%",
        "compound_gross_apy": f"{compound_gross:.1f}%",
        "win_rate": f"{win_rate*100:.0f}%",
        "simple_net_apy": f"{simple_net:.1f}%",
        "compound_net_apy": f"{compound_net:.1f}%",
    }

import json
print(json.dumps(results, indent=2, ensure_ascii=False))
