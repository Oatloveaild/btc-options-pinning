import math
from datetime import date, datetime
from typing import Dict, List, Optional, Tuple

SQRT_2PI = math.sqrt(2.0 * math.pi)


def norm_pdf(x: float) -> float:
    """Standard normal probability density function."""
    return math.exp(-0.5 * x * x) / SQRT_2PI


def norm_cdf(x: float) -> float:
    """Standard normal cumulative distribution function."""
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def classify_expiry(expiry_dt: date) -> str:
    """
    Classify expiry type:
    - daily: non-Friday
    - weekly: Friday, but not last Friday of month
    - monthly: last Friday of month (months other than 3, 6, 9, 12)
    - quarterly: last Friday of March, June, September, December
    """
    if expiry_dt.weekday() != 4:  # Monday=0, Friday=4
        return "daily"

    # Check if last Friday of month: adding 7 days changes the month
    month = expiry_dt.month
    day = expiry_dt.day
    # If day + 7 exceeds month length, or month of day+7 is different
    # Easiest check: check if adding 7 days results in month != expiry_dt.month
    try:
        next_week = date(expiry_dt.year, expiry_dt.month, day + 7)
        is_last_friday = False
    except ValueError:
        is_last_friday = True

    if is_last_friday:
        if month in (3, 6, 9, 12):
            return "quarterly"
        return "monthly"
    return "weekly"


def bs_gamma(spot: float, strike: float, t_years: float, iv_decimal: float, r: float = 0.0) -> float:
    """
    Calculate Black-Scholes gamma.
    Gamma = n(d1) / (S * sigma * sqrt(T))
    Safe against zero or negative values.
    """
    if spot <= 0.0 or strike <= 0.0 or t_years <= 1e-7 or iv_decimal <= 1e-4:
        return 0.0

    try:
        sigma_sqrt_t = iv_decimal * math.sqrt(t_years)
        if sigma_sqrt_t <= 0.0:
            return 0.0
        d1 = (math.log(spot / strike) + (r + 0.5 * iv_decimal * iv_decimal) * t_years) / sigma_sqrt_t
        return norm_pdf(d1) / (spot * sigma_sqrt_t)
    except (ValueError, OverflowError, ZeroDivisionError):
        return 0.0


def bs_delta(spot: float, strike: float, t_years: float, iv_decimal: float, is_call: bool, r: float = 0.0) -> float:
    """
    Calculate Black-Scholes delta.
    Call delta = N(d1), Put delta = N(d1) - 1
    """
    if spot <= 0.0 or strike <= 0.0 or t_years <= 1e-7 or iv_decimal <= 1e-4:
        if is_call:
            return 1.0 if spot >= strike else 0.0
        else:
            return 0.0 if spot >= strike else -1.0

    try:
        sigma_sqrt_t = iv_decimal * math.sqrt(t_years)
        d1 = (math.log(spot / strike) + (r + 0.5 * iv_decimal * iv_decimal) * t_years) / sigma_sqrt_t
        c_delta = norm_cdf(d1)
        return c_delta if is_call else (c_delta - 1.0)
    except (ValueError, OverflowError, ZeroDivisionError):
        return 0.0


def bs_price(spot: float, strike: float, t_years: float, iv_decimal: float, is_call: bool, r: float = 0.0) -> float:
    """
    Calculate Black-Scholes option price in USD.
    Call = S * N(d1) - K * exp(-r*T) * N(d2)
    Put = K * exp(-r*T) * N(-d2) - S * N(-d1)
    """
    if spot <= 0.0 or strike <= 0.0:
        return 0.0
    if t_years <= 1e-7 or iv_decimal <= 1e-4:
        if is_call:
            return max(0.0, spot - strike)
        else:
            return max(0.0, strike - spot)

    try:
        sigma_sqrt_t = iv_decimal * math.sqrt(t_years)
        d1 = (math.log(spot / strike) + (r + 0.5 * iv_decimal * iv_decimal) * t_years) / sigma_sqrt_t
        d2 = d1 - sigma_sqrt_t
        discount = math.exp(-r * t_years)

        if is_call:
            price = spot * norm_cdf(d1) - strike * discount * norm_cdf(d2)
        else:
            price = strike * discount * norm_cdf(-d2) - spot * norm_cdf(-d1)

        return max(0.0, price)
    except (ValueError, OverflowError, ZeroDivisionError):
        return max(0.0, spot - strike) if is_call else max(0.0, strike - spot)


