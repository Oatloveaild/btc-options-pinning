import csv
import logging
import os
import shutil
from pathlib import Path
from typing import Any, Dict, List
from src.config import DATA_DIR, DB_PATH, RAW_DIR
from src.db import get_db, init_db

logger = logging.getLogger("importer")


def clean_val(v: Any, target_type=float):
    if v is None or v == "" or v == "None":
        return None
    try:
        if target_type == int:
            return int(float(v))
        return target_type(v)
    except Exception:
        return None


def import_historical_data(source_dir: str = r"C:\Users\Noname\Downloads\btc_oi_history"):
    source_path = Path(source_dir)
    if not source_path.exists():
        logger.error(f"Source path {source_dir} not found!")
        return

    init_db()
    data_path = source_path / "data"

    # 1. Copy raw JSON files
    src_raw = data_path / "raw"
    if src_raw.exists():
        count_raw = 0
        for f in src_raw.glob("*.json"):
            dst_file = RAW_DIR / f.name
            if not dst_file.exists():
                shutil.copy2(f, dst_file)
                count_raw += 1
        print(f"Copied {count_raw} raw JSON files to {RAW_DIR}")

    with get_db() as conn:
        cursor = conn.cursor()

        # 2. Import snapshots.csv
        snapshots_csv = data_path / "snapshots.csv"
        if snapshots_csv.exists():
            with open(snapshots_csv, "r", encoding="utf-8-sig") as f:
                reader = csv.DictReader(f)
                s_count = 0
                for row in reader:
                    cursor.execute(
                        """
                        INSERT OR REPLACE INTO snapshots (
                            snapshot_time_bkk, label, expiry_date, expiry_time_bkk, hours_to_expiry,
                            index_price, n_strikes, total_call_oi, total_put_oi, total_oi, total_oi_usd,
                            put_call_oi_ratio, max_pain, price_minus_max_pain, price_vs_max_pain_pct,
                            top_oi_strike, price_minus_top_oi, price_vs_top_oi_pct,
                            top_call_strike, top_call_oi, top_put_strike, top_put_oi,
                            top5_strikes, raw_file, call1_strike, call1_oi, call2_strike, call2_oi,
                            put1_strike, put1_oi, put2_strike, put2_oi, expiry_type,
                            prev_snapshot_time_bkk, prev_label, call_oi_chg, put_oi_chg, total_oi_chg,
                            top3_oi_increase, top3_oi_decrease, prev_max_pain, max_pain_moved,
                            prev_call1_strike, call1_moved, prev_put1_strike, put1_moved,
                            net_gex_usd, call_gex_usd, put_gex_usd, max_abs_gex_strike, max_abs_gex_usd,
                            gamma_flip, spot_vs_gamma_flip, gex_gamma_source,
                            dvol, atm_strike, atm_iv, atm_call_iv, atm_put_iv,
                            call25_strike, call25_iv, put25_strike, put25_iv, skew_25d_rr
                        ) VALUES (
                            ?, ?, ?, ?, ?,
                            ?, ?, ?, ?, ?, ?,
                            ?, ?, ?, ?,
                            ?, ?, ?,
                            ?, ?, ?, ?,
                            ?, ?, ?, ?, ?, ?,
                            ?, ?, ?, ?, ?,
                            ?, ?, ?, ?, ?,
                            ?, ?, ?, ?,
                            ?, ?, ?, ?,
                            ?, ?, ?, ?, ?,
                            ?, ?, ?,
                            ?, ?, ?, ?, ?,
                            ?, ?, ?, ?, ?
                        )
                        """,
                        (
                            row["snapshot_time_bkk"], row["label"], row["expiry_date"], row["expiry_time_bkk"],
                            clean_val(row.get("hours_to_expiry")), clean_val(row.get("index_price")),
                            clean_val(row.get("n_strikes"), int), clean_val(row.get("total_call_oi")),
                            clean_val(row.get("total_put_oi")), clean_val(row.get("total_oi")),
                            clean_val(row.get("total_oi_usd")), clean_val(row.get("put_call_oi_ratio")),
                            clean_val(row.get("max_pain")), clean_val(row.get("price_minus_max_pain")),
                            clean_val(row.get("price_vs_max_pain_pct")), clean_val(row.get("top_oi_strike")),
                            clean_val(row.get("price_minus_top_oi")), clean_val(row.get("price_vs_top_oi_pct")),
                            clean_val(row.get("top_call_strike")), clean_val(row.get("top_call_oi")),
                            clean_val(row.get("top_put_strike")), clean_val(row.get("top_put_oi")),
                            row.get("top5_strikes"), row.get("raw_file"),
                            clean_val(row.get("call1_strike")), clean_val(row.get("call1_oi")),
                            clean_val(row.get("call2_strike")), clean_val(row.get("call2_oi")),
                            clean_val(row.get("put1_strike")), clean_val(row.get("put1_oi")),
                            clean_val(row.get("put2_strike")), clean_val(row.get("put2_oi")),
                            row.get("expiry_type"),
                            row.get("prev_snapshot_time_bkk") or None, row.get("prev_label") or None,
                            clean_val(row.get("call_oi_chg")), clean_val(row.get("put_oi_chg")),
                            clean_val(row.get("total_oi_chg")),
                            row.get("top3_oi_increase"), row.get("top3_oi_decrease"),
                            clean_val(row.get("prev_max_pain")), clean_val(row.get("max_pain_moved"), int),
                            clean_val(row.get("prev_call1_strike")), clean_val(row.get("call1_moved"), int),
                            clean_val(row.get("prev_put1_strike")), clean_val(row.get("put1_moved"), int),
                            clean_val(row.get("net_gex_usd")), clean_val(row.get("call_gex_usd")),
                            clean_val(row.get("put_gex_usd")), clean_val(row.get("max_abs_gex_strike")),
                            clean_val(row.get("max_abs_gex_usd")),
                            clean_val(row.get("gamma_flip")), row.get("spot_vs_gamma_flip") or None,
                            row.get("gex_gamma_source") or None,
                            clean_val(row.get("dvol")), clean_val(row.get("atm_strike")),
                            clean_val(row.get("atm_iv")), clean_val(row.get("atm_call_iv")),
                            clean_val(row.get("atm_put_iv")),
                            clean_val(row.get("call25_strike")), clean_val(row.get("call25_iv")),
                            clean_val(row.get("put25_strike")), clean_val(row.get("put25_iv")),
                            clean_val(row.get("skew_25d_rr")),
                        ),
                    )
                    s_count += 1
                print(f"Imported {s_count} snapshots.")

        # 3. Import oi_by_strike.csv
        oi_csv = data_path / "oi_by_strike.csv"
        if oi_csv.exists():
            with open(oi_csv, "r", encoding="utf-8-sig") as f:
                reader = csv.DictReader(f)
                oi_count = 0
                for row in reader:
                    cursor.execute(
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
                            row["snapshot_time_bkk"], row["label"], row["expiry_date"], row.get("expiry_time_bkk"),
                            clean_val(row.get("index_price")), clean_val(row["strike"]),
                            clean_val(row.get("call_oi")), clean_val(row.get("put_oi")),
                            clean_val(row.get("total_oi")), clean_val(row.get("call_oi_usd")),
                            clean_val(row.get("put_oi_usd")), clean_val(row.get("total_oi_usd")),
                            clean_val(row.get("call_volume_24h")), clean_val(row.get("put_volume_24h")),
                            clean_val(row.get("pct_from_spot")), row.get("prev_snapshot_time_bkk") or None,
                            clean_val(row.get("call_oi_chg")), clean_val(row.get("put_oi_chg")),
                            clean_val(row.get("total_oi_chg")), clean_val(row.get("call_iv")),
                            clean_val(row.get("put_iv")), clean_val(row.get("call_delta")),
                            clean_val(row.get("put_delta")), clean_val(row.get("call_gamma")),
                            clean_val(row.get("put_gamma")), clean_val(row.get("call_gex_usd")),
                            clean_val(row.get("put_gex_usd")), clean_val(row.get("net_gex_usd")),
                        ),
                    )
                    oi_count += 1
                print(f"Imported {oi_count} oi_by_strike rows.")

        # 4. Import expiry_results.csv
        results_csv = data_path / "expiry_results.csv"
        if results_csv.exists():
            with open(results_csv, "r", encoding="utf-8-sig") as f:
                reader = csv.DictReader(f)
                r_count = 0
                for row in reader:
                    cursor.execute(
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
                            row["expiry_date"], clean_val(row["settlement_price"]),
                            row.get("snapshot_used") or None, row.get("snapshot_time_bkk") or None,
                            clean_val(row.get("spot_at_snapshot")), clean_val(row.get("max_pain")),
                            clean_val(row.get("top_oi_strike")), clean_val(row.get("dist_max_pain_usd")),
                            clean_val(row.get("dist_max_pain_pct")), clean_val(row.get("within_0.5pct_max_pain"), int),
                            clean_val(row.get("within_1pct_max_pain"), int), clean_val(row.get("dist_top_oi_usd")),
                            clean_val(row.get("dist_top_oi_pct")), clean_val(row.get("within_0.5pct_top_oi"), int),
                            clean_val(row.get("within_1pct_top_oi"), int), clean_val(row.get("spot_to_max_pain_move_pct")),
                            clean_val(row.get("max_pain_prev_1530")), clean_val(row.get("max_pain_prev_2100")),
                            clean_val(row.get("top_oi_prev_1530")), clean_val(row.get("top_oi_prev_2100")),
                            row.get("note"),
                            clean_val(row.get("call1_strike")), clean_val(row.get("call1_oi")),
                            clean_val(row.get("call1_dist_usd")), clean_val(row.get("call1_dist_pct")),
                            clean_val(row.get("call1_within_0.5pct"), int), clean_val(row.get("call1_within_1pct"), int),
                            clean_val(row.get("call2_strike")), clean_val(row.get("call2_oi")),
                            clean_val(row.get("call2_dist_usd")), clean_val(row.get("call2_dist_pct")),
                            clean_val(row.get("call2_within_0.5pct"), int), clean_val(row.get("call2_within_1pct"), int),
                            clean_val(row.get("put1_strike")), clean_val(row.get("put1_oi")),
                            clean_val(row.get("put1_dist_usd")), clean_val(row.get("put1_dist_pct")),
                            clean_val(row.get("put1_within_0.5pct"), int), clean_val(row.get("put1_within_1pct"), int),
                            clean_val(row.get("put2_strike")), clean_val(row.get("put2_oi")),
                            clean_val(row.get("put2_dist_usd")), clean_val(row.get("put2_dist_pct")),
                            clean_val(row.get("put2_within_0.5pct"), int), clean_val(row.get("put2_within_1pct"), int),
                            clean_val(row.get("range_low")), clean_val(row.get("range_high")),
                            clean_val(row.get("range_width_pct")),
                            clean_val(row.get("settled_in_put1_call1_range"), int),
                            clean_val(row.get("spot_in_range_at_snapshot"), int),
                            row.get("expiry_type"), row.get("high_impact_events"),
                            clean_val(row.get("has_high_impact_event"), int),
                            clean_val(row.get("snap_net_gex_usd")), clean_val(row.get("snap_gamma_flip")),
                            row.get("settled_vs_gamma_flip") or None, clean_val(row.get("dist_gamma_flip_pct")),
                            clean_val(row.get("snap_max_abs_gex_strike")), clean_val(row.get("dist_max_abs_gex_pct")),
                            clean_val(row.get("snap_dvol")), clean_val(row.get("snap_atm_iv")),
                            clean_val(row.get("snap_skew_25d_rr")), clean_val(row.get("snap_total_oi_chg")),
                        ),
                    )
                    r_count += 1
                print(f"Imported {r_count} expiry_results.")

        # 5. Import big_expiries.csv
        big_csv = data_path / "big_expiries.csv"
        if big_csv.exists():
            with open(big_csv, "r", encoding="utf-8-sig") as f:
                reader = csv.DictReader(f)
                b_count = 0
                for row in reader:
                    cursor.execute(
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
                            row["snapshot_time_bkk"], row["label"], row["kind"], row["expiry_date"],
                            row.get("expiry_time_bkk"), clean_val(row.get("hours_to_expiry")),
                            clean_val(row.get("index_price")), clean_val(row.get("n_strikes"), int),
                            clean_val(row.get("total_call_oi")), clean_val(row.get("total_put_oi")),
                            clean_val(row.get("total_oi")), clean_val(row.get("total_oi_usd")),
                            clean_val(row.get("put_call_oi_ratio")), clean_val(row.get("max_pain")),
                            clean_val(row.get("price_vs_max_pain_pct")), clean_val(row.get("top_oi_strike")),
                            clean_val(row.get("price_vs_top_oi_pct")),
                            clean_val(row.get("call1_strike")), clean_val(row.get("call1_oi")),
                            clean_val(row.get("call2_strike")), clean_val(row.get("call2_oi")),
                            clean_val(row.get("put1_strike")), clean_val(row.get("put1_oi")),
                            clean_val(row.get("put2_strike")), clean_val(row.get("put2_oi")),
                            row.get("top5_strikes"),
                        ),
                    )
                    b_count += 1
                print(f"Imported {b_count} big_expiries.")

        # 6. Import events.csv
        events_csv = data_path / "events.csv"
        if events_csv.exists():
            with open(events_csv, "r", encoding="utf-8-sig") as f:
                reader = csv.DictReader(f)
                e_count = 0
                for row in reader:
                    cursor.execute(
                        """
                        INSERT OR REPLACE INTO events (
                            date, time_bkk, event, impact, source, country, forecast, previous, added_bkk
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            row["date"], row["time_bkk"], row["event"], row.get("impact", "Medium"),
                            row.get("source", "manual"), row.get("country"),
                            row.get("forecast"), row.get("previous"), row.get("added_bkk"),
                        ),
                    )
                    e_count += 1
                print(f"Imported {e_count} events.")

        conn.commit()


if __name__ == "__main__":
    import_historical_data()
