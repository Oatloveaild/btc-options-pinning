import json
import logging
import math
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from src.config import BKK_TZ, RAW_DIR
from src.db import get_db, log_execution
from src.deribit_client import DeribitClient
from src.events import fetch_forexfactory_events
from src.options_math import (
    bs_delta,
    bs_gamma,
    calculate_gamma_flip,
    calculate_gex_per_strike,
    calculate_max_pain,
    calculate_vol_metrics,
    classify_expiry,
    get_top_strikes,
)

logger = logging.getLogger("collector")


def find_target_expiries(instruments: List[Dict[str, Any]], ref_dt_bkk: datetime, label: str) -> Dict[str, Any]:
    """
    Find target expiry timestamp and big expiry timestamps.
    Deribit expiration_timestamp is in ms UTC (08:00 UTC = 15:00 BKK).
    """
    now_ms = int(ref_dt_bkk.timestamp() * 1000)

    # All active upcoming expiries
    all_expiries_ms = sorted(list(set(inst["expiration_timestamp"] for inst in instruments if inst["expiration_timestamp"] > now_ms)))
    if not all_expiries_ms:
        raise RuntimeError("No active option expiries found on Deribit!")

    def ms_to_bkk_date(ms: int) -> date:
        return datetime.fromtimestamp(ms / 1000.0, tz=BKK_TZ).date()

    target_ms = None
    target_date = None

    if label == "0600":
        today_date = ref_dt_bkk.date()
        for ms in all_expiries_ms:
            if ms_to_bkk_date(ms) == today_date:
                target_ms = ms
                target_date = today_date
                break
    elif label in ("1530", "2100"):
        tomorrow_date = ref_dt_bkk.date() + timedelta(days=1)
        for ms in all_expiries_ms:
            if ms_to_bkk_date(ms) == tomorrow_date:
                target_ms = ms
                target_date = tomorrow_date
                break

    if target_ms is None:
        target_ms = all_expiries_ms[0]
        target_date = ms_to_bkk_date(target_ms)

    # Next weekly, monthly, quarterly
    next_weekly_ms = None
    next_monthly_ms = None
    next_quarterly_ms = None

    for ms in all_expiries_ms:
        exp_d = ms_to_bkk_date(ms)
        ctype = classify_expiry(exp_d)
        if ctype == "weekly" and next_weekly_ms is None and exp_d > target_date:
            next_weekly_ms = ms
        elif ctype == "monthly" and next_monthly_ms is None and exp_d >= target_date:
            next_monthly_ms = ms
        elif ctype == "quarterly" and next_quarterly_ms is None and exp_d >= target_date:
            next_quarterly_ms = ms

    if next_weekly_ms is None:
        for ms in all_expiries_ms:
            if ms_to_bkk_date(ms).weekday() == 4 and ms_to_bkk_date(ms) > target_date:
                next_weekly_ms = ms
                break

    return {
        "all_expiries_ms": all_expiries_ms,
        "target_ms": target_ms,
        "target_date": target_date,
        "weekly_ms": next_weekly_ms,
        "monthly_ms": next_monthly_ms,
        "quarterly_ms": next_quarterly_ms,
    }


