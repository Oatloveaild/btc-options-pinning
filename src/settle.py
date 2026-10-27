import logging
from datetime import date, datetime, timedelta
from typing import Any, Dict, List, Optional
from src.config import BKK_TZ
from src.db import get_db, log_execution
from src.deribit_client import DeribitClient
from src.events import check_high_impact_between
from src.options_math import classify_expiry

logger = logging.getLogger("settle")


def run_settlement(target_date_str: Optional[str] = None) -> Dict[str, Any]:
    """
    Settle expiry:
    1. Fetch delivery price from Deribit
    2. Find best snapshot (0600, or fallback 2100 of yesterday, or latest pre-expiry)
    3. Calculate all distances and hit-rates
    4. Save to expiry_results table
    """
    client = DeribitClient()
    ref_dt_bkk = datetime.now(BKK_TZ)
    run_time_str = ref_dt_bkk.strftime("%Y-%m-%d %H:%M:%S")

    # If target_date_str is None, default to today's date
    if not target_date_str:
        target_date_str = ref_dt_bkk.strftime("%Y-%m-%d")

    # Step 1: Fetch delivery prices
    delivery_prices = client.get_delivery_prices("btc_usd", count=50)
    delivery_map = {item["date"]: float(item["delivery_price"]) for item in delivery_prices}

    settlement_price = delivery_map.get(target_date_str)
    if settlement_price is None:
        msg = f"Delivery price for {target_date_str} not yet available from Deribit."
        logger.warning(msg)
        log_execution(run_time_str, "1530", "settle", "ERROR", msg)
        return {"status": "error", "message": msg}

    target_d = datetime.strptime(target_date_str, "%Y-%m-%d").date()
    expiry_type = classify_expiry(target_d)

    # Step 2: Find best snapshot
    # Priority:
    # 1. label == '0600' on the same day
    # 2. label == '2100' on the day before
    # 3. label == '1530' on the day before
    # 4. Any snapshot strictly before 15:00 BKK on expiry date
    snapshot = None
    snapshot_used = None
    note = ""
    fallback_used = 0

    with get_db() as conn:
        # Check 0600
        cur = conn.execute(
            """
            SELECT * FROM snapshots
            WHERE expiry_date = ? AND label = '0600'
            ORDER BY snapshot_time_bkk DESC LIMIT 1
            """,
            (target_date_str,),
        )
        row = cur.fetchone()
        if row:
            snapshot = dict(row)
            snapshot_used = "0600"
        else:
            # Fallback 2100
            cur = conn.execute(
                """
                SELECT * FROM snapshots
                WHERE expiry_date = ? AND label = '2100'
                ORDER BY snapshot_time_bkk DESC LIMIT 1
                """,
                (target_date_str,),
            )
            row = cur.fetchone()
            if row:
                snapshot = dict(row)
                snapshot_used = "2100(fallback:last pre-expiry)"
                fallback_used = 1
                note = "fallback: used 2100 snapshot"
            else:
                # Any pre-expiry snapshot
                cur = conn.execute(
                    """
                    SELECT * FROM snapshots
                    WHERE expiry_date = ?
                    ORDER BY snapshot_time_bkk DESC LIMIT 1
                    """,
                    (target_date_str,),
                )
                row = cur.fetchone()
                if row:
                    snapshot = dict(row)
                    snapshot_used = f"{snapshot['label']}(fallback)"
                    fallback_used = 1
                    note = f"fallback: used {snapshot['label']} snapshot"

        # Check prior 1530 and 2100 values for trend comparison
        max_pain_prev_1530 = None
        max_pain_prev_2100 = None
        top_oi_prev_1530 = None
        top_oi_prev_2100 = None

        cur_1530 = conn.execute(
            "SELECT max_pain, top_oi_strike FROM snapshots WHERE expiry_date = ? AND label = '1530' LIMIT 1",
            (target_date_str,),
        )
        row_1530 = cur_1530.fetchone()
        if row_1530:
            max_pain_prev_1530 = row_1530["max_pain"]
            top_oi_prev_1530 = row_1530["top_oi_strike"]

        cur_2100 = conn.execute(
            "SELECT max_pain, top_oi_strike FROM snapshots WHERE expiry_date = ? AND label = '2100' LIMIT 1",
            (target_date_str,),
        )
        row_2100 = cur_2100.fetchone()
        if row_2100:
            max_pain_prev_2100 = row_2100["max_pain"]
            top_oi_prev_2100 = row_2100["top_oi_strike"]

    # Check high-impact events between 06:00 and 15:00
    has_high_event, event_summary = check_high_impact_between(target_date_str, "06:00", "15:00")

    if not snapshot:
        # Settlement only
        note = "settlement only - no OI snapshot collected for this expiry"
        with get_db() as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO expiry_results (
                    expiry_date, settlement_price, note, expiry_type,
                    has_high_impact_event, high_impact_events
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (target_date_str, settlement_price, note, expiry_type, has_high_event, event_summary),
            )
            conn.commit()

        log_execution(
            run_time_str, "1530", "settle", "SUCCESS",
            f"Settled {target_date_str} at {settlement_price:.2f} (no snapshot)",
            fallback_used=1,
        )
        return {"status": "success", "note": note, "settlement_price": settlement_price}

    # Calculate metrics with snapshot
    snap_time_bkk = snapshot["snapshot_time_bkk"]
    spot_at_snapshot = snapshot["index_price"]
    max_pain = snapshot["max_pain"]
    top_oi_strike = snapshot["top_oi_strike"]

    dist_max_pain_usd = round(settlement_price - max_pain, 2)
    dist_max_pain_pct = round(dist_max_pain_usd / max_pain * 100.0, 3) if max_pain > 0 else 0.0
    within_0_5_mp = 1 if abs(dist_max_pain_pct) <= 0.5 else 0
    within_1_mp = 1 if abs(dist_max_pain_pct) <= 1.0 else 0

    dist_top_oi_usd = round(settlement_price - top_oi_strike, 2)
    dist_top_oi_pct = round(dist_top_oi_usd / top_oi_strike * 100.0, 3) if top_oi_strike > 0 else 0.0
    within_0_5_top = 1 if abs(dist_top_oi_pct) <= 0.5 else 0
    within_1_top = 1 if abs(dist_top_oi_pct) <= 1.0 else 0

    spot_to_max_pain_move_pct = round((max_pain - spot_at_snapshot) / spot_at_snapshot * 100.0, 3)

    c1_strike = snapshot["call1_strike"]
    c1_oi = snapshot["call1_oi"]
    c1_dist_usd = round(settlement_price - c1_strike, 2) if c1_strike else None
    c1_dist_pct = round(c1_dist_usd / c1_strike * 100.0, 3) if c1_strike else None
    c1_within_0_5 = 1 if c1_dist_pct is not None and abs(c1_dist_pct) <= 0.5 else 0
    c1_within_1 = 1 if c1_dist_pct is not None and abs(c1_dist_pct) <= 1.0 else 0

    c2_strike = snapshot["call2_strike"]
    c2_oi = snapshot["call2_oi"]
    c2_dist_usd = round(settlement_price - c2_strike, 2) if c2_strike else None
    c2_dist_pct = round(c2_dist_usd / c2_strike * 100.0, 3) if c2_strike else None
    c2_within_0_5 = 1 if c2_dist_pct is not None and abs(c2_dist_pct) <= 0.5 else 0
    c2_within_1 = 1 if c2_dist_pct is not None and abs(c2_dist_pct) <= 1.0 else 0

    p1_strike = snapshot["put1_strike"]
    p1_oi = snapshot["put1_oi"]
    p1_dist_usd = round(settlement_price - p1_strike, 2) if p1_strike else None
    p1_dist_pct = round(p1_dist_usd / p1_strike * 100.0, 3) if p1_strike else None
    p1_within_0_5 = 1 if p1_dist_pct is not None and abs(p1_dist_pct) <= 0.5 else 0
    p1_within_1 = 1 if p1_dist_pct is not None and abs(p1_dist_pct) <= 1.0 else 0

    p2_strike = snapshot["put2_strike"]
    p2_oi = snapshot["put2_oi"]
    p2_dist_usd = round(settlement_price - p2_strike, 2) if p2_strike else None
    p2_dist_pct = round(p2_dist_usd / p2_strike * 100.0, 3) if p2_strike else None
    p2_within_0_5 = 1 if p2_dist_pct is not None and abs(p2_dist_pct) <= 0.5 else 0
    p2_within_1 = 1 if p2_dist_pct is not None and abs(p2_dist_pct) <= 1.0 else 0

    range_low = min(p1_strike, c1_strike) if (p1_strike and c1_strike) else None
    range_high = max(p1_strike, c1_strike) if (p1_strike and c1_strike) else None
    range_width_pct = (
        round((range_high - range_low) / range_low * 100.0, 3)
        if (range_low and range_high and range_low > 0)
        else None
    )

    settled_in_range = (
        1 if (range_low and range_high and range_low <= settlement_price <= range_high) else 0
    )
    spot_in_range_at_snap = (
        1 if (range_low and range_high and range_low <= spot_at_snapshot <= range_high) else 0
    )

    # Gamma flip and GEX
    snap_net_gex_usd = snapshot.get("net_gex_usd")
    snap_gamma_flip = snapshot.get("gamma_flip")
    settled_vs_gamma_flip = None
    dist_gamma_flip_pct = None
    if snap_gamma_flip:
        settled_vs_gamma_flip = "above" if settlement_price > snap_gamma_flip else "below"
        dist_gamma_flip_pct = round((settlement_price - snap_gamma_flip) / snap_gamma_flip * 100.0, 3)

    snap_max_abs_gex_strike = snapshot.get("max_abs_gex_strike")
    dist_max_abs_gex_pct = None
    if snap_max_abs_gex_strike and snap_max_abs_gex_strike > 0:
        dist_max_abs_gex_pct = round(
            (settlement_price - snap_max_abs_gex_strike) / snap_max_abs_gex_strike * 100.0, 3
        )

    with get_db() as conn:
        conn.execute(
            """
            INSERT OR REPLACE INTO expiry_results (
                expiry_date, settlement_price, snapshot_used, snapshot_time_bkk,
                spot_at_snapshot, max_pain, top_oi_strike,
                dist_max_pain_usd, dist_max_pain_pct, [within_0.5pct_max_pain], within_1pct_max_pain,
                dist_top_oi_usd, dist_top_oi_pct, [within_0.5pct_top_oi], within_1pct_top_oi,
                spot_to_max_pain_move_pct, max_pain_prev_1530, max_pain_prev_2100,
                top_oi_prev_1530, top_oi_prev_2100, note,
                call1_strike, call1_oi, call1_dist_usd, call1_dist_pct, [call1_within_0.5pct], call1_within_1pct,
                call2_strike, call2_oi, call2_dist_usd, call2_dist_pct, [call2_within_0.5pct], call2_within_1pct,
                put1_strike, put1_oi, put1_dist_usd, put1_dist_pct, [put1_within_0.5pct], put1_within_1pct,
                put2_strike, put2_oi, put2_dist_usd, put2_dist_pct, [put2_within_0.5pct], put2_within_1pct,
                range_low, range_high, range_width_pct,
                settled_in_put1_call1_range, spot_in_range_at_snapshot,
                expiry_type, high_impact_events, has_high_impact_event,
                snap_net_gex_usd, snap_gamma_flip, settled_vs_gamma_flip, dist_gamma_flip_pct,
                snap_max_abs_gex_strike, dist_max_abs_gex_pct,
                snap_dvol, snap_atm_iv, snap_skew_25d_rr, snap_total_oi_chg
            ) VALUES (
                ?, ?, ?, ?,
                ?, ?, ?,
                ?, ?, ?, ?,
                ?, ?, ?, ?,
                ?, ?, ?,
                ?, ?, ?,
                ?, ?, ?, ?, ?, ?,
                ?, ?, ?, ?, ?, ?,
                ?, ?, ?, ?, ?, ?,
                ?, ?, ?, ?, ?, ?,
                ?, ?, ?,
                ?, ?,
                ?, ?, ?,
                ?, ?, ?, ?,
                ?, ?,
                ?, ?, ?, ?
            )
            """,
            (
                target_date_str, settlement_price, snapshot_used, snap_time_bkk,
                spot_at_snapshot, max_pain, top_oi_strike,
                dist_max_pain_usd, dist_max_pain_pct, within_0_5_mp, within_1_mp,
                dist_top_oi_usd, dist_top_oi_pct, within_0_5_top, within_1_top,
                spot_to_max_pain_move_pct, max_pain_prev_1530, max_pain_prev_2100,
                top_oi_prev_1530, top_oi_prev_2100, note,
                c1_strike, c1_oi, c1_dist_usd, c1_dist_pct, c1_within_0_5, c1_within_1,
                c2_strike, c2_oi, c2_dist_usd, c2_dist_pct, c2_within_0_5, c2_within_1,
                p1_strike, p1_oi, p1_dist_usd, p1_dist_pct, p1_within_0_5, p1_within_1,
                p2_strike, p2_oi, p2_dist_usd, p2_dist_pct, p2_within_0_5, p2_within_1,
                range_low, range_high, range_width_pct,
                settled_in_range, spot_in_range_at_snap,
                expiry_type, event_summary, has_high_event,
                snap_net_gex_usd, snap_gamma_flip, settled_vs_gamma_flip, dist_gamma_flip_pct,
                snap_max_abs_gex_strike, dist_max_abs_gex_pct,
                snapshot.get("dvol"), snapshot.get("atm_iv"), snapshot.get("skew_25d_rr"), snapshot.get("total_oi_chg"),
            ),
        )
        conn.commit()

    log_execution(
        run_time_str, "1530", "settle", "SUCCESS",
        f"Settled {target_date_str} at {settlement_price:.2f} using {snapshot_used}. Dist to Max Pain: {dist_max_pain_pct:+.2f}%",
        fallback_used=fallback_used,
    )

    # Settle paper trades and roll next active cycle
    try:
        from src.paper_trade import settle_active_paper_trades
        settle_active_paper_trades(target_date_str, settlement_price)
    except Exception as e:
        logger.warning(f"Paper trade settlement error: {e}")

    # Auto-backup database to Google Drive after settlement
    try:
        from src.gdrive_backup import backup_database
        backup_database()
    except Exception as e:
        logger.warning(f"Google Drive auto-backup error after settlement: {e}")

    # Invalidate in-memory cache for instant fresh settlement stats
    try:
        from src.cache import memory_cache
        memory_cache.clear()
    except Exception:
        pass

    return {
        "status": "success",
        "expiry_date": target_date_str,
        "settlement_price": settlement_price,
        "snapshot_used": snapshot_used,
        "dist_max_pain_pct": dist_max_pain_pct,
        "within_0.5pct_max_pain": within_0_5_mp,
        "within_1pct_max_pain": within_1_mp,
        "settled_in_put1_call1_range": settled_in_range,
    }