def calculate_max_pain(strikes_oi: List[Dict[str, float]]) -> float:
    """
    Calculate Max Pain strike.
    strikes_oi: list of dicts with 'strike', 'call_oi', 'put_oi'.
    Loss to option buyers at price S:
    Loss(S) = sum_i(call_oi_i * max(0, S - K_i)) + sum_i(put_oi_i * max(0, K_i - S))
    Max pain is S in {K_i} that minimizes total payout / buyer loss.
    """
    if not strikes_oi:
        return 0.0

    strikes = [item["strike"] for item in strikes_oi]
    min_loss = float("inf")
    max_pain_strike = strikes[0]

    for candidate in strikes:
        total_loss = 0.0
        for item in strikes_oi:
            k = item["strike"]
            c_oi = item.get("call_oi", 0.0)
            p_oi = item.get("put_oi", 0.0)

            if candidate > k:
                total_loss += c_oi * (candidate - k)
            elif candidate < k:
                total_loss += p_oi * (k - candidate)

        if total_loss < min_loss:
            min_loss = total_loss
            max_pain_strike = candidate

    return max_pain_strike


def get_top_strikes(strikes_oi: List[Dict[str, float]]) -> Dict[str, any]:
    """
    Extract:
    - call1_strike, call1_oi, call2_strike, call2_oi
    - put1_strike, put1_oi, put2_strike, put2_oi
    - top_oi_strike, top_oi
    """
    sorted_calls = sorted(strikes_oi, key=lambda x: x.get("call_oi", 0.0), reverse=True)
    sorted_puts = sorted(strikes_oi, key=lambda x: x.get("put_oi", 0.0), reverse=True)
    sorted_total = sorted(strikes_oi, key=lambda x: (x.get("call_oi", 0.0) + x.get("put_oi", 0.0)), reverse=True)

    c1 = sorted_calls[0] if len(sorted_calls) > 0 else {"strike": 0.0, "call_oi": 0.0}
    c2 = sorted_calls[1] if len(sorted_calls) > 1 else {"strike": 0.0, "call_oi": 0.0}

    p1 = sorted_puts[0] if len(sorted_puts) > 0 else {"strike": 0.0, "put_oi": 0.0}
    p2 = sorted_puts[1] if len(sorted_puts) > 1 else {"strike": 0.0, "put_oi": 0.0}

    t1 = sorted_total[0] if len(sorted_total) > 0 else {"strike": 0.0, "call_oi": 0.0, "put_oi": 0.0}
    t_oi = t1.get("call_oi", 0.0) + t1.get("put_oi", 0.0)

    return {
        "call1_strike": c1["strike"],
        "call1_oi": c1.get("call_oi", 0.0),
        "call2_strike": c2["strike"],
        "call2_oi": c2.get("call_oi", 0.0),
        "put1_strike": p1["strike"],
        "put1_oi": p1.get("put_oi", 0.0),
        "put2_strike": p2["strike"],
        "put2_oi": p2.get("put_oi", 0.0),
        "top_oi_strike": t1["strike"],
        "top_oi": t_oi,
    }