def run_snapshot(label: str = "manual") -> Dict[str, Any]:
    """
    Execute a snapshot cycle:
    1. Fetch Deribit public data for ALL instruments
    2. Process and save ALL active future expiries (including today, weekly, monthly, quarterly, and all upcoming expiries)
    3. Calculate OI change vs previous snapshot for each expiry
    4. Save to SQLite and raw JSON
    """
    ref_dt_bkk = datetime.now(BKK_TZ)
    ref_time_str = ref_dt_bkk.strftime("%Y-%m-%d %H:%M:%S")
    file_ts = ref_dt_bkk.strftime("%Y%m%d_%H%M%S")

    client = DeribitClient()

    try:
        # Step 1: Deribit API Calls
        index_data = client.get_index_price("btc_usd")
        spot = float(index_data["index_price"])

        all_instruments = client.get_instruments("BTC", "option", expired=False)
        book_summary = client.get_book_summary_by_currency("BTC", "option")
        dvol = client.get_dvol("BTC")

        book_map = {item["instrument_name"]: item for item in book_summary}

        # Step 2: Target & All Expiries resolution
        exp_targets = find_target_expiries(all_instruments, ref_dt_bkk, label)
        all_expiries_ms = exp_targets["all_expiries_ms"]
        target_ms = exp_targets["target_ms"]
        target_date = exp_targets["target_date"]
        target_date_str = target_date.strftime("%Y-%m-%d")

        # Query ticker greeks for near-the-money instruments on nearest target expiry
        target_insts = [inst for inst in all_instruments if inst["expiration_timestamp"] == target_ms]
        sorted_by_spot_dist = sorted(target_insts, key=lambda x: abs(x["strike"] - spot))
        ticker_greeks = {}
        for inst in sorted_by_spot_dist[:30]:
            iname = inst["instrument_name"]
            try:
                t_res = client.get_ticker(iname)
                greeks = t_res.get("greeks")
                if greeks:
                    ticker_greeks[iname] = greeks
            except Exception as e:
                logger.warning(f"Ticker error for {iname}: {e}")

        # Process EVERY active future expiry
        processed_expiries = []
        primary_snap_info = {}

        with get_db() as conn:
            for exp_ms in all_expiries_ms:
                exp_dt_bkk = datetime.fromtimestamp(exp_ms / 1000.0, tz=BKK_TZ)
                cur_exp_date = exp_dt_bkk.date()
                cur_exp_date_str = cur_exp_date.strftime("%Y-%m-%d")
                expiry_time_bkk_str = exp_dt_bkk.strftime("%Y-%m-%d %H:%M")

                hours_to_expiry = max(0.0, round((exp_dt_bkk - ref_dt_bkk).total_seconds() / 3600.0, 2))
                t_years = max(1e-6, hours_to_expiry / (24.0 * 365.0))
                expiry_type = classify_expiry(cur_exp_date)

                # Filter instruments for this specific expiry
                cur_insts = [inst for inst in all_instruments if inst["expiration_timestamp"] == exp_ms]
                if not cur_insts:
                    continue

                strikes_set = sorted(list(set(inst["strike"] for inst in cur_insts)))
                strike_data_map = {k: {
                    "strike": k,
                    "call_oi": 0.0,
                    "put_oi": 0.0,
                    "call_iv": 0.0,
                    "put_iv": 0.0,
                    "call_volume": 0.0,
                    "put_volume": 0.0,
                    "call_gamma": 0.0,
                    "put_gamma": 0.0,
                    "call_delta": 0.0,
                    "put_delta": 0.0,
                } for k in strikes_set}

                for inst in cur_insts:
                    iname = inst["instrument_name"]
                    k = inst["strike"]
                    otype = inst["option_type"]
                    b_info = book_map.get(iname, {})

                    oi = float(b_info.get("open_interest", 0.0))
                    vol = float(b_info.get("volume", 0.0))
                    mark_iv = float(b_info.get("mark_iv", 0.0))

                    # Use ticker greeks if available, or BS fallback from mark_iv
                    greeks = ticker_greeks.get(iname)
                    gamma = float(greeks.get("gamma", 0.0)) if greeks else 0.0
                    delta = float(greeks.get("delta", 0.0)) if greeks else 0.0

                    if gamma <= 0.0 and mark_iv > 0:
                        gamma = bs_gamma(spot, k, t_years, mark_iv / 100.0)
                    if delta == 0.0 and mark_iv > 0:
                        delta = bs_delta(spot, k, t_years, mark_iv / 100.0, is_call=(otype == "call"))

                    if otype == "call":
                        strike_data_map[k]["call_oi"] = oi
                        strike_data_map[k]["call_volume"] = vol
                        strike_data_map[k]["call_iv"] = mark_iv
                        strike_data_map[k]["call_gamma"] = gamma
                        strike_data_map[k]["call_delta"] = delta
                    else:
                        strike_data_map[k]["put_oi"] = oi
                        strike_data_map[k]["put_volume"] = vol
                        strike_data_map[k]["put_iv"] = mark_iv
                        strike_data_map[k]["put_gamma"] = gamma
                        strike_data_map[k]["put_delta"] = delta

                strikes_list = [strike_data_map[k] for k in strikes_set]

                # Options math
                max_pain = calculate_max_pain(strikes_list)
                top_strikes = get_top_strikes(strikes_list)

                strikes_gex, net_gex_total, call_gex_total, put_gex_total, max_abs_gex_strike, max_abs_gex_val = (
                    calculate_gex_per_strike(spot, strikes_list, t_years)
                )
                gamma_flip = calculate_gamma_flip(spot, strikes_list, t_years)

                spot_vs_gamma_flip = "at"
                if gamma_flip:
                    spot_vs_gamma_flip = "above" if spot > gamma_flip else "below"

                vol_metrics = calculate_vol_metrics(spot, strikes_list, t_years, dvol)

                total_call_oi = sum(item["call_oi"] for item in strikes_list)
                total_put_oi = sum(item["put_oi"] for item in strikes_list)
                total_oi = total_call_oi + total_put_oi
                total_oi_usd = round(total_oi * spot)
                pc_ratio = round(total_put_oi / total_call_oi, 3) if total_call_oi > 0 else 0.0

                # 24h Volume calculations
                total_call_volume = round(sum(item.get("call_volume", 0.0) for item in strikes_list), 2)
                total_put_volume = round(sum(item.get("put_volume", 0.0) for item in strikes_list), 2)
                total_volume = round(total_call_volume + total_put_volume, 2)
                total_volume_usd = round(total_volume * spot)
                pc_vol_ratio = round(total_put_volume / total_call_volume, 3) if total_call_volume > 0 else 0.0

                # Previous snapshot comparison for this specific expiry
                cur_prev = conn.execute(
                    """
                    SELECT * FROM snapshots
                    WHERE expiry_date = ?
                    ORDER BY snapshot_time_bkk DESC
                    LIMIT 1
                    """,
                    (cur_exp_date_str,),
                )
                prev_snap_row = cur_prev.fetchone()
                prev_snap = dict(prev_snap_row) if prev_snap_row else None

                call_oi_chg = None
                put_oi_chg = None
                total_oi_chg = None
                top3_inc_str = ""
                top3_dec_str = ""
                prev_max_pain = None
                max_pain_moved = 0
                prev_call1_strike = None
                call1_moved = 0
                prev_put1_strike = None
                put1_moved = 0
                prev_snapshot_time_bkk = None
                prev_label = None

                prev_strike_oi = {}
                if prev_snap:
                    prev_snapshot_time_bkk = prev_snap["snapshot_time_bkk"]
                    prev_label = prev_snap["label"]
                    call_oi_chg = round(total_call_oi - prev_snap["total_call_oi"], 1)
                    put_oi_chg = round(total_put_oi - prev_snap["total_put_oi"], 1)
                    total_oi_chg = round(total_oi - prev_snap["total_oi"], 1)

                    prev_max_pain = prev_snap["max_pain"]
                    max_pain_moved = 1 if prev_max_pain != max_pain else 0
                    prev_call1_strike = prev_snap["call1_strike"]
                    call1_moved = 1 if prev_call1_strike != top_strikes["call1_strike"] else 0
                    prev_put1_strike = prev_snap["put1_strike"]
                    put1_moved = 1 if prev_put1_strike != top_strikes["put1_strike"] else 0

                    cur_prev_st = conn.execute(
                        """
                        SELECT strike, call_oi, put_oi, total_oi FROM oi_by_strike
                        WHERE snapshot_time_bkk = ? AND expiry_date = ?
                        """,
                        (prev_snapshot_time_bkk, cur_exp_date_str),
                    )
                    for r in cur_prev_st.fetchall():
                        prev_strike_oi[r["strike"]] = dict(r)

                    strike_diffs = []
                    for item in strikes_gex:
                        k = item["strike"]
                        p_item = prev_strike_oi.get(k, {"call_oi": 0.0, "put_oi": 0.0, "total_oi": 0.0})
                        c_chg = round(item["call_oi"] - p_item.get("call_oi", 0.0), 1)
                        p_chg = round(item["put_oi"] - p_item.get("put_oi", 0.0), 1)
                        t_chg = round((item["call_oi"] + item["put_oi"]) - p_item.get("total_oi", 0.0), 1)
                        item["call_oi_chg"] = c_chg
                        item["put_oi_chg"] = p_chg
                        item["total_oi_chg"] = t_chg
                        strike_diffs.append((k, t_chg))

                    sorted_inc = sorted([x for x in strike_diffs if x[1] > 0], key=lambda x: x[1], reverse=True)[:3]
                    sorted_dec = sorted([x for x in strike_diffs if x[1] < 0], key=lambda x: x[1])[:3]
                    top3_inc_str = ";".join([f"{int(k)}:+{v}" for k, v in sorted_inc])
                    top3_dec_str = ";".join([f"{int(k)}:{v}" for k, v in sorted_dec])

                top5 = sorted(strikes_list, key=lambda x: (x["call_oi"] + x["put_oi"]), reverse=True)[:5]
                top5_str = ";".join([f"{int(x['strike'])}:{x['call_oi']:.1f}C/{x['put_oi']:.1f}P" for x in top5])

                # Save into snapshots table
                snap_dict = {
                    "snapshot_time_bkk": ref_time_str,
                    "label": label,
                    "expiry_date": cur_exp_date_str,
                    "expiry_time_bkk": expiry_time_bkk_str,
                    "hours_to_expiry": hours_to_expiry,
                    "index_price": spot,
                    "n_strikes": len(strikes_list),
                    "total_call_oi": total_call_oi,
                    "total_put_oi": total_put_oi,
                    "total_oi": total_oi,
                    "total_oi_usd": total_oi_usd,
                    "put_call_oi_ratio": pc_ratio,
                    "total_call_volume": total_call_volume,
                    "total_put_volume": total_put_volume,
                    "total_volume": total_volume,
                    "total_volume_usd": total_volume_usd,
                    "put_call_volume_ratio": pc_vol_ratio,
                    "max_pain": max_pain,
                    "price_minus_max_pain": round(spot - max_pain, 2),
                    "price_vs_max_pain_pct": round((spot - max_pain) / max_pain * 100.0, 3) if max_pain > 0 else 0.0,
                    "top_oi_strike": top_strikes["top_oi_strike"],
                    "price_minus_top_oi": round(spot - top_strikes["top_oi_strike"], 2),
                    "price_vs_top_oi_pct": round((spot - top_strikes["top_oi_strike"]) / top_strikes["top_oi_strike"] * 100.0, 3) if top_strikes["top_oi_strike"] > 0 else 0.0,
                    "top_call_strike": top_strikes["call1_strike"],
                    "top_call_oi": top_strikes["call1_oi"],
                    "top_put_strike": top_strikes["put1_strike"],
                    "top_put_oi": top_strikes["put1_oi"],
                    "call1_strike": top_strikes["call1_strike"],
                    "call1_oi": top_strikes["call1_oi"],
                    "call2_strike": top_strikes["call2_strike"],
                    "call2_oi": top_strikes["call2_oi"],
                    "put1_strike": top_strikes["put1_strike"],
                    "put1_oi": top_strikes["put1_oi"],
                    "put2_strike": top_strikes["put2_strike"],
                    "put2_oi": top_strikes["put2_oi"],
                    "top5_strikes": top5_str,
                    "raw_file": f"data/raw/{file_ts}_{label}.json",
                    "expiry_type": expiry_type,
                    "prev_snapshot_time_bkk": prev_snapshot_time_bkk,
                    "prev_label": prev_label,
                    "call_oi_chg": call_oi_chg,
                    "put_oi_chg": put_oi_chg,
                    "total_oi_chg": total_oi_chg,
                    "top3_oi_increase": top3_inc_str,
                    "top3_oi_decrease": top3_dec_str,
                    "prev_max_pain": prev_max_pain,
                    "max_pain_moved": max_pain_moved,
                    "prev_call1_strike": prev_call1_strike,
                    "call1_moved": call1_moved,
                    "prev_put1_strike": prev_put1_strike,
                    "put1_moved": put1_moved,
                    "net_gex_usd": round(net_gex_total),
                    "call_gex_usd": round(call_gex_total),
                    "put_gex_usd": round(put_gex_total),
                    "max_abs_gex_strike": max_abs_gex_strike,
                    "max_abs_gex_usd": round(max_abs_gex_val),
                    "gamma_flip": gamma_flip,
                    "spot_vs_gamma_flip": spot_vs_gamma_flip,
                    "gex_gamma_source": "deribit_ticker",
                    "dvol": vol_metrics["dvol"],
                    "atm_strike": vol_metrics["atm_strike"],
                    "atm_iv": vol_metrics["atm_iv"],
                    "atm_call_iv": vol_metrics["atm_call_iv"],
                    "atm_put_iv": vol_metrics["atm_put_iv"],
                    "call25_strike": vol_metrics["call25_strike"],
                    "call25_iv": vol_metrics["call25_iv"],
                    "put25_strike": vol_metrics["put25_strike"],
                    "put25_iv": vol_metrics["put25_iv"],
                    "skew_25d_rr": vol_metrics["skew_25d_rr"],
                }

                s_cols = ", ".join(snap_dict.keys())
                s_placeholders = ", ".join(["?"] * len(snap_dict))
                conn.execute(
                    f"INSERT OR REPLACE INTO snapshots ({s_cols}) VALUES ({s_placeholders})",
                    list(snap_dict.values()),
                )

                # Save into oi_by_strike table
                for item in strikes_gex:
                    k = item["strike"]
                    c_oi = item["call_oi"]
                    p_oi = item["put_oi"]
                    tot_oi = c_oi + p_oi
                    pct_from_spot = round((k - spot) / spot * 100.0, 2)
                    conn.execute(
                        """
                        INSERT OR REPLACE INTO oi_by_strike (
                            snapshot_time_bkk, label, expiry_date, expiry_time_bkk, index_price,
                            strike, call_oi, put_oi, total_oi, call_oi_usd, put_oi_usd, total_oi_usd,
                            call_volume_24h, put_volume_24h, pct_from_spot, prev_snapshot_time_bkk,
                            call_oi_chg, put_oi_chg, total_oi_chg, call_iv, put_iv,
                            call_delta, put_delta, call_gamma, put_gamma,
                            call_gex_usd, put_gex_usd, net_gex_usd
                        ) VALUES (
                            ?, ?, ?, ?, ?,
                            ?, ?, ?, ?, ?, ?, ?,
                            ?, ?, ?, ?,
                            ?, ?, ?, ?, ?,
                            ?, ?, ?, ?,
                            ?, ?, ?
                        )
                        """,
                        (
                            ref_time_str, label, cur_exp_date_str, expiry_time_bkk_str, spot,
                            k, c_oi, p_oi, tot_oi, round(c_oi * spot), round(p_oi * spot), round(tot_oi * spot),
                            item.get("call_volume", 0.0), item.get("put_volume", 0.0), pct_from_spot, prev_snapshot_time_bkk,
                            item.get("call_oi_chg"), item.get("put_oi_chg"), item.get("total_oi_chg"),
                            item.get("call_iv"), item.get("put_iv"),
                            item.get("call_delta"), item.get("put_delta"),
                            item.get("call_gamma"), item.get("put_gamma"),
                            round(item.get("call_gex_usd", 0.0)), round(item.get("put_gex_usd", 0.0)), round(item.get("net_gex_usd", 0.0)),
                        ),
                    )

                processed_expiries.append(cur_exp_date_str)

                if exp_ms == target_ms:
                    primary_snap_info = {
                        "target_date": cur_exp_date_str,
                        "expiry_type": expiry_type,
                        "spot": spot,
                        "max_pain": max_pain,
                        "top_oi_strike": top_strikes["top_oi_strike"],
                        "total_oi": total_oi,
                        "net_gex_usd": round(net_gex_total),
                        "gamma_flip": gamma_flip,
                    }

            # Update big_expiries table
            big_kinds = [
                ("weekly", exp_targets["weekly_ms"]),
                ("monthly", exp_targets["monthly_ms"]),
                ("quarterly", exp_targets["quarterly_ms"]),
            ]
            for kind, b_ms in big_kinds:
                if not b_ms:
                    continue
                b_insts = [inst for inst in all_instruments if inst["expiration_timestamp"] == b_ms]
                b_strikes_set = sorted(list(set(inst["strike"] for inst in b_insts)))
                b_data_map = {k: {"strike": k, "call_oi": 0.0, "put_oi": 0.0} for k in b_strikes_set}
                for inst in b_insts:
                    iname = inst["instrument_name"]
                    k = inst["strike"]
                    otype = inst["option_type"]
                    b_oi = float(book_map.get(iname, {}).get("open_interest", 0.0))
                    if otype == "call":
                        b_data_map[k]["call_oi"] = b_oi
                    else:
                        b_data_map[k]["put_oi"] = b_oi

                b_strikes_list = [b_data_map[k] for k in b_strikes_set]
                b_mp = calculate_max_pain(b_strikes_list)
                b_top = get_top_strikes(b_strikes_list)
                b_exp_dt = datetime.fromtimestamp(b_ms / 1000.0, tz=BKK_TZ)
                b_hours = max(0.0, round((b_exp_dt - ref_dt_bkk).total_seconds() / 3600.0, 1))

                b_tot_c = sum(x["call_oi"] for x in b_strikes_list)
                b_tot_p = sum(x["put_oi"] for x in b_strikes_list)
                b_tot_oi = b_tot_c + b_tot_p
                b_pc = round(b_tot_p / b_tot_c, 3) if b_tot_c > 0 else 0.0

                b_top5 = sorted(b_strikes_list, key=lambda x: (x["call_oi"] + x["put_oi"]), reverse=True)[:5]
                b_top5_str = ";".join([f"{int(x['strike'])}:{x['call_oi']:.1f}C/{x['put_oi']:.1f}P" for x in b_top5])

                conn.execute(
                    """
                    INSERT OR REPLACE INTO big_expiries (
                        snapshot_time_bkk, label, kind, expiry_date, expiry_time_bkk,
                        hours_to_expiry, index_price, n_strikes, total_call_oi, total_put_oi,
                        total_oi, total_oi_usd, put_call_oi_ratio, max_pain, price_vs_max_pain_pct,
                        top_oi_strike, price_vs_top_oi_pct,
                        call1_strike, call1_oi, call2_strike, call2_oi,
                        put1_strike, put1_oi, put2_strike, put2_oi, top5_strikes
                    ) VALUES (
                        ?, ?, ?, ?, ?,
                        ?, ?, ?, ?, ?,
                        ?, ?, ?, ?, ?,
                        ?, ?,
                        ?, ?, ?, ?,
                        ?, ?, ?, ?, ?
                    )
                    """,
                    (
                        ref_time_str, label, kind, b_exp_dt.strftime("%Y-%m-%d"),
                        b_exp_dt.strftime("%Y-%m-%d %H:%M"), b_hours, spot, len(b_strikes_list),
                        b_tot_c, b_tot_p, b_tot_oi, round(b_tot_oi * spot), b_pc,
                        b_mp, round((spot - b_mp) / b_mp * 100.0, 3) if b_mp > 0 else 0.0,
                        b_top["top_oi_strike"],
                        round((spot - b_top["top_oi_strike"]) / b_top["top_oi_strike"] * 100.0, 3) if b_top["top_oi_strike"] > 0 else 0.0,
                        b_top["call1_strike"], b_top["call1_oi"], b_top["call2_strike"], b_top["call2_oi"],
                        b_top["put1_strike"], b_top["put1_oi"], b_top["put2_strike"], b_top["put2_oi"],
                        b_top5_str,
                    ),
                )

            conn.commit()

        # Step 4: Save raw JSON dump
        raw_filename = f"{file_ts}_{label}_all_expiries.json"
        raw_filepath = RAW_DIR / raw_filename
        raw_dump = {
            "snapshot_time_bkk": ref_dt_bkk.isoformat(),
            "label": label,
            "spot": spot,
            "dvol": dvol,
            "expiries_processed": processed_expiries,
            "instruments_count": len(all_instruments),
        }
        with open(raw_filepath, "w", encoding="utf-8") as f:
            json.dump(raw_dump, f)

        # Step 5: Fetch ForexFactory events
        try:
            fetch_forexfactory_events()
        except Exception as e:
            logger.warning(f"Error fetching ForexFactory: {e}")

        # Step 6: Log execution
        log_execution(
            run_time_bkk=ref_time_str,
            label=label,
            action="snapshot",
            status="SUCCESS",
            message=f"Processed ALL {len(processed_expiries)} active expiries: {', '.join(processed_expiries[:5])}... Spot: {spot:.2f}",
            fallback_used=0,
        )

        # Step 7: Auto-backup database to Google Drive
        try:
            from src.gdrive_backup import backup_database
            backup_database()
        except Exception as e:
            logger.warning(f"Google Drive auto-backup error: {e}")

        # Step 8: Invalidate in-memory cache for instant fresh data
        try:
            from src.cache import memory_cache
            memory_cache.clear()
        except Exception:
            pass

        return {
            "status": "success",
            "snapshot_time_bkk": ref_time_str,
            "expiries_count": len(processed_expiries),
            "expiries": processed_expiries,
            "primary": primary_snap_info,
        }

    except Exception as e:
        logger.error(f"Snapshot failed: {e}", exc_info=True)
        log_execution(
            run_time_bkk=ref_time_str,
            label=label,
            action="snapshot",
            status="ERROR",
            message=str(e),
            fallback_used=0,
        )
        raise e
