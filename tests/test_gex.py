import pytest
from src.options_math import (
    bs_gamma,
    calculate_gex_per_strike,
    calculate_gamma_flip,
)


def test_bs_gamma_edge_cases():
    # Safe against 0 or negative
    assert bs_gamma(0.0, 80000.0, 0.1, 0.5) == 0.0
    assert bs_gamma(80000.0, 0.0, 0.1, 0.5) == 0.0
    assert bs_gamma(80000.0, 80000.0, 0.0, 0.5) == 0.0
    assert bs_gamma(80000.0, 80000.0, 0.1, 0.0) == 0.0
    assert bs_gamma(-100.0, 80000.0, 0.1, 0.5) == 0.0

    # Normal calculation
    gamma = bs_gamma(80000.0, 80000.0, 1.0 / 365.0, 0.5)
    assert gamma > 0.0


def test_gex_calculation():
    spot = 80000.0
    t_years = 1.0 / 365.0
    strikes_data = [
        {"strike": 78000.0, "call_oi": 0.0, "put_oi": 100.0, "call_iv": 50.0, "put_iv": 50.0},
        {"strike": 82000.0, "call_oi": 100.0, "put_oi": 0.0, "call_iv": 50.0, "put_iv": 50.0},
    ]
    res_list, net_gex, call_gex, put_gex, max_strike, max_val = calculate_gex_per_strike(
        spot, strikes_data, t_years
    )
    # Dealer: long calls (positive), short puts (negative)
    assert call_gex > 0
    assert put_gex < 0
    assert round(net_gex, 2) == round(call_gex + put_gex, 2)
    assert max_strike in (78000.0, 82000.0)


def test_gamma_flip_finds_zero_crossing():
    spot = 80000.0
    t_years = 3.0 / 365.0
    # Create strike structure where puts dominate below 79000 and calls dominate above 81000
    strikes_data = [
        {"strike": 76000.0, "call_oi": 0.0, "put_oi": 1000.0, "call_iv": 40.0, "put_iv": 40.0},
        {"strike": 84000.0, "call_oi": 1000.0, "put_oi": 0.0, "call_iv": 40.0, "put_iv": 40.0},
    ]
    flip = calculate_gamma_flip(spot, strikes_data, t_years)
    assert flip is not None
    # Symmetrical so flip should be near 80000
    assert 78000.0 <= flip <= 82000.0


def test_gamma_flip_no_crossing():
    spot = 80000.0
    t_years = 1.0 / 365.0
    # Only calls -> all positive GEX everywhere
    strikes_data = [
        {"strike": 80000.0, "call_oi": 1000.0, "put_oi": 0.0, "call_iv": 50.0, "put_iv": 50.0},
    ]
    flip = calculate_gamma_flip(spot, strikes_data, t_years)
    # No crossing within +/- 15%
    assert flip is None
