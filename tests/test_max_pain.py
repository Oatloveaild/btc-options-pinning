import pytest
from src.options_math import calculate_max_pain, get_top_strikes


def test_calculate_max_pain_simple():
    # Strikes: 80000, 81000, 82000
    # If 81000 has 500 calls and 500 puts, and other strikes have few contracts,
    # let's test a case where 81000 is clearly max pain.
    strikes_oi = [
        {"strike": 80000.0, "call_oi": 10.0, "put_oi": 500.0},
        {"strike": 81000.0, "call_oi": 100.0, "put_oi": 100.0},
        {"strike": 82000.0, "call_oi": 500.0, "put_oi": 10.0},
    ]
    # At 80000:
    # Buyers loss: call buyers lose money (none ITM). Put buyers: 81000 puts get 100*1000 = 100k, 82000 puts get 10*2000 = 20k -> payout 120k
    # At 81000:
    # Call buyers: 80000 calls get 10*1000 = 10k. Put buyers: 82000 puts get 10*1000 = 10k -> payout 20k (MINIMUM!)
    # At 82000:
    # Call buyers: 80000 calls get 10*2000 = 20k, 81000 calls get 100*1000 = 100k -> payout 120k
    mp = calculate_max_pain(strikes_oi)
    assert mp == 81000.0


def test_get_top_strikes():
    strikes_oi = [
        {"strike": 78000.0, "call_oi": 50.0, "put_oi": 1200.0},
        {"strike": 80000.0, "call_oi": 100.0, "put_oi": 800.0},
        {"strike": 82000.0, "call_oi": 1500.0, "put_oi": 200.0},
        {"strike": 84000.0, "call_oi": 900.0, "put_oi": 50.0},
    ]
    top = get_top_strikes(strikes_oi)
    assert top["call1_strike"] == 82000.0
    assert top["call1_oi"] == 1500.0
    assert top["call2_strike"] == 84000.0
    assert top["call2_oi"] == 900.0
    assert top["put1_strike"] == 78000.0
    assert top["put1_oi"] == 1200.0
    assert top["put2_strike"] == 80000.0
    assert top["put2_oi"] == 800.0
    assert top["top_oi_strike"] == 82000.0  # 1500 + 200 = 1700 vs 1250
