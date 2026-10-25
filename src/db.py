import sqlite3
from typing import Any, Dict, List, Optional
from src.config import DB_PATH


def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    with get_db() as conn:
        cursor = conn.cursor()

        # 1. snapshots
        cursor.execute("""
        CREATE TABLE IF NOT EXISTS snapshots (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            snapshot_time_bkk TEXT NOT NULL,
            label TEXT NOT NULL,
            expiry_date TEXT NOT NULL,
            expiry_time_bkk TEXT NOT NULL,
            hours_to_expiry REAL,
            index_price REAL,
            n_strikes INTEGER,
            total_call_oi REAL,
            total_put_oi REAL,
            total_oi REAL,
            total_oi_usd REAL,
            put_call_oi_ratio REAL,
            max_pain REAL,
            price_minus_max_pain REAL,
            price_vs_max_pain_pct REAL,
            top_oi_strike REAL,
            price_minus_top_oi REAL,
            price_vs_top_oi_pct REAL,
            top_call_strike REAL,
            top_call_oi REAL,
            top_put_strike REAL,
            top_put_oi REAL,
            call1_strike REAL,
            call1_oi REAL,
            call2_strike REAL,
            call2_oi REAL,
            put1_strike REAL,
            put1_oi REAL,
            put2_strike REAL,
            put2_oi REAL,
            top5_strikes TEXT,
            raw_file TEXT,
            expiry_type TEXT,
            prev_snapshot_time_bkk TEXT,
            prev_label TEXT,
            call_oi_chg REAL,
            put_oi_chg REAL,
            total_oi_chg REAL,
            top3_oi_increase TEXT,
            top3_oi_decrease TEXT,
            prev_max_pain REAL,
            max_pain_moved INTEGER,
            prev_call1_strike REAL,
            call1_moved INTEGER,
            prev_put1_strike REAL,
            put1_moved INTEGER,
            net_gex_usd REAL,
            call_gex_usd REAL,
            put_gex_usd REAL,
            max_abs_gex_strike REAL,
            max_abs_gex_usd REAL,
            gamma_flip REAL,
            spot_vs_gamma_flip TEXT,
            gex_gamma_source TEXT,
            dvol REAL,
            atm_strike REAL,
            atm_iv REAL,
            atm_call_iv REAL,
            atm_put_iv REAL,
            call25_strike REAL,
            call25_iv REAL,
            put25_strike REAL,
            put25_iv REAL,
            skew_25d_rr REAL,
            UNIQUE(snapshot_time_bkk, expiry_date, label)
        )
        """)

        # 2. oi_by_strike
        cursor.execute("""
        CREATE TABLE IF NOT EXISTS oi_by_strike (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            snapshot_time_bkk TEXT NOT NULL,
            label TEXT NOT NULL,
            expiry_date TEXT NOT NULL,
            expiry_time_bkk TEXT,
            index_price REAL,
            strike REAL NOT NULL,
            call_oi REAL,
            put_oi REAL,
            total_oi REAL,
            call_oi_usd REAL,
            put_oi_usd REAL,
            total_oi_usd REAL,
            call_volume_24h REAL,
            put_volume_24h REAL,
            pct_from_spot REAL,
            prev_snapshot_time_bkk TEXT,
            call_oi_chg REAL,
            put_oi_chg REAL,
            total_oi_chg REAL,
            call_iv REAL,
            put_iv REAL,
            call_delta REAL,
            put_delta REAL,
            call_gamma REAL,
            put_gamma REAL,
            call_gex_usd REAL,
            put_gex_usd REAL,
            net_gex_usd REAL,
            UNIQUE(snapshot_time_bkk, expiry_date, strike)
        )
        """)

        # 3. expiry_results
        cursor.execute("""
        CREATE TABLE IF NOT EXISTS expiry_results (
            expiry_date TEXT PRIMARY KEY,
            settlement_price REAL NOT NULL,
            snapshot_used TEXT,
            snapshot_time_bkk TEXT,
            spot_at_snapshot REAL,
            max_pain REAL,
            top_oi_strike REAL,
            dist_max_pain_usd REAL,
            dist_max_pain_pct REAL,
            [within_0.5pct_max_pain] INTEGER,
            within_1pct_max_pain INTEGER,
            dist_top_oi_usd REAL,
            dist_top_oi_pct REAL,
            [within_0.5pct_top_oi] INTEGER,
            within_1pct_top_oi INTEGER,
            spot_to_max_pain_move_pct REAL,
            max_pain_prev_1530 REAL,
            max_pain_prev_2100 REAL,
            top_oi_prev_1530 REAL,
            top_oi_prev_2100 REAL,
            note TEXT,
            call1_strike REAL,
            call1_oi REAL,
            call1_dist_usd REAL,
            call1_dist_pct REAL,
            [call1_within_0.5pct] INTEGER,
            call1_within_1pct INTEGER,
            call2_strike REAL,
            call2_oi REAL,
            call2_dist_usd REAL,
            call2_dist_pct REAL,
            [call2_within_0.5pct] INTEGER,
            call2_within_1pct INTEGER,
            put1_strike REAL,
            put1_oi REAL,
            put1_dist_usd REAL,
            put1_dist_pct REAL,
            [put1_within_0.5pct] INTEGER,
            put1_within_1pct INTEGER,
            put2_strike REAL,
            put2_oi REAL,
            put2_dist_usd REAL,
            put2_dist_pct REAL,
            [put2_within_0.5pct] INTEGER,
            put2_within_1pct INTEGER,
            range_low REAL,
            range_high REAL,
            range_width_pct REAL,
            settled_in_put1_call1_range INTEGER,
            spot_in_range_at_snapshot INTEGER,
            expiry_type TEXT,
            high_impact_events TEXT,
            has_high_impact_event INTEGER,
            snap_net_gex_usd REAL,
            snap_gamma_flip REAL,
            settled_vs_gamma_flip TEXT,
            dist_gamma_flip_pct REAL,
            snap_max_abs_gex_strike REAL,
            dist_max_abs_gex_pct REAL,
            snap_dvol REAL,
            snap_atm_iv REAL,
            snap_skew_25d_rr REAL,
            snap_total_oi_chg REAL
        )
        """)

        # 4. big_expiries
        cursor.execute("""
        CREATE TABLE IF NOT EXISTS big_expiries (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            snapshot_time_bkk TEXT NOT NULL,
            label TEXT NOT NULL,
            kind TEXT NOT NULL,
            expiry_date TEXT NOT NULL,
            expiry_time_bkk TEXT,
            hours_to_expiry REAL,
            index_price REAL,
            n_strikes INTEGER,
            total_call_oi REAL,
            total_put_oi REAL,
            total_oi REAL,
            total_oi_usd REAL,
            put_call_oi_ratio REAL,
            max_pain REAL,
            price_vs_max_pain_pct REAL,
            top_oi_strike REAL,
            price_vs_top_oi_pct REAL,
            call1_strike REAL,
            call1_oi REAL,
            call2_strike REAL,
            call2_oi REAL,
            put1_strike REAL,
            put1_oi REAL,
            put2_strike REAL,
            put2_oi REAL,
            top5_strikes TEXT,
            UNIQUE(snapshot_time_bkk, kind, expiry_date)
        )
        """)

        # 5. events
        cursor.execute("""
        CREATE TABLE IF NOT EXISTS events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            date TEXT NOT NULL,
            time_bkk TEXT NOT NULL,
            event TEXT NOT NULL,
            impact TEXT NOT NULL,
            source TEXT DEFAULT 'manual',
            country TEXT,
            forecast TEXT,
            previous TEXT,
            added_bkk TEXT,
            UNIQUE(date, time_bkk, event)
        )
        """)

        # 6. execution_logs
        cursor.execute("""
        CREATE TABLE IF NOT EXISTS execution_logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_time_bkk TEXT NOT NULL,
            label TEXT NOT NULL,
            action TEXT NOT NULL,
            status TEXT NOT NULL,
            message TEXT,
            fallback_used INTEGER DEFAULT 0
        )
        """)

        # Helpful indices
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_snapshots_expiry ON snapshots(expiry_date, snapshot_time_bkk)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_oi_by_strike_snap ON oi_by_strike(snapshot_time_bkk, expiry_date)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_events_date ON events(date)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_big_expiries_snap ON big_expiries(snapshot_time_bkk)")

        conn.commit()


def log_execution(run_time_bkk: str, label: str, action: str, status: str, message: str, fallback_used: int = 0):
    try:
        with get_db() as conn:
            conn.execute(
                """
                INSERT INTO execution_logs (run_time_bkk, label, action, status, message, fallback_used)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (run_time_bkk, label, action, status, message, fallback_used)
            )
            conn.commit()
    except Exception as e:
        print(f"Failed to log execution: {e}")