def calculate_gex_per_strike(
    spot: float,
    strikes_data: List[Dict[str, float]],
    t_years: float,
) -> Tuple[List[Dict[str, float]], float, float, float, float, float]:
    """
    Calculate dealer GEX per strike.
    Dealer assumed LONG calls / SHORT puts.
    GEX_usd = gamma * OI * spot^2 * 0.01 (call +, put -)
    Returns:
    (strike_gex_list, net_gex_usd, call_gex_usd, put_gex_usd, max_abs_gex_strike, max_abs_gex_usd)
    """
    total_call_gex = 0.0
    total_put_gex = 0.0
    res_list = []

    max_abs_gex = -1.0
    max_abs_gex_strike = spot

    for item in strikes_data:
        k = item["strike"]
        c_oi = item.get("call_oi", 0.0)
        p_oi = item.get("put_oi", 0.0)

        # Gamma: prefer Deribit gamma if provided and > 0, otherwise compute via BS fallback
        gamma_c = item.get("call_gamma", 0.0)
        gamma_p = item.get("put_gamma", 0.0)

        if gamma_c <= 0.0 and "call_iv" in item and item["call_iv"] > 0:
            gamma_c = bs_gamma(spot, k, t_years, item["call_iv"] / 100.0)
        if gamma_p <= 0.0 and "put_iv" in item and item["put_iv"] > 0:
            gamma_p = bs_gamma(spot, k, t_years, item["put_iv"] / 100.0)

        # Call GEX is positive, Put GEX is negative
        # 1% spot move = spot * 0.01
        multiplier = spot * spot * 0.01
        call_gex = gamma_c * c_oi * multiplier
        put_gex = -gamma_p * p_oi * multiplier
        net_gex = call_gex + put_gex

        total_call_gex += call_gex
        total_put_gex += put_gex

        res_item = dict(item)
        res_item["call_gex_usd"] = call_gex
        res_item["put_gex_usd"] = put_gex
        res_item["net_gex_usd"] = net_gex
        res_item["call_gamma"] = gamma_c
        res_item["put_gamma"] = gamma_p
        res_list.append(res_item)

        if abs(net_gex) > max_abs_gex:
            max_abs_gex = abs(net_gex)
            max_abs_gex_strike = k

    net_gex_total = total_call_gex + total_put_gex
    return res_list, net_gex_total, total_call_gex, total_put_gex, max_abs_gex_strike, max_abs_gex


def calculate_gamma_flip(
    spot: float,
    strikes_data: List[Dict[str, float]],
    t_years: float,
) -> Optional[float]:
    """
    Gamma flip = re-price GEX total using Black-Scholes across spot +/-15% (step 0.05%),
    finding the zero crossing closest to spot.
    """
    if spot <= 0 or t_years <= 1e-7 or not strikes_data:
        return None

    # Step: 0.05% = 0.0005. Range: -15% to +15% (-0.15 to +0.15, 601 steps)
    grid_steps = 600
    p_min = -0.15
    p_max = 0.15
    step_size = (p_max - p_min) / grid_steps

    grid_spots = []
    grid_gex = []

    # Pre-filter strikes with positive OI and valid IV
    valid_strikes = []
    for item in strikes_data:
        k = item["strike"]
        c_oi = item.get("call_oi", 0.0)
        p_oi = item.get("put_oi", 0.0)
        # Use average IV of call/put or whichever is available
        c_iv = item.get("call_iv", 0.0)
        p_iv = item.get("put_iv", 0.0)
        ivs = [x for x in (c_iv, p_iv) if x > 0.0]
        avg_iv = (sum(ivs) / len(ivs)) / 100.0 if ivs else 0.50  # default 50% if unavail
        if c_oi > 0 or p_oi > 0:
            valid_strikes.append((k, c_oi, p_oi, avg_iv))

    if not valid_strikes:
        return None

    for i in range(grid_steps + 1):
        pct = p_min + i * step_size
        s_g = spot * (1.0 + pct)
        grid_spots.append(s_g)

        # Calculate net GEX at s_g
        s_g_mult = s_g * s_g * 0.01
        net_gex_sg = 0.0
        for k, c_oi, p_oi, iv in valid_strikes:
            g = bs_gamma(s_g, k, t_years, iv)
            net_gex_sg += g * (c_oi - p_oi) * s_g_mult
        grid_gex.append(net_gex_sg)

    # Find zero crossings
    crossings = []
    for i in range(len(grid_gex) - 1):
        g1 = grid_gex[i]
        g2 = grid_gex[i + 1]
        s1 = grid_spots[i]
        s2 = grid_spots[i + 1]

        if g1 == 0.0:
            crossings.append(s1)
        elif (g1 > 0 and g2 < 0) or (g1 < 0 and g2 > 0):
            # Linear interpolation for zero crossing: s = s1 - g1 * (s2 - s1) / (g2 - g1)
            denom = g2 - g1
            if denom != 0.0:
                s_zero = s1 - g1 * (s2 - s1) / denom
                crossings.append(s_zero)

    if not crossings:
        return None

    # Pick the crossing closest to current spot
    closest_flip = min(crossings, key=lambda x: abs(x - spot))
    return round(closest_flip, 2)


def calculate_vol_metrics(
    spot: float,
    strikes_data: List[Dict[str, float]],
    t_years: float,
    dvol: Optional[float] = None,
) -> Dict[str, any]:
    """
    Calculate:
    - dvol: passed in or None
    - atm_strike: strike closest to spot
    - atm_iv, atm_call_iv, atm_put_iv
    - call25_strike, call25_iv, put25_strike, put25_iv, skew_25d_rr (call25_iv - put25_iv)
    """
    if not strikes_data:
        return {
            "dvol": dvol,
            "atm_strike": spot,
            "atm_iv": None,
            "atm_call_iv": None,
            "atm_put_iv": None,
            "call25_strike": None,
            "call25_iv": None,
            "put25_strike": None,
            "put25_iv": None,
            "skew_25d_rr": None,
        }

    # ATM Strike
    atm_item = min(strikes_data, key=lambda x: abs(x["strike"] - spot))
    atm_strike = atm_item["strike"]
    atm_c_iv = atm_item.get("call_iv")
    atm_p_iv = atm_item.get("put_iv")
    atm_ivs = [x for x in (atm_c_iv, atm_p_iv) if x is not None and x > 0]
    atm_iv = round(sum(atm_ivs) / len(atm_ivs), 2) if atm_ivs else None

    # 25-delta calls and puts
    # Find call strike with delta closest to 0.25 and put strike with delta closest to -0.25
    best_c25 = None
    min_c_diff = float("inf")
    best_p25 = None
    min_p_diff = float("inf")

    for item in strikes_data:
        k = item["strike"]
        # Call delta
        c_delta = item.get("call_delta")
        c_iv = item.get("call_iv")
        if c_delta is None or c_delta == 0.0:
            if c_iv and c_iv > 0:
                c_delta = bs_delta(spot, k, t_years, c_iv / 100.0, is_call=True)

        if c_delta is not None and c_iv and c_iv > 0:
            diff = abs(c_delta - 0.25)
            if diff < min_c_diff:
                min_c_diff = diff
                best_c25 = (k, c_iv)

        # Put delta
        p_delta = item.get("put_delta")
        p_iv = item.get("put_iv")
        if p_delta is None or p_delta == 0.0:
            if p_iv and p_iv > 0:
                p_delta = bs_delta(spot, k, t_years, p_iv / 100.0, is_call=False)

        if p_delta is not None and p_iv and p_iv > 0:
            diff = abs(p_delta - (-0.25))
            if diff < min_p_diff:
                min_p_diff = diff
                best_p25 = (k, p_iv)

    call25_strike = best_c25[0] if best_c25 else None
    call25_iv = round(best_c25[1], 2) if best_c25 else None
    put25_strike = best_p25[0] if best_p25 else None
    put25_iv = round(best_p25[1], 2) if best_p25 else None

    skew_25d_rr = None
    if call25_iv is not None and put25_iv is not None:
        skew_25d_rr = round(call25_iv - put25_iv, 2)

    return {
        "dvol": dvol,
        "atm_strike": atm_strike,
        "atm_iv": atm_iv,
        "atm_call_iv": round(atm_c_iv, 2) if atm_c_iv else None,
        "atm_put_iv": round(atm_p_iv, 2) if atm_p_iv else None,
        "call25_strike": call25_strike,
        "call25_iv": call25_iv,
        "put25_strike": put25_strike,
        "put25_iv": put25_iv,
        "skew_25d_rr": skew_25d_rr,
    }
